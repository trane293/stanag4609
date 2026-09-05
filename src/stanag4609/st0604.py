"""MISB ST 0604.6 timestamps embedded in compressed motion imagery."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from stanag4609.errors import DecodeError, TruncatedData

MISP_MICROSECOND_IDENTIFIER = b"MISPmicrosectime"
MISP_HEVC_MICROSECOND_UUID = bytes.fromhex("a8687dd4d7593758a5cef0338b6545f1")
MISP_HEVC_NANOSECOND_UUID = bytes.fromhex("cf848278ee23306c9265e8fef22fb8b8")
ST0604_TIMESTAMP_PAYLOAD_SIZE = 28

_MPEG_VIDEO_STREAM_TYPES = frozenset({0x01, 0x02})
_AVC_STREAM_TYPE = 0x1B
_HEVC_STREAM_TYPE = 0x24
_SUPPORTED_STREAM_TYPES = _MPEG_VIDEO_STREAM_TYPES | {
    _AVC_STREAM_TYPE,
    _HEVC_STREAM_TYPE,
}
SUPPORTED_ST0604_STREAM_TYPES = _SUPPORTED_STREAM_TYPES


class TimestampResolution(str, Enum):
    """Resolution represented by an embedded ST 0604 timestamp."""

    MICROSECONDS = "microseconds"
    NANOSECONDS = "nanoseconds"


@dataclass(frozen=True, slots=True)
class TimeStatus:
    """Decoded MISB ST 0603.5 Time Status byte."""

    locked: bool
    discontinuity: bool
    reverse: bool
    raw: int


@dataclass(frozen=True, slots=True)
class EmbeddedVideoTimestamp:
    """One ST 0604 timestamp recovered from a compressed video elementary stream."""

    value: int
    resolution: TimestampResolution
    time_status: TimeStatus
    stream_type: int


@dataclass(frozen=True, slots=True)
class TimestampedVideoAccessUnit:
    """One recognized video access unit and its associated ST 0604 timestamps."""

    index: int
    timestamps: tuple[EmbeddedVideoTimestamp, ...]


def decode_time_status(value: int) -> TimeStatus:
    """Decode the ST 0603.5 status byte and enforce its reserved low bits."""

    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFF:
        raise ValueError("time status must be an unsigned 8-bit integer")
    if value & 0x1F != 0x1F:
        raise DecodeError("ST 0603 Time Status reserved bits 4-0 must all be one")
    discontinuity = bool(value & 0x40)
    return TimeStatus(
        locked=not bool(value & 0x80),
        discontinuity=discontinuity,
        reverse=discontinuity and bool(value & 0x20),
        raw=value,
    )


def encode_time_status(
    *,
    locked: bool = True,
    discontinuity: bool = False,
    reverse: bool = False,
) -> int:
    """Encode an ST 0603.5 status byte with the required reserved-bit pattern."""

    for value, name in (
        (locked, "locked"),
        (discontinuity, "discontinuity"),
        (reverse, "reverse"),
    ):
        if not isinstance(value, bool):
            raise TypeError(f"{name} must be bool")
    if reverse and not discontinuity:
        raise ValueError("reverse requires discontinuity=True")
    return (
        (0 if locked else 0x80)
        | (0x40 if discontinuity else 0)
        | (0x20 if reverse else 0)
        | 0x1F
    )


def _decode_modified_timestamp(data: bytes) -> int:
    if len(data) < 11:
        raise TruncatedData("modified ST 0604 timestamp needs 11 bytes")
    if data[2] != 0xFF or data[5] != 0xFF or data[8] != 0xFF:
        raise DecodeError(
            "modified ST 0604 timestamp requires 0xFF emulation-prevention "
            "bytes after every two timestamp bytes"
        )
    raw = data[0:2] + data[3:5] + data[6:8] + data[9:11]
    return int.from_bytes(raw, "big")


def _encode_modified_timestamp(value: int) -> bytes:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 1 << 64:
        raise ValueError("timestamp must be an unsigned 64-bit integer")
    raw = value.to_bytes(8, "big")
    return raw[0:2] + b"\xff" + raw[2:4] + b"\xff" + raw[4:6] + b"\xff" + raw[6:8]


def decode_timestamp_payload(data: bytes, *, stream_type: int) -> EmbeddedVideoTimestamp:
    """Decode the 28-byte ST 0604 identifier, status, and modified timestamp."""

    if not isinstance(data, bytes):
        raise TypeError("timestamp payload must be bytes")
    if stream_type not in _SUPPORTED_STREAM_TYPES:
        raise ValueError(f"unsupported video stream type 0x{stream_type:02X}")
    if len(data) < ST0604_TIMESTAMP_PAYLOAD_SIZE:
        raise TruncatedData(
            f"ST 0604 timestamp payload needs {ST0604_TIMESTAMP_PAYLOAD_SIZE} bytes, "
            f"got {len(data)}"
        )
    identifier = data[:16]
    if stream_type in _MPEG_VIDEO_STREAM_TYPES | {_AVC_STREAM_TYPE}:
        expected = MISP_MICROSECOND_IDENTIFIER
        resolution = TimestampResolution.MICROSECONDS
    elif identifier == MISP_HEVC_MICROSECOND_UUID:
        expected = MISP_HEVC_MICROSECOND_UUID
        resolution = TimestampResolution.MICROSECONDS
    elif identifier == MISP_HEVC_NANOSECOND_UUID:
        expected = MISP_HEVC_NANOSECOND_UUID
        resolution = TimestampResolution.NANOSECONDS
    else:
        raise DecodeError("unrecognized ST 0604 HEVC timestamp UUID")
    if identifier != expected:
        raise DecodeError("invalid ST 0604 Precision Time Stamp Identifier")
    return EmbeddedVideoTimestamp(
        _decode_modified_timestamp(data[17:28]),
        resolution,
        decode_time_status(data[16]),
        stream_type,
    )


def encode_timestamp_payload(
    value: int,
    *,
    stream_type: int,
    time_status: int = 0x1F,
    resolution: TimestampResolution = TimestampResolution.MICROSECONDS,
) -> bytes:
    """Encode the ST 0604 timestamp payload used by MPEG user data or SEI."""

    if stream_type not in _SUPPORTED_STREAM_TYPES:
        raise ValueError(f"unsupported video stream type 0x{stream_type:02X}")
    if not isinstance(resolution, TimestampResolution):
        raise TypeError("resolution must be a TimestampResolution")
    decode_time_status(time_status)
    if resolution is TimestampResolution.NANOSECONDS:
        if stream_type != _HEVC_STREAM_TYPE:
            raise ValueError("Nano Precision Time Stamp is supported only for H.265/HEVC")
        identifier = MISP_HEVC_NANOSECOND_UUID
    elif stream_type == _HEVC_STREAM_TYPE:
        identifier = MISP_HEVC_MICROSECOND_UUID
    else:
        identifier = MISP_MICROSECOND_IDENTIFIER
    return identifier + bytes((time_status,)) + _encode_modified_timestamp(value)


def _encode_sei_message(payload: bytes) -> bytes:
    payload_type = b"\x05"
    size = len(payload)
    payload_size = bytearray()
    while size >= 0xFF:
        payload_size.append(0xFF)
        size -= 0xFF
    payload_size.append(size)
    return payload_type + bytes(payload_size) + payload + b"\x80"


def _escape_rbsp(data: bytes) -> bytes:
    output = bytearray()
    zero_count = 0
    for value in data:
        if zero_count >= 2 and value <= 3:
            output.append(3)
            zero_count = 0
        output.append(value)
        zero_count = zero_count + 1 if value == 0 else 0
    return bytes(output)


def encode_h262_timestamp_user_data(value: int, *, time_status: int = 0x1F) -> bytes:
    """Encode an H.262/MPEG-2 ``user_data_start_code`` timestamp unit."""

    return b"\x00\x00\x01\xb2" + encode_timestamp_payload(
        value,
        stream_type=0x02,
        time_status=time_status,
    )


def encode_avc_timestamp_sei(value: int, *, time_status: int = 0x1F) -> bytes:
    """Encode an Annex-B AVC ``user_data_unregistered`` SEI NAL unit."""

    payload = encode_timestamp_payload(value, stream_type=_AVC_STREAM_TYPE, time_status=time_status)
    return b"\x00\x00\x01\x06" + _escape_rbsp(_encode_sei_message(payload))


def encode_hevc_timestamp_sei(
    value: int,
    *,
    time_status: int = 0x1F,
    resolution: TimestampResolution = TimestampResolution.MICROSECONDS,
) -> bytes:
    """Encode an Annex-B HEVC prefix ``user_data_unregistered`` SEI NAL unit."""

    payload = encode_timestamp_payload(
        value,
        stream_type=_HEVC_STREAM_TYPE,
        time_status=time_status,
        resolution=resolution,
    )
    return b"\x00\x00\x01\x4e\x01" + _escape_rbsp(_encode_sei_message(payload))


def _unescape_ebsp(data: bytes) -> bytes:
    output = bytearray()
    cursor = 0
    while cursor < len(data):
        if (
            cursor + 3 < len(data)
            and data[cursor] == 0
            and data[cursor + 1] == 0
            and data[cursor + 2] == 3
            and data[cursor + 3] <= 3
        ):
            output.extend(b"\x00\x00")
            cursor += 3
        else:
            output.append(data[cursor])
            cursor += 1
    return bytes(output)


def _sei_payloads(rbsp: bytes) -> tuple[bytes, ...]:
    payloads: list[bytes] = []
    cursor = 0
    while cursor < len(rbsp):
        if rbsp[cursor] == 0x80 and all(value == 0 for value in rbsp[cursor + 1 :]):
            break
        payload_type = 0
        while cursor < len(rbsp) and rbsp[cursor] == 0xFF:
            payload_type += 0xFF
            cursor += 1
        if cursor >= len(rbsp):
            raise TruncatedData("SEI message ends inside payload_type")
        payload_type += rbsp[cursor]
        cursor += 1
        payload_size = 0
        while cursor < len(rbsp) and rbsp[cursor] == 0xFF:
            payload_size += 0xFF
            cursor += 1
        if cursor >= len(rbsp):
            raise TruncatedData("SEI message ends inside payload_size")
        payload_size += rbsp[cursor]
        cursor += 1
        if len(rbsp) - cursor < payload_size:
            raise TruncatedData("SEI message ends inside its declared payload")
        payload = rbsp[cursor : cursor + payload_size]
        cursor += payload_size
        if payload_type == 5:
            payloads.append(payload)
    return tuple(payloads)


def _timestamps_from_unit(unit: bytes, stream_type: int) -> tuple[EmbeddedVideoTimestamp, ...]:
    if not unit:
        return ()
    candidates: tuple[bytes, ...]
    if stream_type in _MPEG_VIDEO_STREAM_TYPES:
        if unit[0] != 0xB2:
            return ()
        offset = unit[1:].find(MISP_MICROSECOND_IDENTIFIER)
        if offset < 0:
            return ()
        candidates = (unit[1 + offset :],)
    elif stream_type == _AVC_STREAM_TYPE:
        if unit[0] & 0x1F != 6:
            return ()
        candidates = _sei_payloads(_unescape_ebsp(unit[1:]))
    else:
        if len(unit) < 2 or (unit[0] >> 1) & 0x3F not in {39, 40}:
            return ()
        candidates = _sei_payloads(_unescape_ebsp(unit[2:]))
    timestamps: list[EmbeddedVideoTimestamp] = []
    identifiers = {
        MISP_MICROSECOND_IDENTIFIER,
        MISP_HEVC_MICROSECOND_UUID,
        MISP_HEVC_NANOSECOND_UUID,
    }
    for candidate in candidates:
        if len(candidate) >= 16 and candidate[:16] in identifiers:
            timestamps.append(decode_timestamp_payload(candidate, stream_type=stream_type))
    return tuple(timestamps)


def _starts_access_unit(unit: bytes, stream_type: int) -> bool:
    if not unit:
        return False
    if stream_type in _MPEG_VIDEO_STREAM_TYPES:
        return unit[0] == 0x00
    if stream_type == _AVC_STREAM_TYPE:
        nal_type = unit[0] & 0x1F
        if not 1 <= nal_type <= 5:
            return False
        rbsp = _unescape_ebsp(unit[1:])
        return bool(rbsp and rbsp[0] & 0x80)
    if len(unit) < 3 or (unit[0] >> 1) & 0x3F > 31:
        return False
    rbsp = _unescape_ebsp(unit[2:])
    return bool(rbsp and rbsp[0] & 0x80)


def _start_codes(data: bytes | bytearray) -> tuple[tuple[int, int], ...]:
    starts: list[tuple[int, int]] = []
    cursor = 0
    while cursor + 3 <= len(data):
        if cursor + 4 <= len(data) and data[cursor : cursor + 4] == b"\x00\x00\x00\x01":
            starts.append((cursor, 4))
            cursor += 4
        elif data[cursor : cursor + 3] == b"\x00\x00\x01":
            starts.append((cursor, 3))
            cursor += 3
        else:
            cursor += 1
    return tuple(starts)


class _AnnexBUnitStream:
    def __init__(self, stream_type: int, max_unit_size: int) -> None:
        if stream_type not in _SUPPORTED_STREAM_TYPES:
            raise ValueError(f"unsupported video stream type 0x{stream_type:02X}")
        if (
            isinstance(max_unit_size, bool)
            or not isinstance(max_unit_size, int)
            or max_unit_size < ST0604_TIMESTAMP_PAYLOAD_SIZE
        ):
            raise ValueError(
                f"max_unit_size must be at least {ST0604_TIMESTAMP_PAYLOAD_SIZE}"
            )
        self.stream_type = stream_type
        self.max_unit_size = max_unit_size
        self._buffer = bytearray()
        self._synchronized = False
        self._finished = False

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, data: bytes | bytearray | memoryview) -> tuple[bytes, ...]:
        if self._finished:
            raise RuntimeError("cannot feed a finished video timestamp parser")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("video data must be bytes-like")
        self._buffer.extend(data)
        starts = _start_codes(self._buffer)
        if not self._synchronized:
            if not starts:
                if len(self._buffer) > 3:
                    del self._buffer[:-3]
                return ()
            first_start = starts[0][0]
            if first_start:
                del self._buffer[:first_start]
            self._synchronized = True
            starts = _start_codes(self._buffer)
        if len(starts) < 2:
            if len(self._buffer) > self.max_unit_size + 4:
                raise DecodeError(
                    f"video elementary-stream unit exceeds {self.max_unit_size} bytes"
                )
            return ()
        units: list[bytes] = []
        for index, (start, prefix_length) in enumerate(starts[:-1]):
            end = starts[index + 1][0]
            if end - start - prefix_length > self.max_unit_size:
                raise DecodeError(
                    f"video elementary-stream unit exceeds {self.max_unit_size} bytes"
                )
            units.append(bytes(self._buffer[start + prefix_length : end]))
        del self._buffer[: starts[-1][0]]
        return tuple(units)

    def finish(self) -> tuple[bytes, ...]:
        if self._finished:
            return ()
        self._finished = True
        if not self._synchronized or not self._buffer:
            self._buffer.clear()
            return ()
        starts = _start_codes(self._buffer)
        if not starts:
            self._buffer.clear()
            return ()
        start, prefix_length = starts[0]
        unit = bytes(self._buffer[start + prefix_length :])
        self._buffer.clear()
        if len(unit) > self.max_unit_size:
            raise DecodeError(
                f"video elementary-stream unit exceeds {self.max_unit_size} bytes"
            )
        return (unit,)

    def reset(self) -> None:
        self._buffer.clear()
        self._synchronized = False
        self._finished = False


class VideoTimestampStreamParser:
    """Incrementally extract ST 0604 timestamps from Annex-B elementary-stream bytes."""

    def __init__(self, stream_type: int, *, max_unit_size: int = 4 * 1024 * 1024) -> None:
        self.stream_type = stream_type
        self.max_unit_size = max_unit_size
        self._units = _AnnexBUnitStream(stream_type, max_unit_size)
        self._access_units = 0

    @property
    def buffered_bytes(self) -> int:
        return self._units.buffered_bytes

    @property
    def access_units(self) -> int:
        """Return recognized MPEG pictures or first AVC/HEVC VCL slices."""

        return self._access_units

    def _process_units(
        self, units: tuple[bytes, ...]
    ) -> tuple[EmbeddedVideoTimestamp, ...]:
        timestamps: list[EmbeddedVideoTimestamp] = []
        for unit in units:
            if _starts_access_unit(unit, self.stream_type):
                self._access_units += 1
            timestamps.extend(_timestamps_from_unit(unit, self.stream_type))
        return tuple(timestamps)

    def feed(self, data: bytes | bytearray | memoryview) -> tuple[EmbeddedVideoTimestamp, ...]:
        """Consume arbitrary chunks and emit timestamps from every completed unit."""

        return self._process_units(self._units.feed(data))

    def finish(self) -> tuple[EmbeddedVideoTimestamp, ...]:
        """Parse the final bounded unit and finish this elementary stream."""

        return self._process_units(self._units.finish())

    def reset(self) -> None:
        """Discard a partial elementary-stream unit at a session boundary."""

        self._units.reset()
        self._access_units = 0


class VideoTimestampedAccessUnitParser:
    """Associate ST 0604 timestamp messages with compressed video access units.

    The association follows the syntax placement used by ST 0604.6: H.262 user
    data attaches to the current picture, AVC user-data SEI attaches to the
    following primary coded picture, and HEVC prefix/suffix SEI attaches to the
    following/current picture respectively. Access units without timestamps are
    emitted too, allowing receivers to prove per-frame coverage rather than
    comparing unrelated totals.
    """

    def __init__(self, stream_type: int, *, max_unit_size: int = 4 * 1024 * 1024) -> None:
        self.stream_type = stream_type
        self.max_unit_size = max_unit_size
        self._units = _AnnexBUnitStream(stream_type, max_unit_size)
        self._access_units = 0
        self._current_index: int | None = None
        self._current_timestamps: list[EmbeddedVideoTimestamp] = []
        self._current_picture_data_started = False
        self._pending_timestamps: list[EmbeddedVideoTimestamp] = []
        self._unassociated_timestamps: list[EmbeddedVideoTimestamp] = []

    @property
    def buffered_bytes(self) -> int:
        return self._units.buffered_bytes

    @property
    def access_units(self) -> int:
        """Return the number of recognized picture/access-unit starts."""

        return self._access_units

    @property
    def unassociated_timestamps(self) -> tuple[EmbeddedVideoTimestamp, ...]:
        """Return timestamps whose required current/following frame never appeared."""

        return tuple(self._unassociated_timestamps)

    def _close_current(self) -> tuple[TimestampedVideoAccessUnit, ...]:
        if self._current_index is None:
            return ()
        access_unit = TimestampedVideoAccessUnit(
            self._current_index,
            tuple(self._current_timestamps),
        )
        self._current_index = None
        self._current_timestamps.clear()
        self._current_picture_data_started = False
        return (access_unit,)

    def _open_access_unit(self) -> tuple[TimestampedVideoAccessUnit, ...]:
        closed = self._close_current()
        self._current_index = self._access_units
        self._access_units += 1
        self._current_timestamps.extend(self._pending_timestamps)
        self._pending_timestamps.clear()
        return closed

    def _process_unit(self, unit: bytes) -> tuple[TimestampedVideoAccessUnit, ...]:
        timestamps = _timestamps_from_unit(unit, self.stream_type)
        starts_access_unit = _starts_access_unit(unit, self.stream_type)

        if self.stream_type in _MPEG_VIDEO_STREAM_TYPES:
            closed = self._open_access_unit() if starts_access_unit else ()
            if timestamps:
                if self._current_index is None or self._current_picture_data_started:
                    self._unassociated_timestamps.extend(timestamps)
                else:
                    self._current_timestamps.extend(timestamps)
            if unit and 0x01 <= unit[0] <= 0xAF:
                self._current_picture_data_started = True
            return closed

        if self.stream_type == _AVC_STREAM_TYPE:
            self._pending_timestamps.extend(timestamps)
            return self._open_access_unit() if starts_access_unit else ()

        nal_type = (unit[0] >> 1) & 0x3F if unit else -1
        if nal_type == 39:
            self._pending_timestamps.extend(timestamps)
        closed = self._open_access_unit() if starts_access_unit else ()
        if nal_type == 40 and timestamps:
            if self._current_index is None:
                self._unassociated_timestamps.extend(timestamps)
            else:
                self._current_timestamps.extend(timestamps)
        return closed

    def _process_units(
        self, units: tuple[bytes, ...]
    ) -> tuple[TimestampedVideoAccessUnit, ...]:
        access_units: list[TimestampedVideoAccessUnit] = []
        for unit in units:
            access_units.extend(self._process_unit(unit))
        return tuple(access_units)

    def feed(
        self, data: bytes | bytearray | memoryview
    ) -> tuple[TimestampedVideoAccessUnit, ...]:
        """Consume chunks and emit access units once the next boundary is known."""

        return self._process_units(self._units.feed(data))

    def finish(self) -> tuple[TimestampedVideoAccessUnit, ...]:
        """Finalize the last unit and access unit, retaining orphan timestamps."""

        access_units = list(self._process_units(self._units.finish()))
        access_units.extend(self._close_current())
        self._unassociated_timestamps.extend(self._pending_timestamps)
        self._pending_timestamps.clear()
        return tuple(access_units)

    def reset(self) -> None:
        """Reset all elementary-stream and association state."""

        self._units.reset()
        self._access_units = 0
        self._current_index = None
        self._current_timestamps.clear()
        self._current_picture_data_started = False
        self._pending_timestamps.clear()
        self._unassociated_timestamps.clear()
