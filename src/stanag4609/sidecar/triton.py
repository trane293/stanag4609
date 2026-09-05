"""Optional NVIDIA Triton AsyncIO client adapter for inference graphs."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeAlias

from stanag4609.sidecar.model import InferenceContext, InferenceOutput

TritonInputBuilder: TypeAlias = Callable[[InferenceContext], Sequence[Any]]
TritonOutputDecoder: TypeAlias = Callable[[Any, InferenceContext], InferenceOutput]
TritonRequestIdBuilder: TypeAlias = Callable[[InferenceContext], str]

_RESERVED_INFER_ARGUMENTS = frozenset(
    {"model_name", "inputs", "model_version", "outputs", "request_id"}
)


class TritonAsyncAdapter:
    """Adapt a Triton HTTP or gRPC AsyncIO client without importing it.

    The caller owns Triton's ``InferInput``/``InferRequestedOutput`` objects and
    model-specific tensor decoding. ``InferenceStage.timeout_seconds`` can wrap
    this adapter and cancel the underlying AsyncIO request on timeout.
    """

    __slots__ = (
        "client",
        "infer_kwargs",
        "input_builder",
        "model_name",
        "model_version",
        "output_decoder",
        "request_id_builder",
        "requested_outputs",
    )

    def __init__(
        self,
        client: Any,
        *,
        model_name: str,
        input_builder: TritonInputBuilder,
        output_decoder: TritonOutputDecoder,
        model_version: str = "",
        requested_outputs: Sequence[Any] | None = None,
        request_id_builder: TritonRequestIdBuilder | None = None,
        infer_kwargs: Mapping[str, Any] | None = None,
    ) -> None:
        if not callable(getattr(client, "infer", None)):
            raise TypeError("Triton client must expose a callable infer method")
        if not isinstance(model_name, str) or not model_name:
            raise ValueError("model_name must be a non-empty string")
        if not callable(input_builder):
            raise TypeError("input_builder must be callable")
        if not callable(output_decoder):
            raise TypeError("output_decoder must be callable")
        if not isinstance(model_version, str):
            raise TypeError("model_version must be a string")
        if requested_outputs is not None and (
            isinstance(requested_outputs, (str, bytes))
            or not isinstance(requested_outputs, Sequence)
        ):
            raise TypeError("requested_outputs must be a sequence or None")
        if request_id_builder is not None and not callable(request_id_builder):
            raise TypeError("request_id_builder must be callable or None")
        if infer_kwargs is not None and not isinstance(infer_kwargs, Mapping):
            raise TypeError("infer_kwargs must be a mapping")
        arguments = dict(infer_kwargs or {})
        collisions = sorted(_RESERVED_INFER_ARGUMENTS.intersection(arguments))
        if collisions:
            raise ValueError(f"infer_kwargs contains reserved arguments {collisions}")
        self.client = client
        self.model_name = model_name
        self.input_builder = input_builder
        self.output_decoder = output_decoder
        self.model_version = model_version
        self.requested_outputs = (
            None if requested_outputs is None else tuple(requested_outputs)
        )
        self.request_id_builder = request_id_builder
        self.infer_kwargs = arguments

    async def __call__(self, context: InferenceContext) -> InferenceOutput:
        if not isinstance(context, InferenceContext):
            raise TypeError("context must be InferenceContext")
        inputs = self.input_builder(context)
        if (
            isinstance(inputs, (str, bytes))
            or not isinstance(inputs, Sequence)
            or not inputs
        ):
            raise TypeError("input_builder must return a non-empty sequence")
        request_id = ""
        if self.request_id_builder is not None:
            request_id = self.request_id_builder(context)
            if not isinstance(request_id, str):
                raise TypeError("request_id_builder must return a string")
        candidate = self.client.infer(
            model_name=self.model_name,
            inputs=list(inputs),
            model_version=self.model_version,
            outputs=(
                None if self.requested_outputs is None else list(self.requested_outputs)
            ),
            request_id=request_id,
            **self.infer_kwargs,
        )
        if not inspect.isawaitable(candidate):
            raise TypeError("Triton AsyncIO client infer method must return an awaitable")
        response = await candidate
        output = self.output_decoder(response, context)
        if not isinstance(output, InferenceOutput):
            raise TypeError("output_decoder must return InferenceOutput")
        return output
