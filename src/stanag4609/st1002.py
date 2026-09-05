"""MISB ST 1002.3 Range Motion Imagery Local Set codecs."""

from __future__ import annotations

import math
import struct
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import IntEnum
from typing import Any

from stanag4609.errors import ChecksumError, DecodeError, NeedMoreData
from stanag4609.imap import IMAPSpecialKind, IMAPSpecialValue
from stanag4609.klv.ber import (
    decode_ber_length,
    decode_ber_oid,
    encode_ber_length,
    encode_ber_oid,
)
from stanag4609.klv.checksum import crc16_ccitt
from stanag4609.klv.local_set import parse_local_set
from stanag4609.klv.model import KLVPacket, LocalSet, LocalSetItem
from stanag4609.klv.stream import KLVStreamParser
from stanag4609.st1202 import (
    GeneralizedTransformation,
    TransformationType,
    decode_generalized_transformation,
    encode_generalized_transformation,
)
from stanag4609.st1303 import (
    MDAP,
    MDAPAlgorithm,
    MDAPElementType,
    MDAPValue,
    decode_mdap,
    encode_mdap,
)

RANGE_IMAGE_LOCAL_SET_KEY = bytes.fromhex(
    "06 0E 2B 34 02 0B 01 01 0E 01 03 03 0C 00 00 00"
)
_DOCUMENT_VERSION = 3
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_FLOAT_FORMATS = {4: ">f", 8: ">d"}


class RangeImageSource(IntEnum):
    COMPUTATIONALLY_EXTRACTED = 0
    RANGE_SENSOR = 1


class RangeDataType(IntEnum):
    PERSPECTIVE = 0
    DEPTH = 1


class RangeCompression(IntEnum):
    NO_COMPRESSION = 0
    PLANAR_FIT = 1


@dataclass(frozen=True, slots=True)
class RangeImageEnumerations:
    """The three fields packed into ST 1002 Item 12."""

    source: RangeImageSource
    data_type: RangeDataType
    compression: RangeCompression

    def __post_init__(self) -> None:
        if not isinstance(self.source, RangeImageSource):
            raise TypeError("source must be a RangeImageSource")
        if not isinstance(self.data_type, RangeDataType):
            raise TypeError("data_type must be a RangeDataType")
        if not isinstance(self.compression, RangeCompression):
            raise TypeError("compression must be a RangeCompression")

    def to_byte(self) -> int:
        return int(self.source) << 6 | int(self.data_type) << 3 | int(self.compression)


@dataclass(frozen=True, slots=True)
class RawRangeValue:
    """Opaque value for a future ST 1002 Local Set item."""

    data: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes):
            raise TypeError("RawRangeValue data must be bytes")


@dataclass(frozen=True, slots=True)
class SectionData:
    """One ST 1002 Section Data Variable Length Pack."""

    section_x: int
    section_y: int
    range_values: MDAP
    uncertainty: MDAP | None = None
    plane: tuple[float, float, float] | None = None
    plane_widths: tuple[int, int, int] | None = field(default=None, compare=False)
    raw: bytes | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        for name, coordinate_value in (
            ("section_x", self.section_x),
            ("section_y", self.section_y),
        ):
            if isinstance(coordinate_value, bool) or not isinstance(coordinate_value, int):
                raise TypeError(f"{name} must be an integer")
        if not isinstance(self.range_values, MDAP):
            raise TypeError("range_values must be an MDAP")
        if self.uncertainty is not None and not isinstance(self.uncertainty, MDAP):
            raise TypeError("uncertainty must be an MDAP or None")
        if self.plane is not None:
            if not isinstance(self.plane, tuple) or len(self.plane) != 3:
                raise ValueError("ST 1002 plane parameters require all three values")
            for plane_value in self.plane:
                if isinstance(plane_value, bool) or not isinstance(plane_value, (int, float)):
                    raise TypeError("ST 1002 plane parameters must be numeric")
                if not math.isfinite(float(plane_value)):
                    raise ValueError("ST 1002 plane parameters must be finite")


@dataclass(frozen=True, slots=True)
class RangeField:
    tag: int
    name: str
    value: Any
    raw: bytes
    item: LocalSetItem


