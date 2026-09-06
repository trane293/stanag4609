from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from stanag4609.errors import LimitExceeded
from stanag4609.sidecar import (
    Detection,
    FrameEnvelope,
    InferenceContext,
    InferenceOutput,
    InferenceResult,
    InferenceStage,
    OnnxRuntimeAdapter,
    Parallel,
    PixelBoundingBox,
    Sequential,
    TritonAsyncAdapter,
    UltralyticsYOLODetector,
    VMTIMetadataEmitter,
    encode_embedded_vmti,
)
from stanag4609.st0601 import UASLocalSet, encode_uas_local_set
from stanag4609.st0903 import (
    AlgorithmLocalSet,
    DetectionStatus,
    Location,
    OntologyLocalSet,
    decode_vmti_local_set,
    encode_location,
    resolve_vtarget_location,
)
from stanag4609.transport.processor import TimedKLVPacket
from stanag4609.transport.psi import KLVCarriage


def _frame() -> FrameEnvelope:
    return FrameEnvelope(
        sequence_number=7,
        pts=90_000,
        width=1920,
        height=1080,
        pixels=b"adapter-owned-frame",
        timestamp_microseconds=1_700_000_000_000_000,
    )


def test_ai_detection_encodes_to_one_based_vmti_pixel_coordinates() -> None:
    detection = Detection(
        target_id=42,
        bounding_box=PixelBoundingBox(left=100, top=200, right=301, bottom=401),
        confidence=0.965,
        label="truck",
        algorithm_id=3,
        status=DetectionStatus.ACTIVE_MOVING,
    )
    encoded = encode_embedded_vmti(
        _frame(),
        (detection,),
        system_name="acme-truck-detector",
        source_sensor="EO Nose",
        algorithms=(AlgorithmLocalSet(3, "truck-detector", "1.0"),),
        ontologies=(
            OntologyLocalSet(
                12,
                "https://example.org/objects.owl",
                "https://example.org/objects.owl#Truck",
                label="truck",
            ),
        ),
        ontology_by_label={"truck": 12},
        leap_seconds=29,
    )
    vmti = decode_vmti_local_set(encoded, standalone=False)
    assert vmti.value(2) == datetime.fromtimestamp(1_700_000_029, tz=timezone.utc)
    assert vmti.value(3) == "acme-truck-detector"
    assert vmti.value(4) == 6
    assert vmti.value(5) == 1
    assert vmti.value(6) == 1
    assert vmti.value(8) == 1920
    assert vmti.value(9) == 1080
    assert vmti.value(10) == "EO Nose"

    target = vmti.targets[0]
    assert target.target_id == 42
    assert target.value(2) == 384_101  # row 201, column 101
    assert target.value(3) == 768_301  # row 401, column 301
    assert target.value(19) == 301
    assert target.value(20) == 201
    assert target.value(1) == 576_201
    assert target.value(5) == 97
    assert target.value(22) == 3
    assert target.value(23) is DetectionStatus.ACTIVE_MOVING
    assert target.value(107)[0].ontology_id == 12
    assert target.value(107)[0].confidence == pytest.approx(97.0, abs=0.5)


def test_geolocated_ai_detection_encodes_absolute_vmti_target_location() -> None:
    location = Location(
        49.2827,
        -123.1207,
        112.0,
        sigma_east=1.0,
        sigma_north=2.0,
        sigma_up=3.0,
        rho_east_north=0.1,
        rho_east_up=0.2,
        rho_north_up=0.3,
    )
    detection = Detection(
        42,
        PixelBoundingBox(100, 200, 301, 401),
        0.965,
        location=location,
    )

    vmti = decode_vmti_local_set(
        encode_embedded_vmti(_frame(), (detection,), leap_seconds=29),
        standalone=False,
    )
    resolved = resolve_vtarget_location(vmti.targets[0])

    assert resolved is not None
    assert resolved.target_id == 42
    assert resolved.latitude == pytest.approx(49.2827, abs=1e-5)
    assert resolved.longitude == pytest.approx(-123.1207, abs=1e-5)
    assert resolved.hae == pytest.approx(112.0, abs=1)
    assert resolved.source == "absolute"
    assert encode_location(vmti.targets[0].value(17)) == encode_location(location)


