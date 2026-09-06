"""Construct an optional Ultralytics YOLO stage for the FMV inference graph."""

from ultralytics import YOLO

from stanag4609.sidecar import InferenceStage, UltralyticsYOLODetector


def vehicle_detection_stage(weights: str = "yolo26n.pt") -> InferenceStage:
    """Return a threaded local detector ready for Sequential or Parallel."""

    model = YOLO(weights)
    detector = UltralyticsYOLODetector(
        model,
        algorithm_id=1,
        mode="track",
        predict_kwargs={"conf": 0.35, "iou": 0.6, "tracker": "bytetrack.yaml"},
    )
    return InferenceStage("ultralytics-vehicles", detector, threaded=True)
