"""MISB ST 1601.2 Geo-Registration Local Set codec."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TypeAlias
from uuid import UUID

from stanag4609.errors import DecodeError, LimitExceeded
from stanag4609.imap import IMAPB, IMAPSpecialValue
from stanag4609.klv.ber import encode_ber_length, encode_ber_oid
from stanag4609.klv.local_set import parse_local_set
from stanag4609.klv.model import LocalSet
from stanag4609.st1303 import (
    MDAP,
    MDAPAlgorithm,
    MDAPElementType,
    MDAPValue,
    decode_mdap,
    encode_mdap,
)

GEO_REGISTRATION_LOCAL_SET_KEY = bytes.fromhex(
    "06 0E 2B 34 02 0B 01 01 0E 01 03 03 01 00 00 00"
)

MappedValue: TypeAlias = int | float | IMAPSpecialValue
_PIXEL_UNCERTAINTY_BOUNDS = ((0.0, 100.0), (0.0, 100.0), (-1.0, 1.0)) * 2
_GEO_UNCERTAINTY_BOUNDS = (
    (0.0, 650.0),
    (0.0, 650.0),
    (-1.0, 1.0),
    (0.0, 1000.0),
    (-1.0, 1.0),
    (-1.0, 1.0),
)


@dataclass(frozen=True, slots=True)
class RawGeoRegistrationValue:
    data: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes):
            raise TypeError("RawGeoRegistrationValue data must be bytes")


@dataclass(frozen=True, slots=True)
class HeterogeneousIMAPArray:
    """A Natural MDAP whose row meanings use different IMAPB bounds."""

    dimensions: tuple[int, int]
    element_size: int
    row_bounds: tuple[tuple[float, float], ...]
    elements: tuple[MappedValue, ...]
    mdap: MDAP | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.dimensions, tuple) or len(self.dimensions) != 2:
            raise ValueError("heterogeneous IMAP array requires two dimensions")
        rows, columns = self.dimensions
        if rows < 1 or columns < 1:
            raise ValueError("heterogeneous IMAP dimensions must be positive")
        if len(self.row_bounds) != rows:
            raise ValueError("heterogeneous IMAP row bounds must match the first dimension")
        if len(self.elements) != rows * columns:
            raise ValueError("heterogeneous IMAP element count does not match dimensions")
        if isinstance(self.element_size, bool) or not isinstance(self.element_size, int):
            raise TypeError("heterogeneous IMAP element_size must be an integer")
        if self.element_size < 1:
            raise ValueError("heterogeneous IMAP element_size must be positive")
        for minimum, maximum in self.row_bounds:
            IMAPB(minimum, maximum, self.element_size)

    @property
    def tie_point_count(self) -> int:
        return self.dimensions[1]

    def element_at(self, row: int, tie_point: int) -> MappedValue:
        if not 0 <= row < self.dimensions[0] or not 0 <= tie_point < self.dimensions[1]:
            raise IndexError("geo-registration array index is outside its dimensions")
        return self.elements[row * self.dimensions[1] + tie_point]


@dataclass(frozen=True, slots=True)
class GeoRegistrationLocalSet:
    """Typed ST 1601.2 value embedded in a contextual parent Local Set."""

    document_version: int
    algorithm_name: str
    algorithm_version: str
    row_column: MDAP | None = None
    latitude_longitude: MDAP | None = None
    second_image_name: str | None = None
    algorithm_configuration_id: UUID | None = None
    elevation: MDAP | None = None
    pixel_uncertainty: HeterogeneousIMAPArray | None = None
    geo_uncertainty: HeterogeneousIMAPArray | None = None
    extensions: Mapping[int, RawGeoRegistrationValue] = field(default_factory=dict)
    local_set: LocalSet | None = field(default=None, compare=False, repr=False)

    @property
    def tie_point_count(self) -> int | None:
        counts = _tie_point_counts(self)
        return next(iter(counts), None)


def _decode_uint(data: bytes) -> int:
    if not data:
        raise DecodeError("ST 1601 Document Version is empty")
    if len(data) > 1 and data[0] == 0:
        raise DecodeError("ST 1601 Document Version must use a minimal unsigned integer")
    value = int.from_bytes(data, "big")
    if value < 1:
        raise DecodeError("ST 1601 Document Version must be positive")
    return value


def _encode_uint(value: int) -> bytes:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("ST 1601 Document Version must be an integer")
    if value < 1:
        raise ValueError("ST 1601 Document Version must be positive")
    return value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")


def _decode_text(data: bytes, *, name: str, max_text_length: int) -> str:
    if len(data) > max_text_length:
        raise LimitExceeded(f"ST 1601 {name} exceeds configured text limit")
    try:
        value = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DecodeError(f"ST 1601 {name} is not valid UTF-8") from error
    try:
        _validate_text(value, name=name)
    except (TypeError, ValueError) as error:
        raise DecodeError(str(error)) from error
    return value


def _validate_text(value: str, *, name: str) -> bytes:
    if not isinstance(value, str):
        raise TypeError(f"ST 1601 {name} must be a string")
    if not value or value != value.strip():
        raise ValueError(f"ST 1601 {name} must be non-empty and trimmed")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError(f"ST 1601 {name} contains a control character")
    return value.encode("utf-8")


def _validate_natural(
    pack: MDAP,
    *,
    rows: set[int],
    element_type: MDAPElementType,
    element_size: int | None,
    name: str,
    error_type: type[Exception],
) -> None:
    if pack.algorithm is not MDAPAlgorithm.NATURAL:
        raise error_type(f"ST 1601 {name} requires the Natural MDAP algorithm")
    if len(pack.dimensions) != 2 or pack.dimensions[0] not in rows:
        expected = " or ".join(str(row) for row in sorted(rows))
        raise error_type(f"ST 1601 {name} first dimension must be {expected}")
    if pack.element_type is not element_type:
        raise error_type(f"ST 1601 {name} has the wrong contextual element type")
    if element_size is not None and pack.element_size != element_size:
        raise error_type(f"ST 1601 {name} requires {element_size}-byte elements")


def _decode_mapped_array(
    data: bytes,
    *,
    row_bounds: tuple[tuple[float, float], ...],
    allowed_rows: set[int],
    name: str,
) -> HeterogeneousIMAPArray:
    pack = decode_mdap(data, element_type=MDAPElementType.RAW)
    _validate_natural(
        pack,
        rows=allowed_rows,
        element_type=MDAPElementType.RAW,
        element_size=None,
        name=name,
        error_type=DecodeError,
    )
    bounds = row_bounds[: pack.dimensions[0]]
    tie_points = pack.dimensions[1]
    decoded: list[MappedValue] = []
    for index, raw in enumerate(pack.elements):
        assert isinstance(raw, bytes)
        row = index // tie_points
        decoded.append(IMAPB(*bounds[row], pack.element_size).decode(raw))
    return HeterogeneousIMAPArray(
        (pack.dimensions[0], tie_points),
        pack.element_size,
        bounds,
        tuple(decoded),
        pack,
    )


def _encode_mapped_array(
    value: HeterogeneousIMAPArray,
    *,
    expected_bounds: tuple[tuple[float, float], ...],
    allowed_rows: set[int],
    name: str,
) -> bytes:
    if not isinstance(value, HeterogeneousIMAPArray):
        raise TypeError(f"ST 1601 {name} must be a HeterogeneousIMAPArray")
    rows, tie_points = value.dimensions
    if rows not in allowed_rows:
        raise ValueError(f"ST 1601 {name} has an invalid first dimension")
    bounds = expected_bounds[:rows]
    if value.row_bounds != bounds:
        raise ValueError(f"ST 1601 {name} row bounds do not match the standard")
    raw_elements: list[MDAPValue] = []
    for index, item in enumerate(value.elements):
        row = index // tie_points
        raw_elements.append(IMAPB(*bounds[row], value.element_size).encode(item))
    return encode_mdap(
        MDAP(
            value.dimensions,
            value.element_size,
            MDAPAlgorithm.NATURAL,
            tuple(raw_elements),
            MDAPElementType.RAW,
        )
    )


def _tie_point_counts(value: GeoRegistrationLocalSet) -> set[int]:
    counts: set[int] = set()
    for array_pack in (value.row_column, value.latitude_longitude):
        if array_pack is not None and len(array_pack.dimensions) == 2:
            counts.add(array_pack.dimensions[1])
    if value.elevation is not None and len(value.elevation.dimensions) == 1:
        counts.add(value.elevation.dimensions[0])
    for mapped_pack in (value.pixel_uncertainty, value.geo_uncertainty):
        if mapped_pack is not None:
            counts.add(mapped_pack.tie_point_count)
    return counts


def _validate_arrays(value: GeoRegistrationLocalSet, *, error_type: type[Exception]) -> None:
    if value.row_column is not None:
        _validate_natural(
            value.row_column,
            rows={2, 4},
            element_type=MDAPElementType.UNSIGNED_INTEGER,
            element_size=None,
            name="Row/Column array",
            error_type=error_type,
        )
    if value.latitude_longitude is not None:
        _validate_natural(
            value.latitude_longitude,
            rows={2},
            element_type=MDAPElementType.IEEE,
            element_size=4,
            name="Latitude/Longitude array",
            error_type=error_type,
        )
    if value.elevation is not None:
        if value.elevation.algorithm is not MDAPAlgorithm.NATURAL:
            raise error_type("ST 1601 Elevation array requires the Natural MDAP algorithm")
        if len(value.elevation.dimensions) != 1:
            raise error_type("ST 1601 Elevation array must be one-dimensional")
        if (
            value.elevation.element_type is not MDAPElementType.IEEE
            or value.elevation.element_size != 2
        ):
            raise error_type("ST 1601 Elevation array requires 2-byte IEEE elements")
    if value.pixel_uncertainty is not None:
        _encode_mapped_array(
            value.pixel_uncertainty,
            expected_bounds=_PIXEL_UNCERTAINTY_BOUNDS,
            allowed_rows={6},
            name="pixel uncertainty array",
        )
        if value.row_column is None or value.row_column.dimensions[0] != 4:
            raise error_type(
                "ST 1601 pixel uncertainty Item 9 requires a four-row Item 4"
            )
    if value.geo_uncertainty is not None:
        _encode_mapped_array(
            value.geo_uncertainty,
            expected_bounds=_GEO_UNCERTAINTY_BOUNDS,
            allowed_rows={3, 6},
            name="geographic uncertainty array",
        )
        if value.latitude_longitude is None:
            raise error_type("ST 1601 geographic uncertainty Item 10 requires Item 5")
        rows = value.geo_uncertainty.dimensions[0]
        if rows == 6 and value.elevation is None:
            raise error_type("ST 1601 six-row Item 10 requires Item 8")
        if rows == 3 and value.elevation is not None:
            raise error_type("ST 1601 three-row Item 10 cannot accompany Item 8")
    counts = _tie_point_counts(value)
    if len(counts) > 1:
        raise error_type("ST 1601 arrays must use the same number of tie points")


def decode_geo_registration_local_set(
    data: bytes,
    *,
    max_text_length: int = 4096,
) -> GeoRegistrationLocalSet:
    """Decode an embedded ST 1601.2 Geo-Registration Local Set value."""
    if not isinstance(data, bytes):
        raise TypeError("ST 1601 data must be bytes")
    if isinstance(max_text_length, bool) or not isinstance(max_text_length, int):
        raise TypeError("max_text_length must be an integer")
    if max_text_length < 1:
        raise ValueError("max_text_length must be positive")
    local_set = parse_local_set(data)
    seen: set[int] = set()
    for item in local_set.items:
        if item.tag in seen:
            raise DecodeError(f"duplicate ST 1601 tag {item.tag}")
        seen.add(item.tag)
    if not {1, 2, 3}.issubset(seen):
        raise DecodeError(
            "ST 1601 requires Document Version, Algorithm Name, and Algorithm Version"
        )
    item1 = local_set.getone(1)
    item2 = local_set.getone(2)
    item3 = local_set.getone(3)
    assert item1 is not None and item2 is not None and item3 is not None
    values: dict[int, object] = {
        1: _decode_uint(item1.value),
        2: _decode_text(item2.value, name="Algorithm Name", max_text_length=max_text_length),
        3: _decode_text(
            item3.value,
            name="Algorithm Version",
            max_text_length=max_text_length,
        ),
    }
    item4 = local_set.getone(4)
    if item4 is not None:
        values[4] = decode_mdap(item4.value, element_type=MDAPElementType.UNSIGNED_INTEGER)
    item5 = local_set.getone(5)
    if item5 is not None:
        values[5] = decode_mdap(item5.value, element_type=MDAPElementType.IEEE)
    item6 = local_set.getone(6)
    if item6 is not None:
        values[6] = _decode_text(
            item6.value,
            name="Second Image Name",
            max_text_length=max_text_length,
        )
    item7 = local_set.getone(7)
    if item7 is not None:
        if len(item7.value) != 16:
            raise DecodeError("ST 1601 Algorithm Configuration Identifier must be 16 bytes")
        values[7] = UUID(bytes=item7.value)
    item8 = local_set.getone(8)
    if item8 is not None:
        values[8] = decode_mdap(item8.value, element_type=MDAPElementType.IEEE)
    item9 = local_set.getone(9)
    if item9 is not None:
        values[9] = _decode_mapped_array(
            item9.value,
            row_bounds=_PIXEL_UNCERTAINTY_BOUNDS,
            allowed_rows={6},
            name="pixel uncertainty array",
        )
    item10 = local_set.getone(10)
    if item10 is not None:
        values[10] = _decode_mapped_array(
            item10.value,
            row_bounds=_GEO_UNCERTAINTY_BOUNDS,
            allowed_rows={3, 6},
            name="geographic uncertainty array",
        )
    extensions = {
        item.tag: RawGeoRegistrationValue(item.value)
        for item in local_set.items
        if item.tag > 10
    }
    result = GeoRegistrationLocalSet(
        document_version=values[1],  # type: ignore[arg-type]
        algorithm_name=values[2],  # type: ignore[arg-type]
        algorithm_version=values[3],  # type: ignore[arg-type]
        row_column=values.get(4),  # type: ignore[arg-type]
        latitude_longitude=values.get(5),  # type: ignore[arg-type]
        second_image_name=values.get(6),  # type: ignore[arg-type]
        algorithm_configuration_id=values.get(7),  # type: ignore[arg-type]
        elevation=values.get(8),  # type: ignore[arg-type]
        pixel_uncertainty=values.get(9),  # type: ignore[arg-type]
        geo_uncertainty=values.get(10),  # type: ignore[arg-type]
        extensions=extensions,
        local_set=local_set,
    )
    _validate_arrays(result, error_type=DecodeError)
    return result


def _item(tag: int, data: bytes) -> bytes:
    return encode_ber_oid(tag) + encode_ber_length(len(data)) + data


def encode_geo_registration_local_set(
    value: GeoRegistrationLocalSet,
    *,
    preserve: bool = False,
    max_text_length: int = 4096,
) -> bytes:
    """Encode an embedded ST 1601.2 Geo-Registration Local Set value."""
    if not isinstance(value, GeoRegistrationLocalSet):
        raise TypeError("value must be a GeoRegistrationLocalSet")
    if preserve and value.local_set is not None:
        return value.local_set.raw
    _validate_arrays(value, error_type=ValueError)
    encoded = [_item(1, _encode_uint(value.document_version))]
    for tag, name, text in (
        (2, "Algorithm Name", value.algorithm_name),
        (3, "Algorithm Version", value.algorithm_version),
    ):
        raw = _validate_text(text, name=name)
        if len(raw) > max_text_length:
            raise LimitExceeded(f"ST 1601 {name} exceeds configured text limit")
        encoded.append(_item(tag, raw))
    for tag, pack in (
        (4, value.row_column),
        (5, value.latitude_longitude),
    ):
        if pack is not None:
            encoded.append(_item(tag, encode_mdap(pack)))
    if value.second_image_name is not None:
        raw = _validate_text(value.second_image_name, name="Second Image Name")
        if len(raw) > max_text_length:
            raise LimitExceeded("ST 1601 Second Image Name exceeds configured text limit")
        encoded.append(_item(6, raw))
    if value.algorithm_configuration_id is not None:
        if not isinstance(value.algorithm_configuration_id, UUID):
            raise TypeError("algorithm_configuration_id must be a UUID")
        encoded.append(_item(7, value.algorithm_configuration_id.bytes))
    if value.elevation is not None:
        encoded.append(_item(8, encode_mdap(value.elevation)))
    if value.pixel_uncertainty is not None:
        encoded.append(
            _item(
                9,
                _encode_mapped_array(
                    value.pixel_uncertainty,
                    expected_bounds=_PIXEL_UNCERTAINTY_BOUNDS,
                    allowed_rows={6},
                    name="pixel uncertainty array",
                ),
            )
        )
    if value.geo_uncertainty is not None:
        encoded.append(
            _item(
                10,
                _encode_mapped_array(
                    value.geo_uncertainty,
                    expected_bounds=_GEO_UNCERTAINTY_BOUNDS,
                    allowed_rows={3, 6},
                    name="geographic uncertainty array",
                ),
            )
        )
    for tag in sorted(value.extensions):
        if isinstance(tag, bool) or not isinstance(tag, int):
            raise TypeError("ST 1601 extension tags must be integers")
        if tag <= 10:
            raise ValueError("ST 1601 extension tags must be greater than 10")
        extension = value.extensions[tag]
        if not isinstance(extension, RawGeoRegistrationValue):
            raise TypeError(f"ST 1601 extension tag {tag} requires RawGeoRegistrationValue")
        encoded.append(_item(tag, extension.data))
    result = b"".join(encoded)
    decode_geo_registration_local_set(result, max_text_length=max_text_length)
    return result
