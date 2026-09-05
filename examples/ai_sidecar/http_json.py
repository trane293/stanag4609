"""Build a bounded stage for a model-specific JSON inference endpoint."""

import base64
from typing import Any

from stanag4609.sidecar import (
    Detection,
    HTTPJSONAdapter,
    InferenceContext,
    InferenceOutput,
    InferenceStage,
    PixelBoundingBox,
)


def encode_request(context: InferenceContext) -> dict[str, Any]:
    """Adapt this application's byte-frame contract to service JSON."""

    pixels = context.frame.pixels
    if not isinstance(pixels, bytes):
        raise TypeError("this example expects JPEG bytes in frame.pixels")
    return {
        "request_id": str(context.frame.sequence_number),
        "image_base64": base64.b64encode(pixels).decode("ascii"),
        "width": context.frame.width,
        "height": context.frame.height,
    }


def decode_response(payload: Any, _: InferenceContext) -> InferenceOutput:
    """Normalize the service's boxes into the stable sidecar model."""

    detections = tuple(
        Detection(
            target_id=int(item["track_id"]),
            bounding_box=PixelBoundingBox(*map(int, item["xyxy"])),
            confidence=float(item["confidence"]),
            label=str(item["label"]),
        )
        for item in payload["detections"]
    )
    return InferenceOutput(detections)


def vehicle_service_stage(endpoint: str, token: str) -> InferenceStage:
    """Return an async graph stage with bounded network and payload costs."""

    adapter = HTTPJSONAdapter(
        endpoint,
        request_encoder=encode_request,
        response_decoder=decode_response,
        headers={"Authorization": f"Bearer {token}"},
        timeout_seconds=0.5,
        max_request_bytes=4 * 1024 * 1024,
        max_response_bytes=512 * 1024,
    )
    return InferenceStage("http-vehicle-detector", adapter, timeout_seconds=0.6)