def test_low_level_vmti_encoder_requires_utc_to_misp_conversion_context() -> None:
    with pytest.raises(ValueError, match="leap_seconds"):
        encode_embedded_vmti(_frame(), ())


@pytest.mark.parametrize(
    "coordinates",
    [
        (0, 0, 0, 1),
        (0, 0, 1, 0),
        (-1, 0, 1, 1),
    ],
)
def test_invalid_half_open_ai_boxes_are_rejected(
    coordinates: tuple[int, int, int, int],
) -> None:
    with pytest.raises(ValueError):
        PixelBoundingBox(*coordinates)


def test_bounding_box_dimensions_are_exposed() -> None:
    box = PixelBoundingBox(10, 20, 31, 42)
    assert box.width == 21
    assert box.height == 22


def test_boxes_outside_the_frame_and_invalid_confidence_are_rejected() -> None:
    with pytest.raises(ValueError, match="frame"):
        encode_embedded_vmti(
            _frame(),
            (Detection(1, PixelBoundingBox(0, 0, 1921, 10), 0.5),),
            leap_seconds=29,
        )
    with pytest.raises(ValueError, match="confidence"):
        Detection(1, PixelBoundingBox(0, 0, 1, 1), 1.1)


def test_label_catalog_and_algorithm_references_are_required() -> None:
    labeled = Detection(1, PixelBoundingBox(0, 0, 1, 1), 0.5, label="truck")
    with pytest.raises(ValueError, match="no ontology mapping"):
        encode_embedded_vmti(_frame(), (labeled,), leap_seconds=29)
    with pytest.raises(TypeError, match="ontology_by_label"):
        encode_embedded_vmti(
            _frame(),
            (labeled,),
            ontology_by_label=[],  # type: ignore[arg-type]
            leap_seconds=29,
        )

    attributed = Detection(1, PixelBoundingBox(0, 0, 1, 1), 0.5, algorithm_id=7)
    with pytest.raises(ValueError, match="algorithmId 7"):
        encode_embedded_vmti(_frame(), (attributed,), leap_seconds=29)


@pytest.mark.parametrize(
    "kwargs, error",
    [
        ({"target_id": 0}, ValueError),
        ({"target_id": True}, TypeError),
        ({"bounding_box": (0, 0, 1, 1)}, TypeError),
        ({"confidence": True}, TypeError),
        ({"label": 3}, TypeError),
        ({"algorithm_id": -1}, ValueError),
        ({"status": 1}, TypeError),
        ({"location": (49.0, -123.0, 100.0)}, TypeError),
    ],
)
def test_detection_contract_rejects_ambiguous_values(
    kwargs: dict[str, object], error: type[Exception]
) -> None:
    values: dict[str, object] = {
        "target_id": 1,
        "bounding_box": PixelBoundingBox(0, 0, 1, 1),
        "confidence": 0.5,
    }
    values.update(kwargs)
    with pytest.raises(error):
        Detection(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs, error",
    [
        ({"sequence_number": -1}, ValueError),
        ({"pts": True}, TypeError),
        ({"pts": 2**33}, ValueError),
        ({"width": 0}, ValueError),
        ({"timestamp_microseconds": -1}, ValueError),
        ({"metadata": []}, TypeError),
        ({"program_number": 0}, ValueError),
        ({"program_number": True}, ValueError),
        ({"video_pid": 0x2000}, ValueError),
        ({"video_pid": True}, ValueError),
    ],
)
def test_frame_contract_validates_timing_shape_and_metadata(
    kwargs: dict[str, object], error: type[Exception]
) -> None:
    values: dict[str, object] = {
        "sequence_number": 0,
        "pts": 0,
        "width": 1,
        "height": 1,
        "pixels": b"x",
    }
    values.update(kwargs)
    with pytest.raises(error):
        FrameEnvelope(**values)  # type: ignore[arg-type]


def test_sequential_graph_exposes_prior_results_to_later_stages() -> None:
    observed: list[tuple[str, ...]] = []

    def detector(context: InferenceContext) -> InferenceOutput:
        observed.append(tuple(result.stage for result in context.results))
        return InferenceOutput()

    async def tracker(context: InferenceContext) -> InferenceOutput:
        observed.append(tuple(result.stage for result in context.results))
        return InferenceOutput()

    graph = Sequential(
        InferenceStage("detector", detector),
        InferenceStage("tracker", tracker),
    )
    result = asyncio.run(graph.run(InferenceContext(_frame())))
    assert observed == [(), ("detector",)]
    assert tuple(item.stage for item in result.results) == ("detector", "tracker")
    assert result.result("missing") is None


