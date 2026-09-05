from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from stanag4609.sidecar import FrameEnvelope
from stanag4609.sidecar.correlation import CorrelationMode, FrameMetadataCorrelator
from stanag4609.st0601 import encode_uas_local_set
from stanag4609.transport.processor import TimedKLVPacket
from stanag4609.transport.psi import KLVCarriage
from stanag4609.transport.timing import PTS_MODULUS


def _metadata(
    pts: int | None,
    *,
    timestamp_microseconds: int,
    program_number: int = 1,
) -> TimedKLVPacket:
    data = encode_uas_local_set({2: timestamp_microseconds, 65: 19})
    synchronous = pts is not None
    return TimedKLVPacket.from_bytes(
        data,
        program_number=program_number,
        pid=0x102,
        carriage=(KLVCarriage.SYNCHRONOUS if synchronous else KLVCarriage.ASYNCHRONOUS),
        pts=pts,
        metadata_service_id=1 if synchronous else None,
    )


def _frame(
    pts: int,
    *,
    program_number: int = 1,
    timestamp_microseconds: int | None = None,
) -> FrameEnvelope:
    return FrameEnvelope(
        1,
        pts,
        640,
        480,
        b"pixels",
        timestamp_microseconds,
        program_number=program_number,
        video_pid=0x101,
    )


def test_exact_correlation_attaches_all_packets_at_the_frame_pts() -> None:
    correlator = FrameMetadataCorrelator(mode=CorrelationMode.EXACT)
    first = _metadata(90_000, timestamp_microseconds=1_700_000_000_000_000)
    second = _metadata(90_000, timestamp_microseconds=1_700_000_000_000_000)
    assert correlator.observe(first)
    assert correlator.observe(second)

    frame = correlator.correlate(_frame(90_000))
    assert frame.metadata == (first, second)
    assert frame.timestamp_microseconds == 1_700_000_000_000_000
    assert correlator.buffered_packets == 2


def test_latest_mode_applies_metadata_from_when_it_becomes_relevant() -> None:
    correlator = FrameMetadataCorrelator(
        mode=CorrelationMode.LATEST,
        maximum_delta_ticks=45_000,
    )
    old = _metadata(90_000, timestamp_microseconds=1_700_000_000_000_000)
    current = _metadata(99_000, timestamp_microseconds=1_700_000_000_100_000)
    correlator.observe(old)
    correlator.observe(current)

    frame = correlator.correlate(_frame(108_000))
    assert frame.metadata == (current,)
    assert frame.timestamp_microseconds == 1_700_000_000_200_000


def test_nearest_mode_is_deterministic_and_prefers_earlier_on_a_tie() -> None:
    correlator = FrameMetadataCorrelator(
        mode=CorrelationMode.NEAREST,
        maximum_delta_ticks=100,
    )
    earlier = _metadata(950, timestamp_microseconds=1_000_000)
    later = _metadata(1_050, timestamp_microseconds=1_001_111)
    correlator.observe(earlier)
    correlator.observe(later)
    assert correlator.correlate(_frame(1_000)).metadata == (earlier,)


def test_correlation_respects_program_offset_age_and_existing_frame_values() -> None:
    correlator = FrameMetadataCorrelator(
        maximum_delta_ticks=10,
        metadata_pts_offset_ticks=-5,
    )
    wrong_program = _metadata(105, timestamp_microseconds=2_000_000, program_number=2)
    right_program = _metadata(105, timestamp_microseconds=3_000_000)
    correlator.observe(wrong_program)
    correlator.observe(right_program)

    matched = correlator.correlate(_frame(100, timestamp_microseconds=9_000_000))
    assert matched.metadata == (right_program,)
    assert matched.timestamp_microseconds == 9_000_000
    assert correlator.correlate(_frame(111)).metadata == ()


def test_correlation_unwraps_pts_across_the_33_bit_rollover() -> None:
    correlator = FrameMetadataCorrelator(maximum_delta_ticks=30)
    packet = _metadata(
        PTS_MODULUS - 10,
        timestamp_microseconds=1_700_000_000_000_000,
    )
    correlator.observe(packet)
    frame = correlator.correlate(_frame(10))
    assert frame.metadata == (packet,)
    assert frame.timestamp_microseconds == 1_700_000_000_000_222


def test_async_metadata_is_counted_but_not_claimed_as_frame_synchronous() -> None:
    correlator = FrameMetadataCorrelator()
    packet = _metadata(None, timestamp_microseconds=1_700_000_000_000_000)
    assert not correlator.observe(packet)
    assert correlator.uncorrelated_async_packets == 1
    assert correlator.buffered_packets == 0
    assert correlator.correlate(_frame(0)).metadata == ()


