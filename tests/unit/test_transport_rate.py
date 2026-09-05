from __future__ import annotations

from fractions import Fraction

import pytest

from stanag4609.errors import DecodeError, LimitExceeded, TruncatedData
from stanag4609.transport.mpegts import (
    ProgramClockReference,
    encode_program_clock_reference,
    parse_transport_packet,
)
from stanag4609.transport.mux import TransportMuxer, encode_pcr_packet, encode_pes_packet
from stanag4609.transport.pcr import PCR_CLOCK_RATE
from stanag4609.transport.psi import ElementaryStreamInfo
from stanag4609.transport.rate import (
    NULL_PACKET_PID,
    PCR_BASE_LAST_BIT_OFFSET,
    TS_PACKET_BITS,
    TransportRateShaper,
    encode_null_packet,
    rewrite_packet_pcr,
)

_BIT_RATE = TS_PACKET_BITS * 1_000


def _muxer() -> TransportMuxer:
    return TransportMuxer(
        transport_stream_id=1,
        program_number=1,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=(ElementaryStreamInfo(0x1B, 0x101, ()),),
    )


def _source_packets() -> tuple[bytes, ...]:
    return _muxer().mux_pes(
        0x101,
        encode_pes_packet(bytes(range(256)), stream_id=0xE0, pts=0),
    )


def test_null_packet_has_required_pid_payload_and_header_shape() -> None:
    raw = encode_null_packet(continuity_counter=15)
    packet = parse_transport_packet(raw)
    assert len(raw) == 188
    assert packet.pid == NULL_PACKET_PID
    assert not packet.payload_unit_start
    assert packet.adaptation_field_control == 1
    assert packet.continuity_counter == 15
    assert packet.payload == b"\xff" * 184

    with pytest.raises(ValueError, match="continuity_counter"):
        encode_null_packet(continuity_counter=16)
    with pytest.raises(ValueError, match="continuity_counter"):
        encode_null_packet(continuity_counter=True)


def test_rate_shaper_schedules_arbitrary_chunks_on_exact_slots() -> None:
    packets = _source_packets()
    source = b"".join(packets)
    shaper = TransportRateShaper(bit_rate=_BIT_RATE, start_at=10)
    assert shaper.bit_rate == _BIT_RATE
    assert shaper.packet_duration == Fraction(1, 1_000)
    assert shaper.next_slot_at == 10

    assert shaper.feed(source[:100]) == ()
    first = shaper.feed(source[100:200])
    rest = shaper.feed(source[200:])
    scheduled = first + rest
    assert [item.packet for item in scheduled] == list(packets)
    assert [item.slot_index for item in scheduled] == list(range(len(packets)))
    assert [item.starts_at for item in scheduled] == [
        Fraction(10) + Fraction(index, 1_000) for index in range(len(packets))
    ]
    assert all(item.ends_at - item.starts_at == Fraction(1, 1_000) for item in scheduled)
    assert all(item.source for item in scheduled)
    assert shaper.scheduled_packets == len(packets)
    assert shaper.buffered_bytes == 0
    assert shaper.finish() == ()
    assert shaper.finish() == ()


def test_rate_shaper_fills_strictly_prior_idle_slots_before_source() -> None:
    source = _source_packets()[0]
    shaper = TransportRateShaper(bit_rate=_BIT_RATE, max_fill_packets=10)
    output = shaper.feed(source, at=Fraction(3, 1_000))
    assert len(output) == 4
    assert [item.starts_at for item in output] == [
        Fraction(0),
        Fraction(1, 1_000),
        Fraction(2, 1_000),
        Fraction(3, 1_000),
    ]
    assert [item.source for item in output] == [False, False, False, True]
    assert [item.pid for item in output[:3]] == [NULL_PACKET_PID] * 3
    assert output[3].packet == source
    assert [parse_transport_packet(item.packet).continuity_counter for item in output[:3]] == [
        0,
        1,
        2,
    ]

    # Exact target time is left available for source; a half-slot target fills it.
    assert shaper.fill_until(at=Fraction(4, 1_000)) == ()
    padded = shaper.fill_until(at=Fraction(9, 2_000))
    assert len(padded) == 1
    assert padded[0].starts_at == Fraction(4, 1_000)