def test_threaded_stage_and_nested_graphs_compose() -> None:
    def processor(_: InferenceContext) -> InferenceOutput:
        return InferenceOutput(data="worker")

    graph = Sequential(
        Parallel(InferenceStage("worker", processor, threaded=True)),
        InferenceStage("after", lambda _: InferenceOutput()),
    )
    result = asyncio.run(graph.run(InferenceContext(_frame())))
    assert result.result("worker").data == "worker"  # type: ignore[union-attr]


def test_stage_timeout_and_invalid_processor_output_are_explicit() -> None:
    async def slow(_: InferenceContext) -> InferenceOutput:
        await asyncio.sleep(0.02)
        return InferenceOutput()

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(
            InferenceStage("slow", slow, timeout_seconds=0.001).run(InferenceContext(_frame()))
        )
    with pytest.raises(TypeError, match="InferenceOutput"):
        asyncio.run(
            InferenceStage("wrong", lambda _: None).run(InferenceContext(_frame()))  # type: ignore[arg-type]
        )


def test_parallel_graph_is_bounded_and_merges_in_declaration_order() -> None:
    active = 0
    peak = 0

    async def processor(context: InferenceContext) -> InferenceOutput:
        nonlocal active, peak
        assert not context.results
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return InferenceOutput()

    graph = Parallel(
        InferenceStage("a", processor),
        InferenceStage("b", processor),
        InferenceStage("c", processor),
        max_concurrency=2,
    )
    result = asyncio.run(graph.run(InferenceContext(_frame())))
    assert peak == 2
    assert tuple(item.stage for item in result.results) == ("a", "b", "c")


def test_graph_rejects_duplicate_stage_names() -> None:
    stage = InferenceStage("duplicate", lambda _: InferenceOutput())
    with pytest.raises(ValueError, match="unique"):
        Sequential(stage, stage)


def test_graph_and_result_contracts_reject_invalid_configuration() -> None:
    stage = InferenceStage("one", lambda _: InferenceOutput())
    with pytest.raises(ValueError, match="at least one"):
        Sequential()
    with pytest.raises(ValueError, match="max_steps"):
        Sequential(stage, max_steps=0)
    with pytest.raises(LimitExceeded):
        Sequential(stage, InferenceStage("two", lambda _: InferenceOutput()), max_steps=1)
    with pytest.raises(ValueError, match="max_concurrency"):
        Parallel(stage, max_concurrency=0)
    with pytest.raises(ValueError, match="stage"):
        InferenceStage("", lambda _: InferenceOutput())
    with pytest.raises(ValueError, match="timeout"):
        InferenceStage("bad-timeout", lambda _: InferenceOutput(), timeout_seconds=0)
    with pytest.raises(ValueError, match="stage"):
        InferenceResult("", InferenceOutput())
    with pytest.raises(TypeError, match="output"):
        InferenceResult("bad", None)  # type: ignore[arg-type]


def test_context_rejects_duplicate_results_and_vmti_rejects_duplicate_targets() -> None:
    context = InferenceContext(_frame()).with_result(InferenceResult("detector", InferenceOutput()))
    with pytest.raises(ValueError, match="already"):
        context.with_result(InferenceResult("detector", InferenceOutput()))

    detection = Detection(1, PixelBoundingBox(0, 0, 1, 1), 0.5)
    with pytest.raises(ValueError, match="unique"):
        encode_embedded_vmti(_frame(), [detection, detection])


class _Array:
    def __init__(self, value: object) -> None:
        self.value = value

    def tolist(self) -> object:
        return self.value


class _Boxes:
    def __init__(self, *, identifiers: object | None = None) -> None:
        self.xyxy = _Array([[-0.2, 10.2, 100.1, 50.8], [1800.9, 1000.1, 1930, 1100]])
        self.conf = _Array([0.91, 0.72])
        self.cls = _Array([0, 1])
        self.id = None if identifiers is None else _Array(identifiers)


class _Result:
    def __init__(self, boxes: object | None = None) -> None:
        self.boxes = boxes
        self.names = {0: "truck", 1: "car"}


