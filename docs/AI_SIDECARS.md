# AI inference sidecars

`stanag4609.sidecar` connects decoded video frames to local models, remote
inference services, and ST 0903 VMTI output. Its core contracts have no model or
array dependency, so you can change inference runtimes without changing how
frames are timed, queued, correlated, or encoded.

Start with [Add AI detections as VMTI](tutorials/ai_to_vmti.md) for a runnable
two-model pipeline and Ultralytics player demo.

## Understand the data flow

The main types each own one part of the boundary:

- `FrameEnvelope` carries pixels or a native frame, dimensions, 90 kHz PTS,
  optional UTC microseconds, program/video PID, and correlated KLV.
- `FrameMetadataCorrelator` joins synchronous metadata to frames on a bounded,
  rollover-safe PTS timeline.
- `AsyncFrameQueue` applies an explicit overload policy to decoded frames.
- `Detection` represents a model-neutral target with a positive ID, half-open
  pixel box, confidence, label, algorithm ID, lifecycle status, and optional
  ground location.
- `InferenceStage`, `Sequential`, and `Parallel` compose sync functions, worker
  functions, and async service calls.
- `VMTIMetadataEmitter` converts a named inference result into a timed ST 0601
  packet containing ST 0903 VMTI.

The focused programs in
[`examples/ai_sidecar`](https://github.com/trane293/stanag4609/tree/main/examples/ai_sidecar)
cover video decoding, frame correlation, bounded queues, graph composition,
Ultralytics, ONNX Runtime, Triton, JSON/HTTP, ontology metadata, and VMTI
emission.

## Decode frames and correlate metadata

Install PyAV when the application needs a first-party video decoder:

```console
python -m pip install 'stanag4609[video-pyav]'
```

```python
from stanag4609.sidecar import PyAVFrameSource

for frame in PyAVFrameSource("flight.ts", video_pid=0x101):
    print(frame.sequence_number, frame.pts, frame.width, frame.height)
```

BGR arrays are returned by default. Set `pixel_format=None` for native PyAV
frames or request another FFmpeg pixel format. Timestamps are rescaled exactly
to the 90 kHz transport clock. Media-relative PTS is never treated as UTC.

Observe timed metadata before correlating a decoded frame:

```python
from stanag4609.sidecar import CorrelationMode, FrameMetadataCorrelator

correlator = FrameMetadataCorrelator(
    mode=CorrelationMode.LATEST,
    maximum_delta_ticks=63_000,
    max_packets=1_024,
)
for event in transform_batch.metadata:
    correlator.observe(event)

correlated_frame = correlator.correlate(decoded_frame)
```

`EXACT` requires equal effective PTS. `LATEST` selects the newest synchronous
metadata that has become relevant. `NEAREST` is useful when an integration has
a known bounded sampling offset; ties select the earlier timestamp. State is
isolated by program and handles 33-bit PTS rollover.

Asynchronous KLV is not attached automatically because ST 1402 does not
guarantee its display synchronization. Present it as a separate feed or apply a
documented application-specific association rule.

## Bound latency and observe inference

```python
from stanag4609.sidecar import AsyncFrameQueue, FrameOverflowPolicy

frames = AsyncFrameQueue(capacity=4, policy=FrameOverflowPolicy.DROP_OLDEST)
result = await frames.put(correlated_frame)
if result.dropped is not None:
    print("dropped", result.dropped.sequence_number)

frame = await frames.get()
try:
    completed = await graph.run(InferenceContext(frame))
finally:
    frames.task_done()
```

Use `BLOCK` when every frame matters and upstream backpressure is safe.
`DROP_OLDEST` favors live latency, `DROP_NEWEST` finishes queued work, and
`RAISE` turns overload into an immediate error. Queue capacity is always
bounded.

Pass one `InferenceMetrics` instance to stages that should share health data.
Snapshots include starts, successes, failures, timeouts, cancellations,
in-flight work, and duration totals. The collector bounds stage names and does
not retain frames, payloads, exception messages, or tracebacks.

## Use Ultralytics

```console
python -m pip install 'stanag4609[ai-ultralytics]'
```

```python
from ultralytics import YOLO

from stanag4609.sidecar import InferenceStage, UltralyticsYOLODetector

model = YOLO("vehicle-model.pt")
detector = UltralyticsYOLODetector(
    model,
    algorithm_id=1,
    mode="track",
    predict_kwargs={"conf": 0.35, "iou": 0.6, "tracker": "bytetrack.yaml"},
)
vehicle_stage = InferenceStage("vehicles", detector, threaded=True)
```

The adapter clips boxes to the frame and converts fractional coordinates to the
half-open pixel convention. Tracking uses `persist=True` by default, so ordered
frames on one adapter can keep stable IDs. `track_id_offset` maps a zero-based
tracker domain into ST 0903's positive IDs. Prediction order is only a per-frame
identifier and should not be presented as a track.

## Use ONNX Runtime

ONNX defines graphs and tensors, not image normalization, output layouts,
non-maximum suppression, or class catalogs. The adapter therefore requires
model-specific input and output hooks:

```python
import onnxruntime as ort

from stanag4609.sidecar import InferenceStage, OnnxRuntimeAdapter

session = ort.InferenceSession(
    "vehicle-detector.onnx",
    providers=["CPUExecutionProvider"],
)
adapter = OnnxRuntimeAdapter(
    session,
    input_builder=preprocess_frame,
    output_decoder=decode_detections,
)
onnx_stage = InferenceStage("onnx-vehicles", adapter, threaded=True)
```

Install the CPU runtime with `python -m pip install 'stanag4609[ai-onnx]'`.
GPU users can install their chosen ONNX Runtime package and provider separately.

## Call HTTP or Triton services

`HTTPJSONAdapter` uses explicit request and response hooks, bounds both payloads,
checks status codes, and moves blocking standard-library I/O off the event loop:

```python
from stanag4609.sidecar import HTTPJSONAdapter, InferenceStage

adapter = HTTPJSONAdapter(
    "https://inference.example/v1/vehicles",
    request_encoder=encode_request,
    response_decoder=decode_response,
    timeout_seconds=0.5,
    max_request_bytes=4 * 1024 * 1024,
    max_response_bytes=512 * 1024,
)
http_stage = InferenceStage("http-vehicles", adapter, timeout_seconds=0.6)
```

Credentials, TLS trust, retries, and the service schema remain application
configuration.

For NVIDIA Triton, install either `ai-triton-http` or `ai-triton-grpc` and pass
an official AsyncIO client to `TritonAsyncAdapter`. Tensor construction and
output decoding remain explicit model hooks. Cancellation from an
`InferenceStage` timeout propagates through the awaited request.

## Compose model stages

```python
from stanag4609.sidecar import Parallel, Sequential

graph = Sequential(
    Parallel(vehicle_stage, onnx_stage, max_concurrency=2),
    fusion_stage,
    tracker_stage,
)
```

Parallel branches receive the same immutable context and merge in declaration
order. Put fusion, classification, or tracking after the branches whose results
they consume.

## Encode detections as VMTI

```python
from stanag4609.sidecar import VMTIMetadataEmitter

emitter = VMTIMetadataEmitter(
    "tracker",
    metadata_pid=0x120,
    leap_seconds=mission_leap_seconds,
    algorithms=algorithms,
    ontologies=ontologies,
    ontology_by_label={"truck": 12},
)
packet = emitter(completed)
batch = transformer.emit_metadata(packet)
```

With one matching correlated ST 0601 parent, the emitter preserves its PID,
service identity, and unrelated or unknown fields while updating Item 2 and
Item 74. Without a parent, it builds a minimal packet on the selected, already
declared metadata PID.

A frame UTC timestamp is required. The MISP leap-second offset comes from the
emitter or a correlated parent's Item 136; Item 137 is retained when present.
The emitter refuses to write Unix microseconds directly into the MISP field.

Algorithm IDs and ontology labels must resolve within the emitted VMTI packet.
An optional application-owned ontology resolver can validate OWL entities and
labels from a trusted map, cache, database, or service. The codec performs no
network request while decoding KLV.

Attach an absolute `Location` to a `Detection` when a geolocation stage has a
defensible WGS-84 result. A pixel box alone does not establish a ground point;
camera-ray and terrain intersection belong in a sensor-specific stage.

Use one bounded `VMTILifecycleState` per process and sensor stream when you need
to validate state transitions or target-ID reuse after `Dropped`. A mid-stream
join should not assume that the first observed target state is its creation.

The [ST 0903 requirement trace](requirements/ST0903.6.md) records the supported
wire behavior and validation context. The [sidecar API reference](api/sidecar.md)
lists the complete public types.

## Know the deployment boundary

The sidecar covers bounded queues, PTS correlation, model graphs, common local
and remote adapters, boxes, tracking IDs, VMTI emission, and lifecycle checks.
Model weights, preprocessing policy, segmentation/pose/oriented-box decoding,
adaptive streaming, WebRTC, credentials, and inference capacity planning remain
part of the application or deployment.
