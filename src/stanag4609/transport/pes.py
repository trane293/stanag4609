"""Incremental Packetized Elementary Stream reconstruction and timing."""

from __future__ import annotations

from dataclasses import dataclass

from stanag4609.errors import DecodeError, LimitExceeded, TruncatedData
from stanag4609.transport.mpegts import TransportPacket

_SPECIAL_STREAM_IDS = {0xBC, 0xBE, 0xBF, 0xF0, 0xF1, 0xF2, 0xF8, 0xFF}


@dataclass(frozen=True, slots=True)
class PESPacket:
    """One complete PES packet with decoded presentation timing."""

    raw: bytes
    offset: int
    stream_id: int
    packet_length: int
    data_alignment_indicator: bool
    pts: int | None
    dts: int | None
    header_data: bytes
    payload: bytes
    transport_packets: tuple[TransportPacket, ...] = ()

    @property
    def pts_seconds(self) -> float | None:
        return None if self.pts is None else self.pts / 90_000

    @property
    def pts_dts_flags(self) -> int:
        """Return the encoded two-bit PTS/DTS mode for ordinary PES syntax."""

        if self.stream_id in _SPECIAL_STREAM_IDS or len(self.raw) < 8:
            return 0
        return self.raw[7] >> 6

    @property
    def escr_flag(self) -> bool:
        """Return whether the ordinary PES header asserts an ESCR field."""

        return bool(
            self.stream_id not in _SPECIAL_STREAM_IDS
            and len(self.raw) >= 8
            and self.raw[7] & 0x20
        )

    def __bytes__(self) -> bytes:
        return self.raw


def decode_timestamp(data: bytes, *, expected_prefix: int) -> int:
    """Decode the five-byte 33-bit PTS/DTS timestamp syntax."""
    if len(data) < 5:
        raise TruncatedData("PES timestamp requires five bytes")
    if data[0] >> 4 != expected_prefix:
        raise DecodeError(
            f"PES timestamp prefix is 0x{data[0] >> 4:X}, expected 0x{expected_prefix:X}"
        )
    if not (data[0] & 1 and data[2] & 1 and data[4] & 1):
        raise DecodeError("PES timestamp marker bit is not set")
    return (
        ((data[0] >> 1) & 0x07) << 30
        | data[1] << 22
        | (data[2] >> 1) << 15
        | data[3] << 7
        | data[4] >> 1
    )


def parse_pes_packet(
    raw: bytes,
    *,
    offset: int = 0,
    transport_packets: tuple[TransportPacket, ...] = (),
) -> PESPacket:
    """Parse one complete MPEG-2 PES packet."""
    if len(raw) < 6:
        raise TruncatedData("PES packet ends inside its six-byte header")
    if raw[:3] != b"\x00\x00\x01":
        raise DecodeError("invalid PES start code prefix")
    stream_id = raw[3]
    packet_length = int.from_bytes(raw[4:6], "big")
    if packet_length:
        expected = 6 + packet_length
        if len(raw) < expected:
            raise TruncatedData(f"PES declares {expected} bytes, observed {len(raw)}")
        if len(raw) != expected:
            raise DecodeError("PES has trailing bytes beyond PES_packet_length")

    if stream_id in _SPECIAL_STREAM_IDS:
        return PESPacket(
            raw,
            offset,
            stream_id,
            packet_length,
            False,
            None,
            None,
            b"",
            raw[6:],
            transport_packets,
        )
    if len(raw) < 9:
        raise TruncatedData("PES packet ends inside its optional header")
    if raw[6] & 0xC0 != 0x80:
        raise DecodeError("PES optional header does not begin with marker bits '10'")
    data_alignment_indicator = bool(raw[6] & 0x04)
    pts_dts_flags = raw[7] >> 6
    if pts_dts_flags == 0x01:
        raise DecodeError("PES PTS_DTS_flags contains forbidden value '01'")
    header_data_length = raw[8]
    payload_start = 9 + header_data_length
    if payload_start > len(raw):
        raise TruncatedData("PES header_data_length overruns packet")
    header_data = raw[9:payload_start]

    pts: int | None = None
    dts: int | None = None
    if pts_dts_flags == 0x02:
        if len(header_data) < 5:
            raise TruncatedData("PES PTS field is truncated")
        pts = decode_timestamp(header_data[:5], expected_prefix=0x2)
    elif pts_dts_flags == 0x03:
        if len(header_data) < 10:
            raise TruncatedData("PES PTS/DTS fields are truncated")
        pts = decode_timestamp(header_data[:5], expected_prefix=0x3)
        dts = decode_timestamp(header_data[5:10], expected_prefix=0x1)

    return PESPacket(
        raw,
        offset,
        stream_id,
        packet_length,
        data_alignment_indicator,
        pts,
        dts,
        header_data,
        raw[payload_start:],
        transport_packets,
    )


