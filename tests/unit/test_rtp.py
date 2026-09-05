from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from stanag4609 import RTPPacketReorderBuffer as PublicRTPPacketReorderBuffer
from stanag4609.errors import DecodeError, TruncatedData
from stanag4609.transport.demux import PATEvent, PESStreamEvent, PMTEvent, TransportDemuxer
from stanag4609.transport.mux import TransportMuxer, encode_pes_packet
from stanag4609.transport.psi import ElementaryStreamInfo
from stanag4609.transport.rtp import (
    RTP_MPEG2_TS_CLOCK_RATE,
    RTP_MPEG2_TS_PAYLOAD_TYPE,
    RTPMPEG2TransportPacketizer,
    RTPMPEG2TransportReceiver,
    RTPPacket,
    RTPPacketReorderBuffer,
    RTPReorderResult,
    parse_rtp_mpeg2_transport,
    parse_rtp_packet,
)


def _ts_packet(pid: int) -> bytes:
    return bytes((0x47, (pid >> 8) & 0x1F, pid & 0xFF, 0x10)) + b"\xff" * 184


def _rtp(sequence: int, *, timestamp: int = 90_000, ssrc: int = 7) -> bytes:
    return RTPPacket(
        payload_type=33,
        sequence_number=sequence,
        timestamp=timestamp,
        ssrc=ssrc,
        payload=_ts_packet(256),
    ).encode()


def test_rtp_packet_round_trips_fixed_optional_headers_and_padding() -> None:
    packet = RTPPacket(
        payload_type=96,
        sequence_number=65535,
        timestamp=0xFEDCBA98,
        ssrc=0x12345678,
        payload=b"payload",
        marker=True,
        csrc=(1, 0xFFFFFFFF),
        extension_profile=0xABCD,
        extension_data=b"ext!data",
        padding_size=4,
    )

    encoded = packet.encode()
    assert encoded[-4:] == b"\x00\x00\x00\x04"
    assert parse_rtp_packet(encoded) == packet


def test_rtp_parser_rejects_truncated_invalid_version_extension_and_padding() -> None:
    with pytest.raises(TruncatedData, match="at least 12"):
        parse_rtp_packet(b"\x80")
    with pytest.raises(DecodeError, match="version"):
        parse_rtp_packet(b"\x40" + b"\x00" * 11)
    with pytest.raises(TruncatedData, match="CSRC"):
        parse_rtp_packet(b"\x81" + b"\x00" * 11)
    with pytest.raises(TruncatedData, match="extension header"):
        parse_rtp_packet(b"\x90" + b"\x00" * 11)
    with pytest.raises(TruncatedData, match="extension data"):
        parse_rtp_packet(b"\x90" + b"\x00" * 11 + b"\x00\x01\x00\x02")
    with pytest.raises(DecodeError, match="padding"):
        parse_rtp_packet(b"\xa0" + b"\x00" * 11)


def test_rfc2250_mp2t_parser_requires_payload_type_integral_ts_and_sync() -> None:
    payload = _ts_packet(1) + _ts_packet(2)
    packet = RTPPacket(33, 1, 2, 3, payload)
    assert parse_rtp_mpeg2_transport(packet.encode()) == packet
    assert RTP_MPEG2_TS_PAYLOAD_TYPE == 33
    assert RTP_MPEG2_TS_CLOCK_RATE == 90_000

    with pytest.raises(DecodeError, match="payload type"):
        parse_rtp_mpeg2_transport(RTPPacket(96, 1, 2, 3, payload).encode())
    with pytest.raises(DecodeError, match="integer number"):
        parse_rtp_mpeg2_transport(RTPPacket(33, 1, 2, 3, payload + b"x").encode())
    with pytest.raises(DecodeError, match="invalid sync"):
        parse_rtp_mpeg2_transport(RTPPacket(33, 1, 2, 3, b"x" * 188).encode())