@dataclass(frozen=True, slots=True)
class RangeImageLocalSet:
    """A standalone or embedded ST 1002.3 Range Image Local Set."""

    timestamp: datetime
    document_version: int
    enumerations: RangeImageEnumerations
    sprm: float | None = None
    sprm_uncertainty: float | None = None
    sprm_row: float | None = None
    sprm_column: float | None = None
    sections_x: int = 1
    sections_y: int = 1
    transformation: GeneralizedTransformation | None = None
    sections: tuple[SectionData, ...] = ()
    leap_seconds: int | None = None
    extensions: Mapping[int, RawRangeValue] = field(default_factory=dict)
    packet: KLVPacket | None = field(default=None, compare=False, repr=False)
    local_set: LocalSet | None = field(default=None, compare=False, repr=False)
    fields: tuple[RangeField, ...] = field(default=(), compare=False, repr=False)
    standalone: bool = field(default=True, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.timestamp, datetime):
            raise TypeError("timestamp must be a datetime")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        if isinstance(self.document_version, bool) or not isinstance(self.document_version, int):
            raise TypeError("ST 1002 Document Version must be an integer")
        if self.document_version != _DOCUMENT_VERSION:
            raise ValueError(
                f"ST 1002 Document Version must be {_DOCUMENT_VERSION}"
            )
        if not isinstance(self.enumerations, RangeImageEnumerations):
            raise TypeError("enumerations must be RangeImageEnumerations")
        for name, value in (
            ("sprm", self.sprm),
            ("sprm_uncertainty", self.sprm_uncertainty),
            ("sprm_row", self.sprm_row),
            ("sprm_column", self.sprm_column),
        ):
            if value is not None:
                _validate_float(value, name=name, error_type=ValueError)
        if self.sprm_uncertainty is not None and self.sprm_uncertainty < 0:
            raise ValueError("SPRM uncertainty must be non-negative")
        for name, value in (("sections_x", self.sections_x), ("sections_y", self.sections_y)):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 1:
                raise ValueError(f"{name} must be positive")
        if self.transformation is not None and not isinstance(
            self.transformation, GeneralizedTransformation
        ):
            raise TypeError("transformation must be a GeneralizedTransformation or None")
        if not isinstance(self.sections, tuple) or any(
            not isinstance(section, SectionData) for section in self.sections
        ):
            raise TypeError("sections must be a tuple of SectionData values")
        if self.leap_seconds is not None and (
            isinstance(self.leap_seconds, bool) or not isinstance(self.leap_seconds, int)
        ):
            raise TypeError("leap_seconds must be an integer or None")

    def effective_sprm_coordinates(
        self,
        *,
        image_rows: int,
        image_columns: int,
    ) -> tuple[float, float]:
        """Return the effective SPRM ``(row, column)`` image coordinates.

        ST 1002.1-07 and ST 1002.1-08 make the image center the default for
        each omitted coordinate independently. Image dimensions are caller
        context because the Range Image Local Set does not carry them.
        """

        rows = _validate_image_dimension(image_rows, name="image_rows")
        columns = _validate_image_dimension(image_columns, name="image_columns")
        row = rows / 2.0 if self.sprm_row is None else float(self.sprm_row)
        column = columns / 2.0 if self.sprm_column is None else float(self.sprm_column)
        return row, column


_FIELD_NAMES = {
    1: "Range Image Precision Time Stamp",
    11: "Document Version",
    12: "Range Image Enumerations",
    13: "SPRM",
    14: "SPRM Uncertainty",
    15: "SPRM Row Coordinate",
    16: "SPRM Column Coordinate",
    17: "Number of Sections in X",
    18: "Number of Sections in Y",
    19: "Generalized Transformation LS",
    20: "Section Data Variable Length Pack",
    21: "CRC-16-CCITT",
    22: "Leap Seconds",
}


def _is_retired_tag(tag: int) -> bool:
    return 2 <= tag <= 10 or 51 <= tag <= 54