class _YOLO:
    names = ("fallback-truck", "fallback-car")

    def __init__(self, results: object) -> None:
        self.results = results
        self.calls: list[tuple[object, dict[str, object]]] = []
        self.track_calls: list[tuple[object, dict[str, object]]] = []

    def predict(self, *, source: object, **kwargs: object) -> object:
        self.calls.append((source, kwargs))
        return self.results

    def track(self, *, source: object, **kwargs: object) -> object:
        self.track_calls.append((source, kwargs))
        return self.results


def test_ultralytics_adapter_normalizes_boxes_labels_and_configuration() -> None:
    model = _YOLO([_Result(_Boxes())])
    adapter = UltralyticsYOLODetector(
        model,
        algorithm_id=7,
        status=DetectionStatus.ACTIVE_STOPPED,
        predict_kwargs={"conf": 0.4},
    )
    output = adapter(InferenceContext(_frame()))

    assert model.calls == [(b"adapter-owned-frame", {"verbose": False, "conf": 0.4})]
    assert len(output.detections) == 2
    first, second = output.detections
    assert first.target_id == 1
    assert first.bounding_box == PixelBoundingBox(0, 10, 101, 51)
    assert first.confidence == pytest.approx(0.91)
    assert first.label == "truck"
    assert first.algorithm_id == 7
    assert first.status is DetectionStatus.ACTIVE_STOPPED
    assert second.bounding_box == PixelBoundingBox(1800, 1000, 1920, 1080)
    assert output.data is model.results[0]


def test_ultralytics_adapter_preserves_stable_tracker_identity_with_offset() -> None:
    model = _YOLO([_Result(_Boxes(identifiers=[40, 41]))])
    adapter = UltralyticsYOLODetector(
        model,
        track_id_offset=1,
        mode="track",
        predict_kwargs={"tracker": "bytetrack.yaml", "persist": False},
    )
    assert [
        detection.target_id for detection in adapter(InferenceContext(_frame())).detections
    ] == [41, 42]
    assert model.track_calls == [
        (
            b"adapter-owned-frame",
            {"verbose": False, "tracker": "bytetrack.yaml", "persist": False},
        )
    ]


def test_ultralytics_tracking_persists_identity_by_default() -> None:
    model = _YOLO([_Result(_Boxes(identifiers=[3, 4]))])
    UltralyticsYOLODetector(model, mode="track")(InferenceContext(_frame()))

    assert model.calls == []
    assert model.track_calls == [(b"adapter-owned-frame", {"verbose": False, "persist": True})]


def test_ultralytics_adapter_handles_empty_results_and_rejects_bad_shapes() -> None:
    empty_result = _Result()
    assert UltralyticsYOLODetector(_YOLO([empty_result]))(
        InferenceContext(_frame())
    ) == InferenceOutput(data=empty_result)

    with pytest.raises(ValueError, match="exactly one"):
        UltralyticsYOLODetector(_YOLO([]))(InferenceContext(_frame()))
    bad_boxes = _Boxes()
    bad_boxes.conf = _Array([0.5])
    with pytest.raises(ValueError, match="differ in length"):
        UltralyticsYOLODetector(_YOLO([_Result(bad_boxes)]))(InferenceContext(_frame()))

    malformed_box = _Boxes()
    malformed_box.xyxy = _Array([[0, 1, 2], [0, 1, 2, 3]])
    with pytest.raises(ValueError, match="four coordinates"):
        UltralyticsYOLODetector(_YOLO([_Result(malformed_box)]))(InferenceContext(_frame()))


def test_ultralytics_adapter_strictly_validates_result_values() -> None:
    with pytest.raises(TypeError, match="context"):
        UltralyticsYOLODetector(_YOLO([]))(object())  # type: ignore[arg-type]

    not_array = _Boxes()
    not_array.conf = object()
    with pytest.raises(TypeError, match="array-like"):
        UltralyticsYOLODetector(_YOLO([_Result(not_array)]))(InferenceContext(_frame()))

    not_numeric = _Boxes()
    not_numeric.conf = _Array([True, 0.5])
    with pytest.raises(TypeError, match="numeric"):
        UltralyticsYOLODetector(_YOLO([_Result(not_numeric)]))(InferenceContext(_frame()))

    nonfinite = _Boxes()
    nonfinite.conf = _Array([float("nan"), 0.5])
    with pytest.raises(ValueError, match="finite"):
        UltralyticsYOLODetector(_YOLO([_Result(nonfinite)]))(InferenceContext(_frame()))

    fractional_class = _Boxes()
    fractional_class.cls = _Array([0.5, 1])
    with pytest.raises(ValueError, match="class ID"):
        UltralyticsYOLODetector(_YOLO([_Result(fractional_class)]))(InferenceContext(_frame()))

    negative_track = _Boxes(identifiers=[-1, 2])
    with pytest.raises(ValueError, match="track ID"):
        UltralyticsYOLODetector(_YOLO([_Result(negative_track)]))(InferenceContext(_frame()))


