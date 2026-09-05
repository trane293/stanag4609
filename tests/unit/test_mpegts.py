from __future__ import annotations

from io import BytesIO

import pytest

from stanag4609.errors import DecodeError, TruncatedData
from stanag4609.transport.mpegts import (
    TS_PACKET_SIZE,
    ProgramClockReference,
    TransportStreamParser,
    encode_program_clock_reference,
    iter_transport_stream,
    parse_transport_packet,
)


def _header(
    pid: int,
    *,
    payload_unit_start: bool = False,
    adaptation_field_control: int = 1,
    continuity_counter: int = 0,
) -> bytes:
    return bytes(
        (
            0x47,
            (0x40 if payload_unit_start else 0) | (pid >> 8),
            pid & 0xFF,
            (adaptation_field_control << 4) | continuity_counter,
        )
    )


def _payload_packet(pid: int, fill: int, *, start: bool = False, cc: int = 0) -> bytes:
    return _header(pid, payload_unit_start=start, continuity_counter=cc) + bytes((fill,)) * 184


def test_parse_payload_only_transport_packet() -> None:
    raw = _payload_packet(0x123, 0xA5, start=True, cc=9)
    packet = parse_transport_packet(raw, offset=376)
    assert len(raw) == TS_PACKET_SIZE
    assert packet.pid == 0x123
    assert packet.payload_unit_start
    assert packet.continuity_counter == 9
    assert packet.payload == bytes((0xA5,)) * 184
    assert packet.adaptation_field == b""
    assert packet.offset == 376
    assert bytes(packet) == raw


def test_parse_adaptation_and_payload_packet() -> None:
    payload = bytes(range(181))
    raw = _header(0x44, adaptation_field_control=3) + b"\x02\x40\xff" + payload
    packet = parse_transport_packet(raw)
    assert packet.random_access_indicator
    assert packet.adaptation_field == b"\x40\xff"
    assert packet.payload == payload


def test_parse_pcr_and_opcr_from_adaptation_field_exactly() -> None:
    pcr = ProgramClockReference(base=0x1ABCDEFFF, extension=299)
    opcr = ProgramClockReference(base=90_000, extension=17)
    adaptation = b"\x18" + encode_program_clock_reference(pcr) + encode_program_clock_reference(
        opcr
    )
    payload = bytes(188 - 5 - len(adaptation))
    raw = (
        _header(0x101, adaptation_field_control=3)
        + bytes((len(adaptation),))
        + adaptation
        + payload
    )
    packet = parse_transport_packet(raw)
    assert packet.pcr == pcr
    assert packet.opcr == opcr
    assert packet.pcr.ticks == (0x1ABCDEFFF * 300) + 299
    assert packet.pcr.seconds == pytest.approx(packet.pcr.ticks / 27_000_000)
    assert not packet.discontinuity_indicator


def test_adaptation_flags_and_clock_reference_encoding_are_validated() -> None:
    discontinuity = b"\xb7\x80" + b"\xff" * 182
    packet = parse_transport_packet(
        _header(0x101, adaptation_field_control=2) + discontinuity
    )
    assert packet.discontinuity_indicator

    with pytest.raises(ValueError, match="base"):
        ProgramClockReference(base=2**33, extension=0)
    with pytest.raises(ValueError, match="extension"):
        ProgramClockReference(base=0, extension=300)
    malformed_pcr = b"\xb7\x10" + b"\x00" * 6 + b"\xff" * 176
    with pytest.raises(DecodeError, match="reserved"):
        parse_transport_packet(
            _header(0x101, adaptation_field_control=2) + malformed_pcr
        )
    truncated_pcr = b"\x01\x10" + b"\xff" * 182
    with pytest.raises(DecodeError, match="PCR"):
        parse_transport_packet(
            _header(0x101, adaptation_field_control=3) + truncated_pcr
        )


def test_parse_adaptation_only_packet_has_no_payload() -> None:
    raw = _header(0x1FFF, adaptation_field_control=2) + b"\xb7\x00" + b"\xff" * 182
    packet = parse_transport_packet(raw)
    assert packet.pid == 0x1FFF
    assert packet.has_adaptation_field
    assert not packet.has_payload
    assert packet.payload == b""


@pytest.mark.parametrize(
    "raw, message",
    [
        (b"\x46" + b"\0" * 187, "sync"),
        (_header(1, adaptation_field_control=0) + b"\0" * 184, "reserved"),
        (_header(1, adaptation_field_control=3) + b"\xb8" + b"\0" * 183, "adaptation"),
        (b"\x47" * 187, "188"),
    ],
)
def test_invalid_transport_packets_are_rejected(raw: bytes, message: str) -> None:
    with pytest.raises(DecodeError, match=message):
        parse_transport_packet(raw)


def test_incremental_parser_accepts_every_split_and_tracks_offsets() -> None:
    raw = _payload_packet(0x100, 1, cc=0) + _payload_packet(0x100, 2, cc=1)
    for split in range(len(raw) + 1):
        parser = TransportStreamParser()
        packets = parser.feed(raw[:split]) + parser.feed(raw[split:]) + parser.finish()
        assert [packet.offset for packet in packets] == [0, 188]
        assert [packet.payload[0] for packet in packets] == [1, 2]


def test_recovery_mode_resynchronizes_live_input() -> None:
    raw = _payload_packet(0x100, 7)
    parser = TransportStreamParser(recover=True)
    assert parser.feed(b"garbage" + raw[:20]) == []
    packets = parser.feed(raw[20:])
    assert len(packets) == 1
    assert packets[0].offset == 7
    assert parser.discarded_bytes == 7


def test_strict_mode_and_finish_reject_misaligned_input() -> None:
    with pytest.raises(DecodeError, match="sync"):
        TransportStreamParser().feed(b"bad" + b"\0" * 185)
    parser = TransportStreamParser()
    parser.feed(_payload_packet(1, 0)[:-1])
    with pytest.raises(TruncatedData, match="187"):
        parser.finish()


def test_iter_transport_stream_reads_without_whole_file_buffering() -> None:
    raw = _payload_packet(2, 3) + _payload_packet(3, 4)
    packets = list(iter_transport_stream(BytesIO(raw), chunk_size=17))
    assert [packet.pid for packet in packets] == [2, 3]
    assert [packet.payload[0] for packet in packets] == [3, 4]
