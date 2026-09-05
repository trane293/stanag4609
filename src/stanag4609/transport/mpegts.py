"""Bounded incremental parsing for 188-byte MPEG-2 Transport Stream packets."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from fractions import Fraction
from typing import BinaryIO

from stanag4609.errors import DecodeError, TruncatedData

TS_PACKET_SIZE = 188
TS_SYNC_BYTE = 0x47


@dataclass(frozen=True, slots=True)
class ProgramClockReference:
    """One exact MPEG-2 Systems 27 MHz program clock reference."""

    base: int
    extension: int

    def __post_init__(self) -> None:
        if isinstance(self.base, bool) or not isinstance(self.base, int):
            raise TypeError("PCR base must be an integer")
        if not 0 <= self.base <= 2**33 - 1:
            raise ValueError("PCR base must be an unsigned 33-bit integer")
        if isinstance(self.extension, bool) or not isinstance(self.extension, int):
            raise TypeError("PCR extension must be an integer")
        if not 0 <= self.extension <= 299:
            raise ValueError("PCR extension must be an integer from 0 to 299")

    @property
    def ticks(self) -> int:
        """Clock value in exact 27 MHz ticks."""

        return (self.base * 300) + self.extension

    @property
    def seconds(self) -> Fraction:
        """Clock value as an exact fraction of a second."""

        return Fraction(self.ticks, 27_000_000)


@dataclass(frozen=True, slots=True)
class AdaptationFieldExtension:
    """Decoded optional fields from an H.222.0 adaptation-field extension."""

    legal_time_window_valid: bool | None = None
    legal_time_window_offset: int | None = None
    piecewise_rate: int | None = None
    splice_type: int | None = None
    dts_next_access_unit: int | None = None

    def __post_init__(self) -> None:
        if (self.legal_time_window_valid is None) != (
            self.legal_time_window_offset is None
        ):
            raise ValueError("legal-time-window validity and offset must appear together")
        if self.legal_time_window_valid is not None and not isinstance(
            self.legal_time_window_valid, bool
        ):
            raise TypeError("legal_time_window_valid must be a boolean or None")
        if self.legal_time_window_offset is not None and not (
            isinstance(self.legal_time_window_offset, int)
            and not isinstance(self.legal_time_window_offset, bool)
            and 0 <= self.legal_time_window_offset <= 0x7FFF
        ):
            raise ValueError("legal_time_window_offset must be an unsigned 15-bit integer")
        if self.piecewise_rate is not None and not (
            isinstance(self.piecewise_rate, int)
            and not isinstance(self.piecewise_rate, bool)
            and 1 <= self.piecewise_rate <= 0x3FFFFF
        ):
            raise ValueError("piecewise_rate must be a positive 22-bit integer")
        if (self.splice_type is None) != (self.dts_next_access_unit is None):
            raise ValueError("splice type and DTS_next_AU must appear together")
        if self.splice_type is not None and not (
            isinstance(self.splice_type, int)
            and not isinstance(self.splice_type, bool)
            and 0 <= self.splice_type <= 0xF
        ):
            raise ValueError("splice_type must be an unsigned 4-bit integer")
        if self.dts_next_access_unit is not None and not (
            isinstance(self.dts_next_access_unit, int)
            and not isinstance(self.dts_next_access_unit, bool)
            and 0 <= self.dts_next_access_unit < 2**33
        ):
            raise ValueError("dts_next_access_unit must be an unsigned 33-bit integer")


@dataclass(frozen=True, slots=True)
class AdaptationField:
    """Typed H.222.0 transport-packet adaptation fields."""

    empty: bool = False
    discontinuity_indicator: bool = False
    random_access_indicator: bool = False
    elementary_stream_priority_indicator: bool = False
    pcr: ProgramClockReference | None = None
    opcr: ProgramClockReference | None = None
    splice_countdown: int | None = None
    transport_private_data: bytes | None = None
    extension: AdaptationFieldExtension | None = None

    def __post_init__(self) -> None:
        for name in (
            "empty",
            "discontinuity_indicator",
            "random_access_indicator",
            "elementary_stream_priority_indicator",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a boolean")
        for name in ("pcr", "opcr"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, ProgramClockReference):
                raise TypeError(f"{name} must be ProgramClockReference or None")
        if self.splice_countdown is not None and not (
            isinstance(self.splice_countdown, int)
            and not isinstance(self.splice_countdown, bool)
            and -128 <= self.splice_countdown <= 127
        ):
            raise ValueError("splice_countdown must be a signed 8-bit integer")
        if self.transport_private_data is not None:
            if not isinstance(self.transport_private_data, bytes):
                raise TypeError("transport_private_data must be bytes or None")
            if len(self.transport_private_data) > 255:
                raise ValueError("transport_private_data cannot exceed 255 bytes")
        if self.extension is not None and not isinstance(
            self.extension, AdaptationFieldExtension
        ):
            raise TypeError("extension must be AdaptationFieldExtension or None")
        if (
            self.extension is not None
            and self.extension.splice_type is not None
            and self.splice_countdown is None
        ):
            raise ValueError("seamless splice extension requires splice_countdown")
        if self.empty and any(
            (
                self.discontinuity_indicator,
                self.random_access_indicator,
                self.elementary_stream_priority_indicator,
                self.pcr is not None,
                self.opcr is not None,
                self.splice_countdown is not None,
                self.transport_private_data is not None,
                self.extension is not None,
            )
        ):
            raise ValueError("empty adaptation field cannot contain flags or data")


def encode_program_clock_reference(clock: ProgramClockReference) -> bytes:
    """Encode the six-byte PCR/OPCR representation from H.222.0 Table 2-6."""

    if not isinstance(clock, ProgramClockReference):
        raise TypeError("clock must be ProgramClockReference")
    base = clock.base
    extension = clock.extension
    return bytes(
        (
            (base >> 25) & 0xFF,
            (base >> 17) & 0xFF,
            (base >> 9) & 0xFF,
            (base >> 1) & 0xFF,
            ((base & 1) << 7) | 0x7E | ((extension >> 8) & 1),
            extension & 0xFF,
        )
    )


def _encode_adaptation_timestamp(splice_type: int, timestamp: int) -> bytes:
    return bytes(
        (
            (splice_type << 4) | (((timestamp >> 30) & 0x07) << 1) | 1,
            (timestamp >> 22) & 0xFF,
            (((timestamp >> 15) & 0x7F) << 1) | 1,
            (timestamp >> 7) & 0xFF,
            ((timestamp & 0x7F) << 1) | 1,
        )
    )


def _validate_stuffing_length(value: int, *, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def encode_adaptation_field_extension(
    extension: AdaptationFieldExtension, *, stuffing_length: int = 0
) -> bytes:
    """Encode extension bytes, excluding the outer one-byte length field."""

    if not isinstance(extension, AdaptationFieldExtension):
        raise TypeError("extension must be AdaptationFieldExtension")
    _validate_stuffing_length(stuffing_length, name="stuffing_length")
    flags = 0x1F
    body = bytearray()
    if extension.legal_time_window_offset is not None:
        flags |= 0x80
        encoded = extension.legal_time_window_offset
        if extension.legal_time_window_valid:
            encoded |= 0x8000
        body.extend(encoded.to_bytes(2, "big"))
    if extension.piecewise_rate is not None:
        flags |= 0x40
        body.extend((0xC00000 | extension.piecewise_rate).to_bytes(3, "big"))
    if extension.splice_type is not None:
        assert extension.dts_next_access_unit is not None
        flags |= 0x20
        body.extend(
            _encode_adaptation_timestamp(
                extension.splice_type, extension.dts_next_access_unit
            )
        )
    result = bytes((flags,)) + bytes(body) + b"\xFF" * stuffing_length
    if len(result) > 255:
        raise ValueError("adaptation field extension cannot exceed 255 bytes")
    return result


def encode_adaptation_field(
    field: AdaptationField,
    *,
    stuffing_length: int = 0,
    extension_stuffing_length: int = 0,
) -> bytes:
    """Encode adaptation bytes, excluding the packet's one-byte length field."""

    if not isinstance(field, AdaptationField):
        raise TypeError("field must be AdaptationField")
    _validate_stuffing_length(stuffing_length, name="stuffing_length")
    _validate_stuffing_length(
        extension_stuffing_length, name="extension_stuffing_length"
    )
    if field.extension is None and extension_stuffing_length:
        raise ValueError("extension_stuffing_length requires an adaptation extension")
    if field.empty:
        if stuffing_length:
            raise ValueError("empty adaptation field cannot contain stuffing")
        return b""
    flags = (
        int(field.discontinuity_indicator) << 7
        | int(field.random_access_indicator) << 6
        | int(field.elementary_stream_priority_indicator) << 5
    )
    body = bytearray()
    if field.pcr is not None:
        flags |= 0x10
        body.extend(encode_program_clock_reference(field.pcr))
    if field.opcr is not None:
        flags |= 0x08
        body.extend(encode_program_clock_reference(field.opcr))
    if field.splice_countdown is not None:
        flags |= 0x04
        body.extend(field.splice_countdown.to_bytes(1, "big", signed=True))
    if field.transport_private_data is not None:
        flags |= 0x02
        body.append(len(field.transport_private_data))
        body.extend(field.transport_private_data)
    if field.extension is not None:
        flags |= 0x01
        encoded_extension = encode_adaptation_field_extension(
            field.extension, stuffing_length=extension_stuffing_length
        )
        body.append(len(encoded_extension))
        body.extend(encoded_extension)
    result = bytes((flags,)) + bytes(body) + b"\xFF" * stuffing_length
    if len(result) > 183:
        raise ValueError("adaptation field cannot exceed 183 bytes")
    return result


