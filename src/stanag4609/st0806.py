"""MISB ST 0806.4 Remote Video Terminal Local Set codecs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from fractions import Fraction
from typing import Any, Literal

from stanag4609.errors import ChecksumError, DecodeError
from stanag4609.klv.ber import encode_ber_length, encode_ber_oid
from stanag4609.klv.checksum import mpeg2_crc32
from stanag4609.klv.local_set import parse_local_set
from stanag4609.klv.model import KLVPacket, LocalSet, LocalSetItem
from stanag4609.klv.stream import KLVStreamParser

RVT_LOCAL_SET_KEY = bytes.fromhex("06 0E 2B 34 02 0B 01 01 0E 01 03 01 02 00 00 00")
POI_LOCAL_SET_KEY = bytes.fromhex("06 0E 2B 34 02 0B 01 01 0E 01 03 01 0C 00 00 00")
AOI_LOCAL_SET_KEY = bytes.fromhex("06 0E 2B 34 02 0B 01 01 0E 01 03 01 0D 00 00 00")
USER_DEFINED_LOCAL_SET_KEY = bytes.fromhex("06 0E 2B 34 02 0B 01 01 0E 01 03 01 0F 00 00 00")
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _timestamp_microseconds(value: int | datetime, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, datetime)):
        raise TypeError(f"{name} requires integer microseconds or an aware datetime")
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} datetime must be timezone-aware")
        delta = value.astimezone(timezone.utc) - _UNIX_EPOCH
        microseconds = (
            delta.days * 86_400_000_000
            + delta.seconds * 1_000_000
            + delta.microseconds
        )
    else:
        microseconds = value
    if not 0 <= microseconds <= 2**64 - 1:
        raise ValueError(f"{name} must fit the ST 0806 uint64 timestamp domain")
    return microseconds


class RVTErrorValue(Enum):
    """Explicit ST 0806 geolocation error indicator."""

    ERROR = "error"


class RVTUserDataType(Enum):
    """Type selected by the two high bits of User Defined LS Item 1."""

    STRING = 0
    SIGNED_INTEGER = 1
    UNSIGNED_INTEGER = 2
    EXPERIMENTAL = 3


@dataclass(frozen=True, slots=True)
class RVTValidationContext:
    """Producer facts needed to validate contextual ST 0806 semantics."""

    metadata_birth_timestamp: int | datetime | None = None

    def __post_init__(self) -> None:
        if self.metadata_birth_timestamp is not None:
            _timestamp_microseconds(
                self.metadata_birth_timestamp,
                name="metadata_birth_timestamp",
            )


@dataclass(frozen=True, slots=True)
class RawRVTValue:
    """Explicit bytes for an extension item that ST 0806 does not define."""

    data: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes):
            raise TypeError("RawRVTValue data must be bytes")


@dataclass(frozen=True, slots=True)
class RVTField:
    tag: int
    name: str
    value: Any
    raw: bytes
    item: LocalSetItem


def _getall(fields: tuple[RVTField, ...], tag: int) -> tuple[RVTField, ...]:
    return tuple(field for field in fields if field.tag == tag)


def _get(fields: tuple[RVTField, ...], tag: int) -> RVTField | None:
    matches = _getall(fields, tag)
    if not matches:
        return None
    if len(matches) > 1:
        raise ValueError(f"tag {tag} occurs {len(matches)} times")
    return matches[0]


@dataclass(frozen=True, slots=True)
class RVTPointOfInterest:
    local_set: LocalSet
    fields: tuple[RVTField, ...]

    def getall(self, tag: int) -> tuple[RVTField, ...]:
        return _getall(self.fields, tag)

    def get(self, tag: int) -> RVTField | None:
        return _get(self.fields, tag)

    def value(self, tag: int, default: Any = None) -> Any:
        result = self.get(tag)
        return default if result is None else result.value


@dataclass(frozen=True, slots=True)
class RVTAreaOfInterest:
    local_set: LocalSet
    fields: tuple[RVTField, ...]

    def getall(self, tag: int) -> tuple[RVTField, ...]:
        return _getall(self.fields, tag)

    def get(self, tag: int) -> RVTField | None:
        return _get(self.fields, tag)

    def value(self, tag: int, default: Any = None) -> Any:
        result = self.get(tag)
        return default if result is None else result.value


@dataclass(frozen=True, slots=True)
class RVTUserDefinedData:
    identifier: int
    data_type: RVTUserDataType
    value: str | int | bytes
    value_length: int | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class RVTLocalSet:
    packet: KLVPacket | None
    local_set: LocalSet
    fields: tuple[RVTField, ...]
    standalone: bool

    def getall(self, tag: int) -> tuple[RVTField, ...]:
        return _getall(self.fields, tag)

    def get(self, tag: int) -> RVTField | None:
        return _get(self.fields, tag)

    def value(self, tag: int, default: Any = None) -> Any:
        result = self.get(tag)
        return default if result is None else result.value


@dataclass(frozen=True, slots=True)
class _Definition:
    name: str
    kind: Literal[
        "uint",
        "timestamp",
        "text",
        "mgrs_grid",
        "latitude",
        "longitude",
        "altitude",
        "poi",
        "aoi",
        "user",
    ]
    length: int | None = None
    minimum: int | None = None
    maximum: int | None = None
    maximum_length: int | None = None


_RVT_DEFINITIONS = {
    1: _Definition("CRC-32", "uint", 4),
    2: _Definition("Precision Time Stamp", "timestamp", 8),
    3: _Definition("Platform True Airspeed", "uint", 2),
    4: _Definition("Platform Indicated Airspeed", "uint", 2),
    5: _Definition("Telemetry Accuracy Indicator", "uint", 1),
    6: _Definition("Frag Circle Radius", "uint", 2),
    7: _Definition("Frame Code", "uint", 4),
    8: _Definition("RVT LS Version Number", "uint", 1),
    9: _Definition("Video Data Rate", "uint", 4),
    10: _Definition("Digital Video File Format", "text", maximum_length=127),
    11: _Definition("User Defined Local Set", "user"),
    12: _Definition("Point of Interest Local Set", "poi"),
    13: _Definition("Area of Interest Local Set", "aoi"),
    14: _Definition("MGRS Zone", "uint", 1, 1, 60),
    15: _Definition("MGRS Latitude Band and Grid Square", "mgrs_grid", 3),
    16: _Definition("MGRS Easting", "uint", 3, 0, 99_999),
    17: _Definition("MGRS Northing", "uint", 3, 0, 99_999),
    18: _Definition("MGRS Zone Second Value", "uint", 1, 1, 60),
    19: _Definition("MGRS Latitude Band and Grid Square Second Value", "mgrs_grid", 3),
    20: _Definition("MGRS Easting Second Value", "uint", 3, 0, 99_999),
    21: _Definition("MGRS Northing Second Value", "uint", 3, 0, 99_999),
}

_POI_DEFINITIONS = {
    1: _Definition("POI/AOI Number", "uint", 2),
    2: _Definition("POI Latitude", "latitude", 4),
    3: _Definition("POI Longitude", "longitude", 4),
    4: _Definition("POI Altitude", "altitude", 2),
    5: _Definition("POI/AOI Type", "uint", 1, 1, 4),
    6: _Definition("POI/AOI Text", "text", maximum_length=2048),
    7: _Definition("POI Source Icon", "text", maximum_length=127),
    8: _Definition("POI/AOI Source ID", "text", maximum_length=255),
    9: _Definition("POI/AOI Label", "text", 16),
    10: _Definition("Operation ID", "text", maximum_length=127),
}

_AOI_DEFINITIONS = {
    1: _Definition("POI/AOI Number", "uint", 2),
    2: _Definition("Corner Latitude Point 1", "latitude", 4),
    3: _Definition("Corner Longitude Point 1", "longitude", 4),
    4: _Definition("Corner Latitude Point 3", "latitude", 4),
    5: _Definition("Corner Longitude Point 3", "longitude", 4),
    6: _Definition("POI/AOI Type", "uint", 1, 1, 4),
    7: _Definition("POI/AOI Text", "text", maximum_length=2048),
    8: _Definition("POI/AOI Source ID", "text", maximum_length=255),
    9: _Definition("POI/AOI Label", "text", 16),
    10: _Definition("Operation ID", "text", maximum_length=127),
}


def _round_fraction(value: Fraction) -> int:
    quotient, remainder = divmod(abs(value.numerator), value.denominator)
    if remainder * 2 >= value.denominator:
        quotient += 1
    return quotient if value >= 0 else -quotient


def _decode_mapped(
    data: bytes, minimum: int, maximum: int, *, signed: bool, error_code: int | None = None
) -> float | RVTErrorValue:
    raw = int.from_bytes(data, "big", signed=signed)
    if raw == error_code:
        return RVTErrorValue.ERROR
    raw_minimum = -(2 ** (len(data) * 8 - 1)) + 1 if signed else 0
    raw_maximum = 2 ** (len(data) * 8 - (1 if signed else 0)) - 1
    return float(
        Fraction(minimum)
        + Fraction((raw - raw_minimum) * (maximum - minimum), raw_maximum - raw_minimum)
    )


def _encode_mapped(
    value: int | float | Fraction | RVTErrorValue,
    length: int,
    minimum: int,
    maximum: int,
    *,
    signed: bool,
    error_code: int | None = None,
) -> bytes:
    if value is RVTErrorValue.ERROR:
        if error_code is None:
            raise ValueError("this ST 0806 mapped value has no error indicator")
        return error_code.to_bytes(length, "big", signed=signed)
    if isinstance(value, bool) or not isinstance(value, (int, float, Fraction)):
        raise TypeError("ST 0806 mapped value must be numeric")
    physical = Fraction(str(value)) if isinstance(value, float) else Fraction(value)
    if not minimum <= physical <= maximum:
        raise ValueError(f"ST 0806 mapped value is outside [{minimum}, {maximum}]")
    raw_minimum = -(2 ** (length * 8 - 1)) + 1 if signed else 0
    raw_maximum = 2 ** (length * 8 - (1 if signed else 0)) - 1
    scaled = Fraction(raw_minimum) + Fraction(
        (physical - minimum) * (raw_maximum - raw_minimum), maximum - minimum
    )
    return _round_fraction(scaled).to_bytes(length, "big", signed=signed)


def _decode_text(data: bytes, definition: _Definition) -> str:
    try:
        value = data.decode("ascii")
    except UnicodeDecodeError as error:
        raise DecodeError(f"ST 0806 {definition.name} is not ISO-7 text") from error
    _validate_text(value, definition)
    return value


def _validate_text(value: str, definition: _Definition) -> bytes:
    if not isinstance(value, str):
        raise TypeError(f"ST 0806 {definition.name} must be a string")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError(f"ST 0806 {definition.name} must be ISO-7 text") from error
    if definition.length is not None and len(encoded) != definition.length:
        raise ValueError(f"ST 0806 {definition.name} requires {definition.length} bytes")
    if definition.maximum_length is not None and len(encoded) > definition.maximum_length:
        raise ValueError(f"ST 0806 {definition.name} exceeds {definition.maximum_length} bytes")
    if definition.kind == "mgrs_grid" and any(
        character not in "ABCDEFGHJKLMNPQRSTUVWXYZ" for character in value
    ):
        raise ValueError(
            f"ST 0806 {definition.name} requires an uppercase MGRS latitude band "
            "and two-letter grid square, omitting I and O"
        )
    return encoded


def _decode_known(item: LocalSetItem, definition: _Definition) -> Any:
    if definition.length is not None and len(item.value) != definition.length:
        raise DecodeError(
            f"ST 0806 {definition.name} requires {definition.length} bytes; "
            f"observed {len(item.value)}"
        )
    try:
        if definition.kind == "uint":
            value: Any = int.from_bytes(item.value, "big")
            if (definition.minimum is not None and value < definition.minimum) or (
                definition.maximum is not None and value > definition.maximum
            ):
                raise DecodeError(f"ST 0806 {definition.name} is outside its range")
            return value
        if definition.kind == "timestamp":
            return _UNIX_EPOCH + timedelta(microseconds=int.from_bytes(item.value, "big"))
        if definition.kind in {"text", "mgrs_grid"}:
            return _decode_text(item.value, definition)
        if definition.kind == "latitude":
            return _decode_mapped(item.value, -90, 90, signed=True, error_code=-(2**31))
        if definition.kind == "longitude":
            return _decode_mapped(item.value, -180, 180, signed=True, error_code=-(2**31))
        if definition.kind == "altitude":
            return _decode_mapped(item.value, -900, 19_000, signed=False)
        if definition.kind == "poi":
            return decode_point_of_interest(item.value)
        if definition.kind == "aoi":
            return decode_area_of_interest(item.value)
        if definition.kind == "user":
            return decode_user_defined_data(item.value)
    except (OverflowError, ValueError) as error:
        if isinstance(error, DecodeError):
            raise
        raise DecodeError(str(error)) from error
    raise AssertionError(f"unsupported ST 0806 field kind {definition.kind}")


def _ensure_unique(local_set: LocalSet, *, repeated: frozenset[int] = frozenset()) -> None:
    observed: set[int] = set()
    for item in local_set.items:
        if item.tag in observed and item.tag not in repeated:
            raise DecodeError(f"ST 0806 tag {item.tag} occurs twice")
        observed.add(item.tag)


def _decode_subordinate(
    data: bytes,
    definitions: dict[int, _Definition],
    required: frozenset[int],
    *,
    kind: Literal["POI", "AOI"],
) -> RVTPointOfInterest | RVTAreaOfInterest:
    if not isinstance(data, bytes):
        raise TypeError(f"ST 0806 {kind} data must be bytes")
    local_set = parse_local_set(data)
    _ensure_unique(local_set)
    present = {item.tag for item in local_set.items}
    if not required <= present:
        phrase = "tags 1, 2, and 3" if kind == "POI" else "tags 1 through 6"
        raise DecodeError(f"ST 0806 {kind} requires {phrase}")
    fields = tuple(
        RVTField(
            item.tag,
            definitions[item.tag].name,
            _decode_known(item, definitions[item.tag]),
            item.value,
            item,
        )
        for item in local_set.items
        if item.tag in definitions
    )
    result_type = RVTPointOfInterest if kind == "POI" else RVTAreaOfInterest
    return result_type(local_set, fields)


def decode_point_of_interest(data: bytes) -> RVTPointOfInterest:
    """Decode an ST 0806 Point of Interest Local Set value."""
    result = _decode_subordinate(data, _POI_DEFINITIONS, frozenset({1, 2, 3}), kind="POI")
    assert isinstance(result, RVTPointOfInterest)
    return result


def decode_area_of_interest(data: bytes) -> RVTAreaOfInterest:
    """Decode an ST 0806 Area of Interest Local Set value."""
    result = _decode_subordinate(data, _AOI_DEFINITIONS, frozenset(range(1, 7)), kind="AOI")
    assert isinstance(result, RVTAreaOfInterest)
    return result


def decode_user_defined_data(data: bytes) -> RVTUserDefinedData:
    """Decode the exact two-item ST 0806 User Defined Local Set."""
    if not isinstance(data, bytes):
        raise TypeError("ST 0806 User Defined data must be bytes")
    local_set = parse_local_set(data)
    if tuple(item.tag for item in local_set.items) != (1, 2):
        raise DecodeError("ST 0806 User Defined Local Set requires exactly tags 1 and 2 in order")
    numeric_id, user_data = local_set.items
    if len(numeric_id.value) != 1:
        raise DecodeError("ST 0806 Numeric ID for Data Type requires one byte")
    header = numeric_id.value[0]
    data_type = RVTUserDataType(header >> 6)
    identifier = header & 0x3F
    if data_type is RVTUserDataType.STRING:
        try:
            value: str | int | bytes = user_data.value.decode("ascii")
        except UnicodeDecodeError as error:
            raise DecodeError("ST 0806 string User Data is not ISO-7") from error
    elif data_type is RVTUserDataType.SIGNED_INTEGER:
        if not user_data.value:
            raise DecodeError("ST 0806 integer User Data cannot be empty")
        value = int.from_bytes(user_data.value, "big", signed=True)
    elif data_type is RVTUserDataType.UNSIGNED_INTEGER:
        if not user_data.value:
            raise DecodeError("ST 0806 integer User Data cannot be empty")
        value = int.from_bytes(user_data.value, "big")
    else:
        value = user_data.value
    return RVTUserDefinedData(identifier, data_type, value, len(user_data.value))


def _encode_integer(value: int, length: int, definition: _Definition) -> bytes:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"ST 0806 {definition.name} must be an integer")
    maximum = (1 << (length * 8)) - 1
    if not 0 <= value <= maximum:
        raise ValueError(f"ST 0806 {definition.name} is outside its {length}-byte range")
    if (definition.minimum is not None and value < definition.minimum) or (
        definition.maximum is not None and value > definition.maximum
    ):
        raise ValueError(f"ST 0806 {definition.name} is outside its permitted range")
    return value.to_bytes(length, "big")


def _encode_known(tag: int, value: Any, definition: _Definition) -> bytes:
    if definition.kind == "uint":
        assert definition.length is not None
        return _encode_integer(value, definition.length, definition)
    if definition.kind == "timestamp":
        return _timestamp_microseconds(
            value,
            name="ST 0806 Precision Time Stamp",
        ).to_bytes(8, "big")
    if definition.kind in {"text", "mgrs_grid"}:
        return _validate_text(value, definition)
    if definition.kind == "latitude":
        return _encode_mapped(value, 4, -90, 90, signed=True, error_code=-(2**31))
    if definition.kind == "longitude":
        return _encode_mapped(value, 4, -180, 180, signed=True, error_code=-(2**31))
    if definition.kind == "altitude":
        return _encode_mapped(value, 2, -900, 19_000, signed=False)
    if definition.kind == "poi":
        if not isinstance(value, RVTPointOfInterest):
            raise TypeError("ST 0806 RVT tag 12 requires RVTPointOfInterest")
        return bytes(value.local_set)
    if definition.kind == "aoi":
        if not isinstance(value, RVTAreaOfInterest):
            raise TypeError("ST 0806 RVT tag 13 requires RVTAreaOfInterest")
        return bytes(value.local_set)
    if definition.kind == "user":
        if not isinstance(value, RVTUserDefinedData):
            raise TypeError("ST 0806 RVT tag 11 requires RVTUserDefinedData")
        return encode_user_defined_data(value)
    raise AssertionError(f"unsupported ST 0806 field kind {definition.kind}")


def _item(tag: int, value: bytes) -> bytes:
    return encode_ber_oid(tag) + encode_ber_length(len(value)) + value


def _validate_mapping(values: Mapping[int, Any], *, name: str) -> None:
    if not isinstance(values, Mapping):
        raise TypeError(f"ST 0806 {name} values must be a mapping")
    if any(isinstance(tag, bool) or not isinstance(tag, int) or tag < 0 for tag in values):
        raise TypeError(f"ST 0806 {name} tags must be non-negative integers")


def _encode_subordinate(
    values: Mapping[int, Any],
    definitions: dict[int, _Definition],
    required: frozenset[int],
    *,
    kind: Literal["POI", "AOI"],
) -> bytes:
    _validate_mapping(values, name=kind)
    if not required <= values.keys():
        phrase = "tags 1, 2, and 3" if kind == "POI" else "tags 1 through 6"
        raise ValueError(f"ST 0806 {kind} requires {phrase}")
    encoded = []
    for tag in sorted(values):
        value = values[tag]
        if tag in definitions:
            raw = _encode_known(tag, value, definitions[tag])
        elif isinstance(value, RawRVTValue):
            raw = value.data
        else:
            raise TypeError(f"untyped ST 0806 {kind} tag {tag} requires RawRVTValue")
        encoded.append(_item(tag, raw))
    result = b"".join(encoded)
    _decode_subordinate(result, definitions, required, kind=kind)
    return result


def encode_point_of_interest(values: Mapping[int, Any]) -> bytes:
    """Encode one ST 0806 Point of Interest Local Set value."""
    return _encode_subordinate(values, _POI_DEFINITIONS, frozenset({1, 2, 3}), kind="POI")


def encode_area_of_interest(values: Mapping[int, Any]) -> bytes:
    """Encode one ST 0806 Area of Interest Local Set value."""
    return _encode_subordinate(values, _AOI_DEFINITIONS, frozenset(range(1, 7)), kind="AOI")


def _minimum_integer_length(value: int, *, signed: bool) -> int:
    bits = (
        value.bit_length() + 1
        if signed and value >= 0
        else (~value).bit_length() + 1
        if signed
        else value.bit_length()
    )
    return max(1, (bits + 7) // 8)


def encode_user_defined_data(record: RVTUserDefinedData) -> bytes:
    """Encode the exact two-item ST 0806 User Defined Local Set."""
    if not isinstance(record, RVTUserDefinedData):
        raise TypeError("record must be an RVTUserDefinedData")
    if isinstance(record.identifier, bool) or not isinstance(record.identifier, int):
        raise TypeError("ST 0806 User Data identifier must be an integer")
    if not 0 <= record.identifier <= 63:
        raise ValueError("ST 0806 User Data identifier must be between 0 and 63")
    if not isinstance(record.data_type, RVTUserDataType):
        raise TypeError("ST 0806 User Data type must be an RVTUserDataType")
    if record.value_length is not None and (
        isinstance(record.value_length, bool)
        or not isinstance(record.value_length, int)
        or record.value_length < 0
    ):
        raise ValueError("ST 0806 User Data value_length must be non-negative")
    if record.data_type is RVTUserDataType.STRING:
        if not isinstance(record.value, str):
            raise TypeError("ST 0806 string User Data must be a string")
        definition = _Definition("string User Data", "text")
        data = _validate_text(record.value, definition)
    elif record.data_type in {
        RVTUserDataType.SIGNED_INTEGER,
        RVTUserDataType.UNSIGNED_INTEGER,
    }:
        if isinstance(record.value, bool) or not isinstance(record.value, int):
            raise TypeError("ST 0806 integer User Data must be an integer")
        signed = record.data_type is RVTUserDataType.SIGNED_INTEGER
        length = record.value_length or _minimum_integer_length(record.value, signed=signed)
        try:
            data = record.value.to_bytes(length, "big", signed=signed)
        except OverflowError as error:
            raise ValueError("ST 0806 User Data integer does not fit value_length") from error
    else:
        if not isinstance(record.value, bytes):
            raise TypeError("ST 0806 experimental User Data must be bytes")
        data = record.value
    if record.value_length is not None and len(data) != record.value_length:
        raise ValueError("ST 0806 User Data does not match value_length")
    numeric_id = record.data_type.value << 6 | record.identifier
    return _item(1, bytes((numeric_id,))) + _item(2, data)


def _parse_single_packet(data: bytes) -> KLVPacket:
    parser = KLVStreamParser(key_prefix=RVT_LOCAL_SET_KEY, max_value_length=64 * 1024 * 1024)
    packets = parser.feed(data)
    packets.extend(parser.finish())
    if len(packets) != 1:
        raise DecodeError(f"expected exactly one ST 0806 packet, observed {len(packets)}")
    return packets[0]


def _validate_metadata_birth_timestamp(
    timestamp: bytes | None,
    context: RVTValidationContext | None,
    *,
    error_type: type[Exception],
) -> None:
    if context is None or context.metadata_birth_timestamp is None:
        return
    if timestamp is None:
        raise error_type(
            "ST 0806 metadata time-of-birth validation requires a Precision Time Stamp"
        )
    expected = _timestamp_microseconds(
        context.metadata_birth_timestamp,
        name="metadata_birth_timestamp",
    )
    observed = int.from_bytes(timestamp, "big")
    if observed != expected:
        raise error_type(
            "ST 0806 Precision Time Stamp does not match producer-supplied metadata "
            "time of birth"
        )


def decode_rvt_local_set(
    data: bytes | KLVPacket,
    *,
    standalone: bool = True,
    verify_checksum: bool = True,
    context: RVTValidationContext | None = None,
) -> RVTLocalSet:
    """Decode a standalone Universal RVT packet or an embedded LS value."""
    if not isinstance(standalone, bool):
        raise TypeError("standalone must be a boolean")
    if not isinstance(verify_checksum, bool):
        raise TypeError("verify_checksum must be a boolean")
    if context is not None and not isinstance(context, RVTValidationContext):
        raise TypeError("context must be an RVTValidationContext or None")
    if standalone:
        packet = data if isinstance(data, KLVPacket) else _parse_single_packet(data)
        if packet.key != RVT_LOCAL_SET_KEY:
            raise DecodeError(f"unexpected Universal Key {packet.key.hex(' ')} for ST 0806 RVT")
        local_set = parse_local_set(packet.value)
    else:
        if not isinstance(data, bytes):
            raise TypeError("embedded ST 0806 RVT data must be bytes")
        packet = None
        local_set = parse_local_set(data)
    if not local_set.items:
        raise DecodeError("ST 0806 RVT Local Set is empty")
    _ensure_unique(local_set, repeated=frozenset({11, 12, 13}))
    timestamps = local_set.getall(2)
    checksums = local_set.getall(1)
    if timestamps and local_set.items[0].tag != 2:
        raise DecodeError("ST 0806 Precision Time Stamp must be the first item")
    if standalone:
        if len(timestamps) != 1:
            raise DecodeError("standalone ST 0806 RVT requires one Precision Time Stamp")
        if len(checksums) != 1 or local_set.items[-1].tag != 1 or len(checksums[0].value) != 4:
            raise ChecksumError("standalone ST 0806 RVT CRC must be the final four-byte item")
        assert packet is not None
        if verify_checksum and mpeg2_crc32(packet.raw) != 0:
            raise ChecksumError("ST 0806 RVT MPEG-2 CRC-32 mismatch")
    elif checksums:
        raise DecodeError("embedded ST 0806 RVT forbids checksum item 1")
    fields = tuple(
        RVTField(
            item.tag,
            _RVT_DEFINITIONS[item.tag].name,
            _decode_known(item, _RVT_DEFINITIONS[item.tag]),
            item.value,
            item,
        )
        for item in local_set.items
        if item.tag in _RVT_DEFINITIONS
    )
    _validate_metadata_birth_timestamp(
        None if not timestamps else timestamps[0].value,
        context,
        error_type=DecodeError,
    )
    return RVTLocalSet(packet, local_set, fields, standalone)


def _instances(tag: int, value: Any) -> tuple[Any, ...]:
    if tag in {11, 12, 13} and isinstance(value, (list, tuple)):
        if not value:
            raise ValueError(f"ST 0806 repeated tag {tag} requires at least one value")
        return tuple(value)
    return (value,)


def encode_rvt_local_set(
    values: Mapping[int, Any],
    *,
    standalone: bool = True,
    context: RVTValidationContext | None = None,
) -> bytes:
    """Encode an ST 0806 RVT Local Set, owning the standalone CRC item."""
    _validate_mapping(values, name="RVT")
    if not isinstance(standalone, bool):
        raise TypeError("standalone must be a boolean")
    if context is not None and not isinstance(context, RVTValidationContext):
        raise TypeError("context must be an RVTValidationContext or None")
    if 1 in values:
        if standalone:
            raise ValueError("do not provide RVT tag 1; CRC is computed automatically")
        raise ValueError("embedded ST 0806 RVT forbids checksum item 1")
    if standalone and 2 not in values:
        raise ValueError("standalone ST 0806 RVT requires tag 2 Precision Time Stamp")
    encoded_timestamp = (
        None
        if 2 not in values
        else _encode_known(2, values[2], _RVT_DEFINITIONS[2])
    )
    _validate_metadata_birth_timestamp(
        encoded_timestamp,
        context,
        error_type=ValueError,
    )
    ordered_tags = (
        [2, *sorted(tag for tag in values if tag != 2)] if 2 in values else sorted(values)
    )
    encoded_items: list[bytes] = []
    for tag in ordered_tags:
        try:
            definition = _RVT_DEFINITIONS[tag]
        except KeyError:
            definition = None
        for value in _instances(tag, values[tag]):
            if definition is not None:
                raw = _encode_known(tag, value, definition)
            elif isinstance(value, RawRVTValue):
                raw = value.data
            else:
                raise TypeError(f"untyped ST 0806 RVT tag {tag} requires RawRVTValue")
            encoded_items.append(_item(tag, raw))
    local_value = b"".join(encoded_items)
    if not standalone:
        decode_rvt_local_set(local_value, standalone=False, context=context)
        return local_value
    crc_header = b"\x01\x04"
    value_length = len(local_value) + len(crc_header) + 4
    prefix = RVT_LOCAL_SET_KEY + encode_ber_length(value_length) + local_value + crc_header
    result = prefix + mpeg2_crc32(prefix).to_bytes(4, "big")
    decode_rvt_local_set(result, context=context)
    return result