def test_rtp_mp2t_packetizer_handles_chunks_wrap_and_discontinuity_marker() -> None:
    packetizer = RTPMPEG2TransportPacketizer(
        ssrc=0x12345678,
        sequence_number=65535,
        packets_per_datagram=2,
    )
    source = _ts_packet(1) + _ts_packet(2) + _ts_packet(3)

    assert packetizer.feed(source[:200], timestamp=100) == ()
    first = packetizer.feed(source[200:400], timestamp=100)
    assert packetizer.feed(source[400:], timestamp=100) == ()
    second = packetizer.finish(timestamp=5, discontinuity=True)

    assert packetizer.buffered_bytes == 0
    assert len(first) == len(second) == 1
    first_packet = parse_rtp_mpeg2_transport(first[0], max_packets=2)
    second_packet = parse_rtp_mpeg2_transport(second[0], max_packets=2)
    assert first_packet.sequence_number == 65535
    assert first_packet.timestamp == 100
    assert first_packet.marker is False
    assert first_packet.payload == source[:376]
    assert second_packet.sequence_number == 0
    assert second_packet.timestamp == 5
    assert second_packet.marker is True
    assert second_packet.payload == source[376:]

    with pytest.raises(ValueError, match="backwards RTP timestamp"):
        packetizer.packetize(_ts_packet(4), timestamp=4)


def test_rtp_mp2t_packetizer_accepts_normal_32_bit_timestamp_wrap() -> None:
    packetizer = RTPMPEG2TransportPacketizer(ssrc=1, sequence_number=1)
    packetizer.packetize(_ts_packet(1), timestamp=0xFFFFFFF0)

    wrapped = parse_rtp_mpeg2_transport(
        packetizer.packetize(_ts_packet(2), timestamp=0x10)
    )

    assert wrapped.timestamp == 0x10
    assert wrapped.marker is False


def test_rtp_receiver_reports_loss_and_drops_late_or_duplicate_payloads() -> None:
    receiver = RTPMPEG2TransportReceiver()
    assert receiver.receive(_rtp(65535)).accepted_payload == _ts_packet(256)
    assert receiver.receive(_rtp(0)).sequence_issue is None

    loss = receiver.receive(_rtp(3))
    assert loss.accepted_payload == _ts_packet(256)
    assert loss.sequence_issue is not None
    assert loss.sequence_issue.kind == "loss"
    assert loss.sequence_issue.expected == 1
    assert loss.sequence_issue.actual == 3
    assert loss.sequence_issue.lost_packets == 2

    late = receiver.receive(_rtp(2))
    assert late.accepted_payload is None
    assert late.sequence_issue is not None
    assert late.sequence_issue.kind == "late_or_duplicate"

    duplicate = receiver.receive(_rtp(3))
    assert duplicate.accepted_payload is None
    assert duplicate.sequence_issue is not None


def test_rtp_receiver_locks_ssrc_until_explicit_reset() -> None:
    receiver = RTPMPEG2TransportReceiver()
    receiver.receive(_rtp(1, ssrc=1))
    with pytest.raises(DecodeError, match="SSRC changed"):
        receiver.receive(_rtp(2, ssrc=2))

    receiver.reset(ssrc=2)
    assert receiver.receive(_rtp(2, ssrc=2)).accepted_payload == _ts_packet(256)


def test_rtp_receiver_reports_unmarked_backwards_clock_but_accepts_wrap() -> None:
    receiver = RTPMPEG2TransportReceiver()
    assert receiver.receive(_rtp(1, timestamp=100)).timestamp_issue is None

    backwards = receiver.receive(_rtp(2, timestamp=50))
    assert backwards.accepted_payload is not None
    assert backwards.timestamp_issue is not None
    assert backwards.timestamp_issue.previous == 100
    assert backwards.timestamp_issue.actual == 50

    marked = RTPPacket(33, 3, 25, 7, _ts_packet(256), marker=True).encode()
    assert receiver.receive(marked).timestamp_issue is None

    receiver.reset()
    assert receiver.receive(_rtp(4, timestamp=0xFFFFFFF0)).timestamp_issue is None
    assert receiver.receive(_rtp(5, timestamp=0x10)).timestamp_issue is None