def _parse_program_clock_reference(
    adaptation_field: bytes,
    cursor: int,
    *,
    name: str,
) -> tuple[ProgramClockReference, int]:
    end = cursor + 6
    if end > len(adaptation_field):
        raise DecodeError(f"MPEG-2 TS adaptation field truncates {name}")
    value = adaptation_field[cursor:end]
    if value[4] & 0x7E != 0x7E:
        raise DecodeError(f"MPEG-2 TS {name} reserved bits must all be one")
    base = (
        (value[0] << 25)
        | (value[1] << 17)
        | (value[2] << 9)
        | (value[3] << 1)
        | (value[4] >> 7)
    )
    extension = ((value[4] & 1) << 8) | value[5]
    if extension > 299:
        raise DecodeError(f"MPEG-2 TS {name} extension exceeds 299")
    return ProgramClockReference(base, extension), end


def _require_adaptation_bytes(
    data: bytes, cursor: int, length: int, *, name: str
) -> tuple[bytes, int]:
    end = cursor + length
    if end > len(data):
        raise DecodeError(f"MPEG-2 TS adaptation field truncates {name}")
    return data[cursor:end], end


def _parse_adaptation_timestamp(value: bytes) -> tuple[int, int]:
    if len(value) != 5 or any(value[index] & 1 != 1 for index in (0, 2, 4)):
        raise DecodeError("MPEG-2 TS DTS_next_AU marker bits must all be one")
    splice_type = value[0] >> 4
    timestamp = (
        ((value[0] >> 1) & 0x07) << 30
        | value[1] << 22
        | (value[2] >> 1) << 15
        | value[3] << 7
        | (value[4] >> 1)
    )
    return splice_type, timestamp


