"""Paced, repeatable stability evidence for the live reference-player gateway."""

from __future__ import annotations

import argparse
import json
import math
import platform
import subprocess
import sys
import time
import tracemalloc
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from stanag4609.player.benchmark import _ffmpeg_version, _sha256, _source_duration
from stanag4609.player.live import LivePlayerGateway

try:
    import resource
except ImportError:  # pragma: no cover - unavailable on Windows
    resource = None  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class LivePlayerSoakEpoch:
    """Result of one isolated gateway and FFmpeg process epoch."""

    epoch: int
    passed: bool
    error: str | None
    elapsed_seconds: float
    maximum_pacing_lag_seconds: float
    input_bytes: int
    input_chunks: int
    metadata_samples: int
    media_fragments: int
    retained_metadata_samples: int
    dropped_metadata_samples: int
    retained_media_fragments: int
    dropped_media_fragments: int
    retained_media_bytes: int

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class LivePlayerSoakBenchmark:
    """Machine-readable result for a paced sequence of reconnect epochs."""

    schema_version: int
    passed: bool
    source: str
    source_bytes: int
    source_sha256: str
    source_duration_seconds: float
    playback_rate: float
    requested_epochs: int
    completed_epochs: int
    elapsed_seconds: float
    total_input_bytes: int
    total_input_chunks: int
    total_metadata_samples: int
    total_media_fragments: int
    maximum_pacing_lag_seconds: float
    python_traced_peak_bytes: int
    python_process_peak_rss_bytes: int | None
    child_process_peak_rss_bytes: int | None
    python: str
    platform: str
    ffmpeg: str
    epochs: tuple[LivePlayerSoakEpoch, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""

        return asdict(self)


def _peak_rss_bytes(who: int) -> int | None:
    if resource is None:
        return None
    maximum = resource.getrusage(who).ru_maxrss
    return int(maximum if sys.platform == "darwin" else maximum * 1024)


def _validate_positive_integer(name: str, value: int, *, minimum: int = 1) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        qualifier = "positive" if minimum == 1 else f"at least {minimum}"
        raise ValueError(f"{name} must be an integer that is {qualifier}")


def _history(gateway: LivePlayerGateway) -> tuple[int, int, int, int, int]:
    retained_media = gateway.media.poll(after_id=-1, timeout=0)
    retained_metadata = gateway.metadata.poll(after_id=-1, timeout=0)
    return (
        len(retained_metadata.items),
        retained_metadata.dropped,
        len(retained_media.items),
        retained_media.dropped,
        sum(len(fragment) for _item_id, fragment in retained_media.items),
    )


def soak_live_player(
    source: Path,
    *,
    epochs: int = 1,
    playback_rate: float = 1.0,
    source_duration_seconds: float | None = None,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    chunk_bytes: int = 1316,
    media_fragments: int = 12,
    metadata_samples: int = 512,
    program_number: int | None = None,
) -> LivePlayerSoakBenchmark:
    """Replay a TS source at average media rate across isolated gateway epochs.

    Every epoch owns a new :class:`LivePlayerGateway` and FFmpeg subprocess.
    This models an explicit source reconnect without joining transport,
    metadata, media, or broadcast state across sessions. Runtime failures are
    recorded in the returned report and stop the campaign; invalid setup still
    raises immediately.
    """

    source_display = str(source.expanduser())
    source = source.expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"input does not exist: {source}")
    _validate_positive_integer("epochs", epochs)
    _validate_positive_integer("chunk_bytes", chunk_bytes, minimum=188)
    _validate_positive_integer("media_fragments", media_fragments, minimum=2)
    _validate_positive_integer("metadata_samples", metadata_samples)
    if program_number is not None and (
        isinstance(program_number, bool)
        or not isinstance(program_number, int)
        or not 1 <= program_number <= 0xFFFF
    ):
        raise ValueError("program_number must be an integer from 1 to 65535 or None")
    if isinstance(playback_rate, bool) or not isinstance(playback_rate, (int, float)):
        raise TypeError("playback_rate must be a finite positive number")
    if not math.isfinite(playback_rate) or playback_rate <= 0:
        raise ValueError("playback_rate must be a finite positive number")
    if source_duration_seconds is None:
        source_duration_seconds = _source_duration(source, ffprobe=ffprobe)
        if source_duration_seconds is None or source_duration_seconds <= 0:
            raise ValueError(
                "source duration is unavailable; install ffprobe or pass source_duration_seconds"
            )
    elif (
        isinstance(source_duration_seconds, bool)
        or not isinstance(source_duration_seconds, (int, float))
        or not math.isfinite(source_duration_seconds)
        or source_duration_seconds <= 0
    ):
        raise ValueError("source_duration_seconds must be a finite positive number or None")

    source_bytes = source.stat().st_size
    if source_bytes == 0:
        raise ValueError("input must not be empty")
    source_hash = _sha256(source)
    ffmpeg_identity = _ffmpeg_version(ffmpeg)
    epoch_results: list[LivePlayerSoakEpoch] = []
    owns_tracing = not tracemalloc.is_tracing()
    if owns_tracing:
        tracemalloc.start()
    else:
        tracemalloc.reset_peak()
    campaign_started = time.perf_counter()
    try:
        for epoch_number in range(1, epochs + 1):
            gateway = LivePlayerGateway(
                ffmpeg=ffmpeg,
                max_metadata_samples=metadata_samples,
                max_media_fragments=media_fragments,
                max_input_chunk_bytes=chunk_bytes,
                program_number=program_number,
            )
            input_chunks = 0
            input_bytes = 0
            maximum_lag = 0.0
            error: str | None = None
            epoch_started = time.perf_counter()
            try:
                gateway.start()
                with source.open("rb") as stream:
                    while chunk := stream.read(chunk_bytes):
                        gateway.feed(chunk)
                        input_chunks += 1
                        input_bytes += len(chunk)
                        target_elapsed = (
                            input_bytes
                            / source_bytes
                            * source_duration_seconds
                            / float(playback_rate)
                        )
                        elapsed = time.perf_counter() - epoch_started
                        maximum_lag = max(maximum_lag, elapsed - target_elapsed)
                        remaining = target_elapsed - elapsed
                        if remaining > 0:
                            time.sleep(remaining)
                gateway.finish(timeout=60)
            except Exception as caught:  # preserve the failed campaign's evidence
                error = f"{type(caught).__name__}: {caught}"
                gateway.close()
            elapsed = time.perf_counter() - epoch_started
            stats = gateway.stats
            history = _history(gateway)
            epoch_results.append(
                LivePlayerSoakEpoch(
                    epoch=epoch_number,
                    passed=error is None,
                    error=error,
                    elapsed_seconds=elapsed,
                    maximum_pacing_lag_seconds=maximum_lag,
                    input_bytes=input_bytes,
                    input_chunks=input_chunks,
                    metadata_samples=stats.metadata_samples,
                    media_fragments=stats.media_fragments,
                    retained_metadata_samples=history[0],
                    dropped_metadata_samples=history[1],
                    retained_media_fragments=history[2],
                    dropped_media_fragments=history[3],
                    retained_media_bytes=history[4],
                )
            )
            if error is not None:
                break
    finally:
        elapsed = time.perf_counter() - campaign_started
        _current, traced_peak = tracemalloc.get_traced_memory()
        if owns_tracing:
            tracemalloc.stop()

    completed = sum(result.passed for result in epoch_results)
    return LivePlayerSoakBenchmark(
        schema_version=1,
        passed=completed == epochs,
        source=source_display,
        source_bytes=source_bytes,
        source_sha256=source_hash,
        source_duration_seconds=source_duration_seconds,
        playback_rate=float(playback_rate),
        requested_epochs=epochs,
        completed_epochs=completed,
        elapsed_seconds=elapsed,
        total_input_bytes=sum(result.input_bytes for result in epoch_results),
        total_input_chunks=sum(result.input_chunks for result in epoch_results),
        total_metadata_samples=sum(result.metadata_samples for result in epoch_results),
        total_media_fragments=sum(result.media_fragments for result in epoch_results),
        maximum_pacing_lag_seconds=max(
            (result.maximum_pacing_lag_seconds for result in epoch_results), default=0.0
        ),
        python_traced_peak_bytes=traced_peak,
        python_process_peak_rss_bytes=_peak_rss_bytes(resource.RUSAGE_SELF) if resource else None,
        child_process_peak_rss_bytes=(
            _peak_rss_bytes(resource.RUSAGE_CHILDREN) if resource else None
        ),
        python=platform.python_version(),
        platform=platform.platform(),
        ffmpeg=ffmpeg_identity,
        epochs=tuple(epoch_results),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run a paced live-player soak and emit its versioned JSON report."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="complete MPEG-2 TS FMV input")
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument(
        "--rate",
        type=float,
        default=1.0,
        help="wall-clock playback multiplier; 1 is real time, 2 is twice real time",
    )
    parser.add_argument(
        "--source-duration",
        type=float,
        help="media duration in seconds when ffprobe cannot determine it",
    )
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--chunk-bytes", type=int, default=1316)
    parser.add_argument("--media-fragments", type=int, default=12)
    parser.add_argument("--metadata-samples", type=int, default=512)
    parser.add_argument("--program-number", type=int)
    parser.add_argument("--output", type=Path, help="also write the JSON result to this path")
    parser.add_argument("--force", action="store_true", help="replace an existing output file")
    arguments = parser.parse_args(argv)
    output = arguments.output.expanduser() if arguments.output is not None else None
    if output is not None:
        if output.exists() and not output.is_file():
            parser.error(f"output is not a regular file: {output}")
        if output.exists() and not arguments.force:
            parser.error(f"output already exists: {output}; pass --force to replace it")
        if not output.parent.is_dir():
            parser.error(f"output directory does not exist: {output.parent}")
    try:
        result = soak_live_player(
            arguments.source,
            epochs=arguments.epochs,
            playback_rate=arguments.rate,
            source_duration_seconds=arguments.source_duration,
            ffmpeg=arguments.ffmpeg,
            ffprobe=arguments.ffprobe,
            chunk_bytes=arguments.chunk_bytes,
            media_fragments=arguments.media_fragments,
            metadata_samples=arguments.metadata_samples,
            program_number=arguments.program_number,
        )
    except (OSError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError) as error:
        parser.error(str(error))
    document = json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n"
    if output is not None:
        mode = "w" if arguments.force else "x"
        try:
            with output.open(mode, encoding="utf-8") as stream:
                stream.write(document)
        except OSError as error:
            parser.error(str(error))
    print(document, end="")
    return 0 if result.passed else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
