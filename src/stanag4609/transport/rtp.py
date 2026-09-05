"""MISB ST 0804 / RFC 2250 carriage of MPEG-2 transport streams over RTP."""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from stanag4609.errors import DecodeError, TruncatedData
from stanag4609.transport.rtcp import (
    RTCPReceptionReport,
    RTCPSenderReport,
    encode_rtcp_sender_compound,
)
from stanag4609.transport.udp import (
    DEFAULT_TS_PACKETS_PER_DATAGRAM,
    MAX_TS_PACKETS_PER_DATAGRAM,
    UdpTransportPacketizer,
    validate_udp_datagram,
)

RTP_VERSION = 2
RTP_HEADER_SIZE = 12
RTP_MPEG2_TS_PAYLOAD_TYPE = 33
RTP_MPEG2_TS_CLOCK_RATE = 90_000
RTP_SEQUENCE_MODULUS = 1 << 16
RTP_TIMESTAMP_MODULUS = 1 << 32


def _uint(value: int, *, bits: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 1 << bits:
        raise ValueError(f"{name} must be an unsigned {bits}-bit integer")
    return value


@dataclass(frozen=True, slots=True)
class RTPPacket:
    """One RTP version 2 packet with its payload separated from optional padding."""

    payload_type: int
    sequence_number: int
    timestamp: int
    ssrc: int
    payload: bytes
    marker: bool = False
    csrc: tuple[int, ...] = ()
    extension_profile: int | None = None
    extension_data: bytes = b""
    padding_size: int = 0

    def __post_init__(self) -> None:
        _uint(self.payload_type, bits=7, name="payload_type")
        _uint(self.sequence_number, bits=16, name="sequence_number")
        _uint(self.timestamp, bits=32, name="timestamp")
        _uint(self.ssrc, bits=32, name="ssrc")
        if not isinstance(self.marker, bool):
            raise TypeError("marker must be bool")
        if not isinstance(self.payload, bytes):
            raise TypeError("payload must be bytes")
        if len(self.csrc) > 15:
            raise ValueError("csrc may contain at most 15 identifiers")
        for identifier in self.csrc:
            _uint(identifier, bits=32, name="csrc identifier")
        if not isinstance(self.extension_data, bytes):
            raise TypeError("extension_data must be bytes")
        if self.extension_profile is None:
            if self.extension_data:
                raise ValueError("extension_data requires extension_profile")
        else:
            _uint(self.extension_profile, bits=16, name="extension_profile")
            if len(self.extension_data) % 4:
                raise ValueError("extension_data length must be a multiple of four bytes")
            if len(self.extension_data) // 4 > 0xFFFF:
                raise ValueError("extension_data exceeds the RTP extension length field")
        if (
            isinstance(self.padding_size, bool)
            or not isinstance(self.padding_size, int)
            or not 0 <= self.padding_size <= 255
        ):
            raise ValueError("padding_size must be an integer from 0 to 255")

    def encode(self) -> bytes:
        """Encode the packet using the RFC 3550 fixed and optional header syntax."""

        first = RTP_VERSION << 6 | len(self.csrc)
        if self.padding_size:
            first |= 0x20
        if self.extension_profile is not None:
            first |= 0x10
        second = self.payload_type | (0x80 if self.marker else 0)
        output = bytearray((first, second))
        output.extend(self.sequence_number.to_bytes(2, "big"))
        output.extend(self.timestamp.to_bytes(4, "big"))
        output.extend(self.ssrc.to_bytes(4, "big"))
        for identifier in self.csrc:
            output.extend(identifier.to_bytes(4, "big"))
        if self.extension_profile is not None:
            output.extend(self.extension_profile.to_bytes(2, "big"))
            output.extend((len(self.extension_data) // 4).to_bytes(2, "big"))
            output.extend(self.extension_data)
        output.extend(self.payload)
        if self.padding_size:
            output.extend(b"\x00" * (self.padding_size - 1))
            output.append(self.padding_size)
        return bytes(output)


def parse_rtp_packet(data: bytes | bytearray | memoryview) -> RTPPacket:
    """Decode one complete RTP version 2 datagram without interpreting its payload."""

    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("RTP datagram must be bytes-like")
    raw = bytes(data)
    if len(raw) < RTP_HEADER_SIZE:
        raise TruncatedData(
            f"RTP datagram needs at least {RTP_HEADER_SIZE} bytes, got {len(raw)}"
        )
    version = raw[0] >> 6
    if version != RTP_VERSION:
        raise DecodeError(f"unsupported RTP version {version}; expected {RTP_VERSION}")
    has_padding = bool(raw[0] & 0x20)
    has_extension = bool(raw[0] & 0x10)
    csrc_count = raw[0] & 0x0F
    offset = RTP_HEADER_SIZE + 4 * csrc_count
    if len(raw) < offset:
        raise TruncatedData("RTP datagram ends inside the CSRC list")
    csrc = tuple(
        int.from_bytes(raw[index : index + 4], "big")
        for index in range(RTP_HEADER_SIZE, offset, 4)
    )
    extension_profile: int | None = None
    extension_data = b""
    if has_extension:
        if len(raw) < offset + 4:
            raise TruncatedData("RTP datagram ends inside the extension header")
        extension_profile = int.from_bytes(raw[offset : offset + 2], "big")
        extension_length = int.from_bytes(raw[offset + 2 : offset + 4], "big") * 4
        offset += 4
        if len(raw) < offset + extension_length:
            raise TruncatedData("RTP datagram ends inside extension data")
        extension_data = raw[offset : offset + extension_length]
        offset += extension_length
    padding_size = raw[-1] if has_padding else 0
    if has_padding and (padding_size == 0 or padding_size > len(raw) - offset):
        raise DecodeError("invalid RTP padding length")
    payload_end = len(raw) - padding_size
    return RTPPacket(
        payload_type=raw[1] & 0x7F,
        sequence_number=int.from_bytes(raw[2:4], "big"),
        timestamp=int.from_bytes(raw[4:8], "big"),
        ssrc=int.from_bytes(raw[8:12], "big"),
        payload=raw[offset:payload_end],
        marker=bool(raw[1] & 0x80),
        csrc=csrc,
        extension_profile=extension_profile,
        extension_data=extension_data,
        padding_size=padding_size,
    )


def parse_rtp_mpeg2_transport(
    data: bytes | bytearray | memoryview,
    *,
    payload_type: int = RTP_MPEG2_TS_PAYLOAD_TYPE,
    max_packets: int = DEFAULT_TS_PACKETS_PER_DATAGRAM,
) -> RTPPacket:
    """Decode and validate one RFC 2250 MPEG-2 TS RTP datagram."""

    _uint(payload_type, bits=7, name="payload_type")
    packet = parse_rtp_packet(data)
    if packet.payload_type != payload_type:
        raise DecodeError(
            f"RTP payload type {packet.payload_type} is not configured MP2T type "
            f"{payload_type}"
        )
    validate_udp_datagram(packet.payload, max_packets=max_packets)
    return packet


def _max_packet_count(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_TS_PACKETS_PER_DATAGRAM
    ):
        raise ValueError(
            f"max_packets must be an integer from 1 to {MAX_TS_PACKETS_PER_DATAGRAM}"
        )
    return value


class RTPMPEG2TransportPacketizer:
    """Wrap bounded MPEG-2 TS payloads in deterministic RFC 2250 RTP packets."""

    def __init__(
        self,
        *,
        ssrc: int | None = None,
        sequence_number: int | None = None,
        payload_type: int = RTP_MPEG2_TS_PAYLOAD_TYPE,
        packets_per_datagram: int = DEFAULT_TS_PACKETS_PER_DATAGRAM,
    ) -> None:
        self.ssrc = secrets.randbits(32) if ssrc is None else _uint(ssrc, bits=32, name="ssrc")
        self.sequence_number = (
            secrets.randbits(16)
            if sequence_number is None
            else _uint(sequence_number, bits=16, name="sequence_number")
        )
        self.payload_type = _uint(payload_type, bits=7, name="payload_type")
        self._transport = UdpTransportPacketizer(
            packets_per_datagram=packets_per_datagram
        )
        self._last_timestamp: int | None = None
        self.sender_packet_count = 0
        self.sender_octet_count = 0

    @property
    def buffered_bytes(self) -> int:
        return self._transport.buffered_bytes

    def packetize(
        self,
        payload: bytes | bytearray | memoryview,
        *,
        timestamp: int,
        discontinuity: bool = False,
    ) -> bytes:
        """Wrap an integral TS payload using a caller-supplied 90 kHz timestamp."""

        timestamp = _uint(timestamp, bits=32, name="timestamp")
        if not isinstance(discontinuity, bool):
            raise TypeError("discontinuity must be bool")
        validate_udp_datagram(payload, max_packets=self._transport.packets_per_datagram)
        if self._last_timestamp is not None and not discontinuity:
            forward_distance = (timestamp - self._last_timestamp) % RTP_TIMESTAMP_MODULUS
            if forward_distance >= RTP_TIMESTAMP_MODULUS // 2:
                raise ValueError("a backwards RTP timestamp requires discontinuity=True")
        packet = RTPPacket(
            payload_type=self.payload_type,
            sequence_number=self.sequence_number,
            timestamp=timestamp,
            ssrc=self.ssrc,
            payload=bytes(payload),
            marker=discontinuity,
        )
        encoded = packet.encode()
        self.sequence_number = (self.sequence_number + 1) % RTP_SEQUENCE_MODULUS
        self.sender_packet_count = (self.sender_packet_count + 1) % RTP_TIMESTAMP_MODULUS
        self.sender_octet_count = (
            self.sender_octet_count + len(packet.payload)
        ) % RTP_TIMESTAMP_MODULUS
        self._last_timestamp = timestamp
        return encoded

    def feed(
        self,
        data: bytes | bytearray | memoryview,
        *,
        timestamp: int,
        discontinuity: bool = False,
    ) -> tuple[bytes, ...]:
        """Packetize complete configured TS groups from arbitrary byte chunks."""

        payloads = self._transport.feed(data)
        return tuple(
            self.packetize(
                payload,
                timestamp=timestamp,
                discontinuity=discontinuity and index == 0,
            )
            for index, payload in enumerate(payloads)
        )

    def finish(
        self,
        *,
        timestamp: int,
        discontinuity: bool = False,
    ) -> tuple[bytes, ...]:
        """Emit the final integral TS payload, if present, and finish the stream."""

        return tuple(
            self.packetize(payload, timestamp=timestamp, discontinuity=discontinuity)
            for payload in self._transport.finish()
        )

    def compound_sender_report(
        self,
        *,
        ntp_seconds: int,
        ntp_fraction: int,
        rtp_timestamp: int,
        cname: str,
        reception_reports: tuple[RTCPReceptionReport, ...] = (),
    ) -> bytes:
        """Build an SR+CNAME compound RTCP packet from generated RTP counters.

        The caller supplies paired NTP/RTP timestamps for the same instant and
        should transmit this only after the counted RTP datagrams were actually
        sent. Socket delivery and RTCP interval scheduling remain external.
        """

        report = RTCPSenderReport(
            ssrc=self.ssrc,
            ntp_seconds=ntp_seconds,
            ntp_fraction=ntp_fraction,
            rtp_timestamp=rtp_timestamp,
            sender_packet_count=self.sender_packet_count,
            sender_octet_count=self.sender_octet_count,
            reception_reports=reception_reports,
        )
        return encode_rtcp_sender_compound(report, cname=cname)


@dataclass(frozen=True, slots=True)
class RTPSequenceIssue:
    """A receiver-observed packet loss or late/duplicate RTP packet."""

    kind: str
    expected: int
    actual: int
    lost_packets: int = 0


@dataclass(frozen=True, slots=True)
class RTPReorderResult:
    """Packets released in sequence order plus definite loss/duplicate observations."""

    packets: tuple[RTPPacket, ...]
    issues: tuple[RTPSequenceIssue, ...]


class RTPPacketReorderBuffer:
    """Bounded sequence-number reorder buffer for one RTP synchronization source."""

    def __init__(
        self,
        *,
        max_reorder_packets: int = 32,
        ssrc: int | None = None,
    ) -> None:
        if (
            isinstance(max_reorder_packets, bool)
            or not isinstance(max_reorder_packets, int)
            or not 1 <= max_reorder_packets < RTP_SEQUENCE_MODULUS // 2
        ):
            raise ValueError(
                "max_reorder_packets must be an integer from 1 to 32767"
            )
        self.max_reorder_packets = max_reorder_packets
        self.ssrc = None if ssrc is None else _uint(ssrc, bits=32, name="ssrc")
        self._expected_sequence: int | None = None
        self._buffer: dict[int, RTPPacket] = {}

    @property
    def buffered_packets(self) -> int:
        """Number of future packets retained while waiting for a gap."""

        return len(self._buffer)

    def _distance(self, sequence_number: int) -> int:
        assert self._expected_sequence is not None
        return (sequence_number - self._expected_sequence) % RTP_SEQUENCE_MODULUS

    def _drain_contiguous(self) -> tuple[RTPPacket, ...]:
        released: list[RTPPacket] = []
        while self._expected_sequence in self._buffer:
            assert self._expected_sequence is not None
            packet = self._buffer.pop(self._expected_sequence)
            released.append(packet)
            self._expected_sequence = (
                self._expected_sequence + 1
            ) % RTP_SEQUENCE_MODULUS
        return tuple(released)

    def _release_nearest(self) -> RTPReorderResult:
        assert self._expected_sequence is not None and self._buffer
        sequence_number = min(self._buffer, key=self._distance)
        distance = self._distance(sequence_number)
        issues: tuple[RTPSequenceIssue, ...] = ()
        if distance:
            issues = (
                RTPSequenceIssue(
                    "loss",
                    self._expected_sequence,
                    sequence_number,
                    distance,
                ),
            )
            self._expected_sequence = sequence_number
        return RTPReorderResult(self._drain_contiguous(), issues)

    def push(self, packet: RTPPacket) -> RTPReorderResult:
        """Retain one packet and release all packets now known to be in order."""

        if not isinstance(packet, RTPPacket):
            raise TypeError("packet must be an RTPPacket")
        if self.ssrc is None:
            self.ssrc = packet.ssrc
        elif packet.ssrc != self.ssrc:
            raise DecodeError(
                f"RTP SSRC changed from 0x{self.ssrc:08x} to 0x{packet.ssrc:08x}; "
                "reset the reorder buffer at the input-session boundary"
            )
        if self._expected_sequence is None:
            self._expected_sequence = (packet.sequence_number + 1) % RTP_SEQUENCE_MODULUS
            return RTPReorderResult((packet,), ())

        distance = self._distance(packet.sequence_number)
        if distance >= RTP_SEQUENCE_MODULUS // 2 or packet.sequence_number in self._buffer:
            return RTPReorderResult(
                (),
                (
                    RTPSequenceIssue(
                        "late_or_duplicate",
                        self._expected_sequence,
                        packet.sequence_number,
                    ),
                ),
            )
        self._buffer[packet.sequence_number] = packet
        if distance == 0:
            return RTPReorderResult(self._drain_contiguous(), ())
        if distance <= self.max_reorder_packets:
            return RTPReorderResult((), ())
        return self._release_nearest()

    def flush(self) -> RTPReorderResult:
        """Declare remaining sequence gaps lost and release every held packet."""

        packets: list[RTPPacket] = []
        issues: list[RTPSequenceIssue] = []
        while self._buffer:
            result = self._release_nearest()
            packets.extend(result.packets)
            issues.extend(result.issues)
        return RTPReorderResult(tuple(packets), tuple(issues))

    def reset(self, *, ssrc: int | None = None) -> None:
        """Start a new RTP session and discard all retained packets."""

        self.ssrc = None if ssrc is None else _uint(ssrc, bits=32, name="ssrc")
        self._expected_sequence = None
        self._buffer.clear()


@dataclass(frozen=True, slots=True)
class RTPTimestampIssue:
    """An obvious backwards RTP clock jump without the RFC 2250 marker bit."""

    previous: int
    actual: int


@dataclass(frozen=True, slots=True)
class RTPTransportReception:
    """Validated RTP packet and whether its TS payload is safe to consume in order."""

    packet: RTPPacket
    accepted_payload: bytes | None
    sequence_issue: RTPSequenceIssue | None
    timestamp_issue: RTPTimestampIssue | None


class RTPMPEG2TransportReceiver:
    """Validate MP2T RTP datagrams and protect an incremental TS demux from reordering."""

    def __init__(
        self,
        *,
        payload_type: int = RTP_MPEG2_TS_PAYLOAD_TYPE,
        max_packets: int = DEFAULT_TS_PACKETS_PER_DATAGRAM,
        ssrc: int | None = None,
    ) -> None:
        self.payload_type = _uint(payload_type, bits=7, name="payload_type")
        self.max_packets = _max_packet_count(max_packets)
        self.ssrc = None if ssrc is None else _uint(ssrc, bits=32, name="ssrc")
        self._expected_sequence: int | None = None
        self._last_timestamp: int | None = None

    def receive(self, data: bytes | bytearray | memoryview) -> RTPTransportReception:
        """Validate one datagram, report gaps, and discard late/duplicate TS payloads."""

        packet = parse_rtp_mpeg2_transport(
            data,
            payload_type=self.payload_type,
            max_packets=self.max_packets,
        )
        return self._receive_validated_packet(packet)

    def receive_packet(self, packet: RTPPacket) -> RTPTransportReception:
        """Consume an already parsed packet after validating its MP2T payload."""

        if not isinstance(packet, RTPPacket):
            raise TypeError("packet must be an RTPPacket")
        if packet.payload_type != self.payload_type:
            raise DecodeError(
                f"RTP payload type {packet.payload_type} is not configured MP2T type "
                f"{self.payload_type}"
            )
        validate_udp_datagram(packet.payload, max_packets=self.max_packets)
        return self._receive_validated_packet(packet)

    def _receive_validated_packet(self, packet: RTPPacket) -> RTPTransportReception:
        if self.ssrc is None:
            self.ssrc = packet.ssrc
        elif packet.ssrc != self.ssrc:
            raise DecodeError(
                f"RTP SSRC changed from 0x{self.ssrc:08x} to 0x{packet.ssrc:08x}; "
                "reset the receiver at the input-session boundary"
            )
        issue: RTPSequenceIssue | None = None
        timestamp_issue: RTPTimestampIssue | None = None
        accepted_payload: bytes | None = packet.payload
        if (
            self._expected_sequence is not None
            and packet.sequence_number != self._expected_sequence
        ):
            distance = (packet.sequence_number - self._expected_sequence) % RTP_SEQUENCE_MODULUS
            if distance < RTP_SEQUENCE_MODULUS // 2:
                issue = RTPSequenceIssue(
                    "loss",
                    self._expected_sequence,
                    packet.sequence_number,
                    distance,
                )
            else:
                issue = RTPSequenceIssue(
                    "late_or_duplicate",
                    self._expected_sequence,
                    packet.sequence_number,
                )
                accepted_payload = None
        if accepted_payload is not None:
            if self._last_timestamp is not None and not packet.marker:
                forward_distance = (
                    packet.timestamp - self._last_timestamp
                ) % RTP_TIMESTAMP_MODULUS
                if forward_distance >= RTP_TIMESTAMP_MODULUS // 2:
                    timestamp_issue = RTPTimestampIssue(
                        self._last_timestamp,
                        packet.timestamp,
                    )
            self._expected_sequence = (packet.sequence_number + 1) % RTP_SEQUENCE_MODULUS
            self._last_timestamp = packet.timestamp
        return RTPTransportReception(packet, accepted_payload, issue, timestamp_issue)

    def reset(self, *, ssrc: int | None = None) -> None:
        """Start a new RTP input session and discard sequence state."""

        self.ssrc = None if ssrc is None else _uint(ssrc, bits=32, name="ssrc")
        self._expected_sequence = None
        self._last_timestamp = None
