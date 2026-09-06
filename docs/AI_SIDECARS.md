# AI inference sidecars

`stanag4609.sidecar` is a first-party, dependency-free integration boundary for
decoded video frames, local models, remote inference services, and ST 0903 VMTI
output. Model runtimes remain optional; the timing and metadata contracts do
not depend on NumPy, PyTorch, CUDA, or a particular video decoder.

## Core model

- `FrameEnvelope` carries a decoded frame handle, dimensions, MPEG PTS, optional
  UTC microseconds, program/video PID identity, and the KLV packets correlated
  with that frame.
- `FrameMetadataCorrelator` attaches synchronous KLV using per-program 33-bit
  PTS timelines, rollover-safe exact/latest/nearest policies, a configurable
  sampling offset, and a bounded packet cache. It can derive frame UTC from a
  correlated MISB Precision Time Stamp without floating-point conversion.
- `PyAVFrameSource` optionally decodes common FFmpeg-supported video into BGR
  arrays or native frames while preserving transport-clock timing.
- `AsyncFrameQueue` bounds decoded-frame memory and latency with explicit
  block, drop-oldest, drop-newest, or raise-on-overload policies. Every enqueue
  reports the exact dropped frame and cumulative accepted/drop counters.
- `PixelBoundingBox` uses the familiar zero-based, half-open
  `[left, top, right, bottom)` convention.
- `Detection` is a model-neutral object detection with a stable target ID,
  confidence, optional label and algorithm ID, and VMTI lifecycle status.
- `InferenceStage` adapts a normal function, a function isolated in a worker
  thread, or an async service call.
- `Sequential` and `Parallel` compose nested processing graphs. Parallel
  branches receive the same immutable context, concurrency is bounded, and
  results merge in declaration order regardless of completion order.
- `encode_embedded_vmti` converts detections to an ST 0903.6 VMTI Local Set for
  ST 0601 Item 74, including the explicit conversion to one-based row-major
  pixel numbering.

