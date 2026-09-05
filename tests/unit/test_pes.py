from __future__ import annotations

import pytest

from stanag4609.errors import DecodeError, LimitExceeded, TruncatedData
from stanag4609.transport.mpegts import parse_transport_packet
from stanag4609.transport.pes import PESAssembler, decode_timestamp, parse_pes_packet

ASYNC_PES = bytes.fromhex("00 00 01 BD 00 06 84 00 00 61 62 63")
SYNC_PES = bytes.fromhex(
    "00 00 01 FC 00 10 80 84 05 21 00 05 BF 21 "
    "00 01 CF 00 03 78 79 7A"
)


def _ts_packet(payload: bytes, *, start: bool, cc: int, pid: int = 0x102) -> bytes:
    if not 0 < len(payload) <= 184:
        raise ValueError
    byte1 = (pid >> 8) | (0x40 if start else 0)
    byte2 = pid & 0xFF
    if len(payload) == 184:
        return bytes((0x47, byte1, byte2, 0x10 | cc)) + payload
    adaptation_length = 183 - len(payload)
    adaptation = b"" if adaptation_length == 0 else b"\x00" + b"\xff" * (
        adaptation_length - 1
    )
    return (
        bytes((0x47, byte1, byte2, 0x30 | cc, adaptation_length))
        + adaptation
        + payload
    )


def test_parse_asynchronous_private_data_pes() -> None:
    packet = parse_pes_packet(ASYNC_PES, offset=188)
    assert packet.stream_id == 0xBD
    assert packet.packet_length == 6
    assert packet.data_alignment_indicator
    assert packet.pts is None
    assert packet.dts is None
    assert packet.payload == b"abc"
    assert packet.offset == 188
    assert bytes(packet) == ASYNC_PES
    assert packet.transport_packets == ()


def test_parse_synchronous_metadata_pes_with_pts() -> None:
    packet = parse_pes_packet(SYNC_PES)
    assert packet.stream_id == 0xFC
    assert packet.pts == 90_000
    assert packet.dts is None
    assert packet.pts_seconds == 1.0
    assert packet.payload == bytes.fromhex("00 01 CF 00 03 78 79 7A")


def test_parse_video_pes_with_pts_and_dts() -> None:
    raw = bytes.fromhex(
        "000001E0 000E 80 C0 0A 310005BF21 1100035F91 78"
    )
    packet = parse_pes_packet(raw)
    assert packet.pts == 90_000
    assert packet.dts == 45_000
    assert packet.payload == b"x"


def test_parse_special_stream_without_optional_header() -> None:
    packet = parse_pes_packet(bytes.fromhex("000001BE0003") + b"abc")
    assert packet.stream_id == 0xBE
    assert packet.header_data == b""
    assert packet.payload == b"abc"


def test_decode_timestamp_validates_prefix_and_marker_bits() -> None:
    assert decode_timestamp(bytes.fromhex("21 00 01 00 01"), expected_prefix=0x2) == 0
    assert decode_timestamp(bytes.fromhex("2F FF FF FF FF"), expected_prefix=0x2) == 2**33 - 1
    with pytest.raises(DecodeError, match="prefix"):
        decode_timestamp(bytes.fromhex("31 00 01 00 01"), expected_prefix=0x2)
    with pytest.raises(DecodeError, match="marker"):
        decode_timestamp(bytes.fromhex("20 00 01 00 01"), expected_prefix=0x2)
    with pytest.raises(TruncatedData):
        decode_timestamp(b"\x21", expected_prefix=0x2)


@pytest.mark.parametrize(
    "raw, message, error",
    [
        (b"\x00", "header", TruncatedData),
        (b"BADBAD", "start code", DecodeError),
        (ASYNC_PES[:-1], "declares", TruncatedData),
        (ASYNC_PES + b"x", "trailing", DecodeError),
        (bytes.fromhex("000001BD0003800001"), "header_data_length", TruncatedData),
        (bytes.fromhex("000001BD0003400000"), "marker bits", DecodeError),
        (bytes.fromhex("000001BD0003804000"), "forbidden", DecodeError),
        (bytes.fromhex("000001BD0006808000") + b"abc", "PTS", TruncatedData),
    ],
)
def test_malformed_pes_is_rejected(
    raw: bytes, message: str, error: type[Exception]
) -> None:
    with pytest.raises(error, match=message):
        parse_pes_packet(raw)


def test_pes_assembler_spans_arbitrary_ts_packets() -> None:
    first = parse_transport_packet(_ts_packet(ASYNC_PES[:5], start=True, cc=0))
    second = parse_transport_packet(_ts_packet(ASYNC_PES[5:], start=False, cc=1))
    assembler = PESAssembler(pid=0x102)
    assert assembler.feed(first) == []
    completed = assembler.feed(second)[0]
    assert completed.raw == ASYNC_PES
    assert completed.transport_packets == (first, second)
    assert assembler.buffered_bytes == 0
    continuation = parse_transport_packet(_ts_packet(b"ignored", start=False, cc=2))
    assert assembler.feed(continuation) == []
    assert assembler.buffered_bytes == 0
    assert assembler.finish() == []


def test_unbounded_pes_finishes_at_next_start_or_end_of_stream() -> None:
    unbounded = bytes.fromhex("000001E00000800000") + b"video"
    packet = parse_transport_packet(_ts_packet(unbounded, start=True, cc=0))
    assembler = PESAssembler(pid=0x102)
    assert assembler.feed(packet) == []
    assert assembler.finish()[0].payload == b"video"

    assembler = PESAssembler(pid=0x102)
    assert assembler.feed(packet) == []
    next_packet = parse_transport_packet(_ts_packet(ASYNC_PES, start=True, cc=1))
    completed = assembler.feed(next_packet)
    assert [item.stream_id for item in completed] == [0xE0, 0xBD]


def test_pes_assembler_rejects_wrong_pid_limits_and_truncation() -> None:
    assembler = PESAssembler(pid=0x102, max_pes_length=10)
    wrong = parse_transport_packet(_ts_packet(ASYNC_PES, start=True, cc=0, pid=3))
    with pytest.raises(ValueError, match="PID"):
        assembler.feed(wrong)
    too_large = parse_transport_packet(_ts_packet(ASYNC_PES[:6], start=True, cc=0))
    with pytest.raises(LimitExceeded):
        assembler.feed(too_large)
    assembler = PESAssembler(pid=0x102)
    partial = parse_transport_packet(_ts_packet(ASYNC_PES[:7], start=True, cc=0))
    assembler.feed(partial)
    with pytest.raises(TruncatedData):
        assembler.finish()