def _parse_adaptation_field_extension(
    data: bytes, *, splicing_point: bool
) -> AdaptationFieldExtension:
    if not data:
        raise DecodeError("MPEG-2 TS adaptation field extension omits flags")
    flags = data[0]
    if flags & 0x1F != 0x1F:
        raise DecodeError("MPEG-2 TS adaptation extension reserved bits must all be one")
    cursor = 1
    ltw_valid = None
    ltw_offset = None
    piecewise_rate = None
    splice_type = None
    dts_next_access_unit = None
    if flags & 0x80:
        value, cursor = _require_adaptation_bytes(
            data, cursor, 2, name="legal time window"
        )
        encoded = int.from_bytes(value, "big")
        ltw_valid = bool(encoded & 0x8000)
        ltw_offset = encoded & 0x7FFF
    if flags & 0x40:
        value, cursor = _require_adaptation_bytes(
            data, cursor, 3, name="piecewise rate"
        )
        if value[0] & 0xC0 != 0xC0:
            raise DecodeError("MPEG-2 TS piecewise-rate reserved bits must all be one")
        piecewise_rate = int.from_bytes(value, "big") & 0x3FFFFF
        if piecewise_rate == 0:
            raise DecodeError("MPEG-2 TS piecewise_rate must be positive")
    if flags & 0x20:
        if not splicing_point:
            raise DecodeError("MPEG-2 TS seamless splice requires splicing_point_flag")
        value, cursor = _require_adaptation_bytes(
            data, cursor, 5, name="seamless splice"
        )
        splice_type, dts_next_access_unit = _parse_adaptation_timestamp(value)
    if any(byte != 0xFF for byte in data[cursor:]):
        raise DecodeError("MPEG-2 TS adaptation extension stuffing must be 0xFF")
    return AdaptationFieldExtension(
        ltw_valid,
        ltw_offset,
        piecewise_rate,
        splice_type,
        dts_next_access_unit,
    )