def _validate_float(value: Any, *, name: str, error_type: type[Exception]) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric")
    result = float(value)
    if math.isinf(result):
        raise error_type(f"ST 1002 {name} cannot be infinite")
    return result


def _validate_image_dimension(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value


def _decode_float(data: bytes, *, name: str) -> float:
    try:
        format_code = _FLOAT_FORMATS[len(data)]
    except KeyError as error:
        raise DecodeError(f"ST 1002 {name} must contain 4 or 8 bytes") from error
    return _validate_float(struct.unpack(format_code, data)[0], name=name, error_type=DecodeError)


def _encode_float(value: Any, width: int, *, name: str) -> bytes:
    numeric = _validate_float(value, name=name, error_type=ValueError)
    try:
        return struct.pack(_FLOAT_FORMATS[width], numeric)
    except (OverflowError, struct.error) as error:
        raise ValueError(f"ST 1002 {name} cannot fit float_width") from error


def _read_element_lengths(data: bytes) -> tuple[bytes, ...]:
    elements: list[bytes] = []
    offset = 0
    while offset < len(data):
        try:
            length, used = decode_ber_length(data, offset, max_value=64 * 1024 * 1024)
        except NeedMoreData as error:
            raise DecodeError("ST 1002 Section Data element length is truncated") from error
        offset += used
        end = offset + length
        if end > len(data):
            raise DecodeError("ST 1002 Section Data element is truncated")
        elements.append(data[offset:end])
        offset = end
    if len(elements) not in {4, 7}:
        raise DecodeError("ST 1002 Section Data requires four or seven elements")
    return tuple(elements)


def _decode_oid_element(data: bytes, *, name: str) -> int:
    if not data:
        raise DecodeError(f"ST 1002 {name} is empty")
    try:
        value, used = decode_ber_oid(data)
    except NeedMoreData as error:
        raise DecodeError(f"ST 1002 {name} is truncated") from error
    if used != len(data):
        raise DecodeError(f"ST 1002 {name} contains trailing bytes")
    if value < 1:
        raise DecodeError(f"ST 1002 {name} must be greater than zero")
    return value


def _validate_range_mdap(pack: MDAP, *, error_type: type[Exception]) -> None:
    if pack.algorithm not in {MDAPAlgorithm.NATURAL, MDAPAlgorithm.IMAP}:
        raise error_type("ST 1002 range MDAP requires the Natural or IMAP algorithm")
    if len(pack.dimensions) != 2:
        raise error_type("ST 1002 range MDAP must have two dimensions")
    if pack.algorithm is MDAPAlgorithm.NATURAL and (
        pack.element_type is not MDAPElementType.IEEE or pack.element_size not in {4, 8}
    ):
        raise error_type("ST 1002 Natural range MDAP requires 4- or 8-byte IEEE values")
    for value in pack.elements:
        if (
            isinstance(value, IMAPSpecialValue)
            and value.kind is not IMAPSpecialKind.POSITIVE_QUIET_NAN
        ):
            raise error_type(
                "ST 1002 range MDAP permits only the positive quiet NaN special value"
            )


def _range_samples(pack: MDAP) -> tuple[MDAPValue, ...]:
    _validate_range_mdap(pack, error_type=ValueError)
    if len(pack.elements) != math.prod(pack.dimensions):
        raise ValueError("ST 1002 range MDAP element count does not match its dimensions")
    return pack.elements


def _numeric_range_sample(value: MDAPValue) -> float | None:
    if isinstance(value, IMAPSpecialValue):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("ST 1002 range values must be numeric or an IMAP special value")
    numeric = float(value)
    if math.isnan(numeric):
        return None
    if math.isinf(numeric):
        raise ValueError("ST 1002 range values cannot be infinite")
    return numeric


def _plane_parameters(plane: tuple[float, float, float]) -> tuple[float, float, float]:
    if not isinstance(plane, tuple) or len(plane) != 3:
        raise ValueError("ST 1002 range plane requires three finite parameters")
    parameters: list[float] = []
    for value in plane:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("ST 1002 range plane parameters must be numeric")
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError("ST 1002 range plane requires three finite parameters")
        parameters.append(numeric)
    return parameters[0], parameters[1], parameters[2]


def fit_range_plane(range_values: MDAP) -> tuple[float, float, float]:
    """Fit the ST 1002 Equation 8 plane to a two-dimensional Range MDAP.

    Coordinates are one-based as specified by ST 1002.3. IEEE NaNs and IMAP
    special values are excluded by the Equation 4/5 mask. The implementation
    solves the three normal equations with partial pivoting and no optional
    numerical dependency.
    """

    samples = _range_samples(range_values)
    _, size_j = range_values.dimensions
    count = 0
    sum_i = sum_j = sum_r = 0.0
    sum_ii = sum_ij = sum_jj = 0.0
    sum_ir = sum_jr = 0.0
    for index, value in enumerate(samples):
        numeric = _numeric_range_sample(value)
        if numeric is None:
            continue
        i = index // size_j + 1
        j = index % size_j + 1
        count += 1
        sum_i += i
        sum_j += j
        sum_r += numeric
        sum_ii += i * i
        sum_ij += i * j
        sum_jj += j * j
        sum_ir += i * numeric
        sum_jr += j * numeric
    matrix = [
        [sum_ii, sum_ij, sum_i, sum_ir],
        [sum_ij, sum_jj, sum_j, sum_jr],
        [sum_i, sum_j, float(count), sum_r],
    ]
    scale = max(1.0, *(abs(value) for row in matrix for value in row[:3]))
    for column in range(3):
        pivot = max(range(column, 3), key=lambda row: abs(matrix[row][column]))
        if abs(matrix[pivot][column]) <= scale * 1e-12:
            raise ValueError(
                "valid samples do not determine a unique ST 1002 range plane"
            )
        matrix[column], matrix[pivot] = matrix[pivot], matrix[column]
        pivot_value = matrix[column][column]
        for entry in range(column, 4):
            matrix[column][entry] /= pivot_value
        for row in range(3):
            if row == column:
                continue
            factor = matrix[row][column]
            for entry in range(column, 4):
                matrix[row][entry] -= factor * matrix[column][entry]
    return matrix[0][3], matrix[1][3], matrix[2][3]


def _apply_range_plane(
    range_values: MDAP,
    plane: tuple[float, float, float],
    *,
    direction: int,
) -> tuple[MDAPValue, ...]:
    samples = _range_samples(range_values)
    a, b, c = _plane_parameters(plane)
    size_j = range_values.dimensions[1]
    result: list[MDAPValue] = []
    for index, value in enumerate(samples):
        numeric = _numeric_range_sample(value)
        if numeric is None:
            result.append(value)
            continue
        i = index // size_j + 1
        j = index % size_j + 1
        result.append(numeric + direction * (a * i + b * j + c))
    return tuple(result)


def subtract_range_plane(
    range_values: MDAP,
    plane: tuple[float, float, float] | None = None,
) -> tuple[tuple[float, float, float], tuple[MDAPValue, ...]]:
    """Apply ST 1002 Equation 9 and return ``(plane, residual_values)``.

    When ``plane`` is omitted, :func:`fit_range_plane` computes Equation 8.
    Unknown values remain unchanged so the caller can build a residual MDAP
    with application-selected IMAP bounds and precision.
    """

    selected = fit_range_plane(range_values) if plane is None else _plane_parameters(plane)
    return selected, _apply_range_plane(range_values, selected, direction=-1)


def reverse_range_plane(
    adjusted_values: MDAP,
    plane: tuple[float, float, float],
) -> tuple[MDAPValue, ...]:
    """Reconstruct range samples with ST 1002 Equation 10.

    IEEE NaNs and IMAP special values pass through unchanged.
    """

    return _apply_range_plane(adjusted_values, plane, direction=1)


def _validate_uncertainty(pack: MDAP, *, error_type: type[Exception]) -> None:
    _validate_range_mdap(pack, error_type=error_type)
    if pack.algorithm is MDAPAlgorithm.IMAP:
        assert pack.imap_bounds is not None
        if pack.imap_bounds[0] != 0:
            raise error_type("ST 1002 uncertainty IMAP minimum must be zero")
    for item in pack.elements:
        if isinstance(item, IMAPSpecialValue):
            continue
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise error_type("ST 1002 uncertainty values must be numeric")
        if not math.isnan(float(item)) and item < 0:
            raise error_type("ST 1002 uncertainty values must be non-negative")


def decode_section_data(data: bytes) -> SectionData:
    """Decode one complete ST 1002 Section Data VLP value."""
    if not isinstance(data, bytes):
        raise TypeError("ST 1002 Section Data must be bytes")
    elements = _read_element_lengths(data)
    section_x = _decode_oid_element(elements[0], name="Section Number X")
    section_y = _decode_oid_element(elements[1], name="Section Number Y")
    range_values = decode_mdap(elements[2], element_type=MDAPElementType.IEEE)
    _validate_range_mdap(range_values, error_type=DecodeError)
    uncertainty = None
    if elements[3]:
        uncertainty = decode_mdap(elements[3], element_type=MDAPElementType.IEEE)
        _validate_uncertainty(uncertainty, error_type=DecodeError)
        if uncertainty.dimensions != range_values.dimensions:
            raise DecodeError("ST 1002 uncertainty dimensions must match range dimensions")
    plane = None
    plane_widths = None
    if len(elements) == 7:
        plane = tuple(
            _decode_float(element, name="plane parameter") for element in elements[4:7]
        )
        plane_widths = tuple(len(element) for element in elements[4:7])
    return SectionData(
        section_x,
        section_y,
        range_values,
        uncertainty,
        plane,  # type: ignore[arg-type]
        plane_widths,  # type: ignore[arg-type]
        data,
    )


def encode_section_data(
    section: SectionData,
    *,
    plane_float_width: int = 8,
    preserve: bool = False,
) -> bytes:
    """Encode one ST 1002 Section Data VLP value."""
    if not isinstance(section, SectionData):
        raise TypeError("section must be a SectionData")
    if plane_float_width not in _FLOAT_FORMATS:
        raise ValueError("plane_float_width must be 4 or 8")
    if preserve and section.raw is not None:
        return section.raw
    if section.section_x < 1 or section.section_y < 1:
        raise ValueError("ST 1002 Section coordinates must be greater than zero")
    _validate_range_mdap(section.range_values, error_type=ValueError)
    uncertainty = b""
    if section.uncertainty is not None:
        _validate_uncertainty(section.uncertainty, error_type=ValueError)
        if section.uncertainty.dimensions != section.range_values.dimensions:
            raise ValueError("ST 1002 uncertainty dimensions must match range dimensions")
        uncertainty = encode_mdap(section.uncertainty)
    elements = [
        encode_ber_oid(section.section_x),
        encode_ber_oid(section.section_y),
        encode_mdap(section.range_values),
        uncertainty,
    ]
    if section.plane is not None:
        elements.extend(
            _encode_float(item, plane_float_width, name="plane parameter")
            for item in section.plane
        )
    return b"".join(encode_ber_length(len(item)) + item for item in elements)


def _decode_enumerations(data: bytes) -> RangeImageEnumerations:
    if len(data) != 1:
        raise DecodeError("ST 1002 Range Image Enumerations must contain one byte")
    value = data[0]
    if value & 0x80:
        raise DecodeError("ST 1002 Range Image Enumerations reserved bit must be zero")
    data_type = (value >> 3) & 0x07
    compression = value & 0x07
    if data_type > 1:
        raise DecodeError(f"ST 1002 reserved data type enumeration {data_type}")
    if compression > 1:
        raise DecodeError(f"ST 1002 reserved compression enumeration {compression}")
    return RangeImageEnumerations(
        RangeImageSource((value >> 6) & 1),
        RangeDataType(data_type),
        RangeCompression(compression),
    )


def _decode_uint(data: bytes, *, name: str) -> int:
    if not data:
        raise DecodeError(f"ST 1002 {name} is empty")
    if len(data) > 1 and data[0] == 0:
        raise DecodeError(f"ST 1002 {name} must use a minimal unsigned integer")
    return int.from_bytes(data, "big")


def _decode_int(data: bytes, *, name: str) -> int:
    if not data:
        raise DecodeError(f"ST 1002 {name} is empty")
    if len(data) > 1 and (
        (data[0] == 0 and not data[1] & 0x80) or (data[0] == 0xFF and data[1] & 0x80)
    ):
        raise DecodeError(f"ST 1002 {name} must use a minimal signed integer")
    return int.from_bytes(data, "big", signed=True)


def _encode_uint(value: int, *, name: str) -> bytes:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"ST 1002 {name} must be an integer")
    if value < 0:
        raise ValueError(f"ST 1002 {name} must be non-negative")
    return value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")


