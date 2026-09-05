"""MISB ST 1206.1 SAR Motion Imagery Metadata Local Set codec."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import IntEnum
from typing import TypeAlias

from stanag4609.errors import DecodeError
from stanag4609.imap import IMAPB, IMAPSpecialValue
from stanag4609.klv.ber import encode_ber_length, encode_ber_oid
from stanag4609.klv.local_set import parse_local_set
from stanag4609.klv.model import KLVPacket, LocalSet
from stanag4609.klv.stream import KLVStreamParser
from stanag4609.st1303 import MDAP, MDAPAlgorithm, MDAPElementType, decode_mdap, encode_mdap

SAR_MOTION_IMAGERY_LOCAL_SET_KEY = bytes.fromhex("06 0E 2B 34 02 0B 01 01 0E 01 03 03 0D 00 00 00")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MappedValue: TypeAlias = float | IMAPSpecialValue


class LookDirection(IntEnum):
    """Sensor look direction relative to the platform velocity vector."""

    LEFT = 0
    RIGHT = 1


class ImagePlane(IntEnum):
    """Plane onto which the SAR image was formed."""

    SLANT = 0
    GROUND = 1
    OTHER = 2


@dataclass(frozen=True, slots=True)
class RawSARValue:
    """Opaque value for a future ST 1206 extension tag."""

    data: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes):
            raise TypeError("RawSARValue data must be bytes")


@dataclass(frozen=True, slots=True)
class SARMotionImageryLocalSet:
    """Typed ST 1206.1 SAR Motion Imagery metadata."""

    ground_plane_squint_angle: MappedValue
    look_direction: LookDirection
    document_version: int
    grazing_angle: MappedValue | None = None
    image_plane: ImagePlane | None = None
    range_resolution: MappedValue | None = None
    cross_range_resolution: MappedValue | None = None
    range_pixel_size: MappedValue | None = None
    cross_range_pixel_size: MappedValue | None = None
    image_rows: int | None = None
    image_columns: int | None = None
    range_direction_angle: MappedValue | None = None
    true_north_direction: MappedValue | None = None
    range_layover_angle: MappedValue | None = None
    ground_aperture_angular_extent: MappedValue | None = None
    aperture_duration: int | None = None
    ground_track_angle: MappedValue | None = None
    minimum_detectable_velocity: MappedValue | None = None
    true_pulse_repetition_frequency: MappedValue | None = None
    pulse_repetition_frequency_scale_factor: MappedValue | None = None
    transmit_rf_center_frequency: MappedValue | None = None
    transmit_rf_bandwidth: MappedValue | None = None
    radar_cross_section_scale_factor_polynomial: MDAP | None = None
    reference_frame_timestamp: datetime | None = None
    reference_frame_grazing_angle: MappedValue | None = None
    reference_frame_ground_plane_squint_angle: MappedValue | None = None
    reference_frame_range_direction_angle: MappedValue | None = None
    reference_frame_range_layover_angle: MappedValue | None = None
    extensions: Mapping[int, RawSARValue] = field(default_factory=dict)
    packet: KLVPacket | None = field(default=None, compare=False, repr=False)
    local_set: LocalSet | None = field(default=None, compare=False, repr=False)
    standalone: bool = field(default=True, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.look_direction, LookDirection):
            raise TypeError("look_direction must be a LookDirection")
        if self.image_plane is not None and not isinstance(self.image_plane, ImagePlane):
            raise ValueError("ST 1206 Image Plane must be an ImagePlane")
        if isinstance(self.document_version, bool) or not isinstance(self.document_version, int):
            raise TypeError("ST 1206 Document Version must be an integer")
        if not 1 <= self.document_version <= 255:
            raise ValueError("ST 1206 Document Version must be between 1 and 255")
        _validate_model(self, error_type=ValueError)

    def radar_cross_section_scale_factor(self, row: float, column: float) -> float:
        """Evaluate the bivariate ST 1206 RCS scale-factor polynomial."""
        row_value = _pixel_coordinate(row, name="row", dimension=self.image_rows)
        column_value = _pixel_coordinate(
            column,
            name="column",
            dimension=self.image_columns,
        )
        polynomial = self.radar_cross_section_scale_factor_polynomial
        if polynomial is None:
            raise LookupError("ST 1206 has no radar cross section polynomial")
        _validate_polynomial(polynomial, error_type=ValueError)
        result = 0.0
        columns = polynomial.dimensions[1]
        for index, coefficient in enumerate(polynomial.elements):
            if isinstance(coefficient, IMAPSpecialValue):
                raise ValueError("ST 1206 polynomial contains a non-numeric IMAP value")
            row_power, column_power = divmod(index, columns)
            result += (
                float(coefficient)
                * row_value**row_power
                * column_value**column_power
            )
        if not math.isfinite(result):
            raise ValueError("ST 1206 radar cross section scale factor is not finite")
        return result

    def radar_cross_section(
        self,
        row: float,
        column: float,
        *,
        pixel_power: float,
    ) -> float:
        """Return target RCS in square metres using ST 1206 Equation 20."""

        if isinstance(pixel_power, bool) or not isinstance(pixel_power, (int, float)):
            raise TypeError("ST 1206 pixel power must be numeric")
        power = float(pixel_power)
        if not math.isfinite(power) or power < 0:
            raise ValueError("ST 1206 pixel power must be finite and non-negative")
        result = self.radar_cross_section_scale_factor(row, column) * power
        if not math.isfinite(result):
            raise ValueError("ST 1206 radar cross section is not finite")
        return result

    def effective_pulse_repetition_frequency(self) -> float:
        """Return effective PRF in hertz using ST 1206 Equation 18."""

        frequency = self.true_pulse_repetition_frequency
        scale = self.pulse_repetition_frequency_scale_factor
        if frequency is None:
            raise LookupError("ST 1206 has no true pulse repetition frequency")
        if scale is None:
            raise LookupError("ST 1206 has no pulse repetition frequency scale factor")
        if isinstance(frequency, IMAPSpecialValue) or isinstance(scale, IMAPSpecialValue):
            raise ValueError("ST 1206 effective PRF requires numeric IMAP values")
        return float(frequency) * float(scale)


def _pixel_coordinate(value: float, *, name: str, dimension: int | None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"ST 1206 pixel {name} must be numeric")
    coordinate = float(value)
    if not math.isfinite(coordinate) or coordinate < 0:
        raise ValueError(f"ST 1206 pixel {name} must be finite and non-negative")
    if dimension is not None and coordinate >= dimension:
        raise ValueError(
            f"ST 1206 pixel {name} {coordinate} is outside image dimension {dimension}"
        )
    return coordinate


_MAPPED_FIELDS = {
    1: ("grazing_angle", "Grazing Angle", 0.0, 90.0, 2),
    2: ("ground_plane_squint_angle", "Ground Plane Squint Angle", -90.0, 90.0, 2),
    5: ("range_resolution", "Range Resolution", 0.0, 1_000_000.0, 4),
    6: ("cross_range_resolution", "Cross-Range Resolution", 0.0, 1_000_000.0, 4),
    7: ("range_pixel_size", "Range Image Plane Pixel Size", 0.0, 1_000_000.0, 4),
    8: ("cross_range_pixel_size", "Cross-Range Image Plane Pixel Size", 0.0, 1_000_000.0, 4),
    11: ("range_direction_angle", "Range Direction Angle", 0.0, 360.0, 2),
    12: ("true_north_direction", "True North Direction", 0.0, 360.0, 2),
    13: ("range_layover_angle", "Range Layover Angle", 0.0, 360.0, 2),
    14: ("ground_aperture_angular_extent", "Ground Aperture Angular Extent", 0.0, 90.0, 2),
    16: ("ground_track_angle", "Ground Track Angle", 0.0, 360.0, 2),
    17: ("minimum_detectable_velocity", "Minimum Detectable Velocity", 0.0, 100.0, 2),
    18: ("true_pulse_repetition_frequency", "True Pulse Repetition Frequency", 0.0, 1_000_000.0, 4),
    19: ("pulse_repetition_frequency_scale_factor", "PRF Scale Factor", 0.0, 1.0, 2),
    20: (
        "transmit_rf_center_frequency",
        "Transmit RF Center Frequency",
        0.0,
        1_000_000_000_000.0,
        4,
    ),
    21: ("transmit_rf_bandwidth", "Transmit RF Bandwidth", 0.0, 100_000_000_000.0, 4),
    24: ("reference_frame_grazing_angle", "Reference Frame Grazing Angle", 0.0, 90.0, 2),
    25: (
        "reference_frame_ground_plane_squint_angle",
        "Reference Frame Ground Plane Squint Angle",
        -90.0,
        90.0,
        2,
    ),
    26: (
        "reference_frame_range_direction_angle",
        "Reference Frame Range Direction Angle",
        0.0,
        360.0,
        2,
    ),
    27: (
        "reference_frame_range_layover_angle",
        "Reference Frame Range Layover Angle",
        0.0,
        360.0,
        2,
    ),
}
_UINT_FIELDS = {
    9: ("image_rows", "Image Rows", 2),
    10: ("image_columns", "Image Columns", 2),
    15: ("aperture_duration", "Aperture Duration", 4),
    28: ("document_version", "Document Version", 1),
}


def _validate_polynomial(polynomial: MDAP, *, error_type: type[Exception]) -> None:
    if not isinstance(polynomial, MDAP):
        raise TypeError("ST 1206 RCS polynomial must be an MDAP")
    if len(polynomial.dimensions) != 2:
        raise error_type("ST 1206 RCS polynomial must be two-dimensional")
    if polynomial.algorithm is not MDAPAlgorithm.IMAP or (
        polynomial.element_type is not MDAPElementType.IMAP
    ):
        raise error_type("ST 1206 RCS polynomial must use the IMAP algorithm")
    if polynomial.imap_bounds is None or polynomial.imap_bounds != (0.0, 1_000_000.0):
        raise error_type("ST 1206 RCS polynomial requires IMAP bounds 0 to 1,000,000")


def _validate_model(value: SARMotionImageryLocalSet, *, error_type: type[Exception]) -> None:
    for attribute, name, minimum, maximum, _length in _MAPPED_FIELDS.values():
        item = getattr(value, attribute)
        if item is None or isinstance(item, IMAPSpecialValue):
            continue
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise TypeError(f"ST 1206 {name} must be numeric")
        if not minimum <= float(item) <= maximum:
            raise error_type(f"ST 1206 {name} is outside [{minimum}, {maximum}]")
    for attribute, name, length in _UINT_FIELDS.values():
        item = getattr(value, attribute)
        if item is None:
            continue
        if isinstance(item, bool) or not isinstance(item, int):
            raise TypeError(f"ST 1206 {name} must be an integer")
        if not 0 <= item < 1 << (8 * length):
            raise error_type(f"ST 1206 {name} does not fit {length} bytes")
    if value.reference_frame_timestamp is not None:
        _encode_timestamp(value.reference_frame_timestamp)
    if value.radar_cross_section_scale_factor_polynomial is not None:
        _validate_polynomial(
            value.radar_cross_section_scale_factor_polynomial,
            error_type=error_type,
        )


def _decode_mapped(data: bytes, tag: int) -> MappedValue:
    _attribute, _name, minimum, maximum, length = _MAPPED_FIELDS[tag]
    if len(data) != length:
        words = {1: "one byte", 2: "two bytes", 4: "four bytes"}
        raise DecodeError(f"ST 1206 Item {tag} must contain {words[length]}")
    return IMAPB(minimum, maximum, length).decode(data)


def _decode_uint(data: bytes, tag: int) -> int:
    _attribute, _name, length = _UINT_FIELDS[tag]
    if len(data) != length:
        raise DecodeError(f"ST 1206 Item {tag} must contain {length} bytes")
    return int.from_bytes(data, "big")


def _decode_timestamp(data: bytes) -> datetime:
    if len(data) != 8:
        raise DecodeError("ST 1206 Reference Frame Precision Time Stamp must contain eight bytes")
    return _EPOCH + timedelta(microseconds=int.from_bytes(data, "big"))


def _encode_timestamp(value: datetime) -> bytes:
    if not isinstance(value, datetime):
        raise TypeError("ST 1206 reference frame timestamp must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("ST 1206 reference frame timestamp must be timezone-aware")
    microseconds = int((value.astimezone(timezone.utc) - _EPOCH) / timedelta(microseconds=1))
    if not 0 <= microseconds < 1 << 64:
        raise ValueError("ST 1206 reference frame timestamp is outside the uint64 range")
    return microseconds.to_bytes(8, "big")


def _parse_packet(data: bytes) -> KLVPacket:
    parser = KLVStreamParser(key_prefix=SAR_MOTION_IMAGERY_LOCAL_SET_KEY)
    packets = parser.feed(data)
    packets.extend(parser.finish())
    if len(packets) != 1:
        raise DecodeError(f"expected one ST 1206 packet, observed {len(packets)}")
    return packets[0]


def decode_sar_motion_imagery_local_set(
    data: bytes | KLVPacket,
    *,
    standalone: bool = True,
) -> SARMotionImageryLocalSet:
    """Decode a standalone Universal ST 1206 packet or embedded Local Set value."""
    if not isinstance(standalone, bool):
        raise TypeError("standalone must be a boolean")
    if standalone:
        if not isinstance(data, (bytes, KLVPacket)):
            raise TypeError("standalone ST 1206 data must be bytes or a KLVPacket")
        packet = data if isinstance(data, KLVPacket) else _parse_packet(data)
        if packet.key != SAR_MOTION_IMAGERY_LOCAL_SET_KEY:
            raise DecodeError("unexpected Universal Key for ST 1206 SAR Local Set")
        local_set = parse_local_set(packet.value)
    else:
        if not isinstance(data, bytes):
            raise TypeError("embedded ST 1206 data must be bytes")
        packet = None
        local_set = parse_local_set(data)
    seen: set[int] = set()
    values: dict[str, object] = {}
    extensions: dict[int, RawSARValue] = {}
    for item in local_set.items:
        if item.tag in seen:
            raise DecodeError(f"duplicate ST 1206 tag {item.tag}")
        seen.add(item.tag)
        if item.tag in _MAPPED_FIELDS:
            values[_MAPPED_FIELDS[item.tag][0]] = _decode_mapped(item.value, item.tag)
        elif item.tag in _UINT_FIELDS:
            values[_UINT_FIELDS[item.tag][0]] = _decode_uint(item.value, item.tag)
        elif item.tag == 3:
            if len(item.value) != 1:
                raise DecodeError("ST 1206 Look Direction must contain one byte")
            try:
                values["look_direction"] = LookDirection(item.value[0])
            except ValueError as error:
                raise DecodeError("unknown ST 1206 Look Direction") from error
        elif item.tag == 4:
            if len(item.value) != 1:
                raise DecodeError("ST 1206 Image Plane must contain one byte")
            try:
                values["image_plane"] = ImagePlane(item.value[0])
            except ValueError as error:
                raise DecodeError("unknown ST 1206 Image Plane") from error
        elif item.tag == 22:
            polynomial = decode_mdap(item.value)
            _validate_polynomial(polynomial, error_type=DecodeError)
            values["radar_cross_section_scale_factor_polynomial"] = polynomial
        elif item.tag == 23:
            values["reference_frame_timestamp"] = _decode_timestamp(item.value)
        else:
            extensions[item.tag] = RawSARValue(item.value)
    required = {2, 3, 28}
    if not required.issubset(seen):
        missing = ", ".join(str(tag) for tag in sorted(required - seen))
        raise DecodeError(f"ST 1206 is missing mandatory Item(s): {missing}")
    try:
        return SARMotionImageryLocalSet(
            **values,  # type: ignore[arg-type]
            extensions=extensions,
            packet=packet,
            local_set=local_set,
            standalone=standalone,
        )
    except ValueError as error:
        raise DecodeError(str(error)) from error


def _item(tag: int, data: bytes) -> bytes:
    return encode_ber_oid(tag) + encode_ber_length(len(data)) + data


def encode_sar_motion_imagery_local_set(
    value: SARMotionImageryLocalSet,
    *,
    standalone: bool = True,
    preserve: bool = False,
) -> bytes:
    """Encode an ST 1206.1 standalone packet or embedded Local Set value."""
    if not isinstance(value, SARMotionImageryLocalSet):
        raise TypeError("value must be a SARMotionImageryLocalSet")
    if not isinstance(standalone, bool) or not isinstance(preserve, bool):
        raise TypeError("standalone and preserve must be booleans")
    if preserve and value.local_set is not None and value.standalone == standalone:
        if standalone:
            assert value.packet is not None
            return value.packet.raw
        return value.local_set.raw
    _validate_model(value, error_type=ValueError)
    encoded: list[bytes] = []
    for tag in range(1, 29):
        if tag in _MAPPED_FIELDS:
            attribute, _name, minimum, maximum, length = _MAPPED_FIELDS[tag]
            item_value = getattr(value, attribute)
            if item_value is not None:
                encoded.append(_item(tag, IMAPB(minimum, maximum, length).encode(item_value)))
        elif tag in _UINT_FIELDS:
            attribute, _name, length = _UINT_FIELDS[tag]
            item_value = getattr(value, attribute)
            if item_value is not None:
                encoded.append(_item(tag, item_value.to_bytes(length, "big")))
        elif tag == 3:
            encoded.append(_item(tag, bytes((int(value.look_direction),))))
        elif tag == 4 and value.image_plane is not None:
            encoded.append(_item(tag, bytes((int(value.image_plane),))))
        elif tag == 22 and value.radar_cross_section_scale_factor_polynomial is not None:
            encoded.append(
                _item(tag, encode_mdap(value.radar_cross_section_scale_factor_polynomial))
            )
        elif tag == 23 and value.reference_frame_timestamp is not None:
            encoded.append(_item(tag, _encode_timestamp(value.reference_frame_timestamp)))
    for tag in sorted(value.extensions):
        if isinstance(tag, bool) or not isinstance(tag, int):
            raise TypeError("ST 1206 extension tags must be integers")
        if tag <= 28:
            raise ValueError("ST 1206 extension tags must be greater than 28")
        extension = value.extensions[tag]
        if not isinstance(extension, RawSARValue):
            raise TypeError(f"ST 1206 extension tag {tag} requires RawSARValue")
        encoded.append(_item(tag, extension.data))
    local_value = b"".join(encoded)
    if not standalone:
        decode_sar_motion_imagery_local_set(local_value, standalone=False)
        return local_value
    packet = SAR_MOTION_IMAGERY_LOCAL_SET_KEY + encode_ber_length(len(local_value)) + local_value
    decode_sar_motion_imagery_local_set(packet)
    return packet