def test_rate_shaper_bounds_idle_fill_before_mutating_schedule() -> None:
    shaper = TransportRateShaper(bit_rate=_BIT_RATE, max_fill_packets=2)
    with pytest.raises(LimitExceeded, match="requires 3 packets"):
        shaper.fill_until(at=Fraction(3, 1_000))
    assert shaper.scheduled_packets == 0
    assert shaper.next_slot_at == 0


def test_rate_shaper_restamps_pcr_at_base_last_bit_byte_arrival_time() -> None:
    original = ProgramClockReference(123, 45)
    raw = encode_pcr_packet(pid=0x101, pcr=original)
    anchor = ProgramClockReference(1_000, 17)
    shaper = TransportRateShaper(
        bit_rate=_BIT_RATE,
        start_at=10,
        clock_anchor=anchor,
        clock_anchor_at=10,
    )
    scheduled = shaper.feed(raw)[0]
    expected_sample_at = Fraction(10) + PCR_BASE_LAST_BIT_OFFSET / _BIT_RATE
    expected_delta = (expected_sample_at - 10) * PCR_CLOCK_RATE
    expected_ticks = anchor.ticks + expected_delta.numerator // expected_delta.denominator

    assert scheduled.original_pcr == original
    assert scheduled.output_pcr is not None
    assert scheduled.output_pcr.ticks == expected_ticks
    assert scheduled.pcr_sample_at == expected_sample_at
    parsed = parse_transport_packet(scheduled.packet)
    assert parsed.pcr == scheduled.output_pcr
    assert not parsed.discontinuity_indicator
    assert scheduled.packet[:6] == raw[:6]
    assert scheduled.packet[12:] == raw[12:]


def test_rate_shaper_without_clock_anchor_preserves_source_pcr() -> None:
    original = ProgramClockReference(123, 45)
    raw = encode_pcr_packet(pid=0x101, pcr=original)
    scheduled = TransportRateShaper(bit_rate=_BIT_RATE).feed(raw)[0]
    assert scheduled.packet == raw
    assert scheduled.original_pcr == original
    assert scheduled.output_pcr == original
    assert scheduled.pcr_sample_at is None


def test_rate_shaper_reanchors_at_declared_source_clock_discontinuity() -> None:
    first_source = ProgramClockReference(50_000, 17)
    next_source = ProgramClockReference(60_000, 0)
    first_raw = encode_pcr_packet(
        pid=0x101,
        pcr=first_source,
        discontinuity=True,
    )
    next_raw = encode_pcr_packet(pid=0x101, pcr=next_source)
    shaper = TransportRateShaper(
        bit_rate=_BIT_RATE,
        clock_anchor=ProgramClockReference(0, 0),
    )
    first, second = shaper.feed(first_raw + next_raw)
    assert first.output_pcr == first_source
    assert parse_transport_packet(first.packet).discontinuity_indicator
    assert second.output_pcr is not None
    assert second.output_pcr.ticks == first_source.ticks + PCR_CLOCK_RATE // 1_000


def test_rate_shaper_defers_discontinuity_reanchor_until_next_pcr() -> None:
    adaptation = b"\x80" + b"\xff" * 182
    discontinuity = bytes((0x47, 0x01, 0x01, 0x20, len(adaptation))) + adaptation
    source_clock = ProgramClockReference(70_000, 29)
    pcr_packet = encode_pcr_packet(pid=0x101, pcr=source_clock)
    shaper = TransportRateShaper(
        bit_rate=_BIT_RATE,
        clock_anchor=ProgramClockReference(0, 0),
    )
    marker, clock = shaper.feed(discontinuity + pcr_packet)
    assert marker.output_pcr is None
    assert clock.output_pcr == source_clock


def test_rate_shaper_does_not_apply_discontinuity_to_another_pid() -> None:
    adaptation = b"\x80" + b"\xff" * 182
    discontinuity = bytes((0x47, 0x01, 0x02, 0x20, len(adaptation))) + adaptation
    source_clock = ProgramClockReference(70_000, 29)
    pcr_packet = encode_pcr_packet(pid=0x101, pcr=source_clock)
    anchor = ProgramClockReference(10_000, 0)
    shaper = TransportRateShaper(bit_rate=_BIT_RATE, clock_anchor=anchor)
    _, clock = shaper.feed(discontinuity + pcr_packet)
    expected_sample_at = Fraction(1, 1_000) + PCR_BASE_LAST_BIT_OFFSET / _BIT_RATE
    expected_delta = expected_sample_at * PCR_CLOCK_RATE

    assert clock.output_pcr is not None
    assert clock.output_pcr.ticks == (
        anchor.ticks + expected_delta.numerator // expected_delta.denominator
    )


