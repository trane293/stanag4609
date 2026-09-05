from __future__ import annotations

from fractions import Fraction

import pytest

from stanag4609.errors import LimitExceeded
from stanag4609.transport.demux import PESStreamEvent, ProgramClockEvent, StreamKind
from stanag4609.transport.metadata import MetadataSTDDescriptor, encode_metadata_au_cell
from stanag4609.transport.mpegts import parse_transport_packet
from stanag4609.transport.mux import encode_pcr_packet, encode_pes_packet, packetize_pes
from stanag4609.transport.pcr import pcr_from_ticks
from stanag4609.transport.pes import parse_pes_packet
from stanag4609.transport.psi import ElementaryStreamInfo, KLVCarriage
from stanag4609.transport.std import (
    IncrementalSynchronousMetadataSTDModel,
    MetadataSTDByte,
    SynchronousMetadataSTDModel,
    metadata_std_bytes_from_pes,
    simulate_synchronous_metadata_pes,
)


def _descriptor(*, rate: int = 8_000, buffer: int = 1_024) -> MetadataSTDDescriptor:
    return MetadataSTDDescriptor.from_physical(
        input_bits_per_second=rate,
        buffer_bytes=buffer,
    )


def _bytes(
    count: int,
    *,
    arrival: Fraction = Fraction(0),
    spacing: Fraction = Fraction(0),
    pts: Fraction | None = None,
    access_unit: bool = True,
) -> tuple[MetadataSTDByte, ...]:
    return tuple(
        MetadataSTDByte(
            arrival_time=arrival + index * spacing,
            enters_main_buffer=pts is not None,
            removal_time=pts,
            access_unit_byte=access_unit and pts is not None,
            source_offset=index,
        )
        for index in range(count)
    )


def test_exact_metadata_std_accepts_boundary_compliant_synchronous_access_unit() -> None:
    model = SynchronousMetadataSTDModel(_descriptor())
    result = model.simulate(
        _bytes(
            8,
            spacing=Fraction(2, 1_000),
            pts=Fraction(1),
        )
    )

    assert result.conformant
    assert result.issues == ()
    assert result.transport_bytes == 8
    assert result.main_buffer_bytes == 8
    assert result.access_unit_bytes == 8
    assert result.access_unit_removal_times == 1
    assert result.maximum_transport_buffer_fullness == 1
    assert result.maximum_main_buffer_fullness == 8
    assert result.final_transport_buffer_fullness == 0
    assert result.final_main_buffer_fullness == 0
    assert result.maximum_decoder_delay == Fraction(1)
    assert result.minimum_decoder_delay == Fraction(986, 1_000)


def test_exact_metadata_std_reports_transport_overflow_and_long_busy_interval() -> None:
    result = SynchronousMetadataSTDModel(_descriptor(rate=400)).simulate(
        _bytes(513, pts=None)
    )

    assert result.maximum_transport_buffer_fullness == 513
    assert {issue.code for issue in result.issues} == {
        "transport_buffer_overflow",
        "transport_buffer_not_emptied",
    }
    overflow = result.issues[0]
    assert overflow.requirement == "ITU-T H.222.0 §2.4.2.6"
    assert overflow.fullness == 513
    assert overflow.capacity == 512


def test_exact_metadata_std_reports_main_buffer_overflow() -> None:
    descriptor = _descriptor(rate=1_600_000, buffer=1_024)
    result = SynchronousMetadataSTDModel(descriptor).simulate(
        _bytes(1_025, pts=Fraction(1))
    )

    assert result.maximum_main_buffer_fullness == 1_025
    issue = next(issue for issue in result.issues if issue.code == "main_buffer_overflow")
    assert issue.requirement == "ITU-T H.222.0 §2.12.10"
    assert issue.fullness == 1_025
    assert issue.capacity == 1_024


def test_exact_metadata_std_reports_pts_underflow_when_bytes_have_not_entered_bn() -> None:
    result = SynchronousMetadataSTDModel(_descriptor(rate=400)).simulate(
        _bytes(10, pts=Fraction(1, 10))
    )

    issue = next(issue for issue in result.issues if issue.code == "main_buffer_underflow")
    assert issue.time == Fraction(1, 10)
    assert issue.fullness == -5
    assert issue.capacity == 1_024
    assert result.final_main_buffer_fullness == 0


def test_exact_metadata_std_processes_entry_before_removal_at_same_instant() -> None:
    result = SynchronousMetadataSTDModel(_descriptor(rate=400)).simulate(
        _bytes(1, pts=Fraction(1, 50))
    )

    assert result.conformant
    assert result.maximum_main_buffer_fullness == 1
    assert result.final_main_buffer_fullness == 0


