"""Optional Ultralytics YOLO result adapter for the model-neutral sidecar API."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from stanag4609.sidecar.model import (
    Detection,
    InferenceContext,
    InferenceOutput,
    PixelBoundingBox,
)
from stanag4609.st0903 import DetectionStatus


def _values(value: Any, *, name: str) -> list[Any]:
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        value = tolist()
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"Ultralytics {name} must be an array-like value with tolist()")
    return list(value)


def _finite_number(value: Any, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"Ultralytics {name} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"Ultralytics {name} must be finite")
    return result


def _label(names: Any, class_id: int) -> str | None:
    value: Any = None
    if isinstance(names, Mapping):
        value = names.get(class_id)
    elif (
        isinstance(names, Sequence)
        and not isinstance(names, (str, bytes))
        and 0 <= class_id < len(names)
    ):
        value = names[class_id]
    return value if isinstance(value, str) else None


class UltralyticsYOLODetector:
    """Adapt one Ultralytics detection result into :class:`InferenceOutput`.

    The adapter deliberately accepts an already-created model object, keeping
    Ultralytics, PyTorch, NumPy, and accelerator libraries out of the core
    dependency set. Use it in a threaded ``InferenceStage`` because normal
    Ultralytics prediction is synchronous.

    Tracker IDs are offset by ``track_id_offset``. The default of one converts
    the common zero-based ID domain into ST 0903's positive target-ID domain;
    untracked detections use their zero-based result index with the same offset.
    """

    __slots__ = (
        "algorithm_id",
        "model",
        "predict_kwargs",
        "status",
        "track_id_offset",
    )

    def __init__(
        self,
        model: Any,
        *,
        algorithm_id: int | None = None,
        status: DetectionStatus = DetectionStatus.ACTIVE_MOVING,
        track_id_offset: int = 1,
        predict_kwargs: Mapping[str, Any] | None = None,
    ) -> None:
        if not callable(getattr(model, "predict", None)):
            raise TypeError("Ultralytics model must expose a callable predict method")
        if algorithm_id is not None and (
            isinstance(algorithm_id, bool)
            or not isinstance(algorithm_id, int)
            or not 0 <= algorithm_id <= 2**24 - 1
        ):
            raise ValueError("algorithm_id must be an integer from 0 to 2^24-1")
        if not isinstance(status, DetectionStatus):
            raise TypeError("status must be DetectionStatus")
        if (
            isinstance(track_id_offset, bool)
            or not isinstance(track_id_offset, int)
            or track_id_offset < 1
        ):
            raise ValueError("track_id_offset must be a positive integer")
        if predict_kwargs is not None and not isinstance(predict_kwargs, Mapping):
            raise TypeError("predict_kwargs must be a mapping")
        self.model = model
        self.algorithm_id = algorithm_id
        self.status = status
        self.track_id_offset = track_id_offset
        self.predict_kwargs = {"verbose": False, **dict(predict_kwargs or {})}

    def __call__(self, context: InferenceContext) -> InferenceOutput:
        if not isinstance(context, InferenceContext):
            raise TypeError("context must be InferenceContext")
        results = tuple(
            self.model.predict(source=context.frame.pixels, **self.predict_kwargs)
        )
        if len(results) != 1:
            raise ValueError(
                f"Ultralytics frame prediction must return exactly one result, got {len(results)}"
            )
        result = results[0]
        boxes = getattr(result, "boxes", None)
        if boxes is None:
            return InferenceOutput(data=result)

        coordinates = _values(getattr(boxes, "xyxy", None), name="boxes.xyxy")
        confidences = _values(getattr(boxes, "conf", None), name="boxes.conf")
        classes = _values(getattr(boxes, "cls", None), name="boxes.cls")
        identifiers_value = getattr(boxes, "id", None)
        identifiers = (
            None
            if identifiers_value is None
            else _values(identifiers_value, name="boxes.id")
        )
        lengths = {len(coordinates), len(confidences), len(classes)}
        if identifiers is not None:
            lengths.add(len(identifiers))
        if len(lengths) != 1:
            raise ValueError("Ultralytics box, confidence, class, and ID arrays differ in length")

        names = getattr(result, "names", getattr(self.model, "names", None))
        detections: list[Detection] = []
        for index, coordinate_value in enumerate(coordinates):
            coordinate = _values(coordinate_value, name=f"boxes.xyxy[{index}]")
            if len(coordinate) != 4:
                raise ValueError("each Ultralytics xyxy box must contain four coordinates")
            left_value, top_value, right_value, bottom_value = (
                _finite_number(value, name="box coordinate") for value in coordinate
            )
            left = max(0, min(context.frame.width, math.floor(left_value)))
            top = max(0, min(context.frame.height, math.floor(top_value)))
            right = max(0, min(context.frame.width, math.ceil(right_value)))
            bottom = max(0, min(context.frame.height, math.ceil(bottom_value)))
            confidence = _finite_number(confidences[index], name="confidence")
            class_value = _finite_number(classes[index], name="class ID")
            class_id = int(class_value)
            if class_value != class_id or class_id < 0:
                raise ValueError("Ultralytics class ID must be a nonnegative integer")
            source_id = index
            if identifiers is not None:
                identifier_value = _finite_number(identifiers[index], name="track ID")
                source_id = int(identifier_value)
                if identifier_value != source_id or source_id < 0:
                    raise ValueError("Ultralytics track ID must be a nonnegative integer")
            detections.append(
                Detection(
                    target_id=source_id + self.track_id_offset,
                    bounding_box=PixelBoundingBox(left, top, right, bottom),
                    confidence=confidence,
                    label=_label(names, class_id),
                    algorithm_id=self.algorithm_id,
                    status=self.status,
                )
            )
        return InferenceOutput(tuple(detections), data=result)