def test_ultralytics_adapter_accepts_sequence_name_catalog() -> None:
    result = _Result(_Boxes())
    del result.names
    output = UltralyticsYOLODetector(_YOLO([result]))(InferenceContext(_frame()))
    assert [detection.label for detection in output.detections] == [
        "fallback-truck",
        "fallback-car",
    ]


@pytest.mark.parametrize(
    "kwargs, error",
    [
        ({"algorithm_id": -1}, ValueError),
        ({"status": 1}, TypeError),
        ({"track_id_offset": 0}, ValueError),
        ({"predict_kwargs": []}, TypeError),
        ({"mode": "segment"}, ValueError),
    ],
)
def test_ultralytics_adapter_validates_configuration(
    kwargs: dict[str, object], error: type[Exception]
) -> None:
    with pytest.raises(error):
        UltralyticsYOLODetector(_YOLO([]), **kwargs)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="predict"):
        UltralyticsYOLODetector(object())
    with pytest.raises(TypeError, match="track"):
        UltralyticsYOLODetector(object(), mode="track")


class _OnnxInput:
    def __init__(self, name: object) -> None:
        self.name = name


class _OnnxSession:
    def __init__(self, input_names: tuple[object, ...] = ("images",)) -> None:
        self.input_names = input_names
        self.calls: list[tuple[object, object]] = []
        self.result: object = ["boxes", "scores"]

    def get_inputs(self) -> list[_OnnxInput]:
        return [_OnnxInput(name) for name in self.input_names]

    def run(self, output_names: object, input_feed: object) -> object:
        self.calls.append((output_names, input_feed))
        return self.result


def test_onnx_adapter_validates_tensors_and_decodes_model_specific_output() -> None:
    session = _OnnxSession()
    seen: list[tuple[tuple[object, ...], InferenceContext]] = []

    def decode(values: tuple[object, ...], context: InferenceContext) -> InferenceOutput:
        seen.append((values, context))
        return InferenceOutput(
            (Detection(9, PixelBoundingBox(1, 2, 30, 40), 0.8, "truck"),),
            data=values,
        )

    adapter = OnnxRuntimeAdapter(
        session,
        input_builder=lambda context: {"images": context.frame.pixels},
        output_decoder=decode,
        output_names=("boxes", "scores"),
    )
    context = InferenceContext(_frame())
    output = adapter(context)

    assert session.calls == [((["boxes", "scores"]), {"images": b"adapter-owned-frame"})]
    assert seen == [(("boxes", "scores"), context)]
    assert output.detections[0].label == "truck"


def test_onnx_adapter_can_defer_input_validation_to_runtime() -> None:
    session = _OnnxSession()
    adapter = OnnxRuntimeAdapter(
        session,
        input_builder=lambda _: {"custom": 1},
        output_decoder=lambda values, _: InferenceOutput(data=values),
        validate_input_names=False,
    )
    output = adapter(InferenceContext(_frame()))
    assert output.data == ("boxes", "scores")
    assert session.calls == [(None, {"custom": 1})]


@pytest.mark.parametrize(
    "feed, message",
    [
        ({}, "missing inputs"),
        ({"images": 1, "extra": 2}, "unexpected inputs"),
    ],
)
def test_onnx_adapter_reports_input_name_mismatches(feed: dict[str, object], message: str) -> None:
    adapter = OnnxRuntimeAdapter(
        _OnnxSession(),
        input_builder=lambda _: feed,
        output_decoder=lambda values, _: InferenceOutput(data=values),
    )
    with pytest.raises(ValueError, match=message):
        adapter(InferenceContext(_frame()))