def test_rtp_receiver_validates_configuration_eagerly() -> None:
    with pytest.raises(ValueError, match="max_packets"):
        RTPMPEG2TransportReceiver(max_packets=0)

    receiver = RTPMPEG2TransportReceiver()
    with pytest.raises(TypeError, match="RTPPacket"):
        receiver.receive_packet(b"packet")  # type: ignore[arg-type]
    with pytest.raises(DecodeError, match="payload type"):
        receiver.receive_packet(RTPPacket(96, 1, 2, 3, _ts_packet(1)))
    with pytest.raises(DecodeError, match="invalid sync"):
        receiver.receive_packet(RTPPacket(33, 1, 2, 3, b"x" * 188))


def test_rtp_reorder_buffer_releases_contiguous_packets_in_sequence() -> None:
    assert PublicRTPPacketReorderBuffer is RTPPacketReorderBuffer
    reorder = RTPPacketReorderBuffer(max_reorder_packets=3)
    packet_1 = parse_rtp_mpeg2_transport(_rtp(1))
    packet_2 = parse_rtp_mpeg2_transport(_rtp(2))
    packet_3 = parse_rtp_mpeg2_transport(_rtp(3))

    assert reorder.push(packet_1) == RTPReorderResult((packet_1,), ())
    assert reorder.push(packet_3) == RTPReorderResult((), ())
    assert reorder.buffered_packets == 1
    assert reorder.push(packet_2) == RTPReorderResult((packet_2, packet_3), ())
    assert reorder.buffered_packets == 0


@given(
    start=st.integers(min_value=0, max_value=65535),
    delivery_offsets=st.permutations(tuple(range(1, 9))),
)
def test_rtp_reorder_buffer_preserves_arbitrary_in_window_permutations(
    start: int,
    delivery_offsets: list[int],
) -> None:
    reorder = RTPPacketReorderBuffer(max_reorder_packets=8)
    packets = {
        offset: parse_rtp_mpeg2_transport(_rtp((start + offset) % 65536))
        for offset in range(9)
    }
    released = list(reorder.push(packets[0]).packets)
    issues = []
    for offset in delivery_offsets:
        result = reorder.push(packets[offset])
        released.extend(result.packets)
        issues.extend(result.issues)

    assert issues == []
    assert [packet.sequence_number for packet in released] == [
        (start + offset) % 65536 for offset in range(9)
    ]
    assert reorder.buffered_packets == 0


def test_rtp_reorder_buffer_declares_gaps_under_pressure_and_at_flush() -> None:
    reorder = RTPPacketReorderBuffer(max_reorder_packets=2)
    packet_10 = parse_rtp_mpeg2_transport(_rtp(10))
    packet_12 = parse_rtp_mpeg2_transport(_rtp(12))
    packet_14 = parse_rtp_mpeg2_transport(_rtp(14))

    assert reorder.push(packet_10).packets == (packet_10,)
    assert reorder.push(packet_12) == RTPReorderResult((), ())
    pressure = reorder.push(packet_14)
    assert pressure.packets == (packet_12,)
    assert len(pressure.issues) == 1
    assert pressure.issues[0].kind == "loss"
    assert pressure.issues[0].expected == 11
    assert pressure.issues[0].actual == 12
    assert pressure.issues[0].lost_packets == 1

    flushed = reorder.flush()
    assert flushed.packets == (packet_14,)
    assert flushed.issues[0].expected == 13
    assert flushed.issues[0].actual == 14
    assert flushed.issues[0].lost_packets == 1
    assert reorder.flush() == RTPReorderResult((), ())


def test_rtp_reorder_buffer_reports_buffered_duplicates_and_late_packets() -> None:
    reorder = RTPPacketReorderBuffer(max_reorder_packets=3)
    packet_1 = parse_rtp_mpeg2_transport(_rtp(1))
    packet_2 = parse_rtp_mpeg2_transport(_rtp(2))
    packet_3 = parse_rtp_mpeg2_transport(_rtp(3))
    reorder.push(packet_1)
    reorder.push(packet_3)

    duplicate = reorder.push(packet_3)
    assert duplicate.packets == ()
    assert duplicate.issues[0].kind == "late_or_duplicate"
    assert duplicate.issues[0].actual == 3
    assert reorder.push(packet_2).packets == (packet_2, packet_3)

    late = reorder.push(packet_2)
    assert late.packets == ()
    assert late.issues[0].kind == "late_or_duplicate"
    assert late.issues[0].expected == 4


