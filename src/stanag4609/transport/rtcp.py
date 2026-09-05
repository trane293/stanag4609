"""RFC 3550 RTCP framing and Sender Report clock synchronization."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from stanag4609.errors import DecodeError, TruncatedData

RTCP_VERSION = 2
RTCP_HEADER_SIZE = 4
RTCP_SENDER_REPORT_TYPE = 200
RTCP_RECEIVER_REPORT_TYPE = 201
RTCP_SDES_PACKET_TYPE = 202
RTCP_CNAME_ITEM_TYPE = 1
RTCP_RECEPTION_REPORT_SIZE = 24
RTCP_SENDER_INFORMATION_SIZE = 24
RTP_TIMESTAMP_MODULUS = 1 << 32
NTP_FRACTION_MODULUS = 1 << 32


def _uint(value: int, *, bits: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 1 << bits:
        raise ValueError(f"{name} must be an unsigned {bits}-bit integer")
    return value


def _sint(value: int, *, bits: int, name: str) -> int:
    lower = -(1 << (bits - 1))
    upper = 1 << (bits - 1)
    if isinstance(value, bool) or not isinstance(value, int) or not lower <= value < upper:
        raise ValueError(f"{name} must be a signed {bits}-bit integer")
    return value


@dataclass(frozen=True, slots=True)
class RTCPPacket:
    """One generically framed RTCP packet, including an uninterpreted payload."""

    packet_type: int
    count: int
    payload: bytes
    padding_size: int = 0

    def __post_init__(self) -> None:
        _uint(self.packet_type, bits=8, name="packet_type")
        _uint(self.count, bits=5, name="count")
        if not isinstance(self.payload, bytes):
            raise TypeError("payload must be bytes")
        if (
            isinstance(self.padding_size, bool)
            or not isinstance(self.padding_size, int)
            or not 0 <= self.padding_size <= 255
        ):
            raise ValueError("padding_size must be an integer from 0 to 255")
        packet_size = RTCP_HEADER_SIZE + len(self.payload) + self.padding_size
        if packet_size % 4:
            raise ValueError("RTCP packet length must be a multiple of four bytes")
        if packet_size // 4 - 1 > 0xFFFF:
            raise ValueError("RTCP packet exceeds the 16-bit length field")

    def encode(self) -> bytes:
        """Encode the RFC 3550 common header, payload, and optional padding."""

        first = RTCP_VERSION << 6 | self.count
        if self.padding_size:
            first |= 0x20
        packet_size = RTCP_HEADER_SIZE + len(self.payload) + self.padding_size
        output = bytearray((first, self.packet_type))
        output.extend((packet_size // 4 - 1).to_bytes(2, "big"))
        output.extend(self.payload)
        if self.padding_size:
            output.extend(b"\x00" * (self.padding_size - 1))
            output.append(self.padding_size)
        return bytes(output)


@dataclass(frozen=True, slots=True)
class RTCPReceptionReport:
    """One 24-byte reception-report block carried by an RTCP sender report."""

    ssrc: int
    fraction_lost: int
    cumulative_packets_lost: int
    extended_highest_sequence: int
    interarrival_jitter: int
    last_sender_report: int
    delay_since_last_sender_report: int

    def __post_init__(self) -> None:
        _uint(self.ssrc, bits=32, name="ssrc")
        _uint(self.fraction_lost, bits=8, name="fraction_lost")
        _sint(
            self.cumulative_packets_lost,
            bits=24,
            name="cumulative_packets_lost",
        )
        _uint(
            self.extended_highest_sequence,
            bits=32,
            name="extended_highest_sequence",
        )
        _uint(self.interarrival_jitter, bits=32, name="interarrival_jitter")
        _uint(self.last_sender_report, bits=32, name="last_sender_report")
        _uint(
            self.delay_since_last_sender_report,
            bits=32,
            name="delay_since_last_sender_report",
        )

    def encode(self) -> bytes:
        """Encode the fixed RFC 3550 reception-report block."""

        output = bytearray(self.ssrc.to_bytes(4, "big"))
        output.append(self.fraction_lost)
        output.extend(self.cumulative_packets_lost.to_bytes(3, "big", signed=True))
        output.extend(self.extended_highest_sequence.to_bytes(4, "big"))
        output.extend(self.interarrival_jitter.to_bytes(4, "big"))
        output.extend(self.last_sender_report.to_bytes(4, "big"))
        output.extend(self.delay_since_last_sender_report.to_bytes(4, "big"))
        return bytes(output)

    @classmethod
    def decode(cls, data: bytes | bytearray | memoryview) -> RTCPReceptionReport:
        """Decode exactly one 24-byte RFC 3550 reception-report block."""

        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("RTCP reception report must be bytes-like")
        raw = bytes(data)
        if len(raw) != RTCP_RECEPTION_REPORT_SIZE:
            raise DecodeError(
                "RTCP reception report must contain exactly "
                f"{RTCP_RECEPTION_REPORT_SIZE} bytes, got {len(raw)}"
            )
        return cls(
            ssrc=int.from_bytes(raw[0:4], "big"),
            fraction_lost=raw[4],
            cumulative_packets_lost=int.from_bytes(raw[5:8], "big", signed=True),
            extended_highest_sequence=int.from_bytes(raw[8:12], "big"),
            interarrival_jitter=int.from_bytes(raw[12:16], "big"),
            last_sender_report=int.from_bytes(raw[16:20], "big"),
            delay_since_last_sender_report=int.from_bytes(raw[20:24], "big"),
        )


@dataclass(frozen=True, slots=True)
class RTCPReceiverReport:
    """An RFC 3550 Receiver Report and its zero or more report blocks."""

    ssrc: int
    reception_reports: tuple[RTCPReceptionReport, ...] = ()

    def __post_init__(self) -> None:
        _uint(self.ssrc, bits=32, name="ssrc")
        _validate_reception_reports(self.reception_reports)

    def to_packet(self, *, padding_size: int = 0) -> RTCPPacket:
        """Build the generic RTCP packet representation for this report."""

        payload = bytearray(self.ssrc.to_bytes(4, "big"))
        for report in self.reception_reports:
            payload.extend(report.encode())
        return RTCPPacket(
            packet_type=RTCP_RECEIVER_REPORT_TYPE,
            count=len(self.reception_reports),
            payload=bytes(payload),
            padding_size=padding_size,
        )

    def encode(self, *, padding_size: int = 0) -> bytes:
        """Encode this Receiver Report as one RTCP packet."""

        return self.to_packet(padding_size=padding_size).encode()

    @classmethod
    def from_packet(cls, packet: RTCPPacket) -> RTCPReceiverReport:
        """Interpret a generically framed packet as an RFC 3550 Receiver Report."""

        if not isinstance(packet, RTCPPacket):
            raise TypeError("packet must be an RTCPPacket")
        if packet.packet_type != RTCP_RECEIVER_REPORT_TYPE:
            raise DecodeError(f"RTCP packet type {packet.packet_type} is not a Receiver Report")
        expected_size = 4 + packet.count * RTCP_RECEPTION_REPORT_SIZE
        if len(packet.payload) != expected_size:
            raise DecodeError(
                f"Receiver Report count {packet.count} requires {expected_size} payload "
                f"bytes including reception report blocks, got {len(packet.payload)}"
            )
        return cls(
            ssrc=int.from_bytes(packet.payload[0:4], "big"),
            reception_reports=_decode_reception_reports(
                packet.payload,
                offset=4,
                count=packet.count,
            ),
        )


def _validate_reception_reports(
    reports: tuple[RTCPReceptionReport, ...],
) -> None:
    if not isinstance(reports, tuple):
        raise TypeError("reception_reports must be a tuple")
    if len(reports) > 31:
        raise ValueError("an RTCP report may contain at most 31 report blocks")
    if not all(isinstance(report, RTCPReceptionReport) for report in reports):
        raise TypeError("reception_reports must contain RTCPReceptionReport values")


def _decode_reception_reports(
    payload: bytes,
    *,
    offset: int,
    count: int,
) -> tuple[RTCPReceptionReport, ...]:
    return tuple(
        RTCPReceptionReport.decode(payload[index : index + RTCP_RECEPTION_REPORT_SIZE])
        for index in range(
            offset,
            offset + count * RTCP_RECEPTION_REPORT_SIZE,
            RTCP_RECEPTION_REPORT_SIZE,
        )
    )


@dataclass(frozen=True, slots=True)
class RTCPSenderReport:
    """An RFC 3550 Sender Report and its zero or more receiver report blocks."""

    ssrc: int
    ntp_seconds: int
    ntp_fraction: int
    rtp_timestamp: int
    sender_packet_count: int
    sender_octet_count: int
    reception_reports: tuple[RTCPReceptionReport, ...] = ()

    def __post_init__(self) -> None:
        _uint(self.ssrc, bits=32, name="ssrc")
        _uint(self.ntp_seconds, bits=32, name="ntp_seconds")
        _uint(self.ntp_fraction, bits=32, name="ntp_fraction")
        _uint(self.rtp_timestamp, bits=32, name="rtp_timestamp")
        _uint(self.sender_packet_count, bits=32, name="sender_packet_count")
        _uint(self.sender_octet_count, bits=32, name="sender_octet_count")
        _validate_reception_reports(self.reception_reports)

    @property
    def ntp_timestamp(self) -> Fraction:
        """Return the exact unsigned 32.32 NTP timestamp in NTP-era seconds."""

        return Fraction(
            self.ntp_seconds * NTP_FRACTION_MODULUS + self.ntp_fraction,
            NTP_FRACTION_MODULUS,
        )

    def to_packet(self, *, padding_size: int = 0) -> RTCPPacket:
        """Build the generic RTCP packet representation for this Sender Report."""

        payload = bytearray(self.ssrc.to_bytes(4, "big"))
        payload.extend(self.ntp_seconds.to_bytes(4, "big"))
        payload.extend(self.ntp_fraction.to_bytes(4, "big"))
        payload.extend(self.rtp_timestamp.to_bytes(4, "big"))
        payload.extend(self.sender_packet_count.to_bytes(4, "big"))
        payload.extend(self.sender_octet_count.to_bytes(4, "big"))
        for report in self.reception_reports:
            payload.extend(report.encode())
        return RTCPPacket(
            packet_type=RTCP_SENDER_REPORT_TYPE,
            count=len(self.reception_reports),
            payload=bytes(payload),
            padding_size=padding_size,
        )

    def encode(self, *, padding_size: int = 0) -> bytes:
        """Encode this Sender Report as one RTCP packet."""

        return self.to_packet(padding_size=padding_size).encode()

    @classmethod
    def from_packet(cls, packet: RTCPPacket) -> RTCPSenderReport:
        """Interpret a generically framed packet as an RFC 3550 Sender Report."""

        if not isinstance(packet, RTCPPacket):
            raise TypeError("packet must be an RTCPPacket")
        if packet.packet_type != RTCP_SENDER_REPORT_TYPE:
            raise DecodeError(f"RTCP packet type {packet.packet_type} is not a Sender Report")
        expected_size = RTCP_SENDER_INFORMATION_SIZE + (packet.count * RTCP_RECEPTION_REPORT_SIZE)
        if len(packet.payload) != expected_size:
            raise DecodeError(
                f"Sender Report count {packet.count} requires {expected_size} payload "
                f"bytes including reception report blocks, got {len(packet.payload)}"
            )
        raw = packet.payload
        reports = _decode_reception_reports(
            raw,
            offset=RTCP_SENDER_INFORMATION_SIZE,
            count=packet.count,
        )
        return cls(
            ssrc=int.from_bytes(raw[0:4], "big"),
            ntp_seconds=int.from_bytes(raw[4:8], "big"),
            ntp_fraction=int.from_bytes(raw[8:12], "big"),
            rtp_timestamp=int.from_bytes(raw[12:16], "big"),
            sender_packet_count=int.from_bytes(raw[16:20], "big"),
            sender_octet_count=int.from_bytes(raw[20:24], "big"),
            reception_reports=reports,
        )


@dataclass(frozen=True, slots=True)
class RTCPSDESItem:
    """One source-description item with its uninterpreted wire value."""

    item_type: int
    value: bytes

    def __post_init__(self) -> None:
        if (
            isinstance(self.item_type, bool)
            or not isinstance(self.item_type, int)
            or not 1 <= self.item_type <= 255
        ):
            raise ValueError("item_type must be an integer from 1 to 255")
        if not isinstance(self.value, bytes):
            raise TypeError("value must be bytes")
        if len(self.value) > 255:
            raise ValueError("SDES item value must contain at most 255 octets")
        if self.item_type == RTCP_CNAME_ITEM_TYPE and not self.value:
            raise ValueError("SDES CNAME must be non-empty")

    @classmethod
    def from_text(cls, item_type: int, text: str) -> RTCPSDESItem:
        """Encode one RFC 3550 UTF-8 source-description value."""

        if not isinstance(text, str):
            raise TypeError("text must be str")
        return cls(item_type, text.encode("utf-8"))

    @property
    def text(self) -> str:
        """Decode this item as strict UTF-8 text."""

        try:
            return self.value.decode("utf-8")
        except UnicodeDecodeError as error:
            raise DecodeError("SDES text is not valid UTF-8") from error

    def encode(self) -> bytes:
        """Encode the type, one-octet length, and value."""

        return bytes((self.item_type, len(self.value))) + self.value


@dataclass(frozen=True, slots=True)
class RTCPSDESChunk:
    """One 32-bit-aligned SDES source chunk."""

    ssrc: int
    items: tuple[RTCPSDESItem, ...] = ()

    def __post_init__(self) -> None:
        _uint(self.ssrc, bits=32, name="ssrc")
        if not isinstance(self.items, tuple):
            raise TypeError("items must be a tuple")
        if not all(isinstance(item, RTCPSDESItem) for item in self.items):
            raise TypeError("items must contain RTCPSDESItem values")

    def encode(self) -> bytes:
        """Encode this chunk, END marker, and chunk-local zero padding."""

        output = bytearray(self.ssrc.to_bytes(4, "big"))
        for item in self.items:
            output.extend(item.encode())
        output.append(0)
        output.extend(b"\x00" * (-len(output) % 4))
        return bytes(output)


@dataclass(frozen=True, slots=True)
class RTCPSDESPacket:
    """An RFC 3550 Source Description packet with up to 31 chunks."""

    chunks: tuple[RTCPSDESChunk, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.chunks, tuple):
            raise TypeError("chunks must be a tuple")
        if len(self.chunks) > 31:
            raise ValueError("an SDES packet may contain at most 31 chunks")
        if not all(isinstance(chunk, RTCPSDESChunk) for chunk in self.chunks):
            raise TypeError("chunks must contain RTCPSDESChunk values")

    def to_packet(self, *, padding_size: int = 0) -> RTCPPacket:
        """Build the generic RTCP packet representation for this SDES packet."""

        return RTCPPacket(
            packet_type=RTCP_SDES_PACKET_TYPE,
            count=len(self.chunks),
            payload=b"".join(chunk.encode() for chunk in self.chunks),
            padding_size=padding_size,
        )

    def encode(self, *, padding_size: int = 0) -> bytes:
        """Encode this Source Description as one RTCP packet."""

        return self.to_packet(padding_size=padding_size).encode()

    @classmethod
    def from_packet(cls, packet: RTCPPacket) -> RTCPSDESPacket:
        """Interpret a generically framed packet as an RFC 3550 SDES packet."""

        if not isinstance(packet, RTCPPacket):
            raise TypeError("packet must be an RTCPPacket")
        if packet.packet_type != RTCP_SDES_PACKET_TYPE:
            raise DecodeError(f"RTCP packet type {packet.packet_type} is not SDES")
        chunks: list[RTCPSDESChunk] = []
        offset = 0
        raw = packet.payload
        for _ in range(packet.count):
            if len(raw) - offset < 4:
                raise TruncatedData("SDES packet ends inside a chunk source identifier")
            chunk_start = offset
            ssrc = int.from_bytes(raw[offset : offset + 4], "big")
            offset += 4
            items: list[RTCPSDESItem] = []
            while True:
                if offset >= len(raw):
                    raise TruncatedData("SDES chunk is missing its END item")
                item_type = raw[offset]
                offset += 1
                if item_type == 0:
                    break
                if offset >= len(raw):
                    raise TruncatedData("SDES item is missing its length")
                item_size = raw[offset]
                offset += 1
                if len(raw) - offset < item_size:
                    raise TruncatedData("SDES packet ends inside an item value")
                items.append(RTCPSDESItem(item_type, raw[offset : offset + item_size]))
                offset += item_size
            aligned_end = chunk_start + ((offset - chunk_start + 3) // 4) * 4
            if aligned_end > len(raw):
                raise TruncatedData("SDES packet ends inside chunk padding")
            if any(raw[offset:aligned_end]):
                raise DecodeError("SDES chunk padding must contain only zero octets")
            offset = aligned_end
            chunks.append(RTCPSDESChunk(ssrc, tuple(items)))
        if offset != len(raw):
            raise DecodeError(
                f"SDES source count leaves {len(raw) - offset} unexpected payload bytes"
            )
        return cls(tuple(chunks))

    def cname_for_ssrc(self, ssrc: int) -> str | None:
        """Return the unique CNAME associated with one source, when present."""

        source = _uint(ssrc, bits=32, name="ssrc")
        cnames = [
            item.text
            for chunk in self.chunks
            if chunk.ssrc == source
            for item in chunk.items
            if item.item_type == RTCP_CNAME_ITEM_TYPE
        ]
        if len(cnames) > 1:
            raise DecodeError(f"SDES contains multiple CNAME items for SSRC 0x{source:08x}")
        return cnames[0] if cnames else None


def parse_rtcp_packets(
    data: bytes | bytearray | memoryview,
) -> tuple[RTCPPacket, ...]:
    """Strictly frame every packet in one compound RTCP datagram.

    Unknown packet types are preserved as generic packets. RFC 3550 permits
    padding only on the final individual packet in a compound datagram.
    """

    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("RTCP datagram must be bytes-like")
    raw = bytes(data)
    if not raw:
        raise DecodeError("RTCP datagram is empty")
    packets: list[RTCPPacket] = []
    offset = 0
    while offset < len(raw):
        remaining = len(raw) - offset
        if remaining < RTCP_HEADER_SIZE:
            raise TruncatedData("RTCP datagram ends inside a common header")
        version = raw[offset] >> 6
        if version != RTCP_VERSION:
            raise DecodeError(f"unsupported RTCP version {version}; expected {RTCP_VERSION}")
        has_padding = bool(raw[offset] & 0x20)
        count = raw[offset] & 0x1F
        packet_type = raw[offset + 1]
        packet_size = (int.from_bytes(raw[offset + 2 : offset + 4], "big") + 1) * 4
        packet_end = offset + packet_size
        if packet_end > len(raw):
            raise TruncatedData(
                f"RTCP packet declares {packet_size} bytes, only {remaining} remain"
            )
        if has_padding and packet_end != len(raw):
            raise DecodeError("RTCP padding is permitted only on the final packet")
        padding_size = raw[packet_end - 1] if has_padding else 0
        payload_size = packet_size - RTCP_HEADER_SIZE
        if has_padding and (padding_size == 0 or padding_size > payload_size):
            raise DecodeError("invalid RTCP padding length")
        payload_end = packet_end - padding_size
        packets.append(
            RTCPPacket(
                packet_type=packet_type,
                count=count,
                payload=raw[offset + RTCP_HEADER_SIZE : payload_end],
                padding_size=padding_size,
            )
        )
        offset = packet_end
    return tuple(packets)


def parse_rtcp_sender_reports(
    data: bytes | bytearray | memoryview,
) -> tuple[RTCPSenderReport, ...]:
    """Decode every Sender Report in a framed RTCP datagram."""

    packets = parse_rtcp_packets(data)
    reports = tuple(
        RTCPSenderReport.from_packet(packet)
        for packet in packets
        if packet.packet_type == RTCP_SENDER_REPORT_TYPE
    )
    if not reports:
        raise DecodeError("RTCP datagram does not contain a Sender Report")
    return reports


def parse_rtcp_sdes_packets(
    data: bytes | bytearray | memoryview,
) -> tuple[RTCPSDESPacket, ...]:
    """Decode every Source Description packet in a framed RTCP datagram."""

    packets = parse_rtcp_packets(data)
    descriptions = tuple(
        RTCPSDESPacket.from_packet(packet)
        for packet in packets
        if packet.packet_type == RTCP_SDES_PACKET_TYPE
    )
    if not descriptions:
        raise DecodeError("RTCP datagram does not contain an SDES packet")
    return descriptions


def validate_rtcp_compound(
    data: bytes | bytearray | memoryview,
) -> tuple[RTCPPacket, ...]:
    """Validate the core RFC 3550 compound-report and CNAME invariants.

    Partial-encryption prefixes from RFC 3550 section 9.1 are intentionally
    outside this cleartext validator.
    """

    packets = parse_rtcp_packets(data)
    if len(packets) < 2:
        raise DecodeError("compound RTCP datagram must contain at least two packets")
    first = packets[0]
    if first.packet_type == RTCP_SENDER_REPORT_TYPE:
        sender_ssrc = RTCPSenderReport.from_packet(first).ssrc
    elif first.packet_type == RTCP_RECEIVER_REPORT_TYPE:
        sender_ssrc = RTCPReceiverReport.from_packet(first).ssrc
    else:
        raise DecodeError("compound RTCP datagram must begin with an SR or RR packet")
    descriptions = tuple(
        RTCPSDESPacket.from_packet(packet)
        for packet in packets
        if packet.packet_type == RTCP_SDES_PACKET_TYPE
    )
    if not descriptions:
        raise DecodeError("compound RTCP datagram must include an SDES CNAME")
    if not any(description.cname_for_ssrc(sender_ssrc) is not None for description in descriptions):
        raise DecodeError(
            f"compound RTCP datagram has no CNAME for the report sender SSRC 0x{sender_ssrc:08x}"
        )
    return packets


def encode_rtcp_sender_compound(
    report: RTCPSenderReport,
    *,
    cname: str,
    additional_packets: tuple[RTCPPacket, ...] = (),
) -> bytes:
    """Encode an SR-first compound RTCP datagram with its mandatory CNAME."""

    if not isinstance(report, RTCPSenderReport):
        raise TypeError("report must be an RTCPSenderReport")
    if not isinstance(additional_packets, tuple):
        raise TypeError("additional_packets must be a tuple")
    if not all(isinstance(packet, RTCPPacket) for packet in additional_packets):
        raise TypeError("additional_packets must contain RTCPPacket values")
    description = RTCPSDESPacket(
        (
            RTCPSDESChunk(
                report.ssrc,
                (RTCPSDESItem.from_text(RTCP_CNAME_ITEM_TYPE, cname),),
            ),
        )
    )
    compound = (
        report.encode()
        + description.encode()
        + b"".join(packet.encode() for packet in additional_packets)
    )
    validate_rtcp_compound(compound)
    return compound


@dataclass(frozen=True, slots=True)
class RTPNTPClockMapping:
    """Exact mapping between one RTP clock and the NTP instant in a Sender Report.

    The mapping is intentionally local to the Sender Report's NTP era. An NTP
    era cannot be inferred from the 64 wire bits alone.
    """

    ssrc: int
    ntp_seconds: int
    ntp_fraction: int
    reference_rtp_timestamp: int
    clock_rate: int

    def __post_init__(self) -> None:
        _uint(self.ssrc, bits=32, name="ssrc")
        _uint(self.ntp_seconds, bits=32, name="ntp_seconds")
        _uint(self.ntp_fraction, bits=32, name="ntp_fraction")
        _uint(
            self.reference_rtp_timestamp,
            bits=32,
            name="reference_rtp_timestamp",
        )
        if (
            isinstance(self.clock_rate, bool)
            or not isinstance(self.clock_rate, int)
            or self.clock_rate <= 0
        ):
            raise ValueError("clock_rate must be a positive integer")

    @classmethod
    def from_sender_report(
        cls,
        report: RTCPSenderReport,
        *,
        clock_rate: int,
    ) -> RTPNTPClockMapping:
        """Create a clock mapping from the paired NTP/RTP sender timestamps."""

        if not isinstance(report, RTCPSenderReport):
            raise TypeError("report must be an RTCPSenderReport")
        return cls(
            report.ssrc,
            report.ntp_seconds,
            report.ntp_fraction,
            report.rtp_timestamp,
            clock_rate,
        )

    @property
    def reference_ntp_timestamp(self) -> Fraction:
        """Return the exact Sender Report NTP instant."""

        return Fraction(
            self.ntp_seconds * NTP_FRACTION_MODULUS + self.ntp_fraction,
            NTP_FRACTION_MODULUS,
        )

    def ntp_timestamp(self, rtp_timestamp: int) -> Fraction:
        """Map a nearby 32-bit RTP timestamp to exact NTP-era seconds."""

        timestamp = _uint(rtp_timestamp, bits=32, name="rtp_timestamp")
        delta = (timestamp - self.reference_rtp_timestamp) % RTP_TIMESTAMP_MODULUS
        if delta >= RTP_TIMESTAMP_MODULUS // 2:
            delta -= RTP_TIMESTAMP_MODULUS
        return self.reference_ntp_timestamp + Fraction(delta, self.clock_rate)

    def rtp_timestamp(self, ntp_timestamp: int | Fraction) -> int:
        """Map an exactly representable nearby NTP instant to the RTP clock."""

        if isinstance(ntp_timestamp, bool) or not isinstance(ntp_timestamp, (int, Fraction)):
            raise TypeError("ntp_timestamp must be an int or Fraction")
        delta = (Fraction(ntp_timestamp) - self.reference_ntp_timestamp) * self.clock_rate
        if delta.denominator != 1:
            raise ValueError("NTP timestamp is not exactly representable on this RTP clock")
        ticks = delta.numerator
        if not -(RTP_TIMESTAMP_MODULUS // 2) <= ticks < RTP_TIMESTAMP_MODULUS // 2:
            raise ValueError("NTP timestamp is outside the unambiguous RTP mapping interval")
        return (self.reference_rtp_timestamp + ticks) % RTP_TIMESTAMP_MODULUS


__all__ = [
    "NTP_FRACTION_MODULUS",
    "RTCP_CNAME_ITEM_TYPE",
    "RTCP_HEADER_SIZE",
    "RTCP_RECEIVER_REPORT_TYPE",
    "RTCP_RECEPTION_REPORT_SIZE",
    "RTCP_SDES_PACKET_TYPE",
    "RTCP_SENDER_INFORMATION_SIZE",
    "RTCP_SENDER_REPORT_TYPE",
    "RTCP_VERSION",
    "RTCPPacket",
    "RTCPReceiverReport",
    "RTCPReceptionReport",
    "RTCPSDESChunk",
    "RTCPSDESItem",
    "RTCPSDESPacket",
    "RTCPSenderReport",
    "RTPNTPClockMapping",
    "encode_rtcp_sender_compound",
    "parse_rtcp_packets",
    "parse_rtcp_sdes_packets",
    "parse_rtcp_sender_reports",
    "validate_rtcp_compound",
]
