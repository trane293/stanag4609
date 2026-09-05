"""Typed, lossless decoding for MISB ST 0903.6 VMTI metadata."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import IntEnum
from fractions import Fraction
from typing import Any

from stanag4609.errors import ChecksumError, DecodeError, NeedMoreData, TruncatedData
from stanag4609.imap import IMAPB, IMAPSpecialValue
from stanag4609.klv.ber import (
    decode_ber_length,
    decode_ber_oid,
    encode_ber_length,
    encode_ber_oid,
)
from stanag4609.klv.checksum import running_sum_16
from stanag4609.klv.local_set import parse_local_set
from stanag4609.klv.model import KLVPacket, LocalSet, LocalSetItem
from stanag4609.klv.stream import KLVStreamParser
from stanag4609.st0903_geo import (
    Acceleration,
    Location,
    ResolvedVMTITargetLocation,
    Velocity,
    decode_acceleration,
    decode_boundary_series,
    decode_location,
    decode_location_series,
    decode_velocity,
    encode_acceleration,
    encode_boundary_series,
    encode_location,
    encode_location_series,
    encode_velocity,
    resolve_vtarget_location,
    validate_boundary_geometry,
)
from stanag4609.st0903_tracker import (
    VTrackerLocalSet,
    decode_vtracker_local_set,
    encode_vtracker_local_set,
)
from stanag4609.st0903_vocab import (
    AlgorithmLocalSet,
    OntologyEntityResolution,
    OntologyLocalSet,
    OntologyResolver,
    PixelRun,
    RawVMTIValue,
    VChipLocalSet,
    VFeatureLocalSet,
    VMaskLocalSet,
    VObjectLocalSet,
    _decode_algorithm_series,
    _decode_ontology_series,
    _decode_vchip_series,
    _decode_vobject_series,
    _encode_algorithm_series,
    _encode_ontology_series,
    _encode_vchip_series,
    _encode_vobject_series,
    decode_algorithm_local_set,
    decode_ontology_local_set,
    decode_vchip_local_set,
    decode_vfeature_local_set,
    decode_vmask_local_set,
    decode_vobject_local_set,
    encode_algorithm_local_set,
    encode_ontology_local_set,
    encode_vchip_local_set,
    encode_vfeature_local_set,
    encode_vmask_local_set,
    encode_vobject_local_set,
    validate_ontology_semantics,
)
from stanag4609.st1204 import (
    MIISCoreIdentifier,
    decode_miis_core_identifier,
    encode_miis_core_identifier,
)

VMTI_KEY = bytes.fromhex("06 0E 2B 34 02 0B 01 01 0E 01 03 03 06 00 00 00")
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_VTARGET_OFFSET = IMAPB(Fraction(-96, 5), Fraction(96, 5), 3)
_VTARGET_HAE = IMAPB(-900, 19_000, 2)
_VTARGET_OFFSET_TAGS = frozenset({10, 11, 13, 14, 15, 16})

__all__ = [
    "VMTI_KEY",
    "Acceleration",
    "AlgorithmLocalSet",
    "DetectionStatus",
    "Location",
    "OntologyEntityResolution",
    "OntologyLocalSet",
    "OntologyResolver",
    "PixelRun",
    "RawVMTIValue",
    "ResolvedVMTITargetLocation",
    "VChipLocalSet",
    "VFeatureLocalSet",
    "VMTIField",
    "VMTILocalSet",
    "VMTIValidationContext",
    "VMaskLocalSet",
    "VObjectLocalSet",
    "VTarget",
    "VTargetData",
    "VTargetField",
    "VTrackerLocalSet",
    "Velocity",
    "decode_acceleration",
    "decode_algorithm_local_set",
    "decode_boundary_series",
    "decode_location",
    "decode_location_series",
    "decode_ontology_local_set",
    "decode_vchip_local_set",
    "decode_velocity",
    "decode_vfeature_local_set",
    "decode_vmask_local_set",
    "decode_vmti_local_set",
    "decode_vobject_local_set",
    "decode_vtracker_local_set",
    "encode_acceleration",
    "encode_algorithm_local_set",
    "encode_boundary_series",
    "encode_location",
    "encode_location_series",
    "encode_ontology_local_set",
    "encode_vchip_local_set",
    "encode_velocity",
    "encode_vfeature_local_set",
    "encode_vmask_local_set",
    "encode_vmti_local_set",
    "encode_vobject_local_set",
    "encode_vtarget",
    "encode_vtracker_local_set",
    "resolve_vtarget_location",
    "validate_boundary_geometry",
    "validate_ontology_semantics",
]


class DetectionStatus(IntEnum):
    """ST 0903.6 target lifecycle state."""

    INACTIVE = 0
    ACTIVE_MOVING = 1
    DROPPED = 2
    ACTIVE_STOPPED = 3
    ACTIVE_COASTING = 4


@dataclass(frozen=True, slots=True)
class VTargetData:
    """Typed input used to encode one ST 0903 VTarget Pack."""

    target_id: int
    values: Mapping[int, Any]


@dataclass(frozen=True, slots=True)
class VMTIField:
    """One recognized top-level VMTI Local Set field."""

    tag: int
    name: str
    value: Any
    raw: bytes
    item: LocalSetItem


@dataclass(frozen=True, slots=True)
class VTargetField:
    """One recognized field within a VTarget Pack."""

    tag: int
    name: str
    value: Any
    raw: bytes
    item: LocalSetItem


@dataclass(frozen=True, slots=True)
class VTarget:
    """A VTarget Pack with its identifier and lossless embedded Local Set."""

    target_id: int
    target_id_octets: bytes
    length_octets: bytes
    local_set: LocalSet
    fields: tuple[VTargetField, ...]
    raw: bytes

    def get(self, tag: int) -> VTargetField | None:
        for field in self.fields:
            if field.tag == tag:
                return field
        return None

    def value(self, tag: int, default: Any = None) -> Any:
        field = self.get(tag)
        return default if field is None else field.value


@dataclass(frozen=True, slots=True)
class VMTILocalSet:
    """Decoded standalone or ST 0601-embedded VMTI Local Set."""

    packet: KLVPacket | None
    local_set: LocalSet
    fields: tuple[VMTIField, ...]
    targets: tuple[VTarget, ...]
    standalone: bool
    algorithms: tuple[AlgorithmLocalSet, ...] = ()
    ontologies: tuple[OntologyLocalSet, ...] = ()

    def get(self, tag: int) -> VMTIField | None:
        for field in self.fields:
            if field.tag == tag:
                return field
        return None

    def value(self, tag: int, default: Any = None) -> Any:
        field = self.get(tag)
        return default if field is None else field.value


@dataclass(frozen=True, slots=True)
class VMTIValidationContext:
    """External producer/frame facts needed by conditional ST 0903 requirements.

    Integer timestamps and frame periods use microseconds. The context performs
    no clock inference: callers supply the VMTI-MI frame time, the containing
    parent time, and/or the frame period when those facts are known.
    """

    vmti_frame_timestamp: int | datetime | None = None
    parent_timestamp: int | datetime | None = None
    frame_period_microseconds: Fraction | int | float | None = None
    frame_width: int | None = None
    frame_height: int | None = None
    total_targets_detected: int | None = None
    different_image_source: bool = False
    ontology_resolver: OntologyResolver | None = None

    def __post_init__(self) -> None:
        if self.vmti_frame_timestamp is not None:
            _timestamp_microseconds(
                self.vmti_frame_timestamp,
                name="vmti_frame_timestamp",
            )
        if self.parent_timestamp is not None:
            _timestamp_microseconds(self.parent_timestamp, name="parent_timestamp")
        if self.frame_period_microseconds is not None:
            _positive_fraction(
                self.frame_period_microseconds,
                name="frame_period_microseconds",
            )
        for name, value in (
            ("frame_width", self.frame_width),
            ("frame_height", self.frame_height),
        ):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= 2**24 - 1
            ):
                raise ValueError(f"{name} must be an integer from 1 to 2^24-1 or None")
        if self.total_targets_detected is not None and (
            isinstance(self.total_targets_detected, bool)
            or not isinstance(self.total_targets_detected, int)
            or not 0 <= self.total_targets_detected <= 2**24 - 1
        ):
            raise ValueError(
                "total_targets_detected must be an integer from 0 to 2^24-1 or None"
            )
        if not isinstance(self.different_image_source, bool):
            raise TypeError("different_image_source must be boolean")
        if self.ontology_resolver is not None and not isinstance(
            self.ontology_resolver, OntologyResolver
        ):
            raise TypeError("ontology_resolver must implement resolve_entity or be None")


_VMTI_NAMES = {
    1: "checkSum",
    2: "precisionTimeStamp",
    3: "vmtiSystemName",
    4: "vmtiLsVersionNum",
    5: "totalNumTargetsDetected",
    6: "numTargetsReported",
    8: "frameWidth",
    9: "frameHeight",
    10: "vmtiSourceSensor",
    11: "vmtiHorizontalFov",
    12: "vmtiVerticalFov",
    13: "miisId",
    101: "vTargetSeries",
    102: "algorithmSeries",
    103: "ontologySeries",
}

_VTARGET_NAMES = {
    1: "targetCentroid",
    2: "boundingBoxTopLeft",
    3: "boundingBoxBottomRight",
    4: "targetPriority",
    5: "targetConfidenceLevel",
    6: "targetHistory",
    7: "percentageOfTargetPixels",
    8: "targetColor",
    9: "targetIntensity",
    10: "targetLocationOffsetLat",
    11: "targetLocationOffsetLon",
    12: "targetHae",
    13: "boundingBoxTopLeftLatOffset",
    14: "boundingBoxTopLeftLonOffset",
    15: "boundingBoxBottomRightLatOffset",
    16: "boundingBoxBottomRightLonOffset",
    17: "targetLocation",
    18: "geospatialContourSeries",
    19: "centroidPixRow",
    20: "centroidPixCol",
    22: "algorithmId",
    23: "detectionStatus",
    101: "vMask",
    104: "vTracker",
    105: "vChip",
    106: "vChipSeries",
    107: "vObjectSeries",
}


def _encode_uint(value: Any, *, name: str, minimum: int, maximum: int) -> bytes:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"ST 0903 {name} requires int")
    if not minimum <= value <= maximum:
        raise ValueError(f"ST 0903 {name} must be between {minimum} and {maximum}")
    length = max(1, (value.bit_length() + 7) // 8)
    return value.to_bytes(length, "big")


def _encode_fixed_uint(
    value: Any,
    *,
    name: str,
    length: int,
    minimum: int = 0,
    maximum: int | None = None,
) -> bytes:
    upper = (1 << (length * 8)) - 1 if maximum is None else maximum
    encoded = _encode_uint(value, name=name, minimum=minimum, maximum=upper)
    return bytes(length - len(encoded)) + encoded


def _encode_vtarget_imap(value: Any, codec: IMAPB, *, name: str) -> bytes:
    if isinstance(value, bool) or not isinstance(value, (int, float, Fraction)):
        raise TypeError(f"ST 0903 {name} requires int, float, or Fraction")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"ST 0903 {name} must be finite")
    numeric = Fraction(str(value)) if isinstance(value, float) else Fraction(value)
    if not codec.minimum <= numeric <= codec.maximum:
        raise ValueError(
            f"ST 0903 {name} must be between {float(codec.minimum)} and "
            f"{float(codec.maximum)}"
        )
    return codec.encode(numeric)


def _timestamp_microseconds(value: int | datetime, *, name: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{name} cannot be boolean")
    if isinstance(value, int):
        micros = value
    elif isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("ST 0903 precisionTimeStamp datetime must be timezone-aware")
        delta = value.astimezone(timezone.utc) - _UNIX_EPOCH
        micros = delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
    else:
        raise TypeError(f"{name} requires integer microseconds or an aware datetime")
    if not 0 <= micros <= 2**64 - 1:
        raise ValueError(f"{name} is outside the unsigned 64-bit range")
    return micros


def _encode_timestamp(value: Any) -> bytes:
    micros = _timestamp_microseconds(value, name="ST 0903 precisionTimeStamp")
    return micros.to_bytes(8, "big")


def _positive_fraction(value: Fraction | int | float, *, name: str) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (Fraction, int, float)):
        raise TypeError(f"{name} must be a Fraction, integer, or float")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
        result = Fraction(str(value))
    else:
        result = Fraction(value)
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _encode_text(value: Any, *, tag: int, max_bytes: int, max_characters: int) -> bytes:
    if not isinstance(value, str):
        raise TypeError(f"ST 0903 item {tag} requires str")
    if not value:
        raise ValueError(f"ST 0903 item {tag} must not be empty")
    if value[0] in "\x00\t\n\r " or value[-1] in "\x00\t\n\r ":
        raise ValueError(f"ST 0903 item {tag} violates ST 0107 trimmed UTF-8 rules")
    if any(
        ord(character) <= 0x08
        or ord(character) in {0x0B, 0x0C, 0x7F}
        or 0x0E <= ord(character) <= 0x1F
        for character in value
    ):
        raise ValueError(f"ST 0903 item {tag} violates ST 0107 UTF-8 control rules")
    if len(value) > max_characters:
        raise ValueError(f"ST 0903 item {tag} exceeds {max_characters} UTF-8 characters")
    encoded = value.encode("utf-8")
    if len(encoded) > max_bytes:
        raise ValueError(f"ST 0903 item {tag} exceeds {max_bytes} encoded bytes")
    return encoded


def _encode_item(tag: int, value: bytes, *, one_byte_tag: bool = False) -> bytes:
    if isinstance(tag, bool) or not isinstance(tag, int):
        raise TypeError("ST 0903 item tag must be an integer")
    maximum = 0xFF if one_byte_tag else (2**63 - 1)
    if not 1 <= tag <= maximum:
        qualifier = "one-byte " if one_byte_tag else ""
        raise ValueError(f"ST 0903 {qualifier}item tag must be between 1 and {maximum}")
    tag_octets = bytes((tag,)) if one_byte_tag else encode_ber_oid(tag)
    return tag_octets + encode_ber_length(len(value)) + value


def _encode_vtarget_value(target_id: int, tag: int, value: Any) -> bytes:
    name = _VTARGET_NAMES.get(tag, f"target item {tag}")
    if tag in {1, 2, 3}:
        return _encode_uint(value, name=name, minimum=1, maximum=2**48 - 1)
    if tag == 4:
        return _encode_fixed_uint(value, name="priority", length=1, minimum=1)
    if tag == 5:
        return _encode_fixed_uint(value, name="confidence", length=1, maximum=100)
    if tag == 6:
        return _encode_uint(value, name=name, minimum=0, maximum=0xFFFF)
    if tag == 7:
        return _encode_fixed_uint(
            value,
            name="percentageOfTargetPixels",
            length=1,
            minimum=1,
            maximum=100,
        )
    if tag == 8:
        if isinstance(value, bytes):
            encoded = value
        elif isinstance(value, tuple) and len(value) == 3:
            if any(isinstance(channel, bool) or not isinstance(channel, int) for channel in value):
                raise TypeError(f"ST 0903 target {target_id} targetColor requires three integers")
            if any(not 0 <= channel <= 255 for channel in value):
                raise ValueError(f"ST 0903 target {target_id} targetColor channels must be 0..255")
            encoded = bytes(value)
        else:
            raise TypeError(
                f"ST 0903 target {target_id} targetColor requires bytes or a three-int tuple"
            )
        if len(encoded) != 3:
            raise ValueError(f"ST 0903 target {target_id} targetColor requires 3 bytes")
        return encoded
    if tag in {9, 22}:
        return _encode_uint(value, name=name, minimum=0, maximum=2**24 - 1)
    if tag in _VTARGET_OFFSET_TAGS:
        return _encode_vtarget_imap(value, _VTARGET_OFFSET, name=name)
    if tag == 12:
        return _encode_vtarget_imap(value, _VTARGET_HAE, name=name)
    if tag == 17:
        if not isinstance(value, Location):
            raise TypeError(f"ST 0903 target {target_id} targetLocation requires Location")
        return encode_location(value)
    if tag == 18:
        if not isinstance(value, tuple) or any(
            not isinstance(location, Location) for location in value
        ):
            raise TypeError(
                f"ST 0903 target {target_id} geospatialContourSeries "
                "requires a tuple of Location values"
            )
        return encode_boundary_series(value)
    if tag in {19, 20}:
        return _encode_uint(value, name=name, minimum=1, maximum=2**32 - 1)
    if tag == 23:
        status = value.value if isinstance(value, DetectionStatus) else value
        return _encode_fixed_uint(status, name="detectionStatus", length=1, minimum=0, maximum=4)
    if tag == 101:
        if not isinstance(value, VMaskLocalSet):
            raise TypeError(f"ST 0903 target {target_id} vMask requires VMaskLocalSet")
        return encode_vmask_local_set(value)
    if tag == 104:
        if not isinstance(value, VTrackerLocalSet):
            raise TypeError(f"ST 0903 target {target_id} vTracker requires VTrackerLocalSet")
        return encode_vtracker_local_set(value)
    if tag == 105:
        if not isinstance(value, VChipLocalSet):
            raise TypeError(f"ST 0903 target {target_id} vChip requires VChipLocalSet")
        return encode_vchip_local_set(value)
    if tag == 106:
        if not isinstance(value, tuple) or any(
            not isinstance(item, VChipLocalSet) for item in value
        ):
            raise TypeError(
                f"ST 0903 target {target_id} vChipSeries requires VChipLocalSet values"
            )
        return _encode_vchip_series(value)
    if tag == 107:
        if not isinstance(value, tuple) or any(
            not isinstance(item, VObjectLocalSet) for item in value
        ):
            raise TypeError(
                f"ST 0903 target {target_id} vObjectSeries requires VObjectLocalSet values"
            )
        return _encode_vobject_series(value)
    if not isinstance(value, RawVMTIValue):
        raise TypeError(f"untyped ST 0903 target {target_id} item {tag} requires RawVMTIValue")
    return value.data


def encode_vtarget(target: VTargetData) -> bytes:
    """Encode one VTarget Pack using its BER-OID target identifier."""

    if not isinstance(target, VTargetData):
        raise TypeError("target must be VTargetData")
    target_id_octets = encode_ber_oid(target.target_id)
    if len(target_id_octets) > 9:
        raise ValueError("ST 0903 targetId must fit in at most 9 BER-OID octets")
    if not target.values:
        raise ValueError(f"ST 0903 target {target.target_id} requires at least one item")
    encoded_items = []
    for tag in sorted(target.values):
        raw = _encode_vtarget_value(target.target_id, tag, target.values[tag])
        encoded_items.append(_encode_item(tag, raw, one_byte_tag=True))

    status = target.values.get(23)
    if status is not None:
        try:
            decoded_status = DetectionStatus(status)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"ST 0903 target {target.target_id} detectionStatus must be from 0 to 4"
            ) from error
        tags = set(target.values)
        if decoded_status in {DetectionStatus.ACTIVE_MOVING, DetectionStatus.ACTIVE_STOPPED}:
            has_centroid = 1 in tags or ({19, 20} <= tags) or ({2, 3} <= tags)
            if not has_centroid:
                raise ValueError(
                    f"ST 0903 target {target.target_id} active status requires "
                    "a centroid representation"
                )
    tags = set(target.values)
    if (19 in tags) != (20 in tags):
        raise ValueError(
            f"ST 0903 target {target.target_id} centroidPixRow and centroidPixCol "
            "must both be present"
        )

    value = target_id_octets + b"".join(encoded_items)
    encoded = encode_ber_length(len(value)) + value
    # Parse our own output to keep writer and reader invariants aligned.
    try:
        _parse_target_series(encoded, max_targets=1)
    except DecodeError as error:
        raise ValueError(str(error)) from error
    return encoded


def _encode_vmti_value(tag: int, value: Any) -> bytes:
    name = _VMTI_NAMES.get(tag, f"item {tag}")
    if tag == 2:
        return _encode_timestamp(value)
    if tag == 3:
        return _encode_text(value, tag=tag, max_bytes=128, max_characters=32)
    if tag == 4:
        return _encode_uint(value, name=name, minimum=1, maximum=0xFFFF)
    if tag in {5, 6}:
        return _encode_uint(value, name=name, minimum=0, maximum=2**24 - 1)
    if tag in {8, 9}:
        return _encode_uint(value, name=name, minimum=1, maximum=2**24 - 1)
    if tag == 10:
        return _encode_text(value, tag=tag, max_bytes=512, max_characters=128)
    if tag in {11, 12}:
        if isinstance(value, bool) or not isinstance(value, (int, float, Fraction)):
            raise TypeError(f"ST 0903 {name} requires int, float, or Fraction")
        numeric = Fraction(str(value)) if isinstance(value, float) else Fraction(value)
        if not 0 <= numeric <= 180:
            raise ValueError(f"ST 0903 {name} must be between 0 and 180")
        return IMAPB(0, 180, 2).encode(numeric)
    if tag == 13:
        if isinstance(value, MIISCoreIdentifier):
            return encode_miis_core_identifier(value)
        if not isinstance(value, bytes):
            raise TypeError("ST 0903 miisId requires MIISCoreIdentifier or bytes")
        try:
            decode_miis_core_identifier(value)
        except DecodeError as error:
            raise ValueError(f"ST 0903 miisId is not conformant with ST 1204: {error}") from error
        return value
    if not isinstance(value, RawVMTIValue):
        raise TypeError(f"untyped ST 0903 item {tag} requires RawVMTIValue")
    return value.data


def encode_vmti_local_set(
    values: Mapping[int, Any],
    *,
    targets: Iterable[VTargetData] = (),
    algorithms: Iterable[AlgorithmLocalSet] = (),
    ontologies: Iterable[OntologyLocalSet] = (),
    standalone: bool = False,
    context: VMTIValidationContext | None = None,
) -> bytes:
    """Encode an embedded or standalone ST 0903 VMTI Local Set.

    The target series, reported-target count, and standalone checksum are
    structural fields owned by this function. Unknown values must use
    :class:`RawVMTIValue` so a caller cannot accidentally imply typed support.
    """

    if 1 in values:
        raise ValueError("ST 0903 checksum is owned by encode_vmti_local_set")
    if 101 in values:
        raise ValueError("ST 0903 vTargetSeries is owned by the targets argument")
    if 102 in values:
        raise ValueError("ST 0903 algorithmSeries is owned by the algorithms argument")
    if 103 in values:
        raise ValueError("ST 0903 ontologySeries is owned by the ontologies argument")
    if 4 not in values:
        raise ValueError("ST 0903 vmtiLsVersionNum (tag 4) is required")
    if context is not None and not isinstance(context, VMTIValidationContext):
        raise TypeError("context must be a VMTIValidationContext or None")
    target_items = tuple(targets)
    algorithm_items = tuple(algorithms)
    ontology_items = tuple(ontologies)
    if any(not isinstance(item, VTargetData) for item in target_items):
        raise TypeError("targets must contain only VTargetData values")
    if any(not isinstance(item, AlgorithmLocalSet) for item in algorithm_items):
        raise TypeError("algorithms must contain only AlgorithmLocalSet values")
    if any(not isinstance(item, OntologyLocalSet) for item in ontology_items):
        raise TypeError("ontologies must contain only OntologyLocalSet values")
    _validate_vocabulary(
        target_items,
        algorithm_items,
        ontology_items,
        error_type=ValueError,
        ontology_resolver=context.ontology_resolver if context else None,
    )
    _validate_target_geospatial_context(target_items, standalone=standalone, error_type=ValueError)
    _validate_frame_width_requirement(
        target_items,
        values.get(8),
        error_type=ValueError,
    )
    _validate_external_context(
        values,
        target_items,
        standalone=standalone,
        context=context,
        error_type=ValueError,
    )
    frame_width = _frame_dimension(values.get(8), context.frame_width if context else None)
    frame_height = _frame_dimension(values.get(9), context.frame_height if context else None)
    _validate_target_pixels(
        target_items,
        frame_width,
        frame_height,
        error_type=ValueError,
    )
    _validate_target_masks(
        target_items,
        frame_width,
        frame_height,
        error_type=ValueError,
    )
    encoded_targets = tuple(encode_vtarget(target) for target in target_items)
    if 6 in values and values[6] != len(encoded_targets):
        raise ValueError("ST 0903 numTargetsReported must equal the number of supplied targets")

    effective = dict(values)
    effective[6] = len(encoded_targets)
    output_tags = set(effective)
    if encoded_targets:
        output_tags.add(101)
    if algorithm_items:
        output_tags.add(102)
    if ontology_items:
        output_tags.add(103)
    ordered_tags = [2] if 2 in output_tags else []
    ordered_tags.extend(sorted(tag for tag in output_tags if tag != 2))
    encoded_items: list[bytes] = []
    for tag in ordered_tags:
        raw = (
            b"".join(encoded_targets)
            if tag == 101
            else _encode_algorithm_series(algorithm_items)
            if tag == 102
            else _encode_ontology_series(ontology_items)
            if tag == 103
            else _encode_vmti_value(tag, effective[tag])
        )
        encoded_items.append(_encode_item(tag, raw))

    local_value = b"".join(encoded_items)
    if not standalone:
        decode_vmti_local_set(local_value, standalone=False)
        return local_value

    for required_tag, required_name in (
        (2, "precisionTimeStamp"),
        (11, "vmtiHorizontalFov"),
        (12, "vmtiVerticalFov"),
        (13, "miisId"),
    ):
        if required_tag not in effective:
            raise ValueError(f"standalone ST 0903 {required_name} is required")
    local_value += _encode_item(1, b"\x00\x00")
    packet = VMTI_KEY + encode_ber_length(len(local_value)) + local_value
    result = packet[:-2] + running_sum_16(packet[:-2]).to_bytes(2, "big")
    decode_vmti_local_set(result)
    return result


def _require_length(item: LocalSetItem, minimum: int, maximum: int) -> None:
    if not minimum <= len(item.value) <= maximum:
        expected = str(minimum) if minimum == maximum else f"between {minimum} and {maximum}"
        raise DecodeError(
            f"ST 0903 item {item.tag} requires {expected} byte(s), observed {len(item.value)}"
        )


def _decode_uint(item: LocalSetItem, minimum: int, maximum: int) -> int:
    _require_length(item, minimum, maximum)
    if minimum != maximum and len(item.value) > 1 and item.value[0] == 0:
        raise DecodeError(
            f"ST 0903 item {item.tag} must use minimal unsigned integer encoding"
        )
    return int.from_bytes(item.value, "big")


def _decode_text(item: LocalSetItem, *, max_bytes: int, max_characters: int) -> str:
    _require_length(item, 1, max_bytes)
    try:
        value = item.value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DecodeError(f"ST 0903 item {item.tag} is not valid UTF-8") from error
    try:
        _encode_text(
            value,
            tag=item.tag,
            max_bytes=max_bytes,
            max_characters=max_characters,
        )
    except ValueError as error:
        raise DecodeError(str(error)) from error
    return value


def _decode_vmti_field(
    item: LocalSetItem,
    targets: tuple[VTarget, ...],
    algorithms: tuple[AlgorithmLocalSet, ...],
    ontologies: tuple[OntologyLocalSet, ...],
) -> VMTIField | None:
    name = _VMTI_NAMES.get(item.tag)
    if name is None:
        return None
    if item.tag == 1:
        value: Any = _decode_uint(item, 2, 2)
    elif item.tag == 2:
        micros = _decode_uint(item, 8, 8)
        try:
            value = _UNIX_EPOCH + timedelta(microseconds=micros)
        except OverflowError as error:
            raise DecodeError("ST 0903 precisionTimeStamp is outside datetime range") from error
    elif item.tag == 3:
        value = _decode_text(item, max_bytes=128, max_characters=32)
    elif item.tag == 10:
        value = _decode_text(item, max_bytes=512, max_characters=128)
    elif item.tag == 4:
        value = _decode_uint(item, 1, 2)
        if value == 0:
            raise DecodeError("ST 0903 vmtiLsVersionNum must be between 1 and 65535")
    elif item.tag in {5, 6, 8, 9}:
        value = _decode_uint(item, 1, 3)
        if item.tag in {8, 9} and value == 0:
            raise DecodeError(f"ST 0903 {name} must not be zero")
    elif item.tag in {11, 12}:
        _require_length(item, 2, 2)
        value = IMAPB(0, 180, 2).decode(item.value)
        if isinstance(value, IMAPSpecialValue):
            raise DecodeError(f"ST 0903 {name} does not permit IMAP special values")
    elif item.tag == 13:
        value = decode_miis_core_identifier(item.value)
    elif item.tag == 101:
        value = targets
    elif item.tag == 102:
        value = algorithms
    elif item.tag == 103:
        value = ontologies
    else:
        value = item.value
    return VMTIField(item.tag, name, value, item.value, item)


def _decode_vtarget_field(item: LocalSetItem, target_id: int) -> VTargetField | None:
    name = _VTARGET_NAMES.get(item.tag)
    if name is None:
        return None
    if item.tag in {1, 2, 3}:
        value: Any = _decode_uint(item, 1, 6)
        if value == 0:
            raise DecodeError(f"ST 0903 target {target_id} {name} must be positive")
    elif item.tag in {19, 20}:
        value = _decode_uint(item, 1, 4)
        if value == 0:
            raise DecodeError(f"ST 0903 target {target_id} {name} must be positive")
    elif item.tag in {4, 5, 7, 23}:
        value = _decode_uint(item, 1, 1)
        if item.tag == 4 and value == 0:
            raise DecodeError(f"ST 0903 target {target_id} priority must be positive")
        if item.tag == 5 and value > 100:
            raise DecodeError(f"ST 0903 target {target_id} confidence must be from 0 to 100")
        if item.tag == 7 and not 1 <= value <= 100:
            raise DecodeError(
                f"ST 0903 target {target_id} percentageOfTargetPixels must be from 1 to 100"
            )
        if item.tag == 23:
            try:
                value = DetectionStatus(value)
            except ValueError as error:
                raise DecodeError(
                    f"ST 0903 target {target_id} detectionStatus must be from 0 to 4"
                ) from error
    elif item.tag == 6:
        value = _decode_uint(item, 1, 2)
    elif item.tag == 8:
        _require_length(item, 3, 3)
        value = tuple(item.value)
    elif item.tag in {9, 22}:
        value = _decode_uint(item, 1, 3)
    elif item.tag in _VTARGET_OFFSET_TAGS:
        value = _decode_vtarget_imap(item, _VTARGET_OFFSET, name=name)
    elif item.tag == 12:
        value = _decode_vtarget_imap(item, _VTARGET_HAE, name=name)
    elif item.tag == 17:
        value = decode_location(item.value)
    elif item.tag == 18:
        value = decode_boundary_series(item.value)
    elif item.tag == 101:
        value = decode_vmask_local_set(item.value)
    elif item.tag == 104:
        value = decode_vtracker_local_set(item.value)
    elif item.tag == 105:
        value = decode_vchip_local_set(item.value)
    elif item.tag == 106:
        value = _decode_vchip_series(item.value)
    elif item.tag == 107:
        value = _decode_vobject_series(item.value)
    else:
        value = item.value
    return VTargetField(item.tag, name, value, item.value, item)


def _decode_vtarget_imap(item: LocalSetItem, codec: IMAPB, *, name: str) -> Any:
    _require_length(item, codec.length, codec.length)
    value = codec.decode(item.value)
    if isinstance(value, IMAPSpecialValue):
        raise DecodeError(f"ST 0903 {name} does not permit IMAP special values")
    return value


def _validate_vtarget(target: VTarget) -> None:
    tags = {item.tag for item in target.local_set.items}
    if (19 in tags) != (20 in tags):
        raise DecodeError(
            f"ST 0903 target {target.target_id} centroidPixRow and centroidPixCol "
            "must both be present"
        )
    status = target.value(23)
    if status in {DetectionStatus.ACTIVE_MOVING, DetectionStatus.ACTIVE_STOPPED}:
        has_centroid = 1 in tags or ({19, 20} <= tags) or ({2, 3} <= tags)
        if not has_centroid:
            raise DecodeError(
                f"ST 0903 target {target.target_id} active status requires "
                "a centroid representation"
            )


def _validate_target_geospatial_context(
    targets: tuple[VTargetData, ...] | tuple[VTarget, ...],
    *,
    standalone: bool,
    error_type: type[Exception],
) -> None:
    if not standalone:
        return
    for target in targets:
        if isinstance(target, VTargetData):
            tags = set(target.values)
            status = target.values.get(23)
        else:
            tags = {item.tag for item in target.local_set.items}
            status = target.value(23)
        if tags & _VTARGET_OFFSET_TAGS:
            raise error_type(
                f"standalone ST 0903 target {target.target_id} cannot contain parent-relative "
                "offset Items 10-11 and 13-16"
            )
        if status in {
            DetectionStatus.ACTIVE_MOVING,
            DetectionStatus.ACTIVE_STOPPED,
            DetectionStatus.ACTIVE_COASTING,
            1,
            3,
            4,
        } and 17 not in tags:
            raise error_type(
                f"standalone ST 0903 target {target.target_id} active target requires "
                "targetLocation Item 17"
            )


def _validate_vocabulary(
    targets: tuple[VTargetData, ...] | tuple[VTarget, ...],
    algorithms: tuple[AlgorithmLocalSet, ...],
    ontologies: tuple[OntologyLocalSet, ...],
    *,
    error_type: type[Exception],
    ontology_resolver: OntologyResolver | None = None,
) -> None:
    algorithm_ids = {item.algorithm_id for item in algorithms}
    if len(algorithm_ids) != len(algorithms):
        raise error_type("ST 0903 Algorithm Series requires unique algorithm identifiers")
    ontology_ids = {item.ontology_id for item in ontologies}
    if len(ontology_ids) != len(ontologies):
        raise error_type("ST 0903 Ontology Series requires unique ontology identifiers")
    for ontology in ontologies:
        if ontology.parent_id is not None and ontology.parent_id not in ontology_ids:
            raise error_type(
                f"ST 0903 Ontology parentId {ontology.parent_id} is not in the Ontology Series"
            )
    if ontology_resolver is not None:
        try:
            validate_ontology_semantics(ontologies, ontology_resolver)
        except (TypeError, ValueError) as error:
            raise error_type(str(error)) from error
    for target in targets:
        if isinstance(target, VTargetData):
            algorithm_id = target.values.get(22)
            tracker = target.values.get(104)
            objects = target.values.get(107, ())
        else:
            algorithm_id = target.value(22)
            tracker = target.value(104)
            objects = target.value(107, ())
        if algorithm_id is not None and algorithm_id not in algorithm_ids:
            raise error_type(
                f"ST 0903 target {target.target_id} algorithmId {algorithm_id} "
                "is not in the Algorithm Series"
            )
        if (
            isinstance(tracker, VTrackerLocalSet)
            and tracker.algorithm_id is not None
            and tracker.algorithm_id not in algorithm_ids
        ):
            raise error_type(
                f"ST 0903 target {target.target_id} VTracker algorithmId "
                f"{tracker.algorithm_id} is not in the Algorithm Series"
            )
        for object_value in objects:
            if object_value.ontology_id not in ontology_ids:
                raise error_type(
                    f"ST 0903 VObject ontologyId {object_value.ontology_id} "
                    "is not in the Ontology Series"
                )
            for feature in object_value.features:
                if feature.ontology_id not in ontology_ids:
                    raise error_type(
                        f"ST 0903 VFeature ontologyId {feature.ontology_id} "
                        "is not in the Ontology Series"
                    )


def _validate_target_masks(
    targets: tuple[VTargetData, ...] | tuple[VTarget, ...],
    frame_width: int | None,
    frame_height: int | None,
    *,
    error_type: type[Exception],
) -> None:
    if frame_width is None or frame_height is None:
        return
    for target in targets:
        mask = target.values.get(101) if isinstance(target, VTargetData) else target.value(101)
        if not isinstance(mask, VMaskLocalSet):
            continue
        try:
            mask.validate_for_frame(frame_width, frame_height)
        except (TypeError, ValueError) as error:
            raise error_type(f"ST 0903 target {target.target_id}: {error}") from error


def _frame_dimension(declared: object | None, external: int | None) -> int | None:
    if isinstance(declared, int) and not isinstance(declared, bool) and declared > 0:
        return declared
    return external


def _validate_target_pixels(
    targets: tuple[VTargetData, ...] | tuple[VTarget, ...],
    frame_width: int | None,
    frame_height: int | None,
    *,
    error_type: type[Exception],
) -> None:
    pixel_limit = (
        frame_width * frame_height
        if frame_width is not None and frame_height is not None
        else None
    )
    pixel_names = {
        1: "targetCentroid",
        2: "boundingBoxTopLeft",
        3: "boundingBoxBottomRight",
    }
    for target in targets:
        value = target.values.get if isinstance(target, VTargetData) else target.value
        if pixel_limit is not None:
            for tag, name in pixel_names.items():
                pixel = value(tag)
                if isinstance(pixel, int) and not isinstance(pixel, bool) and pixel > pixel_limit:
                    raise error_type(
                        f"ST 0903 target {target.target_id} {name} pixel {pixel} is outside "
                        f"the {frame_width}x{frame_height} frame"
                    )

        row = value(19)
        if (
            frame_height is not None
            and isinstance(row, int)
            and not isinstance(row, bool)
            and row > frame_height
        ):
            raise error_type(
                f"ST 0903 target {target.target_id} centroidPixRow {row} is outside "
                f"the {frame_height}-row frame"
            )
        column = value(20)
        if (
            frame_width is not None
            and isinstance(column, int)
            and not isinstance(column, bool)
            and column > frame_width
        ):
            raise error_type(
                f"ST 0903 target {target.target_id} centroidPixCol {column} is outside "
                f"the {frame_width}-column frame"
            )

        top_left = value(2)
        bottom_right = value(3)
        if (
            frame_width is not None
            and isinstance(top_left, int)
            and not isinstance(top_left, bool)
            and isinstance(bottom_right, int)
            and not isinstance(bottom_right, bool)
        ):
            top_index = top_left - 1
            bottom_index = bottom_right - 1
            top_row, top_column = divmod(top_index, frame_width)
            bottom_row, bottom_column = divmod(bottom_index, frame_width)
            if top_row > bottom_row or top_column > bottom_column:
                raise error_type(
                    f"ST 0903 target {target.target_id} bounding-box top-left must not be "
                    "below or right of its bottom-right"
                )


def _validate_frame_width_requirement(
    targets: tuple[VTargetData, ...] | tuple[VTarget, ...],
    frame_width: object | None,
    *,
    error_type: type[Exception],
) -> None:
    for target in targets:
        if isinstance(target, VTargetData):
            tags = set(target.values)
            mask = target.values.get(101)
        else:
            tags = {item.tag for item in target.local_set.items}
            mask = target.value(101)
        if tags & {1, 2, 3} or isinstance(mask, VMaskLocalSet):
            if frame_width is None:
                raise error_type(
                    "ST 0903 VMTI frameWidth Item 8 is required when a target "
                    "uses a pixel-number representation"
                )
            return


def _validate_external_context(
    values: Mapping[int, Any],
    targets: tuple[VTargetData, ...] | tuple[VTarget, ...],
    *,
    standalone: bool,
    context: VMTIValidationContext | None,
    error_type: type[Exception],
) -> None:
    if context is None:
        return
    reported_targets = len(targets)
    declared_total = values.get(5)
    if context.total_targets_detected is not None:
        if declared_total is not None and declared_total != context.total_targets_detected:
            raise error_type(
                "ST 0903 totalNumTargetsDetected does not match the processing-model "
                "target count"
            )
        if (
            context.total_targets_detected != reported_targets
            and declared_total is None
        ):
            raise error_type(
                "ST 0903 totalNumTargetsDetected Item 5 is required when the processing-model "
                "target count differs from numTargetsReported"
            )
    child_timestamp = (
        _timestamp_microseconds(values[2], name="ST 0903 precisionTimeStamp")
        if 2 in values
        else None
    )
    frame_timestamp = (
        _timestamp_microseconds(
            context.vmti_frame_timestamp,
            name="vmti_frame_timestamp",
        )
        if context.vmti_frame_timestamp is not None
        else None
    )
    parent_timestamp = (
        _timestamp_microseconds(context.parent_timestamp, name="parent_timestamp")
        if context.parent_timestamp is not None
        else None
    )
    if frame_timestamp is not None:
        if child_timestamp is not None and child_timestamp != frame_timestamp:
            raise error_type(
                "ST 0903 precisionTimeStamp must equal the VMTI-MI frame timestamp"
            )
        if (
            not standalone
            and parent_timestamp is not None
            and parent_timestamp != frame_timestamp
            and child_timestamp is None
        ):
            raise error_type(
                "embedded ST 0903 requires precisionTimeStamp when the parent and "
                "VMTI-MI frame timestamps differ"
            )
    if context.different_image_source:
        for tag, name in (
            (11, "vmtiHorizontalFov"),
            (12, "vmtiVerticalFov"),
            (13, "miisId"),
        ):
            if tag not in values:
                raise error_type(
                    f"ST 0903 {name} is required when VMTI-MI differs from user-MI"
                )
    for tag, expected, name in (
        (8, context.frame_width, "frameWidth"),
        (9, context.frame_height, "frameHeight"),
    ):
        if expected is not None and tag in values and values[tag] != expected:
            raise error_type(
                f"ST 0903 {name} does not match the VMTI-MI frame dimension"
            )
    reference_timestamp = frame_timestamp if frame_timestamp is not None else child_timestamp
    if (
        parent_timestamp is not None
        and reference_timestamp is not None
        and context.frame_period_microseconds is not None
        and abs(reference_timestamp - parent_timestamp)
        > 2
        * _positive_fraction(
            context.frame_period_microseconds,
            name="frame_period_microseconds",
        )
    ):
        for target in targets:
            tags = (
                set(target.values)
                if isinstance(target, VTargetData)
                else {item.tag for item in target.local_set.items}
            )
            if tags & _VTARGET_OFFSET_TAGS:
                raise error_type(
                    "ST 0903 parent-relative target offsets are prohibited when "
                    "VMTI-MI timing differs from the parent by more than two frames"
                )


def _parse_target_series(data: bytes, *, max_targets: int) -> tuple[VTarget, ...]:
    targets: list[VTarget] = []
    target_ids: set[int] = set()
    cursor = 0
    while cursor < len(data):
        if len(targets) >= max_targets:
            raise DecodeError(f"ST 0903 target series exceeds configured maximum {max_targets}")
        pack_start = cursor
        try:
            pack_length, length_used = decode_ber_length(data, cursor, max_value=len(data) - cursor)
        except NeedMoreData as error:
            raise TruncatedData("truncated ST 0903 VTarget Pack length") from error
        cursor += length_used
        pack_end = cursor + pack_length
        if pack_end > len(data):
            raise TruncatedData(
                f"ST 0903 VTarget Pack declares {pack_length} bytes, "
                f"only {len(data) - cursor} remain"
            )
        try:
            target_id, id_used = decode_ber_oid(data, cursor, max_octets=9)
        except NeedMoreData as error:
            raise TruncatedData("truncated ST 0903 VTarget targetId") from error
        if id_used >= pack_length:
            raise DecodeError(f"ST 0903 target {target_id} requires at least one TLV item")
        if target_id in target_ids:
            raise DecodeError(f"ST 0903 targetId {target_id} occurs twice in one series")
        target_ids.add(target_id)
        target_id_octets = data[cursor : cursor + id_used]
        embedded = data[cursor + id_used : pack_end]
        local_set = parse_local_set(embedded)
        seen: set[int] = set()
        for item in local_set.items:
            if len(item.tag_octets) != 1:
                raise DecodeError("ST 0903 embedded VTarget items require one-byte UINT tags")
            if item.tag in seen:
                raise DecodeError(f"ST 0903 target {target_id} item {item.tag} occurs twice")
            seen.add(item.tag)
        fields = tuple(
            field
            for item in local_set.items
            if (field := _decode_vtarget_field(item, target_id)) is not None
        )
        target = VTarget(
            target_id,
            target_id_octets,
            data[pack_start : pack_start + length_used],
            local_set,
            fields,
            data[pack_start:pack_end],
        )
        _validate_vtarget(target)
        targets.append(target)
        cursor = pack_end
    if not targets:
        raise DecodeError("ST 0903 vTargetSeries must contain at least one VTarget Pack")
    return tuple(targets)


def _parse_single_packet(data: bytes) -> KLVPacket:
    parser = KLVStreamParser(key_prefix=VMTI_KEY, max_value_length=64 * 1024 * 1024)
    packets = parser.feed(data)
    packets.extend(parser.finish())
    if len(packets) != 1:
        raise DecodeError(f"expected exactly one ST 0903 packet, observed {len(packets)}")
    return packets[0]


def decode_vmti_local_set(
    data: bytes | KLVPacket,
    *,
    standalone: bool | None = None,
    verify_checksum: bool = True,
    max_targets: int = 100_000,
    context: VMTIValidationContext | None = None,
) -> VMTILocalSet:
    """Decode one standalone Universal KLV or embedded VMTI Local Set value."""

    if max_targets <= 0:
        raise ValueError("max_targets must be positive")
    if context is not None and not isinstance(context, VMTIValidationContext):
        raise TypeError("context must be a VMTIValidationContext or None")
    if isinstance(data, KLVPacket):
        packet = data
        inferred_standalone = True
    elif standalone is True or (standalone is None and data.startswith(VMTI_KEY)):
        packet = _parse_single_packet(data)
        inferred_standalone = True
    else:
        packet = None
        inferred_standalone = False
    is_standalone = inferred_standalone if standalone is None else standalone
    if is_standalone and packet is None:
        raise DecodeError("standalone VMTI requires a Universal KLV packet")
    if packet is not None and packet.key != VMTI_KEY:
        raise DecodeError(f"unexpected Universal Key {packet.key.hex(' ')} for ST 0903 VMTI")

    raw_value = packet.value if packet is not None else bytes(data)
    local_set = parse_local_set(raw_value)
    if not local_set.items:
        raise DecodeError("ST 0903 VMTI Local Set is empty")
    seen: set[int] = set()
    for item in local_set.items:
        if item.tag in seen:
            raise DecodeError(f"ST 0903 VMTI item {item.tag} occurs twice")
        seen.add(item.tag)

    timestamp = local_set.getone(2)
    if timestamp is not None and local_set.items[0].tag != 2:
        raise DecodeError("ST 0903 precisionTimeStamp must be the first item")
    if local_set.getone(4) is None:
        raise DecodeError("ST 0903 vmtiLsVersionNum (version) is required")
    reported_item = local_set.getone(6)
    if reported_item is None:
        raise DecodeError("ST 0903 numTargetsReported is required")

    checksum = local_set.getone(1)
    if is_standalone:
        assert packet is not None
        if checksum is None or local_set.items[-1].tag != 1 or len(checksum.value) != 2:
            raise ChecksumError("standalone ST 0903 checksum must be the final 2-byte item")
        if verify_checksum:
            expected = int.from_bytes(checksum.value, "big")
            observed = running_sum_16(packet.raw[:-2])
            if observed != expected:
                raise ChecksumError(
                    f"ST 0903 checksum mismatch: expected 0x{expected:04X}, "
                    f"computed 0x{observed:04X}"
                )
        for required_tag, required_name in (
            (2, "precisionTimeStamp"),
            (11, "vmtiHorizontalFov"),
            (12, "vmtiVerticalFov"),
            (13, "miisId"),
        ):
            if local_set.getone(required_tag) is None:
                raise DecodeError(f"standalone ST 0903 {required_name} is required")
    elif checksum is not None:
        raise ChecksumError("embedded ST 0903 checksum must be omitted")

    target_item = local_set.getone(101)
    targets = (
        _parse_target_series(target_item.value, max_targets=max_targets)
        if target_item is not None
        else ()
    )
    _validate_target_geospatial_context(targets, standalone=is_standalone, error_type=DecodeError)
    reported = _decode_uint(reported_item, 1, 3)
    if reported != len(targets):
        raise DecodeError(
            f"ST 0903 numTargetsReported reports {reported}, "
            f"but vTargetSeries contains {len(targets)}"
        )

    algorithm_item = local_set.getone(102)
    algorithms = (
        _decode_algorithm_series(algorithm_item.value) if algorithm_item is not None else ()
    )
    ontology_item = local_set.getone(103)
    ontologies = _decode_ontology_series(ontology_item.value) if ontology_item is not None else ()
    _validate_vocabulary(
        targets,
        algorithms,
        ontologies,
        error_type=DecodeError,
        ontology_resolver=context.ontology_resolver if context else None,
    )
    width_item = local_set.getone(8)
    height_item = local_set.getone(9)
    frame_width = _decode_uint(width_item, 1, 3) if width_item is not None else None
    _validate_frame_width_requirement(
        targets,
        frame_width,
        error_type=DecodeError,
    )
    decoded_context_values: dict[int, Any] = {}
    for tag in (2, 5, 8, 9, 11, 12, 13):
        context_item = local_set.getone(tag)
        if context_item is None:
            continue
        if tag == 2:
            decoded_context_values[tag] = _decode_uint(context_item, 8, 8)
        elif tag in {5, 8, 9}:
            decoded_context_values[tag] = _decode_uint(context_item, 1, 3)
        else:
            decoded_context_values[tag] = context_item.value
    _validate_external_context(
        decoded_context_values,
        targets,
        standalone=is_standalone,
        context=context,
        error_type=DecodeError,
    )
    frame_height = _decode_uint(height_item, 1, 3) if height_item is not None else None
    effective_width = _frame_dimension(frame_width, context.frame_width if context else None)
    effective_height = _frame_dimension(frame_height, context.frame_height if context else None)
    _validate_target_pixels(
        targets,
        effective_width,
        effective_height,
        error_type=DecodeError,
    )
    _validate_target_masks(
        targets,
        effective_width,
        effective_height,
        error_type=DecodeError,
    )

    fields = tuple(
        field
        for item in local_set.items
        if (field := _decode_vmti_field(item, targets, algorithms, ontologies)) is not None
    )
    return VMTILocalSet(
        packet=packet,
        local_set=local_set,
        fields=fields,
        targets=targets,
        standalone=is_standalone,
        algorithms=algorithms,
        ontologies=ontologies,
    )