def test_exact_metadata_std_reports_one_tick_excessive_delay_but_accepts_boundary() -> None:
    boundary = SynchronousMetadataSTDModel(_descriptor()).simulate(
        _bytes(1, pts=Fraction(1))
    )
    over = SynchronousMetadataSTDModel(_descriptor()).simulate(
        _bytes(1, pts=Fraction(90_001, 90_000))
    )

    assert not any(issue.code == "excessive_delay" for issue in boundary.issues)
    issue = next(issue for issue in over.issues if issue.code == "excessive_delay")
    assert issue.requirement == "ST 1402.2 ST 1402-12"
    assert issue.delay == Fraction(90_001, 90_000)
    assert issue.permitted_delay == Fraction(1)


def test_exact_metadata_std_distinguishes_pes_overhead_from_access_unit_delay() -> None:
    values = (
        MetadataSTDByte(
            arrival_time=0,
            enters_main_buffer=True,
            removal_time=1,
            access_unit_byte=False,
        ),
        MetadataSTDByte(
            arrival_time=Fraction(1, 2),
            enters_main_buffer=True,
            removal_time=1,
            access_unit_byte=True,
        ),
    )
    result = SynchronousMetadataSTDModel(_descriptor()).simulate(values)

    assert result.main_buffer_bytes == 2
    assert result.access_unit_bytes == 1
    assert result.maximum_decoder_delay == Fraction(1, 2)


def test_exact_metadata_std_validates_configuration_and_event_order() -> None:
    with pytest.raises(TypeError, match="MetadataSTDDescriptor"):
        SynchronousMetadataSTDModel(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="transport_buffer_size"):
        SynchronousMetadataSTDModel(_descriptor(), transport_buffer_size=0)
    with pytest.raises(TypeError, match="MetadataSTDByte"):
        SynchronousMetadataSTDModel(_descriptor()).simulate([object()])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="nondecreasing"):
        SynchronousMetadataSTDModel(_descriptor()).simulate(
            (
                MetadataSTDByte(arrival_time=1),
                MetadataSTDByte(arrival_time=0),
            )
        )
    with pytest.raises(ValueError, match="removal_time"):
        MetadataSTDByte(arrival_time=0, enters_main_buffer=True)
    with pytest.raises(ValueError, match="access_unit_byte"):
        MetadataSTDByte(arrival_time=0, access_unit_byte=True)
    with pytest.raises(ValueError, match="source_offset"):
        MetadataSTDByte(arrival_time=0, source_offset=-1)


def _clock_event(ticks: int, *, offset: int, discontinuity: bool = False) -> ProgramClockEvent:
    packet = parse_transport_packet(
        encode_pcr_packet(pid=0x101, pcr=pcr_from_ticks(ticks), discontinuity=discontinuity),
        offset=offset,
    )
    assert packet.pcr is not None
    return ProgramClockEvent(1, packet.pcr, None, discontinuity, packet)


def _sync_pes_event(
    data: bytes,
    *,
    offset: int = 188,
    pts: int = 180_000,
) -> PESStreamEvent:
    cell = encode_metadata_au_cell(data)
    pes_raw = encode_pes_packet(cell, stream_id=0xFC, pts=pts)
    raw_packets, _ = packetize_pes(pes_raw, pid=0x102)
    packets = tuple(
        parse_transport_packet(raw, offset=offset + index * 188)
        for index, raw in enumerate(raw_packets)
    )
    pes = parse_pes_packet(pes_raw, offset=offset, transport_packets=packets)
    return PESStreamEvent(
        1,
        ElementaryStreamInfo(0x15, 0x102, ()),
        StreamKind.KLV,
        KLVCarriage.SYNCHRONOUS,
        pes,
    )


def test_pcr_adapter_derives_exact_transport_and_access_unit_byte_times() -> None:
    event = _sync_pes_event(b"x" * 300)
    following_offset = 188 + len(event.pes.transport_packets) * 188
    values = metadata_std_bytes_from_pes(
        event,
        (
            _clock_event(27_000_000, offset=0),
            _clock_event(54_000_000, offset=following_offset),
        ),
    )

    assert len(values) == len(event.pes.transport_packets) * 188
    assert sum(value.enters_main_buffer for value in values) == len(event.pes.raw)
    assert sum(value.access_unit_byte for value in values) == 300
    assert values[0].arrival_time == 1 + Fraction(178, following_offset)
    first_access = next(value for value in values if value.access_unit_byte)
    assert first_access.source_offset == 211
    assert first_access.arrival_time == 1 + Fraction(201, following_offset)
    assert first_access.removal_time == 2


