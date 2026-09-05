"""Dependency-free frame and inference result envelopes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from stanag4609.st0903 import DetectionStatus
from stanag4609.st0903_geo import Location
from stanag4609.transport.processor import TimedKLVPacket


def _require_nonnegative_integer(value: int, *, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


@dataclass(frozen=True, slots=True)
class PixelBoundingBox:
    """A conventional zero-based, half-open AI bounding box.

    ``right`` and ``bottom`` are excluded. This matches common NumPy, OpenCV,
    ONNX, PyTorch, and TensorFlow post-processing conventions; the VMTI bridge
    performs the explicit conversion to ST 0903's one-based pixel positions.
    """

    left: int
    top: int
    right: int
    bottom: int

    def __post_init__(self) -> None:
        for name in ("left", "top", "right", "bottom"):
            _require_nonnegative_integer(getattr(self, name), name=name)
        if self.right <= self.left:
            raise ValueError("bounding box right must be greater than left")
        if self.bottom <= self.top:
            raise ValueError("bounding box bottom must be greater than top")

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


@dataclass(frozen=True, slots=True)
class Detection:
    """One model-neutral object detection ready for VMTI conversion.

    ``location`` is an optional absolute WGS-84 target position. The VMTI
    bridge emits it as ST 0903 VTarget Item 17 without deriving coordinates
    from pixel geometry or silently inventing an uncertainty model.
    """

    target_id: int
    bounding_box: PixelBoundingBox
    confidence: float
    label: str | None = None
    algorithm_id: int | None = None
    status: DetectionStatus = DetectionStatus.ACTIVE_MOVING
    location: Location | None = None

    def __post_init__(self) -> None:
        if isinstance(self.target_id, bool) or not isinstance(self.target_id, int):
            raise TypeError("target_id must be an integer")
        if not 1 <= self.target_id <= 2**63 - 1:
            raise ValueError("target_id must be between 1 and 2^63-1")
        if not isinstance(self.bounding_box, PixelBoundingBox):
            raise TypeError("bounding_box must be PixelBoundingBox")
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
            raise TypeError("confidence must be numeric")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        object.__setattr__(self, "confidence", float(self.confidence))
        if self.label is not None and not isinstance(self.label, str):
            raise TypeError("label must be str or None")
        if self.algorithm_id is not None and (
            isinstance(self.algorithm_id, bool)
            or not isinstance(self.algorithm_id, int)
            or not 0 <= self.algorithm_id <= 2**24 - 1
        ):
            raise ValueError("algorithm_id must be an integer from 0 to 2^24-1")
        if not isinstance(self.status, DetectionStatus):
            raise TypeError("status must be DetectionStatus")
        if self.location is not None and not isinstance(self.location, Location):
            raise TypeError("location must be an ST 0903 Location or None")


@dataclass(frozen=True, slots=True)
class FrameEnvelope:
    """A decoded video frame with transport timing and correlated KLV.

    ``pixels`` deliberately has no prescribed runtime type. Optional adapters
    may carry an AV frame, NumPy array, GPU tensor, shared-memory reference, or
    application-specific handle without making any of those packages core
    dependencies. ``timestamp_microseconds`` is an optional UTC/POSIX count
    since 1970; ST 0601/ST 0903 producers must convert it to continuous MISP
    time using the applicable leap-second offset.
    """

    sequence_number: int
    pts: int
    width: int
    height: int
    pixels: Any
    timestamp_microseconds: int | None = None
    metadata: tuple[TimedKLVPacket, ...] = ()
    program_number: int = 1
    video_pid: int | None = None

    def __post_init__(self) -> None:
        _require_nonnegative_integer(self.sequence_number, name="sequence_number")
        if isinstance(self.pts, bool) or not isinstance(self.pts, int):
            raise TypeError("pts must be an integer")
        if not 0 <= self.pts <= 2**33 - 1:
            raise ValueError("pts must be an unsigned 33-bit integer")
        for name in ("width", "height"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.timestamp_microseconds is not None and (
            isinstance(self.timestamp_microseconds, bool)
            or not isinstance(self.timestamp_microseconds, int)
            or not 0 <= self.timestamp_microseconds <= 2**64 - 1
        ):
            raise ValueError("timestamp_microseconds must be an unsigned 64-bit integer")
        if not isinstance(self.metadata, tuple) or any(
            not isinstance(item, TimedKLVPacket) for item in self.metadata
        ):
            raise TypeError("metadata must be a tuple of TimedKLVPacket values")
        if (
            isinstance(self.program_number, bool)
            or not isinstance(self.program_number, int)
            or not 1 <= self.program_number <= 0xFFFF
        ):
            raise ValueError("program_number must be between 1 and 65535")
        if any(item.program_number != self.program_number for item in self.metadata):
            raise ValueError("frame metadata must belong to the same program")
        if self.video_pid is not None and (
            isinstance(self.video_pid, bool)
            or not isinstance(self.video_pid, int)
            or not 0 <= self.video_pid <= 0x1FFF
        ):
            raise ValueError("video_pid must be an integer from 0 to 8191 or None")


@dataclass(frozen=True, slots=True)
class InferenceOutput:
    """Model-neutral output from one inference stage."""

    detections: tuple[Detection, ...] = ()
    data: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.detections, tuple) or any(
            not isinstance(item, Detection) for item in self.detections
        ):
            raise TypeError("detections must be a tuple of Detection values")


@dataclass(frozen=True, slots=True)
class InferenceResult:
    """A named stage output retained for downstream processors and observability."""

    stage: str
    output: InferenceOutput

    def __post_init__(self) -> None:
        if not self.stage:
            raise ValueError("stage must not be empty")
        if not isinstance(self.output, InferenceOutput):
            raise TypeError("output must be InferenceOutput")


@dataclass(frozen=True, slots=True)
class InferenceContext:
    """Immutable frame context accumulated through an inference graph."""

    frame: FrameEnvelope
    results: tuple[InferenceResult, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.frame, FrameEnvelope):
            raise TypeError("frame must be FrameEnvelope")
        if not isinstance(self.results, tuple) or any(
            not isinstance(item, InferenceResult) for item in self.results
        ):
            raise TypeError("results must be a tuple of InferenceResult values")

    def result(self, stage: str) -> InferenceOutput | None:
        """Return one prior stage output by name."""

        matches = tuple(item.output for item in self.results if item.stage == stage)
        if not matches:
            return None
        if len(matches) > 1:
            raise ValueError(f"stage {stage!r} occurs more than once")
        return matches[0]

    def with_result(self, result: InferenceResult) -> InferenceContext:
        if self.result(result.stage) is not None:
            raise ValueError(f"stage {result.stage!r} already produced a result")
        return InferenceContext(self.frame, (*self.results, result))
