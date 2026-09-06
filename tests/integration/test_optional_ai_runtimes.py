from __future__ import annotations

import pytest

from stanag4609.sidecar import (
    FrameEnvelope,
    InferenceContext,
    InferenceOutput,
    OnnxRuntimeAdapter,
)


@pytest.mark.integration
def test_onnx_runtime_adapter_executes_a_real_inference_session() -> None:
    numpy = pytest.importorskip("numpy")
    onnx = pytest.importorskip("onnx")
    onnxruntime = pytest.importorskip("onnxruntime")

    tensor = onnx.helper.make_tensor_value_info(
        "images", onnx.TensorProto.FLOAT, [1, 1]
    )
    output = onnx.helper.make_tensor_value_info(
        "detections", onnx.TensorProto.FLOAT, [1, 1]
    )
    graph = onnx.helper.make_graph(
        [onnx.helper.make_node("Identity", ["images"], ["detections"])],
        "stanag4609-sidecar-smoke",
        [tensor],
        [output],
    )
    model = onnx.helper.make_model(
        graph,
        ir_version=8,
        opset_imports=[onnx.helper.make_opsetid("", 13)],
    )
    session = onnxruntime.InferenceSession(
        model.SerializeToString(),
        providers=["CPUExecutionProvider"],
    )
    pixels = numpy.array([[3.5]], dtype=numpy.float32)
    context = InferenceContext(
        FrameEnvelope(
            sequence_number=0,
            pts=0,
            width=1,
            height=1,
            pixels=pixels,
        )
    )
    adapter = OnnxRuntimeAdapter(
        session,
        input_builder=lambda item: {"images": item.frame.pixels},
        output_decoder=lambda values, _context: InferenceOutput(data=values[0]),
        output_names=("detections",),
    )

    result = adapter(context)

    numpy.testing.assert_array_equal(result.data, pixels)