def test_onnx_adapter_rejects_malformed_hooks_session_and_results() -> None:
    session = _OnnxSession()
    with pytest.raises(TypeError, match="context"):
        OnnxRuntimeAdapter(
            session,
            input_builder=lambda _: {},
            output_decoder=lambda values, _: InferenceOutput(data=values),
        )(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="mapping"):
        OnnxRuntimeAdapter(
            session,
            input_builder=lambda _: [],  # type: ignore[arg-type,return-value]
            output_decoder=lambda values, _: InferenceOutput(data=values),
        )(InferenceContext(_frame()))

    session.result = object()
    with pytest.raises(TypeError, match="sequence"):
        OnnxRuntimeAdapter(
            session,
            input_builder=lambda _: {"images": 1},
            output_decoder=lambda values, _: InferenceOutput(data=values),
        )(InferenceContext(_frame()))

    session.result = []
    with pytest.raises(TypeError, match="InferenceOutput"):
        OnnxRuntimeAdapter(
            session,
            input_builder=lambda _: {"images": 1},
            output_decoder=lambda *_: None,  # type: ignore[arg-type,return-value]
        )(InferenceContext(_frame()))


@pytest.mark.parametrize(
    "session, kwargs, message",
    [
        (object(), {}, "run method"),
        (_OnnxSession(), {"input_builder": None}, "input_builder"),
        (_OnnxSession(), {"output_decoder": None}, "output_decoder"),
        (_OnnxSession(), {"output_names": "boxes"}, "output_names"),
        (_OnnxSession(), {"validate_input_names": 1}, "validate_input_names"),
    ],
)
def test_onnx_adapter_validates_configuration(
    session: object, kwargs: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "input_builder": lambda _: {},
        "output_decoder": lambda *_: InferenceOutput(),
    }
    values.update(kwargs)
    with pytest.raises(TypeError, match=message):
        OnnxRuntimeAdapter(session, **values)  # type: ignore[arg-type]

    class RunOnly:
        def run(self, *_: object) -> list[object]:
            return []

    with pytest.raises(TypeError, match="get_inputs"):
        OnnxRuntimeAdapter(
            RunOnly(),
            input_builder=lambda _: {},
            output_decoder=lambda *_: InferenceOutput(),
        )

    malformed_metadata = _OnnxSession((None,))
    adapter = OnnxRuntimeAdapter(
        malformed_metadata,
        input_builder=lambda _: {},
        output_decoder=lambda *_: InferenceOutput(),
    )
    with pytest.raises(TypeError, match="metadata"):
        adapter(InferenceContext(_frame()))


class _TritonClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def infer(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        await asyncio.sleep(0)
        return {"output": "response"}


def test_triton_async_adapter_builds_and_decodes_one_request() -> None:
    client = _TritonClient()
    seen: list[tuple[object, InferenceContext]] = []

    def decode(response: object, context: InferenceContext) -> InferenceOutput:
        seen.append((response, context))
        return InferenceOutput(data=response)

    adapter = TritonAsyncAdapter(
        client,
        model_name="vehicles",
        model_version="7",
        input_builder=lambda context: (context.frame.pixels,),
        output_decoder=decode,
        requested_outputs=("boxes", "scores"),
        request_id_builder=lambda context: f"frame-{context.frame.sequence_number}",
        infer_kwargs={"client_timeout": 0.2},
    )
    context = InferenceContext(_frame())
    output = asyncio.run(adapter(context))

    assert client.calls == [
        {
            "model_name": "vehicles",
            "inputs": [b"adapter-owned-frame"],
            "model_version": "7",
            "outputs": ["boxes", "scores"],
            "request_id": "frame-7",
            "client_timeout": 0.2,
        }
    ]
    assert seen == [({"output": "response"}, context)]
    assert output.data == {"output": "response"}


def test_triton_adapter_composes_as_native_async_inference_stage() -> None:
    adapter = TritonAsyncAdapter(
        _TritonClient(),
        model_name="vehicles",
        input_builder=lambda _: ("input",),
        output_decoder=lambda response, _: InferenceOutput(data=response),
    )
    result = asyncio.run(
        InferenceStage("triton", adapter, timeout_seconds=1).run(InferenceContext(_frame()))
    )
    assert result.result("triton").data == {"output": "response"}  # type: ignore[union-attr]


def test_triton_adapter_rejects_malformed_runtime_values() -> None:
    adapter = TritonAsyncAdapter(
        _TritonClient(),
        model_name="vehicles",
        input_builder=lambda _: (),
        output_decoder=lambda response, _: InferenceOutput(data=response),
    )
    with pytest.raises(TypeError, match="context"):
        asyncio.run(adapter(object()))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="non-empty sequence"):
        asyncio.run(adapter(InferenceContext(_frame())))

    bad_id = TritonAsyncAdapter(
        _TritonClient(),
        model_name="vehicles",
        input_builder=lambda _: (1,),
        output_decoder=lambda response, _: InferenceOutput(data=response),
        request_id_builder=lambda _: 1,  # type: ignore[arg-type,return-value]
    )
    with pytest.raises(TypeError, match="return a string"):
        asyncio.run(bad_id(InferenceContext(_frame())))

    class SyncClient:
        def infer(self, **_: object) -> object:
            return object()

    sync = TritonAsyncAdapter(
        SyncClient(),
        model_name="vehicles",
        input_builder=lambda _: (1,),
        output_decoder=lambda response, _: InferenceOutput(data=response),
    )
    with pytest.raises(TypeError, match="awaitable"):
        asyncio.run(sync(InferenceContext(_frame())))

    bad_output = TritonAsyncAdapter(
        _TritonClient(),
        model_name="vehicles",
        input_builder=lambda _: (1,),
        output_decoder=lambda *_: None,  # type: ignore[arg-type,return-value]
    )
    with pytest.raises(TypeError, match="InferenceOutput"):
        asyncio.run(bad_output(InferenceContext(_frame())))


