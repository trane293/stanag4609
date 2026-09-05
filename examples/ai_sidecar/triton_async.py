"""Construct an NVIDIA Triton AsyncIO stage for a model-specific contract."""

from collections.abc import Callable, Sequence
from typing import Any

from tritonclient.grpc.aio import InferenceServerClient

from stanag4609.sidecar import (
    InferenceContext,
    InferenceOutput,
    InferenceStage,
    TritonAsyncAdapter,
)


def triton_detection_stage(
    url: str,
    model_name: str,
    build_inputs: Callable[[InferenceContext], Sequence[Any]],
    decode_response: Callable[[Any, InferenceContext], InferenceOutput],
) -> tuple[InferenceServerClient, InferenceStage]:
    """Return the caller-owned client and a cancellable async stage."""

    client = InferenceServerClient(url=url)
    adapter = TritonAsyncAdapter(
        client,
        model_name=model_name,
        input_builder=build_inputs,
        output_decoder=decode_response,
        request_id_builder=lambda context: (
            f"{context.frame.program_number}:{context.frame.sequence_number}"
        ),
    )
    return client, InferenceStage("triton-detector", adapter, timeout_seconds=0.150)
