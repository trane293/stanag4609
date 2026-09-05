"""ST 1402 UDP datagram boundaries for MPEG-2 transport streams."""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from stanag4609.errors import DecodeError, TruncatedData
from stanag4609.transport.mpegts import TS_PACKET_SIZE

DEFAULT_TS_PACKETS_PER_DATAGRAM = 7
MAX_TS_PACKETS_PER_DATAGRAM = 65_507 // TS_PACKET_SIZE


def _packet_count(value: int, *, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_TS_PACKETS_PER_DATAGRAM
    ):
        raise ValueError(
            f"{name} must be an integer from 1 to {MAX_TS_PACKETS_PER_DATAGRAM}"
        )
    return value


def validate_udp_datagram(
    datagram: bytes | bytearray | memoryview,
    *,
    max_packets: int = MAX_TS_PACKETS_PER_DATAGRAM,
    validate_sync: bool = True,
) -> int:
    """Validate one ST 1402 UDP payload and return its TS packet count."""

    if not isinstance(datagram, (bytes, bytearray, memoryview)):
        raise TypeError("datagram must be bytes-like")
    _packet_count(max_packets, name="max_packets")
    if not isinstance(validate_sync, bool):
        raise TypeError("validate_sync must be bool")
    length = len(datagram)
    if not length:
        raise DecodeError("UDP transport datagram must contain at least one TS packet")
    if length % TS_PACKET_SIZE:
        raise DecodeError(
            f"UDP transport datagram length {length} is not an integer number of "
            f"{TS_PACKET_SIZE}-byte TS packets"
        )
    packets = length // TS_PACKET_SIZE
    if packets > max_packets:
        raise DecodeError(
            f"UDP transport datagram contains {packets} TS packets, exceeds {max_packets}"
        )
    if validate_sync:
        for packet_index in range(packets):
            offset = packet_index * TS_PACKET_SIZE
            if datagram[offset] != 0x47:
                raise DecodeError(
                    f"UDP transport datagram TS packet {packet_index} has invalid sync byte"
                )
    return packets


class UdpTransportPacketizer:
    """Group arbitrarily chunked TS bytes into bounded ST 1402 UDP payloads."""

    def __init__(
        self,
        *,
        packets_per_datagram: int = DEFAULT_TS_PACKETS_PER_DATAGRAM,
        validate_sync: bool = True,
    ) -> None:
        self.packets_per_datagram = _packet_count(
            packets_per_datagram, name="packets_per_datagram"
        )
        if not isinstance(validate_sync, bool):
            raise TypeError("validate_sync must be bool")
        self.validate_sync = validate_sync
        self._datagram_size = self.packets_per_datagram * TS_PACKET_SIZE
        self._buffer = bytearray()
        self._finished = False

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, data: bytes | bytearray | memoryview) -> tuple[bytes, ...]:
        """Consume TS bytes and return every complete configured UDP payload."""

        if self._finished:
            raise RuntimeError("cannot feed a finished UDP transport packetizer")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("transport data must be bytes-like")
        self._buffer.extend(data)
        datagrams: list[bytes] = []
        while len(self._buffer) >= self._datagram_size:
            datagram = bytes(self._buffer[: self._datagram_size])
            validate_udp_datagram(
                datagram,
                max_packets=self.packets_per_datagram,
                validate_sync=self.validate_sync,
            )
            del self._buffer[: self._datagram_size]
            datagrams.append(datagram)
        return tuple(datagrams)

    def finish(self) -> tuple[bytes, ...]:
        """Emit a smaller final datagram or reject a partial TS packet."""

        if self._finished:
            return ()
        self._finished = True
        if len(self._buffer) % TS_PACKET_SIZE:
            raise TruncatedData(
                f"transport stream ended with {len(self._buffer) % TS_PACKET_SIZE} "
                "byte(s) of a partial TS packet"
            )
        if not self._buffer:
            return ()
        datagram = bytes(self._buffer)
        validate_udp_datagram(
            datagram,
            max_packets=self.packets_per_datagram,
            validate_sync=self.validate_sync,
        )
        self._buffer.clear()
        return (datagram,)


def iter_udp_datagrams(
    chunks: Iterable[bytes],
    *,
    packets_per_datagram: int = DEFAULT_TS_PACKETS_PER_DATAGRAM,
    validate_sync: bool = True,
) -> Iterator[bytes]:
    """Yield complete UDP payloads from an iterable of arbitrary TS chunks."""

    packetizer = UdpTransportPacketizer(
        packets_per_datagram=packets_per_datagram,
        validate_sync=validate_sync,
    )
    for chunk in chunks:
        yield from packetizer.feed(chunk)
    yield from packetizer.finish()