def test_rtp_reorder_buffer_handles_sequence_wrap_and_session_reset() -> None:
    reorder = RTPPacketReorderBuffer(max_reorder_packets=2, ssrc=7)
    packet_last = parse_rtp_mpeg2_transport(_rtp(65535))
    packet_zero = parse_rtp_mpeg2_transport(_rtp(0))
    packet_one = parse_rtp_mpeg2_transport(_rtp(1))

    assert reorder.push(packet_last).packets == (packet_last,)
    assert reorder.push(packet_one).packets == ()
    assert reorder.push(packet_zero).packets == (packet_zero, packet_one)
    packet_three = parse_rtp_mpeg2_transport(_rtp(3))
    assert reorder.push(packet_three).packets == ()
    assert reorder.buffered_packets == 1

    with pytest.raises(DecodeError, match="SSRC changed"):
        reorder.push(parse_rtp_mpeg2_transport(_rtp(2, ssrc=8)))
    reorder.reset(ssrc=8)
    assert reorder.buffered_packets == 0
    changed = parse_rtp_mpeg2_transport(_rtp(2, ssrc=8))
    assert reorder.push(changed).packets == (changed,)


def test_rtp_reorder_buffer_validates_configuration_and_packet_type() -> None:
    with pytest.raises(ValueError, match="max_reorder_packets"):
        RTPPacketReorderBuffer(max_reorder_packets=32768)
    reorder = RTPPacketReorderBuffer()
    with pytest.raises(TypeError, match="RTPPacket"):
        reorder.push(b"packet")  # type: ignore[arg-type]


def test_rtp_receive_payload_composes_with_program_aware_live_demux() -> None:
    video = ElementaryStreamInfo(0x1B, 0x101, ())
    muxer = TransportMuxer(
        transport_stream_id=1,
        program_number=1,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=(video,),
    )
    transport = b"".join(
        muxer.program_tables()
        + muxer.mux_pes(0x101, encode_pes_packet(b"video", stream_id=0xE0, pts=0))
    )
    sender = RTPMPEG2TransportPacketizer(
        ssrc=7,
        sequence_number=10,
        packets_per_datagram=1,
    )
    datagrams = sender.feed(transport, timestamp=0) + sender.finish(timestamp=1)
    reorder = RTPPacketReorderBuffer()
    receiver = RTPMPEG2TransportReceiver(max_packets=1)
    demuxer = TransportDemuxer()

    events = []
    delivery = datagrams[:1] + datagrams[2:3] + datagrams[1:2] + datagrams[3:]
    for datagram in delivery:
        packet = parse_rtp_mpeg2_transport(datagram, max_packets=1)
        for released in reorder.push(packet).packets:
            reception = receiver.receive_packet(released)
            assert reception.accepted_payload is not None
            events.extend(demuxer.feed(reception.accepted_payload))
    for released in reorder.flush().packets:
        reception = receiver.receive_packet(released)
        assert reception.accepted_payload is not None
        events.extend(demuxer.feed(reception.accepted_payload))
    events.extend(demuxer.finish())

    assert any(isinstance(event, PATEvent) for event in events)
    assert any(isinstance(event, PMTEvent) for event in events)
    pes = next(event for event in events if isinstance(event, PESStreamEvent))
    assert pes.pes.payload == b"video"


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"payload_type": 128}, "payload_type"),
        ({"sequence_number": 65536}, "sequence_number"),
        ({"timestamp": -1}, "timestamp"),
        ({"ssrc": 1 << 32}, "ssrc"),
        ({"csrc": tuple(range(16))}, "csrc"),
        ({"extension_data": b"abcd"}, "extension_profile"),
        ({"extension_profile": 1, "extension_data": b"x"}, "multiple of four"),
        ({"padding_size": 256}, "padding_size"),
    ],
)
def test_rtp_packet_validates_header_fields(
    kwargs: dict[str, object], error: str
) -> None:
    values: dict[str, object] = {
        "payload_type": 33,
        "sequence_number": 1,
        "timestamp": 2,
        "ssrc": 3,
        "payload": b"",
    }
    values.update(kwargs)
    with pytest.raises((TypeError, ValueError), match=error):
        RTPPacket(**values)  # type: ignore[arg-type]