class PESAssembler:
    """Reconstruct one elementary PID's PES packets from live TS packets."""

    def __init__(self, *, pid: int, max_pes_length: int = 64 * 1024 * 1024) -> None:
        if not 0 <= pid <= 0x1FFF:
            raise ValueError("PID must fit in 13 bits")
        if max_pes_length < 6:
            raise ValueError("max_pes_length must be at least six bytes")
        self.pid = pid
        self.max_pes_length = max_pes_length
        self._buffer = bytearray()
        self._offset = 0
        self._synchronized = False
        self._transport_packets: list[TransportPacket] = []

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, packet: TransportPacket) -> list[PESPacket]:
        if packet.pid != self.pid:
            raise ValueError(f"expected PID {self.pid}, observed PID {packet.pid}")
        if not packet.has_payload or not packet.payload:
            if self._synchronized:
                self._transport_packets.append(packet)
            return []

        completed: list[PESPacket] = []
        if packet.payload_unit_start:
            if self._buffer:
                completed.append(self._finish_buffer())
            self._synchronized = True
            self._offset = packet.offset
            self._transport_packets = []
        elif not self._synchronized:
            return []

        self._transport_packets.append(packet)
        self._buffer.extend(packet.payload)
        if len(self._buffer) > self.max_pes_length:
            raise LimitExceeded(
                f"PES exceeds configured limit {self.max_pes_length} bytes on PID {self.pid}"
            )
        if len(self._buffer) < 6:
            return completed
        packet_length = int.from_bytes(self._buffer[4:6], "big")
        if packet_length == 0:
            return completed
        expected = 6 + packet_length
        if expected > self.max_pes_length:
            raise LimitExceeded(
                f"PES declares {expected} bytes, exceeding limit {self.max_pes_length}"
            )
        if len(self._buffer) > expected:
            raise DecodeError("TS payload contains bytes beyond declared PES packet")
        if len(self._buffer) == expected:
            completed.append(self._take_complete())
        return completed

    def finish(self) -> list[PESPacket]:
        if not self._buffer:
            return []
        return [self._finish_buffer()]

    def _finish_buffer(self) -> PESPacket:
        if len(self._buffer) < 6:
            raise TruncatedData("PES stream ended inside its six-byte header")
        packet_length = int.from_bytes(self._buffer[4:6], "big")
        expected = 6 + packet_length if packet_length else len(self._buffer)
        if len(self._buffer) != expected:
            raise TruncatedData(
                f"PES declares {expected} bytes, observed {len(self._buffer)} at boundary"
            )
        return self._take_complete()

    def _take_complete(self) -> PESPacket:
        raw = bytes(self._buffer)
        transport_packets = tuple(self._transport_packets)
        self._buffer.clear()
        self._transport_packets.clear()
        self._synchronized = False
        return parse_pes_packet(
            raw,
            offset=self._offset,
            transport_packets=transport_packets,
        )
