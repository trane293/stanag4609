from __future__ import annotations

import pytest

from stanag4609.errors import DecodeError, TruncatedData
from stanag4609.transport.udp import (
    DEFAULT_TS_PACKETS_PER_DATAGRAM,
    MAX_TS_PACKETS_PER_DATAGRAM,
    UdpTransportPacketizer,
    iter_udp_datagrams,
    validate_udp_datagram,
)


def _packet(pid: int) -> bytes:
    return bytes((0x47, (pid >> 8) & 0x1F, pid & 0xFF, 0x10)) + b"\xff" * 184


def test_udp_packetizer_accepts_arbitrary_chunks_and_emits_integer_packets() -> None:
    source = b"".join(_packet(index) for index in range(15))
    chunks = (source[index : index + 197] for index in range(0, len(source), 197))
    datagrams = list(iter_udp_datagrams(chunks))

    assert [len(item) // 188 for item in datagrams] == [7, 7, 1]
    assert b"".join(datagrams) == source
    assert all(validate_udp_datagram(item) in {1, 7} for item in datagrams)
    assert DEFAULT_TS_PACKETS_PER_DATAGRAM == 7


def test_udp_packetizer_buffers_only_one_incomplete_datagram_and_finishes_once() -> None:
    packetizer = UdpTransportPacketizer(packets_per_datagram=3)
    assert packetizer.feed(_packet(1) + _packet(2)) == ()
    assert packetizer.buffered_bytes == 376
    assert packetizer.feed(_packet(3)) == ((_packet(1) + _packet(2) + _packet(3)),)
    assert packetizer.finish() == ()
    assert packetizer.finish() == ()
    with pytest.raises(RuntimeError, match="finished"):
        packetizer.feed(b"")


def test_udp_packetizer_rejects_partial_packet_and_bad_sync() -> None:
    packetizer = UdpTransportPacketizer()
    packetizer.feed(_packet(1)[:100])
    with pytest.raises(TruncatedData, match="partial TS packet"):
        packetizer.finish()

    bad = bytearray(_packet(1))
    bad[0] = 0x46
    with pytest.raises(DecodeError, match="invalid sync"):
        list(iter_udp_datagrams((bytes(bad),), packets_per_datagram=1))
    assert list(
        iter_udp_datagrams((bytes(bad),), packets_per_datagram=1, validate_sync=False)
    ) == [bytes(bad)]


def test_validate_udp_datagram_rejects_nonintegral_empty_and_oversized_payloads() -> None:
    with pytest.raises(DecodeError, match="at least one"):
        validate_udp_datagram(b"")
    with pytest.raises(DecodeError, match="integer number"):
        validate_udp_datagram(_packet(1) + b"x")
    with pytest.raises(DecodeError, match="exceeds"):
        validate_udp_datagram(_packet(1) + _packet(2), max_packets=1)
    with pytest.raises(TypeError, match="bytes-like"):
        validate_udp_datagram("not bytes")  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [0, True, MAX_TS_PACKETS_PER_DATAGRAM + 1])
def test_udp_packet_count_configuration_is_bounded(value: int) -> None:
    with pytest.raises(ValueError):
        UdpTransportPacketizer(packets_per_datagram=value)
    with pytest.raises(ValueError):
        validate_udp_datagram(_packet(1), max_packets=value)


def test_udp_sync_configuration_must_be_boolean() -> None:
    with pytest.raises(TypeError, match="validate_sync"):
        UdpTransportPacketizer(validate_sync=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="validate_sync"):
        validate_udp_datagram(_packet(1), validate_sync=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="bytes-like"):
        UdpTransportPacketizer().feed("data")  # type: ignore[arg-type]
