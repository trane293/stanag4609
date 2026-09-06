from __future__ import annotations

import asyncio

import pytest

from stanag4609.sidecar import (
    FrameEnvelope,
    InferenceContext,
    InferenceOutput,
    OnnxRuntimeAdapter,
    TritonAsyncAdapter,
)


def _context(pixels: object) -> InferenceContext:
    return InferenceContext(
        FrameEnvelope(
            sequence_number=7,
            pts=90_000,
            width=1,
            height=1,
            pixels=pixels,
        )
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
    context = _context(pixels)
    adapter = OnnxRuntimeAdapter(
        session,
        input_builder=lambda item: {"images": item.frame.pixels},
        output_decoder=lambda values, _context: InferenceOutput(data=values[0]),
        output_names=("detections",),
    )

    result = adapter(context)

    numpy.testing.assert_array_equal(result.data, pixels)


@pytest.mark.integration
def test_triton_http_asyncio_client_objects_compose_with_adapter() -> None:
    numpy = pytest.importorskip("numpy")
    http = pytest.importorskip("tritonclient.http")
    http_aio = pytest.importorskip("tritonclient.http.aio")
    calls: list[dict[str, object]] = []
    response = object()

    class Client(http_aio.InferenceServerClient):
        async def infer(self, **kwargs: object) -> object:
            calls.append(kwargs)
            return response

    async def exercise() -> tuple[InferenceOutput, object, object]:
        client = Client(url="127.0.0.1:8000")
        tensor = http.InferInput("images", [1, 1], "FP32")
        tensor.set_data_from_numpy(numpy.array([[1.0]], dtype=numpy.float32))
        requested = http.InferRequestedOutput("detections")
        adapter = TritonAsyncAdapter(
            client,
            model_name="vehicles",
            model_version="1",
            input_builder=lambda _context: (tensor,),
            output_decoder=lambda value, _context: InferenceOutput(data=value),
            requested_outputs=(requested,),
            request_id_builder=lambda context: str(context.frame.sequence_number),
            infer_kwargs={"timeout": 0.25},
        )
        try:
            result = await adapter(_context(tensor))
            return result, tensor, requested
        finally:
            await client.close()

    result, tensor, requested = asyncio.run(exercise())

    assert result.data is response
    assert calls == [
        {
            "model_name": "vehicles",
            "inputs": [tensor],
            "model_version": "1",
            "outputs": [requested],
            "request_id": "7",
            "timeout": 0.25,
        }
    ]
    assert isinstance(tensor, http.InferInput)
    assert isinstance(requested, http.InferRequestedOutput)


@pytest.mark.integration
def test_triton_grpc_asyncio_client_objects_compose_with_adapter() -> None:
    numpy = pytest.importorskip("numpy")
    grpc = pytest.importorskip("tritonclient.grpc")
    grpc_aio = pytest.importorskip("tritonclient.grpc.aio")
    calls: list[dict[str, object]] = []
    response = object()

    class Client(grpc_aio.InferenceServerClient):
        async def infer(self, **kwargs: object) -> object:
            calls.append(kwargs)
            return response

    async def exercise() -> tuple[InferenceOutput, object, object]:
        client = Client(url="127.0.0.1:8001")
        tensor = grpc.InferInput("images", [1, 1], "FP32")
        tensor.set_data_from_numpy(numpy.array([[1.0]], dtype=numpy.float32))
        requested = grpc.InferRequestedOutput("detections")
        adapter = TritonAsyncAdapter(
            client,
            model_name="vehicles",
            input_builder=lambda _context: (tensor,),
            output_decoder=lambda value, _context: InferenceOutput(data=value),
            requested_outputs=(requested,),
            request_id_builder=lambda context: str(context.frame.sequence_number),
            infer_kwargs={"timeout": 0.25, "client_timeout": 0.5},
        )
        try:
            result = await adapter(_context(tensor))
            return result, tensor, requested
        finally:
            await client.close()

    result, tensor, requested = asyncio.run(exercise())

    assert result.data is response
    assert calls[0]["model_name"] == "vehicles"
    assert calls[0]["request_id"] == "7"
    assert calls[0]["timeout"] == 0.25
    assert calls[0]["client_timeout"] == 0.5
    assert calls[0]["inputs"] == [tensor]
    assert calls[0]["outputs"] == [requested]
    assert isinstance(tensor, grpc.InferInput)
    assert isinstance(requested, grpc.InferRequestedOutput)
