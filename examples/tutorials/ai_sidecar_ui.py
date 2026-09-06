"""Run real Ultralytics inference against an FMV file in the reference UI."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator, Sequence
from contextlib import suppress
from dataclasses import asdict, replace
from datetime import datetime
from functools import partial
from http.server import ThreadingHTTPServer
from itertools import chain
from pathlib import Path
from typing import Any

from stanag4609 import (
    PTS_CLOCK_RATE,
    PTS_MODULUS,
    AlgorithmLocalSet,
    OntologyLocalSet,
)
from stanag4609.player import extract_overlay_detections
from stanag4609.player.server import PlayerHTTPRequestHandler, prepare_player_assets
from stanag4609.sidecar import (
    FrameEnvelope,
    InferenceContext,
    InferenceOutput,
    InferenceResult,
    PyAVFrameSource,
    UltralyticsYOLODetector,
    VMTIMetadataEmitter,
)

_ROAD_CLASSES = (2, 3, 5, 7)  # COCO car, motorcycle, bus, truck
_LABELS = ("car", "motorcycle", "bus", "truck")


class _PreparedPrediction:
    """Expose one precomputed Ultralytics result through the adapter contract."""

    def __init__(self, result: Any) -> None:
        self.result = result

    def predict(self, *, source: Any, **_kwargs: Any) -> tuple[Any, ...]:
        del source
        return (self.result,)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run real YOLO vehicle inference and show ST 0903 in the FMV player"
    )
    parser.add_argument("source", type=Path, help="input MPEG-2 transport stream")
    parser.add_argument("--output", type=Path, default=Path("work/ai-sidecar-ui"))
    parser.add_argument("--weights", default="yolo11n.pt")
    parser.add_argument("--confidence", type=float, default=0.35)
    parser.add_argument("--image-size", type=int, default=960)
    parser.add_argument(
        "--inference-mode",
        choices=("track", "predict"),
        default="track",
        help="Ultralytics tracking with persistent IDs, or independent predictions",
    )
    parser.add_argument(
        "--tracker",
        default="bytetrack.yaml",
        help="Ultralytics tracker configuration used in track mode",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=24,
        help="batch size for predict mode; track mode processes ordered frames singly",
    )
    parser.add_argument("--device", default=None, help="for example cpu, mps, or 0")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    return parser


def _sampled_frames(
    media: Path, samples: Sequence[dict[str, Any]]
) -> Iterator[tuple[int, FrameEnvelope]]:
    try:
        frames = iter(PyAVFrameSource(media))
    except RuntimeError as error:  # pragma: no cover - optional dependency guidance
        raise SystemExit(str(error)) from error
    try:
        first = next(frames, None)
        if first is None:
            raise SystemExit(f"could not decode prepared video: {media}")
        origin_pts = first.pts
        sample_index = 0
        for frame in chain((first,), frames):
            frame_time = ((frame.pts - origin_pts) % PTS_MODULUS) / PTS_CLOCK_RATE
            while (
                sample_index < len(samples)
                and float(samples[sample_index]["time_seconds"]) <= frame_time
            ):
                sample = samples[sample_index]
                yield (
                    sample_index,
                    replace(
                        frame,
                        sequence_number=sample_index,
                        pts=int(sample["pts"]),
                        timestamp_microseconds=_timestamp(sample),
                    ),
                )
                sample_index += 1
    finally:
        close = getattr(frames, "close", None)
        if callable(close):
            close()


def _timestamp(sample: dict[str, Any]) -> int:
    value = sample["fields"]["Precision Time Stamp"]["value"]
    return int(datetime.fromisoformat(value).timestamp() * 1_000_000)


def _frame_corners(sample: dict[str, Any]) -> tuple[tuple[float, float], ...] | None:
    """Return image-ordered WGS84 corners from the player's footprint feature."""

    for feature in sample.get("geospatial", ()):
        if feature.get("properties", {}).get("role") != "frame_footprint":
            continue
        geometry = feature.get("geometry", {})
        coordinates = geometry.get("coordinates", ())
        if geometry.get("type") != "Polygon" or not coordinates:
            return None
        ring = coordinates[0]
        if len(ring) < 4:
            return None
        return tuple((float(point[0]), float(point[1])) for point in ring[:4])
    return None


