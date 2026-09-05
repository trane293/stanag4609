"""MISB ST 1602.2 Composite Imaging Local Set codec."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from stanag4609.errors import DecodeError
from stanag4609.klv.ber import encode_ber_length, encode_ber_oid
from stanag4609.klv.local_set import parse_local_set
from stanag4609.klv.model import LocalSet

COMPOSITE_IMAGING_LOCAL_SET_KEY = bytes.fromhex(
    "06 0E 2B 34 02 0B 01 01 0E 01 03 03 02 00 00 00"
)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_UNSIGNED_TAGS = frozenset({2, 3, 4, 5, 6, 9, 10, 13, 14, 17, 18})
_SIGNED_TAGS = frozenset({7, 8, 11, 12, 15, 16})
_DIMENSION_TAGS = frozenset({3, 4, 5, 6, 9, 10, 13, 14})


@dataclass(frozen=True, slots=True)
class RawCompositeValue:
    data: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes):
            raise TypeError("RawCompositeValue data must be bytes")


@dataclass(frozen=True, slots=True)
class CompositeImagingLocalSet:
    """One source/sub-image mapping embedded in an ST 1607 segment."""

    document_version: int
    sub_image_rows: int
    sub_image_columns: int
    sub_image_position_x: int
    sub_image_position_y: int
    z_order: int
    timestamp: datetime | None = None
    source_image_rows: int | None = None
    source_image_columns: int | None = None
    source_aoi_rows: int | None = None
    source_aoi_columns: int | None = None
    source_aoi_position_x: int | None = None
    source_aoi_position_y: int | None = None
    active_rows: int | None = None
    active_columns: int | None = None
    active_offset_x: int | None = None
    active_offset_y: int | None = None
    transparency: int = 0
    extensions: Mapping[int, RawCompositeValue] = field(default_factory=dict)
    local_set: LocalSet | None = field(default=None, compare=False, repr=False)

    @property
    def sub_image_rectangle(self) -> tuple[int, int, int, int]:
        return (
            self.sub_image_position_x,
            self.sub_image_position_y,
            self.sub_image_columns,
            self.sub_image_rows,
        )

    @property
    def active_rectangle(self) -> tuple[int, int, int, int] | None:
        if self.active_rows is None or self.active_columns is None:
            return None
        return (
            self.sub_image_position_x + (self.active_offset_x or 0),
            self.sub_image_position_y + (self.active_offset_y or 0),
            self.active_columns,
            self.active_rows,
        )


_ATTRIBUTES = {
    2: "document_version",
    3: "source_image_rows",
    4: "source_image_columns",
    5: "source_aoi_rows",
    6: "source_aoi_columns",
    7: "source_aoi_position_x",
    8: "source_aoi_position_y",
    9: "sub_image_rows",
    10: "sub_image_columns",
    11: "sub_image_position_x",
    12: "sub_image_position_y",
    13: "active_rows",
    14: "active_columns",
    15: "active_offset_x",
    16: "active_offset_y",
    17: "transparency",
    18: "z_order",
}


def _decode_integer(data: bytes, *, signed: bool, tag: int) -> int:
    if not data:
        raise DecodeError(f"ST 1602 Item {tag} is empty")
    if len(data) > 1:
        if not signed and data[0] == 0:
            raise DecodeError(f"ST 1602 Item {tag} must use a minimal unsigned integer")
        if signed and (
            (data[0] == 0 and not data[1] & 0x80)
            or (data[0] == 0xFF and data[1] & 0x80)
        ):
            raise DecodeError(f"ST 1602 Item {tag} must use a minimal signed integer")
    return int.from_bytes(data, "big", signed=signed)


def _encode_integer(value: int, *, signed: bool, tag: int) -> bytes:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"ST 1602 Item {tag} must be an integer")
    if not signed and value < 0:
        raise ValueError(f"ST 1602 Item {tag} must be non-negative")
    length = 1
    if signed:
        while not -(1 << (8 * length - 1)) <= value < (1 << (8 * length - 1)):
            length += 1
    else:
        length = max(1, (value.bit_length() + 7) // 8)
    return value.to_bytes(length, "big", signed=signed)


def _decode_timestamp(data: bytes) -> datetime:
    if len(data) != 8:
        raise DecodeError("ST 1602 Precision Time Stamp must contain eight bytes")
    return _EPOCH + timedelta(microseconds=int.from_bytes(data, "big"))


def _encode_timestamp(value: datetime) -> bytes:
    if not isinstance(value, datetime):
        raise TypeError("ST 1602 timestamp must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("ST 1602 timestamp must be timezone-aware")
    microseconds = int((value.astimezone(timezone.utc) - _EPOCH) / timedelta(microseconds=1))
    if not 0 <= microseconds < 1 << 64:
        raise ValueError("ST 1602 timestamp is outside the uint64 range")
    return microseconds.to_bytes(8, "big")


def _validate(value: CompositeImagingLocalSet, *, error_type: type[Exception]) -> None:
    for name, item in (("Transparency", value.transparency), ("Z-Order", value.z_order)):
        if isinstance(item, bool) or not isinstance(item, int):
            raise TypeError(f"ST 1602 {name} must be an integer")
    if not 0 <= value.transparency <= 255:
        raise error_type("ST 1602 Transparency must be between 0 and 255")
    if not 1 <= value.z_order <= 255:
        raise error_type("ST 1602 Z-Order must be between 1 and 255")
    integer_values = {
        tag: getattr(value, attribute) for tag, attribute in _ATTRIBUTES.items()
    }
    for tag, item in integer_values.items():
        if item is None:
            continue
        if isinstance(item, bool) or not isinstance(item, int):
            raise TypeError(f"ST 1602 Item {tag} must be an integer")
        if tag in _UNSIGNED_TAGS and item < 0:
            raise error_type(f"ST 1602 Item {tag} must be non-negative")
        if tag in _DIMENSION_TAGS and item < 1:
            raise error_type(f"ST 1602 dimension Item {tag} must be positive")
    if value.document_version < 1:
        raise error_type("ST 1602 Document Version must be positive")
    active_dimensions = (value.active_rows, value.active_columns)
    if (active_dimensions[0] is None) != (active_dimensions[1] is None):
        raise error_type("ST 1602 Active Sub-Image rows and columns must occur together")
    for tag, offset in ((15, value.active_offset_x), (16, value.active_offset_y)):
        if offset is not None and offset <= 0:
            raise error_type(f"ST 1602 active offset Item {tag} must be greater than zero")


def decode_composite_imaging_local_set(data: bytes) -> CompositeImagingLocalSet:
    """Decode an embedded ST 1602.2 Composite Imaging Local Set value."""
    if not isinstance(data, bytes):
        raise TypeError("ST 1602 data must be bytes")
    local_set = parse_local_set(data)
    seen: set[int] = set()
    decoded: dict[int, int | datetime] = {}
    extensions: dict[int, RawCompositeValue] = {}
    for item in local_set.items:
        if item.tag in seen:
            raise DecodeError(f"duplicate ST 1602 tag {item.tag}")
        seen.add(item.tag)
        if item.tag == 1:
            decoded[1] = _decode_timestamp(item.value)
        elif item.tag in _UNSIGNED_TAGS | _SIGNED_TAGS:
            if item.tag in {17, 18} and len(item.value) != 1:
                raise DecodeError(f"ST 1602 Item {item.tag} must contain one byte")
            decoded[item.tag] = _decode_integer(
                item.value,
                signed=item.tag in _SIGNED_TAGS,
                tag=item.tag,
            )
        else:
            extensions[item.tag] = RawCompositeValue(item.value)
    required = {2, 9, 10, 11, 12, 18}
    if not required.issubset(seen):
        missing = ", ".join(str(tag) for tag in sorted(required - seen))
        raise DecodeError(f"ST 1602 is missing mandatory Item(s): {missing}")
    value = CompositeImagingLocalSet(
        document_version=decoded[2],  # type: ignore[arg-type]
        sub_image_rows=decoded[9],  # type: ignore[arg-type]
        sub_image_columns=decoded[10],  # type: ignore[arg-type]
        sub_image_position_x=decoded[11],  # type: ignore[arg-type]
        sub_image_position_y=decoded[12],  # type: ignore[arg-type]
        z_order=decoded[18],  # type: ignore[arg-type]
        timestamp=decoded.get(1),  # type: ignore[arg-type]
        source_image_rows=decoded.get(3),  # type: ignore[arg-type]
        source_image_columns=decoded.get(4),  # type: ignore[arg-type]
        source_aoi_rows=decoded.get(5),  # type: ignore[arg-type]
        source_aoi_columns=decoded.get(6),  # type: ignore[arg-type]
        source_aoi_position_x=decoded.get(7),  # type: ignore[arg-type]
        source_aoi_position_y=decoded.get(8),  # type: ignore[arg-type]
        active_rows=decoded.get(13),  # type: ignore[arg-type]
        active_columns=decoded.get(14),  # type: ignore[arg-type]
        active_offset_x=decoded.get(15),  # type: ignore[arg-type]
        active_offset_y=decoded.get(16),  # type: ignore[arg-type]
        transparency=decoded.get(17, 0),  # type: ignore[arg-type]
        extensions=extensions,
        local_set=local_set,
    )
    _validate(value, error_type=DecodeError)
    return value


def _item(tag: int, data: bytes) -> bytes:
    return encode_ber_oid(tag) + encode_ber_length(len(data)) + data


def encode_composite_imaging_local_set(
    value: CompositeImagingLocalSet,
    *,
    preserve: bool = False,
) -> bytes:
    """Encode an embedded ST 1602.2 Composite Imaging Local Set value."""
    if not isinstance(value, CompositeImagingLocalSet):
        raise TypeError("value must be a CompositeImagingLocalSet")
    if not isinstance(preserve, bool):
        raise TypeError("preserve must be a boolean")
    if preserve and value.local_set is not None:
        return value.local_set.raw
    _validate(value, error_type=ValueError)
    encoded: list[bytes] = []
    if value.timestamp is not None:
        encoded.append(_item(1, _encode_timestamp(value.timestamp)))
    for tag, attribute in _ATTRIBUTES.items():
        item_value = getattr(value, attribute)
        if item_value is None or (tag == 17 and item_value == 0):
            continue
        encoded.append(
            _item(
                tag,
                _encode_integer(item_value, signed=tag in _SIGNED_TAGS, tag=tag),
            )
        )
    for tag in sorted(value.extensions):
        if isinstance(tag, bool) or not isinstance(tag, int):
            raise TypeError("ST 1602 extension tags must be integers")
        if tag <= 18:
            raise ValueError("ST 1602 extension tags must be greater than 18")
        extension = value.extensions[tag]
        if not isinstance(extension, RawCompositeValue):
            raise TypeError(f"ST 1602 extension tag {tag} requires RawCompositeValue")
        encoded.append(_item(tag, extension.data))
    result = b"".join(encoded)
    decode_composite_imaging_local_set(result)
    return result
