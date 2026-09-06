# Adapter guide

Adapters connect codec, inference, network, GIS, and user-interface runtimes to
the dependency-free protocol core. They should translate at the boundary and
keep library timing and backpressure contracts visible.

## What belongs in an adapter

An adapter may:

- turn decoded video frames into `FrameEnvelope` values;
- invoke a local model or remote inference service;
- translate model-specific output into `Detection` values;
- consume transport batches, metadata sidecars, or GeoJSON feature collections;
- expose the live pipeline through a web server, message bus, or media runtime.

An adapter should not silently invent timestamps, discard unknown KLV, grow an
unbounded queue, or make a model-specific tensor layout part of the protocol
API.

## First-party adapter surface

| Integration | Status | Package extra or entry point |
| --- | --- | --- |
| Generic sync/async Python inference | Implemented | Core `InferenceStage` API |
| Sequential and bounded-parallel graphs | Implemented | Core `Sequential` and `Parallel` APIs |
| Ultralytics YOLO | Implemented | `ai-ultralytics` |
| ONNX Runtime | Implemented | `ai-onnx` |
| NVIDIA Triton HTTP or gRPC AsyncIO | Implemented | `ai-triton-http`, `ai-triton-grpc` |
| Generic JSON-over-HTTP inference | Implemented | Core `HTTPJSONAdapter` |
| PyAV/FFmpeg video frame decode | Implemented | `video-pyav` |
| PyAV/FFmpeg audio decode | Implemented | `audio-pyav` |
| FFmpeg media remux/player preparation | Implemented | External `ffmpeg` executable |
| GStreamer element/plugin | Planned | Application bridge today |

“Implemented” means the repository has a public adapter and tests. PyAV and
ONNX Runtime also have dependency-installed CI jobs; the ONNX job constructs a
real graph and executes it through `InferenceSession` and the first-party
adapter. It does not claim that every model architecture, execution provider,
server configuration, or downstream application has been certified.

## Bring any Python model

Wrap a callable and return model-neutral detections. Use `threaded=True` when a
synchronous model would otherwise block the event loop.

```python
from stanag4609.sidecar import (
    Detection,
    InferenceOutput,
    InferenceStage,
    PixelBoundingBox,
)

def detect_trucks(context):
    model_rows = truck_model(context.frame.pixels)
    return InferenceOutput(
        detections=tuple(
            Detection(
                target_id=row.track_id,
                bounding_box=PixelBoundingBox(
                    row.left, row.top, row.right, row.bottom
                ),
                confidence=row.confidence,
                label="truck",
            )
            for row in model_rows
        )
    )

truck_stage = InferenceStage("trucks", detect_trucks, threaded=True)
```

## Decode video for any model

Install the optional backend and iterate a file, URL, or file-like object:

```console
pip install 'stanag4609[video-pyav]'
```

```python
from stanag4609.sidecar import PyAVFrameSource

for frame in PyAVFrameSource("flight.ts", video_pid=0x101):
    # frame.pixels is a BGR NumPy array; frame.pts uses the 90 kHz TS clock.
    submit_to_model(frame)
```

Set `pixel_format=None` to retain native PyAV `VideoFrame` objects, or select a
different FFmpeg pixel format for a model-specific preprocessor. The adapter
does not guess an absolute UTC time from media-relative PTS. Feed synchronous
KLV into `FrameMetadataCorrelator` when the model needs the matching ST 0601
timestamp or platform/sensor context.

For production graphs, timeouts, deterministic merging, frame/KLV correlation,
queue overload policy, and VMTI encoding are covered in
[AI and VMTI sidecars](AI_SIDECARS.md).

## Review checklist

- Define the accepted frame/tensor format and output schema.
- Define timestamp ownership and correlation policy.
- Bound every queue, request, and batch.
- Propagate cancellation and surface timeout or partial-result behavior.
- Preserve stable target identity across frames when emitting tracks.
- Test malformed output, empty detections, cancellation, and overload.
- Keep optional imports lazy so `import stanag4609` remains dependency-free.