def _annotate_with_yolo(
    *,
    media: Path,
    timeline: dict[str, Any],
    weights: str,
    confidence: float,
    image_size: int,
    batch_size: int,
    device: str | None,
    inference_mode: str,
    tracker: str,
) -> tuple[int, int]:
    try:
        from ultralytics import YOLO  # type: ignore[attr-defined]
    except ImportError as error:  # pragma: no cover - optional dependency guidance
        raise SystemExit(
            "install the demo dependency: pip install 'stanag4609[ai-ultralytics]'"
        ) from error

    model = YOLO(weights)
    algorithms = (
        AlgorithmLocalSet(
            1,
            f"Ultralytics {Path(weights).stem}",
            Path(weights).stem,
            "detector",
            1,
        ),
    )
    ontologies = tuple(
        OntologyLocalSet(
            index + 1,
            "https://cocodataset.org/#explore",
            f"https://cocodataset.org/#explore#{label}",
            label=label,
        )
        for index, label in enumerate(_LABELS)
    )
    emitter = VMTIMetadataEmitter(
        "yolo",
        metadata_pid=0x120,
        metadata_service_id=7,
        leap_seconds=35,
        algorithms=algorithms,
        ontologies=ontologies,
        ontology_by_label={label: index + 1 for index, label in enumerate(_LABELS)},
    )
    samples = timeline["samples"]
    detected_frames = detection_count = 0
    pending: list[tuple[int, Any]] = []
    inference_kwargs: dict[str, Any] = {
        "classes": list(_ROAD_CLASSES),
        "conf": confidence,
        "imgsz": image_size,
        "verbose": False,
    }
    if device is not None:
        inference_kwargs["device"] = device
    tracked_detector = (
        UltralyticsYOLODetector(
            model,
            algorithm_id=1,
            mode="track",
            predict_kwargs={**inference_kwargs, "tracker": tracker},
        )
        if inference_mode == "track"
        else None
    )

    def record(sample_index: int, frame: FrameEnvelope, output: InferenceOutput) -> None:
        nonlocal detected_frames, detection_count
        sample = samples[sample_index]
        context = InferenceContext(frame).with_result(InferenceResult("yolo", output))
        packet = emitter(context)
        vmti = packet.decoded.value(74)
        sample["detections"] = [
            asdict(item)
            for item in extract_overlay_detections(
                vmti,
                frame_corners=_frame_corners(sample),
            )
        ]
        sample["fields"]["AI Sidecar"] = {
            "value": (f"real {Path(weights).name} {inference_mode} inference -> ST 0903 VMTI")
        }
        if output.detections:
            detected_frames += 1
            detection_count += len(output.detections)

    def process(batch: list[tuple[int, Any]]) -> None:
        if tracked_detector is not None:
            for sample_index, frame in batch:
                record(sample_index, frame, tracked_detector(InferenceContext(frame)))
            return
        frames = [frame.pixels for _, frame in batch]
        results = model.predict(frames, **inference_kwargs)
        for (sample_index, frame), result in zip(batch, results, strict=True):
            detector = UltralyticsYOLODetector(_PreparedPrediction(result), algorithm_id=1)
            record(sample_index, frame, detector(InferenceContext(frame)))

    for item in _sampled_frames(media, samples):
        pending.append(item)
        if len(pending) == (1 if tracked_detector is not None else batch_size):
            process(pending)
            print(f"inference: {pending[-1][0] + 1}/{len(samples)} samples", flush=True)
            pending.clear()
    if pending:
        process(pending)
        print(f"inference: {pending[-1][0] + 1}/{len(samples)} samples", flush=True)
    return detected_frames, detection_count


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 1 <= args.port <= 65_535:
        raise SystemExit("--port must be between 1 and 65535")
    if not 0 < args.confidence <= 1:
        raise SystemExit("--confidence must be greater than 0 and at most 1")
    if args.image_size < 32 or args.batch_size < 1:
        raise SystemExit("--image-size must be at least 32 and --batch-size must be positive")

    assets = prepare_player_assets(args.source, args.output)
    timeline = json.loads(assets.timeline.read_text(encoding="utf-8"))
    detected_frames, detection_count = _annotate_with_yolo(
        media=assets.media,
        timeline=timeline,
        weights=args.weights,
        confidence=args.confidence,
        image_size=args.image_size,
        batch_size=args.batch_size,
        device=args.device,
        inference_mode=args.inference_mode,
        tracker=args.tracker,
    )
    assets.timeline.write_text(json.dumps(timeline, separators=(",", ":")), encoding="utf-8")

    handler = partial(PlayerHTTPRequestHandler, directory=str(assets.root))
    with ThreadingHTTPServer((args.host, args.port), handler) as server:
        url = f"http://{args.host}:{server.server_port}/"
        print(
            f"AI sidecar UI: {url} model={args.weights} "
            f"mode={args.inference_mode} "
            f"detected_frames={detected_frames} detections={detection_count}",
            flush=True,
        )
        with suppress(KeyboardInterrupt):
            server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