@dataclass(frozen=True, slots=True)
class TransportPacket:
    """One parsed MPEG-2 TS packet with its exact source bytes."""

    raw: bytes
    offset: int
    pid: int
    transport_error_indicator: bool
    payload_unit_start: bool
    transport_priority: bool
    scrambling_control: int
    adaptation_field_control: int
    continuity_counter: int
    adaptation_field: bytes
    payload: bytes
    discontinuity_indicator: bool
    pcr: ProgramClockReference | None
    opcr: ProgramClockReference | None
    elementary_stream_priority_indicator: bool = False
    splice_countdown: int | None = None
    transport_private_data: bytes | None = None
    adaptation_field_extension: AdaptationFieldExtension | None = None

    @property
    def has_adaptation_field(self) -> bool:
        return self.adaptation_field_control in {2, 3}

    @property
    def has_payload(self) -> bool:
        return self.adaptation_field_control in {1, 3}

    @property
    def random_access_indicator(self) -> bool:
        return bool(self.adaptation_field and self.adaptation_field[0] & 0x40)

    @property
    def adaptation(self) -> AdaptationField | None:
        """Return the typed adaptation fields, or ``None`` when absent."""

        if not self.has_adaptation_field:
            return None
        return AdaptationField(
            empty=not self.adaptation_field,
            discontinuity_indicator=self.discontinuity_indicator,
            random_access_indicator=self.random_access_indicator,
            elementary_stream_priority_indicator=(
                self.elementary_stream_priority_indicator
            ),
            pcr=self.pcr,
            opcr=self.opcr,
            splice_countdown=self.splice_countdown,
            transport_private_data=self.transport_private_data,
            extension=self.adaptation_field_extension,
        )

    def __bytes__(self) -> bytes:
        return self.raw


def parse_transport_packet(raw: bytes, *, offset: int = 0) -> TransportPacket:
    """Parse and validate the structure of one 188-byte TS packet."""
    if len(raw) != TS_PACKET_SIZE:
        raise DecodeError(
            f"MPEG-2 TS packet must be {TS_PACKET_SIZE} bytes, observed {len(raw)}"
        )
    if raw[0] != TS_SYNC_BYTE:
        raise DecodeError(
            f"invalid MPEG-2 TS sync byte 0x{raw[0]:02X} at stream offset {offset}"
        )

    byte1 = raw[1]
    byte3 = raw[3]
    adaptation_field_control = (byte3 >> 4) & 0x03
    if adaptation_field_control == 0:
        raise DecodeError("reserved MPEG-2 TS adaptation_field_control value 0")

    adaptation_field = b""
    discontinuity_indicator = False
    pcr = None
    opcr = None
    elementary_stream_priority_indicator = False
    splice_countdown = None
    transport_private_data = None
    adaptation_field_extension = None
    payload_start = 4
    if adaptation_field_control in {2, 3}:
        adaptation_length = raw[4]
        adaptation_end = 5 + adaptation_length
        if adaptation_end > TS_PACKET_SIZE:
            raise DecodeError(
                f"MPEG-2 TS adaptation field overruns packet: {adaptation_length} bytes"
            )
        if adaptation_field_control == 2 and adaptation_end != TS_PACKET_SIZE:
            raise DecodeError("adaptation-only MPEG-2 TS packet does not fill 188 bytes")
        adaptation_field = raw[5:adaptation_end]
        payload_start = adaptation_end
        if adaptation_field:
            flags = adaptation_field[0]
            discontinuity_indicator = bool(flags & 0x80)
            elementary_stream_priority_indicator = bool(flags & 0x20)
            cursor = 1
            if flags & 0x10:
                pcr, cursor = _parse_program_clock_reference(
                    adaptation_field, cursor, name="PCR"
                )
            if flags & 0x08:
                opcr, cursor = _parse_program_clock_reference(
                    adaptation_field, cursor, name="OPCR"
                )
            if flags & 0x04:
                value, cursor = _require_adaptation_bytes(
                    adaptation_field, cursor, 1, name="splice countdown"
                )
                splice_countdown = int.from_bytes(value, "big", signed=True)
            if flags & 0x02:
                length_value, cursor = _require_adaptation_bytes(
                    adaptation_field, cursor, 1, name="private-data length"
                )
                transport_private_data, cursor = _require_adaptation_bytes(
                    adaptation_field,
                    cursor,
                    length_value[0],
                    name="transport private data",
                )
            if flags & 0x01:
                length_value, cursor = _require_adaptation_bytes(
                    adaptation_field, cursor, 1, name="extension length"
                )
                extension_data, cursor = _require_adaptation_bytes(
                    adaptation_field,
                    cursor,
                    length_value[0],
                    name="adaptation field extension",
                )
                adaptation_field_extension = _parse_adaptation_field_extension(
                    extension_data, splicing_point=bool(flags & 0x04)
                )
            if any(byte != 0xFF for byte in adaptation_field[cursor:]):
                raise DecodeError("MPEG-2 TS adaptation field stuffing must be 0xFF")

    has_payload = adaptation_field_control in {1, 3}
    payload = raw[payload_start:] if has_payload else b""
    return TransportPacket(
        raw=raw,
        offset=offset,
        pid=((byte1 & 0x1F) << 8) | raw[2],
        transport_error_indicator=bool(byte1 & 0x80),
        payload_unit_start=bool(byte1 & 0x40),
        transport_priority=bool(byte1 & 0x20),
        scrambling_control=(byte3 >> 6) & 0x03,
        adaptation_field_control=adaptation_field_control,
        continuity_counter=byte3 & 0x0F,
        adaptation_field=adaptation_field,
        payload=payload,
        discontinuity_indicator=discontinuity_indicator,
        pcr=pcr,
        opcr=opcr,
        elementary_stream_priority_indicator=elementary_stream_priority_indicator,
        splice_countdown=splice_countdown,
        transport_private_data=transport_private_data,
        adaptation_field_extension=adaptation_field_extension,
    )