@pytest.mark.parametrize(
    "client, kwargs, error, message",
    [
        (object(), {}, TypeError, "infer method"),
        (_TritonClient(), {"model_name": ""}, ValueError, "model_name"),
        (_TritonClient(), {"input_builder": None}, TypeError, "input_builder"),
        (_TritonClient(), {"output_decoder": None}, TypeError, "output_decoder"),
        (_TritonClient(), {"model_version": 1}, TypeError, "model_version"),
        (_TritonClient(), {"requested_outputs": "boxes"}, TypeError, "requested_outputs"),
        (_TritonClient(), {"request_id_builder": 1}, TypeError, "request_id_builder"),
        (_TritonClient(), {"infer_kwargs": []}, TypeError, "infer_kwargs"),
        (
            _TritonClient(),
            {"infer_kwargs": {"model_name": "other"}},
            ValueError,
            "reserved arguments",
        ),
    ],
)
def test_triton_adapter_validates_configuration(
    client: object,
    kwargs: dict[str, object],
    error: type[Exception],
    message: str,
) -> None:
    values: dict[str, object] = {
        "model_name": "vehicles",
        "input_builder": lambda _: (1,),
        "output_decoder": lambda response, _: InferenceOutput(data=response),
    }
    values.update(kwargs)
    with pytest.raises(error, match=message):
        TritonAsyncAdapter(client, **values)  # type: ignore[arg-type]


def _detection_context(*, metadata: tuple[TimedKLVPacket, ...] = ()) -> InferenceContext:
    frame = replace(_frame(), metadata=metadata)
    return InferenceContext(frame).with_result(
        InferenceResult(
            "tracker",
            InferenceOutput((Detection(42, PixelBoundingBox(10, 20, 30, 40), 0.9),)),
        )
    )


def test_vmti_emitter_creates_timed_parent_for_media_only_stream() -> None:
    packet = VMTIMetadataEmitter(
        "tracker",
        metadata_pid=0x120,
        metadata_service_id=7,
        leap_seconds=29,
        random_access=True,
    )(_detection_context())

    assert packet.program_number == 1
    assert packet.pid == 0x120
    assert packet.pts == 90_000
    assert packet.metadata_service_id == 7
    assert packet.random_access
    assert isinstance(packet.decoded, UASLocalSet)
    assert packet.decoded.value(2) == datetime.fromtimestamp(1_700_000_029, tz=timezone.utc)
    assert packet.decoded.value(136) == 29
    assert packet.decoded.utc_timestamp() == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)
    assert packet.decoded.value(65) == 19
    assert packet.decoded.value(74).targets[0].target_id == 42


