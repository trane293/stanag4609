"""Construct a model-specific ONNX Runtime stage without coupling the core."""

from collections.abc import Callable, Mapping, Sequence
from typing import Any

import onnxruntime as ort

from stanag4609.sidecar import (
    InferenceContext,
    InferenceOutput,
    InferenceStage,
    OnnxRuntimeAdapter,
)


def onnx_detection_stage(
    model_path: str,
    preprocess: Callable[[InferenceContext], Mapping[str, Any]],
    postprocess: Callable[[tuple[Any, ...], InferenceContext], InferenceOutput],
    *,
    providers: Sequence[str] = ("CPUExecutionProvider",),
) -> InferenceStage:
    """Build a threaded stage using application-owned tensor transforms."""

    session = ort.InferenceSession(model_path, providers=list(providers))
    adapter = OnnxRuntimeAdapter(
        session,
        input_builder=preprocess,
        output_decoder=postprocess,
    )
    return InferenceStage("onnx-detector", adapter, threaded=True)