class TransportStreamParser:
    """Incrementally parse live or stored MPEG-2 TS input.

    Chunks may end at any byte. Completed packets are released immediately. In
    recovery mode, corrupt bytes are discarded until a structurally valid sync
    candidate is found; the amount discarded remains observable.
    """

    def __init__(self, *, recover: bool = False) -> None:
        self.recover = recover
        self._buffer = bytearray()
        self._offset = 0
        self._discarded_bytes = 0

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    @property
    def stream_offset(self) -> int:
        return self._offset

    @property
    def discarded_bytes(self) -> int:
        return self._discarded_bytes

    def feed(self, data: bytes | bytearray | memoryview) -> list[TransportPacket]:
        """Consume a chunk and return all newly completed TS packets."""
        self._buffer.extend(data)
        packets: list[TransportPacket] = []
        cursor = 0
        discarded = 0
        while len(self._buffer) - cursor >= TS_PACKET_SIZE:
            if self._buffer[cursor] != TS_SYNC_BYTE:
                if not self.recover:
                    raise DecodeError(
                        f"invalid MPEG-2 TS sync byte 0x{self._buffer[cursor]:02X} "
                        f"at stream offset {self._offset + cursor}"
                    )
                index = self._buffer.find(TS_SYNC_BYTE, cursor + 1)
                if index < 0:
                    next_cursor = len(self._buffer) - (TS_PACKET_SIZE - 1)
                    discarded += next_cursor - cursor
                    cursor = next_cursor
                    break
                discarded += index - cursor
                cursor = index
                if len(self._buffer) - cursor < TS_PACKET_SIZE:
                    break
            raw = bytes(self._buffer[cursor : cursor + TS_PACKET_SIZE])
            try:
                packet = parse_transport_packet(raw, offset=self._offset + cursor)
            except DecodeError:
                if not self.recover:
                    raise
                cursor += 1
                discarded += 1
                continue
            cursor += TS_PACKET_SIZE
            packets.append(packet)
        if cursor:
            del self._buffer[:cursor]
            self._offset += cursor
            self._discarded_bytes += discarded
        return packets

    def finish(self) -> list[TransportPacket]:
        """Signal end of input and reject a partial trailing packet."""
        packets = self.feed(b"")
        if self._buffer:
            raise TruncatedData(
                f"MPEG-2 TS ended with {len(self._buffer)} incomplete byte(s) "
                f"at offset {self._offset}"
            )
        return packets

def iter_transport_stream(
    stream: BinaryIO,
    *,
    chunk_size: int = 64 * 1024,
    parser: TransportStreamParser | None = None,
) -> Iterator[TransportPacket]:
    """Yield TS packets incrementally from a binary file, pipe, or socket file."""
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")
    active = parser or TransportStreamParser()
    while chunk := stream.read(chunk_size):
        yield from active.feed(chunk)
    yield from active.finish()