def test_vmti_emitter_preserves_correlated_parent_and_refreshes_timestamp() -> None:
    parent_bytes = encode_uas_local_set(
        {2: 1_600_000_000_000_000, 13: 40, 14: -75, 65: 19, 136: 28}
    )
    parent = TimedKLVPacket.from_bytes(
        parent_bytes,
        program_number=1,
        pid=0x121,
        carriage=KLVCarriage.SYNCHRONOUS,
        pts=80_000,
        metadata_service_id=3,
    )
    packet = VMTIMetadataEmitter("tracker", leap_seconds=29)(_detection_context(metadata=(parent,)))

    assert packet.pid == 0x121
    assert packet.metadata_service_id == 3
    assert packet.pts == 90_000
    assert packet.decoded.value(13) == pytest.approx(40)
    assert packet.decoded.value(14) == pytest.approx(-75)
    assert packet.decoded.value(136) == 29
    assert packet.decoded.value(2) == datetime.fromtimestamp(1_700_000_029, tz=timezone.utc)
    assert packet.decoded.utc_timestamp() == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)
    assert packet.decoded.value(74).targets[0].target_id == 42


def test_vmti_emitter_honors_parent_timestamp_correction_offset() -> None:
    parent = TimedKLVPacket.from_bytes(
        encode_uas_local_set({2: 1_600_000_000_000_000, 65: 19, 136: 29, 137: 125_000}),
        program_number=1,
        pid=0x121,
        carriage=KLVCarriage.SYNCHRONOUS,
        pts=80_000,
        metadata_service_id=3,
    )

    packet = VMTIMetadataEmitter("tracker")(_detection_context(metadata=(parent,)))

    assert packet.decoded.misp_timestamp_microseconds == 1_700_000_028_875_000
    assert packet.decoded.value(137) == 125_000
    assert packet.decoded.utc_timestamp() == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)


def test_vmti_emitter_requires_unambiguous_parent_stage_and_time() -> None:
    emitter = VMTIMetadataEmitter("missing", metadata_pid=0x120)
    with pytest.raises(ValueError, match="has no result"):
        emitter(InferenceContext(_frame()))

    without_time = replace(_frame(), timestamp_microseconds=None)
    context = InferenceContext(without_time).with_result(
        InferenceResult("tracker", InferenceOutput())
    )
    with pytest.raises(ValueError, match="UTC timestamp"):
        VMTIMetadataEmitter("tracker", metadata_pid=0x120)(context)
    with pytest.raises(ValueError, match="leap_seconds"):
        VMTIMetadataEmitter("tracker", metadata_pid=0x120)(_detection_context())
    with pytest.raises(ValueError, match="metadata_pid"):
        VMTIMetadataEmitter("tracker", leap_seconds=29)(_detection_context())

    parent_bytes = encode_uas_local_set({2: 1_600_000_000_000_000, 65: 19, 136: 29})
    parents = tuple(
        TimedKLVPacket.from_bytes(
            parent_bytes,
            program_number=1,
            pid=pid,
            carriage=KLVCarriage.SYNCHRONOUS,
            pts=80_000,
            metadata_service_id=0,
        )
        for pid in (0x120, 0x121)
    )
    with pytest.raises(ValueError, match="multiple correlated"):
        VMTIMetadataEmitter("tracker")(_detection_context(metadata=parents))
    selected = VMTIMetadataEmitter("tracker", metadata_pid=0x121)(
        _detection_context(metadata=parents)
    )
    assert selected.pid == 0x121


@pytest.mark.parametrize(
    "args, kwargs, error",
    [
        (("",), {}, ValueError),
        (("tracker",), {"metadata_pid": -1}, ValueError),
        (("tracker",), {"metadata_service_id": 256}, ValueError),
        (("tracker",), {"uas_version": True}, ValueError),
        (("tracker",), {"leap_seconds": True}, ValueError),
        (("tracker",), {"random_access": 1}, TypeError),
        (("tracker",), {"ontology_by_label": []}, TypeError),
    ],
)
def test_vmti_emitter_validates_configuration(
    args: tuple[object, ...], kwargs: dict[str, object], error: type[Exception]
) -> None:
    with pytest.raises(error):
        VMTIMetadataEmitter(*args, **kwargs)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="context"):
        VMTIMetadataEmitter("tracker", metadata_pid=0x120)(object())  # type: ignore[arg-type]
