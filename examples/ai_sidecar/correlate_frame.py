"""Attach synchronous KLV to a decoded frame using its MPEG presentation time."""

from stanag4609 import (
    CorrelationMode,
    FrameEnvelope,
    FrameMetadataCorrelator,
    TimedKLVPacket,
    encode_uas_local_set,
)
from stanag4609.transport import KLVCarriage

PTS = 900_000
TIMESTAMP_MICROSECONDS = 1_700_000_000_000_000

metadata = TimedKLVPacket.from_bytes(
    encode_uas_local_set({2: TIMESTAMP_MICROSECONDS, 65: 19}),
    program_number=1,
    pid=0x102,
    carriage=KLVCarriage.SYNCHRONOUS,
    pts=PTS,
    metadata_service_id=1,
)
frame = FrameEnvelope(
    sequence_number=300,
    pts=PTS,
    width=1920,
    height=1080,
    pixels=b"decoder-owned frame handle",
    program_number=1,
    video_pid=0x101,
)

correlator = FrameMetadataCorrelator(mode=CorrelationMode.EXACT)
correlated = correlator.correlate_after_observing(frame, metadata)
print(correlated.timestamp_microseconds, len(correlated.metadata))