Runnable, dependency-free examples live in
[`examples/ai_sidecar`](https://github.com/trane293/stanag4609/tree/main/examples/ai_sidecar):
`pipeline_graph.py` shows
nested sequential/parallel execution, `correlate_frame.py` joins a decoded
frame to synchronous KLV, `decode_video.py` turns PyAV output into public frame
envelopes, `queue_frames.py` demonstrates low-latency overload
handling, and `ontology_vmti.py` shows a labeled detection becoming Algorithm,
Ontology, and VObject metadata. `ultralytics_yolo.py` is the optional packaged
YOLO adapter example; `onnx_runtime.py` demonstrates the model-specific hooks
used by the packaged ONNX Runtime adapter, `triton_async.py` constructs a
cancellable remote inference stage, and `http_json.py` adapts an ordinary
bounded JSON endpoint. `emit_vmti_packet.py` closes the loop from a named
inference result to timed ST 0601.

## Decode video into frame envelopes

Install the optional backend and iterate a file, URL, or file-like object:

```console
pip install 'stanag4609[video-pyav]'
```

```python
from stanag4609.sidecar import PyAVFrameSource

for frame in PyAVFrameSource("flight.ts", video_pid=0x101):
    # BGR pixels by default; PTS is always on the 90 kHz transport clock.
    model_input = frame.pixels
```

The adapter selects video stream zero by default and accepts `video_stream=N`
for other tracks. Set `pixel_format=None` to retain native PyAV `VideoFrame`
objects, or request a different FFmpeg pixel format for model-specific
preprocessing. Native timestamps are rescaled with exact rational arithmetic
and wrapped into the unsigned 33-bit MPEG PTS domain. Absolute UTC is not
guessed from media-relative PTS; correlate synchronous KLV when it is needed.

## Correlate KLV with decoded frames

```python
from stanag4609.sidecar import CorrelationMode, FrameMetadataCorrelator

correlator = FrameMetadataCorrelator(
    mode=CorrelationMode.LATEST,
    maximum_delta_ticks=63_000,  # 0.7 seconds on the 90 kHz PTS clock
    metadata_pts_offset_ticks=0,
    max_packets=1024,
)

for packet in transform_batch.metadata:
    correlator.observe(packet)

frame = correlator.correlate(decoded_frame_envelope)
```

`EXACT` requires equal effective PTS values. `LATEST` follows ST 1402's rule
that a synchronous metadata PTS signals when the access unit becomes relevant.
`NEAREST` is useful only when an integration has a known bounded sampling
offset. A tie selects the earlier relevance time. All policies isolate PTS
epochs by program and handle the 33-bit rollover.

Asynchronous KLV is counted by the correlator but never attached automatically:
ST 1402 explicitly says that its synchronization through decoding cannot be
guaranteed. An application may present asynchronous packets as an independent
metadata feed, or supply its own documented timestamp-association policy.

Segmentation models can emit standards-native `VMaskLocalSet` values using a
clockwise row-major `pixel_contour`, a tuple of `PixelRun` values, or both.
`validate_for_frame(width, height)` checks contour direction and every pixel
bound before the mask is placed in VTarget Item 101. Converting a model's
bitmap or polygon tensor to that model-neutral representation remains explicit
application post-processing because thresholding and contour simplification
change the semantic result.

## Bound live inference latency

```python
from stanag4609.sidecar import AsyncFrameQueue, FrameOverflowPolicy

frames = AsyncFrameQueue(
    capacity=4,
    policy=FrameOverflowPolicy.DROP_OLDEST,
)

result = await frames.put(correlated_frame)
if result.dropped is not None:
    metrics.increment("frames_dropped", sequence=result.dropped.sequence_number)

frame = await frames.get()
try:
    inference_result = await graph.run(InferenceContext(frame))
finally:
    frames.task_done()
```

Use `BLOCK` when every frame must be processed and upstream backpressure is
safe. `DROP_OLDEST` keeps a live display close to real time; `DROP_NEWEST`
finishes already-queued work; `RAISE` makes overload explicit for archival or
test pipelines. The queue never silently grows beyond its declared capacity.

## Observe inference health and latency

Share one bounded `InferenceMetrics` collector across local, HTTP, Triton, and
nested graph stages:

```python
from stanag4609.sidecar import InferenceMetrics, InferenceStage

metrics = InferenceMetrics(max_stages=64)
detector = InferenceStage("vehicle-detector", model, metrics=metrics)
remote = InferenceStage(
    "remote-classifier",
    service,
    timeout_seconds=0.2,
    metrics=metrics,
)

for stage in metrics.snapshot():
    export_gauge("inference_in_flight", stage.in_flight, stage=stage.stage)
    export_counter("inference_failed", stage.failed, stage=stage.stage)
    export_counter("inference_timed_out", stage.timed_out, stage=stage.stage)
    export_counter("inference_cancelled", stage.cancelled, stage=stage.stage)
    export_counter("inference_duration_seconds", stage.total_duration_seconds,
                   stage=stage.stage)
```

Snapshots are immutable and stage-name sorted. The collector records starts,
successes, failures, timeouts, cancellations, in-flight work, total/max/last
monotonic duration, and only the last exception's type name. It never retains
frames, metadata, inference payloads, exception messages, or tracebacks, and
its stage cardinality is explicitly bounded. The collector is thread-safe so
the same instance can span concurrent branches and worker-thread stages.

## Bring a local model

Install the optional adapter dependency without changing the core install:

```console
pip install 'stanag4609[ai-ultralytics]'
```

Pass an already-created model to the first-party adapter. It consumes the
documented `Results.boxes.xyxy`, `conf`, `cls`, and optional tracker `id`
arrays, clips coordinates to the frame, and converts them to the model-neutral
contract:

```python
from ultralytics import YOLO

from stanag4609.sidecar import InferenceStage, UltralyticsYOLODetector

model = YOLO("your-vehicle-model.pt")
detector = UltralyticsYOLODetector(
    model,
    algorithm_id=1,
    mode="track",
    predict_kwargs={"conf": 0.35, "iou": 0.6, "tracker": "bytetrack.yaml"},
)
vehicle_stage = InferenceStage("vehicle-detector", detector, threaded=True)
```

The adapter uses `floor` for left/top and `ceil` for right/bottom so fractional
model boxes retain all covered pixels. In `track` mode it calls Ultralytics
tracking with `persist=True` by default; keep ordered frames on the same adapter
instance. The configurable `track_id_offset` (default `1`) maps Ultralytics'
zero-based tracker domain into ST 0903's positive target IDs. In `predict` mode,
or before a tracker confirms a track, result order is used only as a per-frame
identifier and must not be described as persistent identity.

The adapter follows the official
[Ultralytics prediction](https://docs.ultralytics.com/modes/predict/) and
[tracking](https://docs.ultralytics.com/modes/track/) result contracts
and accepts duck-typed result objects in tests, so importing `stanag4609` never
imports Ultralytics or PyTorch.

## Bring any JSON inference service

`HTTPJSONAdapter` turns an ordinary HTTP or HTTPS JSON endpoint into an async
inference processor without adding an HTTP dependency. The two hooks make the
wire schema explicit: one serializes the frame and earlier graph results, and
the other converts service output to `InferenceOutput`.

```python
from stanag4609.sidecar import HTTPJSONAdapter, InferenceStage

adapter = HTTPJSONAdapter(
    "https://inference.example/v1/vehicles",
    request_encoder=encode_frame_as_service_json,
    response_decoder=decode_service_json,
    headers={"Authorization": f"Bearer {token}"},
    timeout_seconds=0.5,
    max_request_bytes=4 * 1024 * 1024,
    max_response_bytes=512 * 1024,
)
http_stage = InferenceStage("http-vehicles", adapter, timeout_seconds=0.6)
```

The adapter emits UTF-8 `application/json`, bounds both request and response,
checks declared and actual response sizes, rejects non-success status codes,
and runs blocking standard-library I/O outside the event loop. The endpoint,
credentials, TLS trust, retry policy, and model schema remain application
configuration. Keep the stage timeout slightly larger than the adapter's
socket timeout. For high-throughput connection pooling, streaming tensors, or
shared memory, use Triton or write an async processor around the organization's
chosen HTTP client.

## Bring a Triton inference service

Install either transport used by NVIDIA's official AsyncIO clients:

```console
pip install 'stanag4609[ai-triton-grpc]'
pip install 'stanag4609[ai-triton-http]'
```

The same adapter accepts the gRPC or HTTP AsyncIO client because both expose an
awaitable `infer()` method. Tensor construction and output decoding remain
explicit because they belong to the deployed model contract, not to STANAG
4609:

```python
from stanag4609.sidecar import InferenceStage, TritonAsyncAdapter

adapter = TritonAsyncAdapter(
    triton_async_client,
    model_name="vehicles",
    input_builder=build_triton_inputs,
    output_decoder=decode_triton_response,
    requested_outputs=(boxes_output, scores_output),
    request_id_builder=lambda context: str(context.frame.sequence_number),
)
triton_stage = InferenceStage("triton-vehicles", adapter, timeout_seconds=0.150)
```

Reserved request fields are first-class adapter arguments. The shared Triton
request timeout is `timeout`; gRPC additionally accepts `client_timeout`.
Headers, transport-specific compression, sequence flags, and parameters pass
through `infer_kwargs`. Unsupported options are deliberately left for the
installed client to reject rather than silently translated. Stage timeout
cancellation propagates through the awaited Triton request. NVIDIA's official
clients provide HTTP and gRPC APIs plus shared-memory transports; those objects
remain on the adapter side of the boundary. See the
[Triton client documentation](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/client/README.html).

## Sequential and parallel models

Graphs may be nested. In this example the two detectors run concurrently, then
fusion and tracking consume both results in a deterministic order.

```python
from stanag4609.sidecar import InferenceContext, Parallel, Sequential

graph = Sequential(
    Parallel(
        vehicle_stage,
        triton_stage,
        max_concurrency=2,
    ),
    fusion_stage,
    tracker_stage,
)

context = await graph.run(InferenceContext(frame))
tracked = context.result("tracker")
```

ONNX Runtime is particularly relevant because its stable `InferenceSession`
API selects hardware-specific execution providers without coupling the FMV
pipeline to one framework. See the
[official ONNX Runtime Python API](https://onnxruntime.ai/docs/api/python/api_summary).

## Run an ONNX model

ONNX standardizes model graphs and tensors, but not object-detection tensor
layouts, image normalization, non-maximum suppression, or class catalogs. The
first-party adapter therefore owns session invocation and input-name
validation while requiring explicit model-specific hooks:

```python
import onnxruntime as ort

from stanag4609.sidecar import InferenceStage, OnnxRuntimeAdapter

session = ort.InferenceSession(
    "vehicle-detector.onnx",
    providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
)
adapter = OnnxRuntimeAdapter(
    session,
    input_builder=preprocess_frame_to_named_tensors,
    output_decoder=decode_model_outputs_to_inference_output,
)
onnx_stage = InferenceStage("onnx-vehicles", adapter, threaded=True)
```

Install the CPU runtime with `pip install 'stanag4609[ai-onnx]'`. GPU users can
install the appropriate ONNX Runtime package and execution provider themselves,
then install the base `stanag4609` package without that extra. `output_names`
can limit session outputs; input-name validation is enabled by default and can
be disabled only for unusual session wrappers. The adapter uses the official
[`InferenceSession.run()` contract](https://onnxruntime.ai/docs/api/python/api_summary.html)
without importing ONNX Runtime or NumPy from the core package.

## Convert detections to VMTI

The high-level emitter selects a named graph result and creates a
`TimedKLVPacket` at the decoded frame's PTS:

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
packet = emitter(context)
batch = transformer.emit_metadata(packet)
transport_sink.write(batch.transport)
```

When `context.frame.metadata` contains exactly one matching synchronous ST 0601
packet, its PID/service identity and every unrelated or unknown field are
preserved while the Precision Time Stamp and Item 74 are refreshed for the
frame. `metadata_pid` may disambiguate multiple correlated parents. With no
parent, the emitter requires that PID and builds a minimal ST 0601 packet on the
already-declared stream. A frame UTC timestamp is always required. Its
leap-second offset must come from `leap_seconds` or a correlated parent's Item
136; the emitter refuses to write UTC/POSIX microseconds directly into MISP
Item 2. Parent Item 137 is retained and included in the inverse conversion.

The lower-level conversion API remains available when an application needs to
control parent mutation itself:

```python
from datetime import datetime, timedelta, timezone

from stanag4609 import (
    AlgorithmLocalSet,
    FieldDecodingMode,
    OntologyLocalSet,
    ST0601ValidationContext,
    UASLocalSet,
    VMTIValidationContext,
    update_uas_local_set,
    utc_to_misp_timestamp,
)
from stanag4609.sidecar import encode_embedded_vmti

output = context.result("tracker")
algorithms = (
    AlgorithmLocalSet(7, "vehicle-detector", "1.0", "detector", 1),
)
ontologies = (
    OntologyLocalSet(
        ontology_id=12,
        ontology_iri="https://example.org/fmv-objects.owl",
        entity_iri="https://example.org/fmv-objects.owl#Truck",
        label="truck",
    ),
)
parent = next(
    packet for packet in frame.metadata if isinstance(packet.decoded, UASLocalSet)
)
leap_seconds = parent.decoded.value(136)
correction_offset = parent.decoded.value(137, 0)
frame_utc = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
    microseconds=frame.timestamp_microseconds
)
frame_misp_microseconds = utc_to_misp_timestamp(
    frame_utc,
    leap_seconds=leap_seconds,
    correction_offset=correction_offset,
)
vmti = encode_embedded_vmti(
    frame,
    output.detections,
    system_name="vehicle-pipeline",
    source_sensor="EO Nose",
    algorithms=algorithms,
    ontologies=ontologies,
    ontology_by_label={"truck": 12},
    leap_seconds=leap_seconds,
    correction_offset=correction_offset,
)

updated_bytes = update_uas_local_set(
    parent.decoded,
    {2: frame_misp_microseconds, 74: vmti},
    field_decoding=FieldDecodingMode.PRESERVE,
    context=ST0601ValidationContext(
        metadata_birth_timestamp=frame_misp_microseconds,
        vmti_context=VMTIValidationContext(
            vmti_frame_timestamp=frame_misp_microseconds,
            frame_width=frame.width,
            frame_height=frame.height,
        ),
    ),
)
updated_packet = parent.with_bytes(updated_bytes)
batch = transformer.emit_metadata(updated_packet)
transport_sink.write(batch.transport)
```

Supplying the frame facts as external context makes the relay prove that the
parent ST 0601 Item 2 represents the metadata time of birth and that Item 74
describes the same image dimensions and instant. The ST 0601 bridge derives
the VMTI parent timestamp from Item 2. A mismatch fails the update instead of
silently associating new AI results with the wrong video instant.

Every `Detection.algorithm_id` must resolve to an `AlgorithmLocalSet` in the
same VMTI packet. Likewise, `ontology_by_label` maps a convenient model label
to an `OntologyLocalSet`; the bridge emits a standards-native VObject rather
than smuggling the label into an arbitrary text field. The core validates
packet-local references without fetching an ontology. Verifying that an IRI
identifies OWL content and that its entity and label match is available through
an application-supplied resolver.

### Validate ontology semantics without hidden network access

Implement `OntologyResolver` when a producer or receiver has a trusted local
vocabulary, ontology cache, database, or controlled ontology service. The KLV
codec never performs network I/O itself:

```python
from stanag4609 import (
    OntologyEntityResolution,
    VMTIValidationContext,
    decode_vmti_local_set,
)


class VehicleOntologyResolver:
    def resolve_entity(self, ontology_iri: str, entity_iri: str):
        known = {
            (
                "https://example.org/fmv-objects.owl",
                "https://example.org/fmv-objects.owl#Truck",
            ): OntologyEntityResolution(
                ontology_iri="https://example.org/fmv-objects.owl",
                entity_iri="https://example.org/fmv-objects.owl#Truck",
                is_owl_ontology=True,
                rdfs_labels=frozenset({"truck"}),
                skos_preferred_labels=frozenset({"cargo truck"}),
            )
        }
        return known.get((ontology_iri, entity_iri))


vmti = decode_vmti_local_set(
    vmti_bytes,
    standalone=False,
    context=VMTIValidationContext(
        ontology_resolver=VehicleOntologyResolver(),
    ),
)
```

For every Ontology Local Set, the resolver proves that the referenced document
is an OWL ontology and the entity belongs to it. When Item 6 supplies a label,
the validator requires an exact, case-sensitive match against either the
resolved `rdfs:label` or `skos:prefLabel`. Returning `None` rejects an unknown
entity. Omitting the resolver preserves structural, offline decoding.

## Validate frame and parent context

Some ST 0903 requirements depend on facts outside the VMTI Local Set itself.
Pass those facts explicitly when encoding or decoding instead of relying on
ambient state:

```python
from stanag4609 import VMTIValidationContext, decode_vmti_local_set

validation = VMTIValidationContext(
    vmti_frame_timestamp=frame.utc_microseconds,
    parent_timestamp=parent_timestamp,
    frame_period_microseconds=1_000_000 / frame_rate,
    frame_width=frame.width,
    frame_height=frame.height,
    total_targets_detected=len(all_model_detections),
    different_image_source=False,
)
vmti = decode_vmti_local_set(
    vmti_bytes,
    standalone=False,
    context=validation,
)
```

The context checks timestamp agreement, conditional embedded timestamps,
whether a culled target subset needs the total target count, pixel-number
frame-width declarations, declared frame dimensions, the two-frame limit for
parent-relative offsets, and the FOV/MIIS fields required when VMTI describes a
different image source. When decoding ST 0601 Item 74,
applications can re-decode that item's retained bytes with this context once
the video-frame and parent timing facts are known.

Resolve an embedded target's geospatial position with the parent frame center:

```python
from stanag4609 import resolve_vtarget_location

for target in vmti.targets:
    location = resolve_vtarget_location(
        target,
        frame_center_latitude=uas.value(23),
        frame_center_longitude=uas.value(24),
    )
    if location is not None:
        publish_detection(target.target_id, location.latitude, location.longitude)
```

Absolute VTarget Location Item 17 wins when present; otherwise the resolver
adds embedded Items 10 and 11 to the parent ST 0601 frame center and retains
Item 12 as HAE.

An inference or geolocation stage can attach a known absolute WGS-84 position
directly to the model-neutral detection before VMTI emission:

```python
from stanag4609 import Location
from stanag4609.sidecar import Detection, PixelBoundingBox

detection = Detection(
    target_id=42,
    bounding_box=PixelBoundingBox(100, 200, 301, 401),
    confidence=0.965,
    label="truck",
    algorithm_id=3,
    location=Location(49.2827, -123.1207, 112.0),
)
```

`VMTIMetadataEmitter` writes the complete `Location` as VTarget Item 17. The
player and GeoJSON adapter then expose the same latitude, longitude, HAE, and
location source. A pixel bounding box alone does not determine a ground
position: use a sensor-specific ray/terrain intersection or other explicit
geolocator stage, and omit `location` when no defensible position is available.

## Validate target lifecycles

Use one bounded state object per VMTI process/sensor stream to detect invalid
state transitions and target-ID reuse after `Dropped`:

```python
from stanag4609 import VMTILifecycleState

lifecycle = VMTILifecycleState(max_target_ids=100_000)
for vmti in decoded_vmti_packets:
    snapshot = lifecycle.observe(vmti)
    for issue in snapshot.issues:
        metrics.increment(issue.code, target_id=issue.target_id)
```

Targets omitted from a packet remain in history because ST 0903 permits a
producer to report only a subset of its target list. A target observed as
`Dropped` remains retired so later reuse is visible. Set
`assume_stream_start=True` only when the receiver knows it began at the
producer's lifecycle boundary; otherwise a first observation of `Inactive` or
`Coasting` is valid for a mid-stream join.

## Live-system responsibilities still in progress

The graph API already provides bounded branch concurrency, timeouts, immutable
inputs, deterministic results, a bounded synchronous PTS correlator, an
explicit-overflow decoded-frame queue, optional PyAV video decoding, and
cross-frame VMTI lifecycle checks. The optional Ultralytics adapter currently
covers object-detection boxes but not segmentation masks,
pose, oriented boxes, or an integrated tracking invocation. The reference
player renders synchronized pixel bounding boxes and centroids in both
recorded and bounded low-latency MSE modes. Sub-second WebRTC delivery,
adaptive bitrate, and inference scheduling remain deployment concerns;
applications should not infer those capabilities merely from the stable
sidecar data model.