def test_rate_shaper_keeps_reanchored_pid_clock_independent() -> None:
    discontinuous_clock = ProgramClockReference(70_000, 29)
    first_pid = encode_pcr_packet(
        pid=0x101,
        pcr=discontinuous_clock,
        discontinuity=True,
    )
    second_pid_clock = ProgramClockReference(90_000, 0)
    second_pid = encode_pcr_packet(pid=0x102, pcr=second_pid_clock)
    anchor = ProgramClockReference(10_000, 0)
    shaper = TransportRateShaper(bit_rate=_BIT_RATE, clock_anchor=anchor)
    first, second = shaper.feed(first_pid + second_pid)
    second_sample_at = Fraction(1, 1_000) + PCR_BASE_LAST_BIT_OFFSET / _BIT_RATE
    second_delta = second_sample_at * PCR_CLOCK_RATE

    assert first.output_pcr == discontinuous_clock
    assert second.output_pcr is not None
    assert second.output_pcr.ticks == (
        anchor.ticks + second_delta.numerator // second_delta.denominator
    )


def test_rewrite_packet_pcr_is_byte_exact_outside_fixed_field() -> None:
    original = ProgramClockReference(1, 2)
    replacement = ProgramClockReference(3, 4)
    raw = encode_pcr_packet(pid=0x101, pcr=original)
    rewritten = rewrite_packet_pcr(raw, replacement)
    assert rewritten[:6] == raw[:6]
    assert rewritten[12:] == raw[12:]
    assert parse_transport_packet(rewritten).pcr == replacement

    opcr = ProgramClockReference(5, 6)
    adaptation = (
        b"\x18"
        + encode_program_clock_reference(original)
        + encode_program_clock_reference(opcr)
        + b"\xff" * 170
    )
    with_opcr = bytes((0x47, 0x01, 0x01, 0x20, len(adaptation))) + adaptation
    rewritten_with_opcr = rewrite_packet_pcr(with_opcr, replacement)
    parsed = parse_transport_packet(rewritten_with_opcr)
    assert parsed.pcr == replacement
    assert parsed.opcr == opcr
    assert rewritten_with_opcr[12:] == with_opcr[12:]

    with pytest.raises(TypeError, match="must be bytes"):
        rewrite_packet_pcr(bytearray(raw), replacement)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ProgramClockReference"):
        rewrite_packet_pcr(raw, object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="does not contain"):
        rewrite_packet_pcr(encode_null_packet(), replacement)


def test_rate_shaper_rejects_invalid_configuration_and_lifecycle_calls() -> None:
    with pytest.raises(TypeError, match="bit_rate"):
        TransportRateShaper(bit_rate=True)
    with pytest.raises(ValueError, match="positive"):
        TransportRateShaper(bit_rate=0)
    with pytest.raises(ValueError, match="finite"):
        TransportRateShaper(bit_rate=float("inf"))
    with pytest.raises(TypeError, match="clock_anchor"):
        TransportRateShaper(bit_rate=1, clock_anchor=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="requires clock_anchor"):
        TransportRateShaper(bit_rate=1, clock_anchor_at=0)
    with pytest.raises(ValueError, match="max_fill_packets"):
        TransportRateShaper(bit_rate=1, max_fill_packets=True)

    shaper = TransportRateShaper(bit_rate=_BIT_RATE)
    with pytest.raises(TypeError, match="bytes-like"):
        shaper.feed("packet")  # type: ignore[arg-type]
    shaper.fill_until(at=1)
    with pytest.raises(DecodeError, match="monotonic"):
        shaper.fill_until(at=0)

    partial = TransportRateShaper(bit_rate=_BIT_RATE)
    partial.feed(b"\x47")
    with pytest.raises(TruncatedData, match="1 trailing byte"):
        partial.finish()
    with pytest.raises(RuntimeError, match="finished"):
        partial.feed(b"")
    with pytest.raises(RuntimeError, match="finished"):
        partial.fill_until(at=0)
