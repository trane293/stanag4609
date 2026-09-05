"""Bridge one completed inference graph into a timed ST 0601/VMTI packet."""

from stanag4609.sidecar import (
    Detection,
    FrameEnvelope,
    InferenceContext,
    InferenceOutput,
    InferenceResult,
    PixelBoundingBox,
    VMTIMetadataEmitter,
)

frame = FrameEnvelope(
    sequence_number=1,
    pts=90_000,
    width=1920,
    height=1080,
    pixels=None,
    timestamp_microseconds=1_700_000_000_000_000,
)
completed = InferenceContext(frame).with_result(
    InferenceResult(
        "tracker",
        InferenceOutput(
            (Detection(42, PixelBoundingBox(100, 200, 301, 401), 0.965),)
        ),
    )
)
packet = VMTIMetadataEmitter("tracker", metadata_pid=0x120, leap_seconds=29)(completed)
print(packet.pid, packet.pts, packet.decoded.value(74).targets[0].target_id)
