# Add AI detections as VMTI

ST 0903 VMTI gives detections a standards-native place in an FMV stream. This
tutorial starts with a deterministic model-neutral pipeline, then shows how to
run Ultralytics detection and tracking against a real recording.

The runnable examples live in the project repository. From a source checkout,
install the development environment and run the dependency-free example:

```console
python -m pip install -e '.[dev]'
python examples/tutorials/ai_to_vmti.py
```

It executes two detector branches concurrently, fuses their results, and emits
a timed ST 0601 packet containing VMTI:

```text
PID=0x0120 PTS=900000 targets=[101, 202] KLV_bytes=429
```

The two target IDs come from the vehicle and person branches. Packet length
depends on the fields and detections that you encode.

## Run a real detector in the player

Install the local-model and video-decoding extras:

```console
python -m pip install -e '.[ai-ultralytics,video-pyav]'
```

Then run YOLO11 against the Esri `Truck.ts` fixture from the
[inspection tutorial](inspect_fmv.md):

```console
python examples/tutorials/ai_sidecar_ui.py \
  samples/private/esri-fmv/Truck.ts \
  --weights yolo11n.pt \
  --inference-mode track \
  --tracker bytetrack.yaml
```

Ultralytics downloads the small YOLO11n weights on first use. Inference runs
before the local server starts; the command then prints its URL and a summary:

```text
AI sidecar UI: http://127.0.0.1:8767/ model=yolo11n.pt mode=track detected_frames=488 detections=1155
```

Detection counts can vary with model and runtime versions. Open the URL, start
playback, and seek through the recording. Telemetry, map geometry, detection
boxes, the activity feed, and the class-stacked timeline all follow the same
media playhead.

![Reference player showing YOLO11 detections encoded as ST 0903](../assets/screenshots/ai-sidecar-player.jpg)

The demo filters COCO car, motorcycle, bus, and truck classes at confidence
`0.35`. Tracking is enabled by default. Ultralytics ByteTrack IDs become
positive ST 0903 target IDs, so the same object can retain its identity across
frames. Use `--inference-mode predict` for independent frames or
`--tracker botsort.yaml` for BoT-SORT. The `--confidence`, `--image-size`, and
`--device` options tune inference without changing the VMTI boundary.

Pixel-space boxes are encoded in VMTI and projected into the current ST 0601
frame footprint for the map. A target carrying absolute or parent-offset
coordinates is instead plotted at its resolved ground position.

## Compose a model-neutral pipeline

The example builds a graph from ordinary sync or async Python callables:

```python
from stanag4609.sidecar import InferenceStage, Parallel, Sequential


def build_pipeline(vehicle_detector, person_detector, fuse):
    return Sequential(
        Parallel(
            InferenceStage("vehicles", vehicle_detector, threaded=True),
            InferenceStage("people", person_detector, timeout_seconds=0.2),
            max_concurrency=2,
        ),
        InferenceStage("fused", fuse),
    )
```

Parallel results merge in declaration order. Put stages that depend on those
results, such as fusion or classification, after the parallel group. For live
input, place frames in `AsyncFrameQueue` and choose an overload policy so slow
inference cannot grow memory without bound.

`VMTIMetadataEmitter` converts the final detections into `TimedKLVPacket`
objects. Each detection may carry a stable target ID, half-open pixel box,
confidence, algorithm ID, and label. Algorithm and ontology local sets let
downstream consumers understand which model produced a target and what its
label means.

If the frame was correlated with a source ST 0601 packet, unrelated and unknown
wire fields are preserved. Otherwise, the emitter can create a minimal
timestamped parent on an explicitly selected metadata PID.

## Choose an inference adapter

The protocol pipeline is independent of the model runtime:

- use the built-in Ultralytics adapter for local YOLO prediction or tracking;
- use ONNX Runtime for a portable local model;
- use Triton HTTP or gRPC for managed inference; or
- adapt an application service to the JSON/HTTP boundary.

[AI sidecars](../AI_SIDECARS.md) covers queues, frame correlation, adapters,
metrics, and VMTI emission. The
[`examples/ai_sidecar`](https://github.com/trane293/stanag4609/tree/main/examples/ai_sidecar)
directory contains focused examples for each supported runtime.

Keep preprocessing and model-output decoding in the adapter. Keep PTS
correlation, overload policy, and VMTI encoding in the library pipeline.
