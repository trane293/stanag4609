"""First-party contracts for frame inference and ST 0903 VMTI emission."""

from stanag4609.sidecar.correlation import CorrelationMode, FrameMetadataCorrelator
from stanag4609.sidecar.http import (
    HTTPInferenceError,
    HTTPJSONAdapter,
    HTTPJSONRequestEncoder,
    HTTPJSONResponseDecoder,
)
from stanag4609.sidecar.metrics import InferenceMetrics, InferenceStageMetrics
from stanag4609.sidecar.model import (
    Detection,
    FrameEnvelope,
    InferenceContext,
    InferenceOutput,
    InferenceResult,
    PixelBoundingBox,
)
from stanag4609.sidecar.onnx import (
    OnnxInputBuilder,
    OnnxOutputDecoder,
    OnnxRuntimeAdapter,
)
from stanag4609.sidecar.pipeline import InferenceStage, Parallel, Sequential
from stanag4609.sidecar.pyav import PyAVFrameSource
from stanag4609.sidecar.queue import (
    AsyncFrameQueue,
    FrameOverflowPolicy,
    FrameQueuePutResult,
)
from stanag4609.sidecar.triton import (
    TritonAsyncAdapter,
    TritonInputBuilder,
    TritonOutputDecoder,
    TritonRequestIdBuilder,
)
from stanag4609.sidecar.ultralytics import UltralyticsYOLODetector
from stanag4609.sidecar.vmti import VMTIMetadataEmitter, encode_embedded_vmti

__all__ = [
    "AsyncFrameQueue",
    "CorrelationMode",
    "Detection",
    "FrameEnvelope",
    "FrameMetadataCorrelator",
    "FrameOverflowPolicy",
    "FrameQueuePutResult",
    "HTTPInferenceError",
    "HTTPJSONAdapter",
    "HTTPJSONRequestEncoder",
    "HTTPJSONResponseDecoder",
    "InferenceContext",
    "InferenceMetrics",
    "InferenceOutput",
    "InferenceResult",
    "InferenceStage",
    "InferenceStageMetrics",
    "OnnxInputBuilder",
    "OnnxOutputDecoder",
    "OnnxRuntimeAdapter",
    "Parallel",
    "PixelBoundingBox",
    "PyAVFrameSource",
    "Sequential",
    "TritonAsyncAdapter",
    "TritonInputBuilder",
    "TritonOutputDecoder",
    "TritonRequestIdBuilder",
    "UltralyticsYOLODetector",
    "VMTIMetadataEmitter",
    "encode_embedded_vmti",
]