def test_pcr_adapter_rejects_unbracketed_or_discontinuous_timeline() -> None:
    event = _sync_pes_event(b"metadata")
    first = _clock_event(0, offset=0)
    last_offset = 188 + len(event.pes.transport_packets) * 188
    with pytest.raises(ValueError, match="bracket"):
        metadata_std_bytes_from_pes(event, (first, _clock_event(1, offset=188)))
    with pytest.raises(ValueError, match="discontinuity"):
        metadata_std_bytes_from_pes(
            event,
            (first, _clock_event(27_000_000, offset=last_offset, discontinuity=True)),
        )


def test_recorded_pes_audit_detects_aggregate_main_buffer_overflow() -> None:
    first = _sync_pes_event(b"a" * 600, pts=270_000)
    second_offset = (
        first.pes.transport_packets[-1].offset
        + len(first.pes.transport_packets[-1].raw)
    )
    second = _sync_pes_event(b"b" * 600, offset=second_offset, pts=270_000)
    following_offset = (
        second.pes.transport_packets[-1].offset
        + len(second.pes.transport_packets[-1].raw)
    )
    result = simulate_synchronous_metadata_pes(
        _descriptor(rate=1_600_000),
        (first, second),
        (
            _clock_event(27_000_000, offset=0),
            _clock_event(54_000_000, offset=following_offset),
        ),
    )

    assert result.maximum_main_buffer_fullness > 1_024
    assert any(issue.code == "main_buffer_overflow" for issue in result.issues)


def test_incremental_metadata_std_retains_occupancy_across_windows() -> None:
    model = IncrementalSynchronousMetadataSTDModel(
        _descriptor(rate=1_600_000, buffer=1_024)
    )
    spacing = Fraction(1, 200_000)
    assert model.feed(_bytes(600, spacing=spacing, pts=1)) == ()
    assert model.advance(Fraction(1, 10)) == ()
    issues = model.feed(
        _bytes(600, arrival=Fraction(1, 5), spacing=spacing, pts=1)
    )
    issues += model.advance(1)

    assert any(issue.code == "main_buffer_overflow" for issue in issues)
    result = model.finish()
    assert result.maximum_main_buffer_fullness == 1_200
    assert result.final_main_buffer_fullness == 0
    assert result.transport_bytes == 1_200
    assert model.pending_timeline_events == 0


def test_incremental_metadata_std_retires_events_at_watermarks() -> None:
    model = IncrementalSynchronousMetadataSTDModel(_descriptor())
    model.feed(_bytes(10, pts=1))
    assert model.pending_timeline_events == 11
    model.advance(Fraction(1, 10))
    assert model.pending_timeline_events == 1
    model.advance(1)
    assert model.pending_timeline_events == 0
    assert model.pending_removal_groups == 0


def test_incremental_metadata_std_validates_lifecycle_and_watermarks() -> None:
    model = IncrementalSynchronousMetadataSTDModel(_descriptor())
    with pytest.raises(TypeError, match="iterable"):
        model.feed(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="MetadataSTDByte"):
        model.feed([object()])  # type: ignore[list-item]
    model.advance(Fraction(1, 10))
    with pytest.raises(ValueError, match="processed watermark"):
        model.feed(_bytes(1, arrival=0, pts=1))
    with pytest.raises(ValueError, match="nondecreasing"):
        model.advance(0)
    model.finish()
    with pytest.raises(RuntimeError, match="finished"):
        model.feed(())
    with pytest.raises(RuntimeError, match="finished"):
        model.advance(2)
    with pytest.raises(RuntimeError, match="finished"):
        model.finish()


def test_incremental_metadata_std_bounds_pending_state_atomically() -> None:
    timeline_limited = IncrementalSynchronousMetadataSTDModel(
        _descriptor(), max_pending_timeline_events=2
    )
    with pytest.raises(LimitExceeded, match="timeline"):
        timeline_limited.feed(_bytes(2, pts=1))
    assert timeline_limited.pending_timeline_events == 0

    groups_limited = IncrementalSynchronousMetadataSTDModel(
        _descriptor(), max_pending_removal_groups=1
    )
    with pytest.raises(LimitExceeded, match="removal groups"):
        groups_limited.feed(
            (
                MetadataSTDByte(0, True, 1),
                MetadataSTDByte(0, True, 2),
            )
        )
    assert groups_limited.pending_removal_groups == 0


def test_incremental_metadata_std_matches_one_shot_for_one_complete_batch() -> None:
    values = _bytes(10, pts=Fraction(1, 10))
    descriptor = _descriptor(rate=400)
    expected = SynchronousMetadataSTDModel(descriptor).simulate(values)
    incremental = IncrementalSynchronousMetadataSTDModel(descriptor)
    incremental.feed(values)

    assert incremental.finish() == expected