def test_correlation_cache_is_bounded_and_clearable() -> None:
    correlator = FrameMetadataCorrelator(max_packets=2)
    for pts in (1, 2, 3):
        correlator.observe(_metadata(pts, timestamp_microseconds=pts))
    assert correlator.buffered_packets == 2
    assert correlator.dropped_packets == 1
    assert correlator.correlate(_frame(1)).metadata == ()
    correlator.clear()
    assert correlator.buffered_packets == 0
    assert correlator.reference_for(1) is None


def test_frame_envelope_exposes_program_and_video_pid() -> None:
    frame = _frame(0, program_number=7)
    assert frame.program_number == 7
    assert frame.video_pid == 0x101


@pytest.mark.parametrize(
    ("kwargs", "error", "message"),
    [
        ({"mode": "latest"}, TypeError, "mode"),
        ({"max_packets": 0}, ValueError, "max_packets"),
        ({"maximum_delta_ticks": -1}, ValueError, "maximum_delta_ticks"),
        ({"maximum_delta_ticks": PTS_MODULUS // 2}, ValueError, "maximum_delta_ticks"),
        ({"metadata_pts_offset_ticks": True}, ValueError, "offset"),
        ({"metadata_pts_offset_ticks": PTS_MODULUS // 2}, ValueError, "offset"),
    ],
)
def test_correlator_validates_configuration(
    kwargs: dict[str, object], error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        FrameMetadataCorrelator(**kwargs)  # type: ignore[arg-type]


def test_correlator_validates_inputs_and_frame_program_fields() -> None:
    correlator = FrameMetadataCorrelator()
    with pytest.raises(TypeError, match="TimedKLVPacket"):
        correlator.observe(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="FrameEnvelope"):
        correlator.correlate(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="program_number"):
        _frame(0, program_number=0)
    values = dict(sequence_number=0, pts=0, width=1, height=1, pixels=b"x")
    with pytest.raises(ValueError, match="video_pid"):
        FrameEnvelope(**values, video_pid=0x2000)
    wrong_program = _metadata(0, timestamp_microseconds=0, program_number=2)
    with pytest.raises(ValueError, match="same program"):
        FrameEnvelope(**values, metadata=(wrong_program,))


def test_timestamp_extraction_accepts_aware_datetimes_without_float_rounding() -> None:
    packet = _metadata(0, timestamp_microseconds=1_700_000_000_000_001)
    assert packet.decoded.value(2) == datetime(
        2023, 11, 14, 22, 13, 20, 1, tzinfo=timezone.utc
    )
    frame = FrameMetadataCorrelator().correlate_after_observing(_frame(0), packet)
    assert frame.timestamp_microseconds == 1_700_000_000_000_001


def test_missing_absolute_timestamp_still_attaches_synchronous_metadata() -> None:
    packet = replace(_metadata(100, timestamp_microseconds=1), decoded=None)
    correlator = FrameMetadataCorrelator(mode=CorrelationMode.EXACT)
    frame = correlator.correlate_after_observing(_frame(100), packet)
    assert frame.metadata == (packet,)
    assert frame.timestamp_microseconds is None

    class NaiveTimestamp:
        @staticmethod
        def value(_: int) -> datetime:
            return datetime(2024, 1, 1)

    naive = replace(packet, decoded=NaiveTimestamp())
    other = FrameMetadataCorrelator(mode=CorrelationMode.EXACT)
    assert other.correlate_after_observing(_frame(100), naive).timestamp_microseconds is None


def test_nearest_future_metadata_derives_an_earlier_frame_timestamp() -> None:
    packet = _metadata(1_050, timestamp_microseconds=2_000_000)
    correlator = FrameMetadataCorrelator(
        mode=CorrelationMode.NEAREST,
        maximum_delta_ticks=100,
    )
    frame = correlator.correlate_after_observing(_frame(1_000), packet)
    assert frame.timestamp_microseconds == 1_999_444


def test_programs_keep_independent_pts_epochs() -> None:
    correlator = FrameMetadataCorrelator(maximum_delta_ticks=20)
    first = _metadata(PTS_MODULUS - 5, timestamp_microseconds=1, program_number=1)
    second = _metadata(5, timestamp_microseconds=2, program_number=2)
    correlator.observe(first)
    correlator.observe(second)
    assert correlator.reference_for(1) == PTS_MODULUS - 5
    assert correlator.reference_for(2) == 5
    assert correlator.correlate(_frame(5, program_number=1)).metadata == (first,)