def _encode_int(value: int) -> bytes:
    length = 1
    while not -(1 << (8 * length - 1)) <= value < (1 << (8 * length - 1)):
        length += 1
    return value.to_bytes(length, "big", signed=True)


def _decode_timestamp(data: bytes) -> datetime:
    if len(data) != 8:
        raise DecodeError("ST 1002 Precision Time Stamp must contain eight bytes")
    return _EPOCH + timedelta(microseconds=int.from_bytes(data, "big"))


def _encode_timestamp(value: datetime) -> bytes:
    if not isinstance(value, datetime):
        raise TypeError("timestamp must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    microseconds = int((value.astimezone(timezone.utc) - _EPOCH) / timedelta(microseconds=1))
    if not 0 <= microseconds < 1 << 64:
        raise ValueError("timestamp is outside the ST 1002 uint64 range")
    return microseconds.to_bytes(8, "big")


def _parse_packet(data: bytes) -> KLVPacket:
    parser = KLVStreamParser(key_prefix=RANGE_IMAGE_LOCAL_SET_KEY)
    packets = parser.feed(data)
    packets.extend(parser.finish())
    if len(packets) != 1:
        raise DecodeError(f"expected one ST 1002 packet, observed {len(packets)}")
    return packets[0]


def _validate_sections(
    sections_x: int,
    sections_y: int,
    sections: tuple[SectionData, ...],
    enumerations: RangeImageEnumerations,
    *,
    error_type: type[Exception],
) -> None:
    if sections_x > 1 and sections_y > 1:
        raise error_type("ST 1002 requires a simple Section layout with one axis equal to one")
    if sections and len(sections) != sections_x * sections_y:
        raise error_type(
            f"ST 1002 Section Data count {len(sections)} does not match "
            f"{sections_x} x {sections_y}"
        )
    coordinates: set[tuple[int, int]] = set()
    uncertainty_presence = {section.uncertainty is not None for section in sections}
    if len(uncertainty_presence) > 1:
        raise error_type("ST 1002 uncertainty must be present or absent in every Section")
    for section in sections:
        coordinate = section.section_x, section.section_y
        if coordinate in coordinates:
            raise error_type(f"duplicate ST 1002 Section coordinate {coordinate}")
        coordinates.add(coordinate)
        if not 1 <= section.section_x <= sections_x or not 1 <= section.section_y <= sections_y:
            raise error_type(f"ST 1002 Section coordinate {coordinate} is outside the layout")
        if section.section_x > 1 and section.section_y != 1:
            raise error_type("ST 1002 Section X greater than one requires Section Y equal one")
        if section.section_y > 1 and section.section_x != 1:
            raise error_type("ST 1002 Section Y greater than one requires Section X equal one")
        if enumerations.compression is RangeCompression.PLANAR_FIT and section.plane is None:
            raise error_type("ST 1002 Planar Fit requires plane parameters in every Section")
        if (
            enumerations.compression is RangeCompression.NO_COMPRESSION
            and section.plane is not None
        ):
            raise error_type("ST 1002 plane parameters require Planar Fit compression")


def decode_range_image_local_set(
    data: bytes | KLVPacket,
    *,
    standalone: bool = True,
    verify_checksum: bool = True,
) -> RangeImageLocalSet:
    """Decode a standalone Universal ST 1002 packet or embedded Local Set value."""
    if not isinstance(standalone, bool) or not isinstance(verify_checksum, bool):
        raise TypeError("standalone and verify_checksum must be booleans")
    if standalone:
        packet = data if isinstance(data, KLVPacket) else _parse_packet(data)
        if packet.key != RANGE_IMAGE_LOCAL_SET_KEY:
            raise DecodeError("unexpected Universal Key for ST 1002 Range Image Local Set")
        local_set = parse_local_set(packet.value)
    else:
        if not isinstance(data, bytes):
            raise TypeError("embedded ST 1002 data must be bytes")
        packet = None
        local_set = parse_local_set(data)
    seen: set[int] = set()
    for item in local_set.items:
        if item.tag in seen and item.tag != 20:
            raise DecodeError(f"duplicate ST 1002 tag {item.tag}")
        seen.add(item.tag)
        if _is_retired_tag(item.tag):
            raise DecodeError(f"retired ST 1002 tag {item.tag} must not be used")
    if not local_set.items or local_set.items[0].tag != 1 or len(local_set.getall(1)) != 1:
        raise DecodeError("ST 1002 Precision Time Stamp item 1 must occur once and first")
    checksums = local_set.getall(21)
    if standalone:
        if len(checksums) != 1 or local_set.items[-1].tag != 21 or len(checksums[0].value) != 2:
            raise ChecksumError("standalone ST 1002 CRC must be the final two-byte item")
        assert packet is not None
        if verify_checksum and crc16_ccitt(packet.raw) != 0:
            raise ChecksumError("ST 1002 CRC-16-CCITT mismatch")
    elif checksums:
        raise DecodeError("embedded ST 1002 forbids CRC item 21")
    if len(local_set.getall(11)) != 1:
        raise DecodeError("ST 1002 Document Version item 11 is required once")
    if len(local_set.getall(12)) != 1:
        raise DecodeError("ST 1002 Range Image Enumerations item 12 is required once")

    timestamp_item = local_set.getone(1)
    version_item = local_set.getone(11)
    enumeration_item = local_set.getone(12)
    assert timestamp_item is not None and version_item is not None and enumeration_item is not None
    timestamp = _decode_timestamp(timestamp_item.value)
    document_version = _decode_uint(version_item.value, name="Document Version")
    if document_version != _DOCUMENT_VERSION:
        raise DecodeError(
            f"ST 1002 Document Version must be {_DOCUMENT_VERSION}"
        )
    enumerations = _decode_enumerations(enumeration_item.value)
    values: dict[int, Any] = {1: timestamp, 11: document_version, 12: enumerations}
    float_items = (
        (13, "SPRM"),
        (14, "SPRM uncertainty"),
        (15, "SPRM row"),
        (16, "SPRM column"),
    )
    for tag, name in float_items:
        optional_item = local_set.getone(tag)
        if optional_item is not None:
            values[tag] = _decode_float(optional_item.value, name=name)
    if 14 in values and not math.isnan(values[14]) and values[14] < 0:
        raise DecodeError("ST 1002 SPRM uncertainty must be non-negative")
    sections_x = 1
    sections_y = 1
    for tag in (17, 18):
        section_count_item = local_set.getone(tag)
        if section_count_item is not None:
            values[tag] = _decode_oid_element(
                section_count_item.value, name=_FIELD_NAMES[tag]
            )
    sections_x = values.get(17, 1)
    sections_y = values.get(18, 1)
    transformation = None
    item19 = local_set.getone(19)
    if item19 is not None:
        transformation = decode_generalized_transformation(item19.value)
        if transformation.transformation_type is not TransformationType.CHILD_PARENT:
            raise DecodeError("ST 1002 Generalized Transformation must use Child-Parent type")
        values[19] = transformation
    sections = tuple(decode_section_data(item.value) for item in local_set.getall(20))
    _validate_sections(sections_x, sections_y, sections, enumerations, error_type=DecodeError)
    leap_seconds = None
    item22 = local_set.getone(22)
    if item22 is not None:
        leap_seconds = _decode_int(item22.value, name="Leap Seconds")
        values[22] = leap_seconds
    extensions = {
        item.tag: RawRangeValue(item.value)
        for item in local_set.items
        if item.tag not in _FIELD_NAMES
    }
    fields = tuple(
        RangeField(
            item.tag,
            _FIELD_NAMES.get(item.tag, f"Unknown Tag {item.tag}"),
            sections[sum(1 for prior in local_set.items[:index] if prior.tag == 20)]
            if item.tag == 20
            else values.get(item.tag, RawRangeValue(item.value)),
            item.value,
            item,
        )
        for index, item in enumerate(local_set.items)
    )
    return RangeImageLocalSet(
        timestamp,
        document_version,
        enumerations,
        values.get(13),
        values.get(14),
        values.get(15),
        values.get(16),
        sections_x,
        sections_y,
        transformation,
        sections,
        leap_seconds,
        extensions,
        packet,
        local_set,
        fields,
        standalone,
    )


def _item(tag: int, value: bytes) -> bytes:
    return encode_ber_oid(tag) + encode_ber_length(len(value)) + value


def encode_range_image_local_set(
    value: RangeImageLocalSet,
    *,
    standalone: bool = True,
    float_width: int = 8,
    preserve: bool = False,
) -> bytes:
    """Encode an ST 1002.3 Range Image Local Set with owned standalone CRC."""
    if not isinstance(value, RangeImageLocalSet):
        raise TypeError("value must be a RangeImageLocalSet")
    if not isinstance(standalone, bool) or not isinstance(preserve, bool):
        raise TypeError("standalone and preserve must be booleans")
    if float_width not in _FLOAT_FORMATS:
        raise ValueError("float_width must be 4 or 8")
    if preserve and value.local_set is not None and value.standalone == standalone:
        if standalone:
            assert value.packet is not None
            return value.packet.raw
        return value.local_set.raw
    _validate_sections(
        value.sections_x,
        value.sections_y,
        value.sections,
        value.enumerations,
        error_type=ValueError,
    )
    if value.transformation is not None and (
        value.transformation.transformation_type is not TransformationType.CHILD_PARENT
    ):
        raise ValueError("ST 1002 Generalized Transformation must use Child-Parent type")
    encoded = [_item(1, _encode_timestamp(value.timestamp))]
    encoded.append(_item(11, _encode_uint(value.document_version, name="Document Version")))
    encoded.append(_item(12, bytes((value.enumerations.to_byte(),))))
    for tag, name, item_value in (
        (13, "SPRM", value.sprm),
        (14, "SPRM uncertainty", value.sprm_uncertainty),
        (15, "SPRM row", value.sprm_row),
        (16, "SPRM column", value.sprm_column),
    ):
        if item_value is not None:
            encoded.append(_item(tag, _encode_float(item_value, float_width, name=name)))
    if value.sections_x != 1:
        encoded.append(_item(17, encode_ber_oid(value.sections_x)))
    if value.sections_y != 1:
        encoded.append(_item(18, encode_ber_oid(value.sections_y)))
    if value.transformation is not None:
        encoded.append(_item(19, encode_generalized_transformation(value.transformation)))
    encoded.extend(_item(20, encode_section_data(section)) for section in value.sections)
    if value.leap_seconds is not None:
        encoded.append(_item(22, _encode_int(value.leap_seconds)))
    for tag in sorted(value.extensions):
        if isinstance(tag, bool) or not isinstance(tag, int):
            raise TypeError("ST 1002 extension tags must be integers")
        if tag in _FIELD_NAMES or _is_retired_tag(tag) or tag < 1:
            raise ValueError(f"tag {tag} is not an ST 1002 extension slot")
        extension = value.extensions[tag]
        if not isinstance(extension, RawRangeValue):
            raise TypeError(f"ST 1002 extension tag {tag} requires RawRangeValue")
        encoded.append(_item(tag, extension.data))
    local_value = b"".join(encoded)
    if not standalone:
        decode_range_image_local_set(local_value, standalone=False)
        return local_value
    crc_header = encode_ber_oid(21) + encode_ber_length(2)
    outer_length = len(local_value) + len(crc_header) + 2
    prefix = RANGE_IMAGE_LOCAL_SET_KEY + encode_ber_length(outer_length) + local_value + crc_header
    packet = prefix + crc16_ccitt(prefix).to_bytes(2, "big")
    decode_range_image_local_set(packet)
    return packet
