"""Typed, lossless decoding for the MISB ST 0601 UAS Local Set."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum, IntEnum, IntFlag
from fractions import Fraction
from types import MappingProxyType
from typing import Any, Literal
from uuid import UUID

from stanag4609.errors import ChecksumError, DecodeError, NeedMoreData
from stanag4609.imap import IMAPB, IMAPSpecialValue, imapa_length
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
from stanag4609.st0102 import SecurityLocalSet, decode_security_local_set
from stanag4609.st0806 import RVTLocalSet, decode_rvt_local_set
from stanag4609.st0903 import (
    VMTILocalSet,
    VMTIValidationContext,
    decode_vmti_local_set,
)
from stanag4609.st1002 import (
    RangeImageLocalSet,
    decode_range_image_local_set,
    encode_range_image_local_set,
)
from stanag4609.st1010 import SDCCFLP, decode_sdcc_flp, encode_sdcc_flp
from stanag4609.st1204 import (
    MIISCoreIdentifier,
    decode_miis_core_identifier,
    encode_miis_core_identifier,
)
from stanag4609.st1206 import (
    SARMotionImageryLocalSet,
    decode_sar_motion_imagery_local_set,
    encode_sar_motion_imagery_local_set,
)
from stanag4609.st1601 import (
    GeoRegistrationLocalSet,
    decode_geo_registration_local_set,
    encode_geo_registration_local_set,
)
from stanag4609.st1602 import (
    CompositeImagingLocalSet,
    decode_composite_imaging_local_set,
    encode_composite_imaging_local_set,
)

ST0601_KEY = bytes.fromhex("06 0E 2B 34 02 0B 01 01 0E 01 03 01 01 00 00 00")
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _timestamp_microseconds(value: int | datetime, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, datetime)):
        raise TypeError(f"{name} requires integer microseconds or an aware datetime")
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} datetime must be timezone-aware")
        delta = value.astimezone(timezone.utc) - _UNIX_EPOCH
        micros = delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
    else:
        micros = value
    if not 0 <= micros <= 2**64 - 1:
        raise ValueError(f"{name} is outside the unsigned 64-bit range")
    return micros


def _time_adjustment(
    value: int,
    *,
    name: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} is outside [{minimum}, {maximum}]")
    return value


def _epoch_datetime(microseconds: int, *, name: str) -> datetime:
    try:
        return _UNIX_EPOCH + timedelta(microseconds=microseconds)
    except OverflowError as error:
        raise ValueError(f"{name} is outside Python's datetime range") from error


def misp_timestamp_to_utc(
    timestamp: int | datetime,
    *,
    leap_seconds: int,
    correction_offset: int = 0,
) -> datetime:
    """Convert an ST 0601/MISP Precision Time Stamp coordinate to UTC.

    ST 0601 Item 2 is a continuous MISP Time System count, not a UTC/POSIX
    timestamp. Item 137 is added to that count before Item 136's accumulated
    leap-second offset is removed. A :class:`datetime` input is interpreted as
    the existing decoded *coordinate representation* of the MISP count; its
    ``timezone.utc`` marker does not make the unadjusted value UTC.
    """

    misp_microseconds = _timestamp_microseconds(
        timestamp,
        name="MISP Precision Time Stamp",
    )
    leaps = _time_adjustment(
        leap_seconds,
        name="leap_seconds",
        minimum=-(2**31),
        maximum=2**31 - 1,
    )
    correction = _time_adjustment(
        correction_offset,
        name="correction_offset",
        minimum=-(2**63),
        maximum=2**63 - 1,
    )
    utc_microseconds = misp_microseconds + correction - leaps * 1_000_000
    return _epoch_datetime(utc_microseconds, name="converted UTC timestamp")


def utc_to_misp_timestamp(
    timestamp: datetime,
    *,
    leap_seconds: int,
    correction_offset: int = 0,
) -> int:
    """Convert an aware UTC datetime to ST 0601 Item 2 microseconds.

    This is the inverse of :func:`misp_timestamp_to_utc` for a caller-supplied
    leap-second value. The returned integer is suitable for
    :func:`encode_uas_local_set`; include the same leap-second value as Item 136
    when the receiver must perform the conversion without an external table.
    """

    if not isinstance(timestamp, datetime):
        raise TypeError("UTC timestamp must be a datetime")
    utc_microseconds = _timestamp_microseconds(timestamp, name="UTC timestamp")
    leaps = _time_adjustment(
        leap_seconds,
        name="leap_seconds",
        minimum=-(2**31),
        maximum=2**31 - 1,
    )
    correction = _time_adjustment(
        correction_offset,
        name="correction_offset",
        minimum=-(2**63),
        maximum=2**63 - 1,
    )
    misp_microseconds = utc_microseconds + leaps * 1_000_000 - correction
    if not 0 <= misp_microseconds <= 2**64 - 1:
        raise ValueError("converted MISP timestamp is outside the unsigned 64-bit range")
    return misp_microseconds

# ST 0601.19 Table 1 entries marked "Y" in the SDCC column. These are the
# only individual UAS Local Set items that may form an ST 1010 Refined Source
# List immediately before Item 102.
ST0601_SDCC_SOURCE_TAGS = frozenset(
    {
        *range(5, 23),
        50,
        51,
        52,
        64,
        75,
        79,
        80,
        90,
        91,
        92,
        93,
        96,
        104,
        113,
        114,
        117,
        118,
        119,
    }
)


class SpecialValue(Enum):
    """Non-numeric MISB values that must remain semantically distinct."""

    UNKNOWN = "unknown"
    OUT_OF_RANGE = "out_of_range"
    OFF_EARTH = "off_earth"
    ERROR = "error"
    RESERVED = "reserved"


class IcingDetected(IntEnum):
    """ST 0601 Item 34 icing-detector state."""

    DETECTOR_OFF = 0
    NO_ICING_DETECTED = 1
    ICING_DETECTED = 2


class GenericFlagData(IntFlag):
    """ST 0601 Item 47 aircraft and image Boolean flags."""

    LASER_RANGE = 1 << 0
    AUTO_TRACK = 1 << 1
    IR_BLACK_HOT = 1 << 2
    ICING_DETECTED = 1 << 3
    SLANT_RANGE_MEASURED = 1 << 4
    IMAGE_INVALID = 1 << 5


class SensorFieldOfViewName(IntEnum):
    """ST 0601 Item 63 generic sensor lens selection."""

    ULTRANARROW = 0
    NARROW = 1
    MEDIUM = 2
    WIDE = 3
    ULTRAWIDE = 4
    NARROW_MEDIUM = 5
    TWO_X_ULTRANARROW = 6
    FOUR_X_ULTRANARROW = 7
    CONTINUOUS_ZOOM = 8


class OperationalMode(IntEnum):
    """ST 0601 Item 77 motion-imagery operational category."""

    OTHER = 0
    OPERATIONAL = 1
    TRAINING = 2
    EXERCISE = 3
    MAINTENANCE = 4
    TEST = 5


class PositioningMethodSource(IntFlag):
    """ST 0601 Item 124 navigation-source bit set."""

    INS = 1 << 0
    GPS = 1 << 1
    GALILEO = 1 << 2
    QZSS = 1 << 3
    NAVIC = 1 << 4
    GLONASS = 1 << 5
    BEIDOU_1 = 1 << 6
    BEIDOU_2 = 1 << 7


class PlatformStatus(IntEnum):
    """ST 0601 Item 125 platform flight-lifecycle mode."""

    ACTIVE = 0
    PRE_FLIGHT = 1
    PRE_FLIGHT_TAXIING = 2
    RUN_UP = 3
    TAKE_OFF = 4
    INGRESS = 5
    MANUAL_OPERATION = 6
    AUTOMATED_ORBIT = 7
    TRANSITIONING = 8
    EGRESS = 9
    LANDING = 10
    LANDED_TAXIING = 11
    LANDED_PARKED = 12


class SensorControlMode(IntEnum):
    """ST 0601 Item 126 sensor-control operational status."""

    OFF = 0
    HOME_POSITION = 1
    UNCONTROLLED = 2
    MANUAL_CONTROL = 3
    CALIBRATING = 4
    AUTO_HOLDING_POSITION = 5
    AUTO_TRACKING = 6


class WeaponLoad(int):
    """ST 0601 Item 60 packed station, substation, type, and variant."""

    def __new__(cls, value: int) -> WeaponLoad:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("Weapon Load must be an integer")
        if not 0 <= value <= 0xFFFF:
            raise ValueError("Weapon Load must fit in two bytes")
        return int.__new__(cls, value)

    @classmethod
    def from_components(
        cls,
        station_number: int,
        substation_number: int,
        weapon_type: int,
        weapon_variant: int,
    ) -> WeaponLoad:
        components = (
            station_number,
            substation_number,
            weapon_type,
            weapon_variant,
        )
        if any(
            isinstance(component, bool) or not isinstance(component, int)
            for component in components
        ):
            raise TypeError("Weapon Load components must be integers")
        if any(not 0 <= component <= 0xF for component in components):
            raise ValueError("Weapon Load components must be four-bit integers")
        return cls(
            station_number << 12
            | substation_number << 8
            | weapon_type << 4
            | weapon_variant
        )

    @property
    def station_number(self) -> int:
        return (self >> 12) & 0xF

    @property
    def substation_number(self) -> int:
        return (self >> 8) & 0xF

    @property
    def weapon_type(self) -> int:
        return (self >> 4) & 0xF

    @property
    def weapon_variant(self) -> int:
        return self & 0xF


class WeaponFired(int):
    """ST 0601 Item 61 packed released-weapon station and substation."""

    def __new__(cls, value: int) -> WeaponFired:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("Weapon Fired must be an integer")
        if not 0 <= value <= 0xFF:
            raise ValueError("Weapon Fired must fit in one byte")
        return int.__new__(cls, value)

    @classmethod
    def from_components(cls, station_number: int, substation_number: int) -> WeaponFired:
        components = (station_number, substation_number)
        if any(
            isinstance(component, bool) or not isinstance(component, int)
            for component in components
        ):
            raise TypeError("Weapon Fired components must be integers")
        if any(not 0 <= component <= 0xF for component in components):
            raise ValueError("Weapon Fired components must be four-bit integers")
        return cls(station_number << 4 | substation_number)

    @property
    def station_number(self) -> int:
        return (self >> 4) & 0xF

    @property
    def substation_number(self) -> int:
        return self & 0xF


class LaserPRFCode(int):
    """ST 0601 Item 62 three/four-digit laser code using digits 1 through 8."""

    def __new__(cls, value: int) -> LaserPRFCode:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("Laser PRF Code must be an integer")
        digits = str(value)
        if len(digits) not in {3, 4} or any(digit not in "12345678" for digit in digits):
            raise ValueError(
                "Laser PRF Code must have three or four digits, each from 1 through 8"
            )
        return int.__new__(cls, value)


_SEMANTIC_INTEGER_TYPES: Mapping[int, type[int]] = MappingProxyType(
    {
        34: IcingDetected,
        47: GenericFlagData,
        60: WeaponLoad,
        61: WeaponFired,
        62: LaserPRFCode,
        63: SensorFieldOfViewName,
        77: OperationalMode,
        124: PositioningMethodSource,
        125: PlatformStatus,
        126: SensorControlMode,
    }
)


class UpdateAction(Enum):
    """Sentinel actions accepted by lossless Local Set updates."""

    DELETE = "delete"


class FieldDecodingMode(Enum):
    """Policy for malformed values carried by otherwise valid known fields."""

    STRICT = "strict"
    PRESERVE = "preserve"


class ST0601Semantic(Enum):
    """Logical values that have multiple ST 0601 wire representations."""

    PLATFORM_PITCH = "platform_pitch"
    PLATFORM_ROLL = "platform_roll"
    PLATFORM_ANGLE_OF_ATTACK = "platform_angle_of_attack"
    PLATFORM_SIDESLIP = "platform_sideslip"
    TARGET_WIDTH = "target_width"
    DENSITY_ALTITUDE = "density_altitude"
    SENSOR_HEIGHT = "sensor_height"
    ALTERNATE_PLATFORM_HEIGHT = "alternate_platform_height"
    FRAME_CENTER_HEIGHT = "frame_center_height"


class VerticalDatum(Enum):
    """Vertical reference used to interpret an ST 0601 elevation value."""

    MSL = "msl"
    HAE = "hae"


@dataclass(frozen=True, slots=True)
class RepresentationPreference:
    """Normative preferred-to-legacy tag order for one logical value."""

    semantic: ST0601Semantic
    tags: tuple[int, ...]
    requirement_ids: tuple[str, ...]


ST0601_REPRESENTATION_PREFERENCES: Mapping[
    ST0601Semantic, RepresentationPreference
] = MappingProxyType(
    {
        ST0601Semantic.PLATFORM_PITCH: RepresentationPreference(
            ST0601Semantic.PLATFORM_PITCH, (90, 6), ("ST 0601.8-16",)
        ),
        ST0601Semantic.PLATFORM_ROLL: RepresentationPreference(
            ST0601Semantic.PLATFORM_ROLL, (91, 7), ("ST 0601.8-16",)
        ),
        ST0601Semantic.PLATFORM_ANGLE_OF_ATTACK: RepresentationPreference(
            ST0601Semantic.PLATFORM_ANGLE_OF_ATTACK,
            (92, 50),
            ("ST 0601.8-16",),
        ),
        ST0601Semantic.PLATFORM_SIDESLIP: RepresentationPreference(
            ST0601Semantic.PLATFORM_SIDESLIP, (93, 52), ("ST 0601.8-16",)
        ),
        ST0601Semantic.TARGET_WIDTH: RepresentationPreference(
            ST0601Semantic.TARGET_WIDTH,
            (96, 22),
            ("ST 0601.9-20", "ST 0601.9-21"),
        ),
        ST0601Semantic.DENSITY_ALTITUDE: RepresentationPreference(
            ST0601Semantic.DENSITY_ALTITUDE,
            (103, 38),
            ("ST 0601.9-20", "ST 0601.9-21"),
        ),
        ST0601Semantic.SENSOR_HEIGHT: RepresentationPreference(
            ST0601Semantic.SENSOR_HEIGHT,
            (104, 75, 15),
            ("ST 0601.8-17", "ST 0601.9-20", "ST 0601.9-21"),
        ),
        ST0601Semantic.ALTERNATE_PLATFORM_HEIGHT: RepresentationPreference(
            ST0601Semantic.ALTERNATE_PLATFORM_HEIGHT,
            (105, 76, 69),
            ("ST 0601.8-17", "ST 0601.9-20", "ST 0601.9-21"),
        ),
        ST0601Semantic.FRAME_CENTER_HEIGHT: RepresentationPreference(
            ST0601Semantic.FRAME_CENTER_HEIGHT, (78, 25), ("ST 0601.8-17",)
        ),
    }
)


DELETE = UpdateAction.DELETE


@dataclass(frozen=True, slots=True)
class ST0601FieldExpectation:
    """Producer-supplied expected value for one decoded ST 0601 field."""

    value: Any
    absolute_tolerance: Fraction | int | float | None = None

    def __post_init__(self) -> None:
        tolerance = self.absolute_tolerance
        if tolerance is None:
            return
        if isinstance(tolerance, bool) or not isinstance(
            tolerance, (Fraction, int, float)
        ):
            raise TypeError("absolute_tolerance must be numeric or None")
        try:
            normalized = Fraction(tolerance)
        except (OverflowError, ValueError) as error:
            raise ValueError("absolute_tolerance must be finite") from error
        if normalized < 0:
            raise ValueError("absolute_tolerance must be non-negative")
        object.__setattr__(self, "absolute_tolerance", normalized)

    def matches(self, observed: object) -> bool:
        """Return whether a decoded value satisfies this expectation."""

        tolerance = self.absolute_tolerance
        if tolerance is None:
            return bool(observed == self.value)
        if isinstance(observed, bool) or isinstance(self.value, bool):
            return False
        if not isinstance(observed, (Fraction, int, float)) or not isinstance(
            self.value, (Fraction, int, float)
        ):
            return False
        try:
            difference = abs(Fraction(observed) - Fraction(self.value))
        except (OverflowError, ValueError):
            return False
        return difference <= tolerance


@dataclass(frozen=True, slots=True)
class ST0601ValidationContext:
    """External facts required to validate conditional ST 0601 semantics.

    ``metadata_birth_timestamp`` is the producer-known time of birth shared by
    all metadata carried in one UAS Datalink Local Set instance. Integer values
    are MISP Time System microseconds since the epoch. An aware ``datetime`` is
    accepted as a coordinate representation for that continuous count; use
    :func:`utc_to_misp_timestamp` before encoding a civil UTC instant.
    ``imap_system_precisions`` maps
    variable-length IMAP tags to the producer precision each value must retain.
    ``vmti_context`` supplies facts about the imagery processed by an embedded
    Item 74 VMTI set; its parent timestamp is checked against and then derived
    from the enclosing ST 0601 Item 2. ``field_expectations`` supplies
    authoritative producer or test-harness values for singleton root fields;
    explicit tolerances account for mapped-value quantization.
    """

    metadata_birth_timestamp: int | datetime | None = None
    imap_system_precisions: Mapping[int, int | float | Fraction] = field(
        default_factory=dict
    )
    vmti_context: VMTIValidationContext | None = None
    field_expectations: Mapping[int, ST0601FieldExpectation] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        if self.metadata_birth_timestamp is not None:
            _timestamp_microseconds(
                self.metadata_birth_timestamp,
                name="metadata_birth_timestamp",
            )
        if not isinstance(self.imap_system_precisions, Mapping):
            raise TypeError("imap_system_precisions must be a mapping")
        if self.vmti_context is not None and not isinstance(
            self.vmti_context, VMTIValidationContext
        ):
            raise TypeError("vmti_context must be a VMTIValidationContext or None")
        if not isinstance(self.field_expectations, Mapping):
            raise TypeError("field_expectations must be a mapping")
        expectations = dict(self.field_expectations)
        for tag, expectation in expectations.items():
            if isinstance(tag, bool) or not isinstance(tag, int):
                raise TypeError("ST 0601 field expectation tag must be an integer")
            definition = FIELD_DEFINITIONS.get(tag)
            if definition is None or definition.multiple or tag in {1, 143}:
                raise ValueError(
                    f"ST 0601 tag {tag} is not a known singleton field expectation"
                )
            if not isinstance(expectation, ST0601FieldExpectation):
                raise TypeError(
                    "field_expectations values must be ST0601FieldExpectation instances"
                )
        precisions = dict(self.imap_system_precisions)
        for tag, precision in precisions.items():
            if isinstance(tag, bool) or not isinstance(tag, int):
                raise TypeError("ST 0601 IMAP precision tag must be an integer")
            definition = FIELD_DEFINITIONS.get(tag)
            if definition is None or definition.kind != "imap":
                raise ValueError(f"ST 0601 tag {tag} is not a variable-length IMAP field")
            assert definition.physical_min is not None
            assert definition.physical_max is not None
            required = imapa_length(
                definition.physical_min,
                definition.physical_max,
                precision,
            )
            maximum_length = definition.maximum_length or 8
            if required > maximum_length:
                raise ValueError(
                    f"requested precision needs {required} bytes but ST 0601 tag {tag} "
                    f"allows at most {maximum_length}"
                )
        object.__setattr__(self, "imap_system_precisions", MappingProxyType(precisions))
        object.__setattr__(self, "field_expectations", MappingProxyType(expectations))

    def required_imap_length(self, tag: int) -> int | None:
        """Return the shortest wire length for one configured IMAP tag."""

        if isinstance(tag, bool) or not isinstance(tag, int):
            raise TypeError("ST 0601 IMAP precision tag must be an integer")
        precision = self.imap_system_precisions.get(tag)
        if precision is None:
            return None
        definition = FIELD_DEFINITIONS[tag]
        assert definition.physical_min is not None
        assert definition.physical_max is not None
        return imapa_length(
            definition.physical_min,
            definition.physical_max,
            precision,
        )


@dataclass(frozen=True, slots=True)
class RawFieldValue:
    """Explicit wire value for adding or replacing an untyped extension tag."""

    data: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes):
            raise TypeError("RawFieldValue data must be bytes")


@dataclass(frozen=True, slots=True)
class FieldDefinition:
    tag: int
    name: str
    kind: Literal[
        "uint",
        "sint",
        "mapped",
        "imap",
        "vmti",
        "miis",
        "security",
        "rvt",
        "range_image",
        "geo_registration",
        "composite_imaging",
        "sar",
        "sdcc",
        "segment",
        "amend",
        "horizon",
        "frame_rate",
        "control_command",
        "command_verification",
        "active_wavelengths",
        "country_codes",
        "wavelengths",
        "airbase_locations",
        "payload_list",
        "active_payloads",
        "weapons_stores",
        "waypoint_list",
        "view_domain",
        "text",
        "timestamp",
    ]
    length: int | None
    units: str | None = None
    signed: bool = False
    physical_min: Fraction | None = None
    physical_max: Fraction | None = None
    special_raw: int | None = None
    special_value: SpecialValue | None = None
    integer_min: int | None = None
    integer_max: int | None = None
    maximum_length: int | None = None
    multiple: bool = False


@dataclass(frozen=True, slots=True)
class DecodedField:
    definition: FieldDefinition
    value: Any
    raw: bytes
    item: LocalSetItem


@dataclass(frozen=True, slots=True)
class ResolvedUASField:
    """One selected ST 0601 representation and lower-priority fields ignored."""

    preference: RepresentationPreference
    field: DecodedField
    ignored: tuple[DecodedField, ...] = ()

    @property
    def semantic(self) -> ST0601Semantic:
        return self.preference.semantic

    @property
    def tag(self) -> int:
        return self.field.definition.tag

    @property
    def value(self) -> Any:
        return self.field.value


@dataclass(frozen=True, slots=True)
class ResolvedTargetElevation:
    """Item 42 with its receiver-visible MSL or HAE interpretation."""

    field: DecodedField
    datum: VerticalDatum | None
    frame_height: ResolvedUASField | None

    @property
    def value(self) -> Any:
        return self.field.value

    @property
    def basis_tags(self) -> tuple[int, ...]:
        if self.frame_height is None:
            return ()
        return (
            self.frame_height.tag,
            *(field.definition.tag for field in self.frame_height.ignored),
        )


@dataclass(frozen=True, slots=True)
class FieldDecodingIssue:
    """A known field that could not be typed but remains available losslessly."""

    tag: int
    name: str
    message: str
    raw: bytes
    item: LocalSetItem


@dataclass(frozen=True, slots=True)
class IMAPFieldValue:
    """A value and selected wire length for a variable-length IMAP field."""

    value: int | float | Fraction
    length: int

    @classmethod
    def for_precision(
        cls,
        tag: int,
        value: int | float | Fraction,
        precision: int | float | Fraction,
    ) -> IMAPFieldValue:
        """Select the shortest ST 0601 IMAP length meeting ``precision``.

        ST 0107.3-09 requires variable-length IMAP values to use the fewest
        bytes that preserve the producer's system precision. The ST 1201
        IMAPA length-selection process supplies that byte count, while the
        field is encoded with its ST 0601-defined IMAPB bounds.
        """

        if isinstance(tag, bool) or not isinstance(tag, int):
            raise TypeError("ST 0601 IMAP tag must be an integer")
        definition = FIELD_DEFINITIONS.get(tag)
        if definition is None or definition.kind != "imap":
            raise ValueError(f"ST 0601 tag {tag} is not a variable-length IMAP field")
        assert definition.physical_min is not None
        assert definition.physical_max is not None
        length = imapa_length(
            definition.physical_min,
            definition.physical_max,
            precision,
        )
        maximum_length = definition.maximum_length or 8
        if length > maximum_length:
            raise ValueError(
                f"requested precision needs {length} bytes but ST 0601 tag {tag} "
                f"allows at most {maximum_length}"
            )
        selected = cls(value, length)
        encode_field_value(tag, selected)
        return selected


@dataclass(frozen=True, slots=True)
class ImageHorizonPixelPack:
    """ST 0601 Item 81 horizon endpoints and optional WGS-84 coordinates.

    ``None`` means an optional trailing coordinate was omitted by truncation.
    :attr:`SpecialValue.ERROR` means the producer explicitly transmitted the
    signed-int32 minimum error indicator for that coordinate.
    """

    start_x: int
    start_y: int
    end_x: int
    end_y: int
    start_latitude: int | float | Fraction | SpecialValue | None = None
    start_longitude: int | float | Fraction | SpecialValue | None = None
    end_latitude: int | float | Fraction | SpecialValue | None = None
    end_longitude: int | float | Fraction | SpecialValue | None = None


@dataclass(frozen=True, slots=True)
class SensorFrameRatePack:
    """ST 0601 Item 127 frame-rate ratio encoded as BER-OID integers."""

    numerator: int
    denominator: int = 1

    @property
    def ratio(self) -> Fraction:
        return Fraction(self.numerator, self.denominator)

    @property
    def frames_per_second(self) -> float:
        return float(self.ratio)


@dataclass(frozen=True, slots=True)
class ControlCommand:
    """ST 0601 Item 115 command record."""

    command_id: int
    command: str
    command_time: datetime | None = None


@dataclass(frozen=True, slots=True)
class ControlCommandVerificationList:
    """ST 0601 Item 116 acknowledged Control Command identifiers."""

    command_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ActiveWavelengthList:
    """ST 0601 Item 121 identifiers of wavelengths currently in use."""

    wavelength_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class CountryCodes:
    """ST 0601 Item 122 platform and operational country-code pack."""

    coding_method: int
    overflight: str | SpecialValue
    operator: str | SpecialValue | None = None
    manufacture: str | SpecialValue | None = None


@dataclass(frozen=True, slots=True)
class WavelengthRecord:
    """One custom sensor wavelength definition from ST 0601 Item 128."""

    wavelength_id: int
    minimum_nm: int | float | Fraction
    maximum_nm: int | float | Fraction
    name: str


@dataclass(frozen=True, slots=True)
class WavelengthsList:
    """ST 0601 Item 128 list of custom wavelength definitions."""

    records: tuple[WavelengthRecord, ...]


@dataclass(frozen=True, slots=True)
class AirbaseLocation:
    """One WGS-84 airbase location with optional height above ellipsoid."""

    latitude: int | float | Fraction
    longitude: int | float | Fraction
    hae: int | float | Fraction | None = None


@dataclass(frozen=True, slots=True)
class AirbaseLocations:
    """ST 0601 Item 130 take-off and optional recovery locations."""

    takeoff: AirbaseLocation | SpecialValue
    recovery: AirbaseLocation | SpecialValue | None = None

    @property
    def effective_recovery(self) -> AirbaseLocation | SpecialValue:
        return self.takeoff if self.recovery is None else self.recovery


@dataclass(frozen=True, slots=True)
class PayloadRecord:
    """One platform payload definition from ST 0601 Item 138."""

    payload_id: int
    payload_type: int
    name: str


@dataclass(frozen=True, slots=True)
class PayloadList:
    """A complete or distributed fragment of the ST 0601 payload table."""

    total_count: int
    records: tuple[PayloadRecord, ...]


@dataclass(frozen=True, slots=True)
class ActivePayloads:
    """Payload identifiers selected in the ST 0601 Item 139 bit set."""

    payload_ids: frozenset[int]


@dataclass(frozen=True, slots=True)
class WeaponStatus:
    """Defined general and engagement bits in an Item 140 weapon status."""

    general_status: int
    fuze_enabled: bool = False
    laser_enabled: bool = False
    target_enabled: bool = False
    weapon_armed: bool = False

    @property
    def raw(self) -> int:
        return (
            self.general_status
            | (
                int(self.fuze_enabled)
                | (int(self.laser_enabled) << 1)
                | (int(self.target_enabled) << 2)
                | (int(self.weapon_armed) << 3)
            )
            << 8
        )

    @classmethod
    def from_raw(cls, value: int) -> WeaponStatus:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("Weapon Status must be an integer")
        if not 0 <= value <= 0xFFF:
            raise ValueError("Weapon Status contains reserved status bits")
        if value & 0xFF > 127:
            raise ValueError("Weapon General Status must be between 0 and 127")
        return cls(
            value & 0xFF,
            bool(value & 0x100),
            bool(value & 0x200),
            bool(value & 0x400),
            bool(value & 0x800),
        )


@dataclass(frozen=True, slots=True)
class WeaponStore:
    """One physical weapon store, its status, and display type."""

    station_id: int
    hardpoint_id: int
    carriage_id: int
    store_id: int
    status: WeaponStatus
    weapon_type: str

    @property
    def address(self) -> tuple[int, int, int, int]:
        return self.station_id, self.hardpoint_id, self.carriage_id, self.store_id


@dataclass(frozen=True, slots=True)
class WeaponsStores:
    """Distributed list fragment carried by ST 0601 Item 140."""

    records: tuple[WeaponStore, ...]


@dataclass(frozen=True, slots=True)
class WaypointInfo:
    """Control mode and creation source flags for an Item 141 waypoint."""

    manual: bool = False
    ad_hoc: bool = False

    @property
    def raw(self) -> int:
        return int(self.manual) | (int(self.ad_hoc) << 1)

    @classmethod
    def from_raw(cls, value: int) -> WaypointInfo:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("Waypoint Info Value must be an integer")
        if not 0 <= value <= 3:
            raise ValueError("reserved Info Value bits must be zero")
        return cls(manual=bool(value & 1), ad_hoc=bool(value & 2))


@dataclass(frozen=True, slots=True)
class WaypointRecord:
    """One waypoint record from a complete or distributed waypoint list."""

    waypoint_id: int
    prosecution_order: int
    info: WaypointInfo | None = None
    location: AirbaseLocation | None = None


@dataclass(frozen=True, slots=True)
class WaypointList:
    """Distributed list fragment carried by ST 0601 Item 141."""

    records: tuple[WaypointRecord, ...]


@dataclass(frozen=True, slots=True)
class ViewDomainPair:
    """Starting angle and positive angular range for one sensor axis."""

    start: int | float | Fraction
    angular_range: int | float | Fraction

    @property
    def end(self) -> float:
        return float(_as_fraction(self.start) + _as_fraction(self.angular_range))

    @property
    def normalized_end(self) -> float:
        """Return the end angle wrapped into the circular [0, 360) domain."""
        return self.end % 360


@dataclass(frozen=True, slots=True)
class ViewDomain:
    """Azimuth, elevation, and roll limits from ST 0601 Item 142."""

    azimuth: ViewDomainPair | SpecialValue | None = None
    elevation: ViewDomainPair | SpecialValue | None = None
    roll: ViewDomainPair | SpecialValue | None = None


@dataclass(frozen=True, slots=True)
class MetadataSubstreamID:
    """Local or universal identifier for an Amend/Segment metadata substream."""

    local_id: int
    universal_id: UUID | None = None


def _branch_getall(fields: tuple[DecodedField, ...], tag: int) -> tuple[DecodedField, ...]:
    return tuple(field for field in fields if field.definition.tag == tag)


def _branch_get(fields: tuple[DecodedField, ...], tag: int) -> DecodedField | None:
    matches = _branch_getall(fields, tag)
    if not matches:
        return None
    if len(matches) > 1:
        raise ValueError(f"tag {tag} occurs {len(matches)} times")
    return matches[0]


@dataclass(frozen=True, slots=True)
class SegmentLocalSet:
    """A typed ST 1607 Segment Local Set embedded in ST 0601 Item 100."""

    local_set: LocalSet
    fields: tuple[DecodedField, ...]
    substream_id: MetadataSubstreamID
    issues: tuple[FieldDecodingIssue, ...] = ()

    def getall(self, tag: int) -> tuple[DecodedField, ...]:
        return _branch_getall(self.fields, tag)

    def get(self, tag: int) -> DecodedField | None:
        return _branch_get(self.fields, tag)

    def value(self, tag: int, default: Any = None) -> Any:
        field = self.get(tag)
        return default if field is None else field.value


@dataclass(frozen=True, slots=True)
class AmendLocalSet:
    """A typed ST 1607 Amend Local Set embedded in ST 0601 Item 101."""

    local_set: LocalSet
    fields: tuple[DecodedField, ...]
    substream_id: MetadataSubstreamID
    issues: tuple[FieldDecodingIssue, ...] = ()

    def getall(self, tag: int) -> tuple[DecodedField, ...]:
        return _branch_getall(self.fields, tag)

    def get(self, tag: int) -> DecodedField | None:
        return _branch_get(self.fields, tag)

    def value(self, tag: int, default: Any = None) -> Any:
        field = self.get(tag)
        return default if field is None else field.value


@dataclass(frozen=True, slots=True)
class UASLocalSet:
    packet: KLVPacket
    local_set: LocalSet
    fields: tuple[DecodedField, ...]
    issues: tuple[FieldDecodingIssue, ...] = ()

    def getall(self, tag: int) -> tuple[DecodedField, ...]:
        return tuple(field for field in self.fields if field.definition.tag == tag)

    def get(self, tag: int) -> DecodedField | None:
        matches = self.getall(tag)
        if not matches:
            return None
        if len(matches) > 1:
            raise ValueError(f"tag {tag} occurs {len(matches)} times")
        return matches[0]

    def value(self, tag: int, default: Any = None) -> Any:
        field = self.get(tag)
        return default if field is None else field.value

    @property
    def misp_timestamp_microseconds(self) -> int | None:
        """Return the exact Item 2 MISP count, without implying UTC."""

        items = self.local_set.getall(2)
        if len(items) != 1 or len(items[0].value) != 8:
            return None
        return int.from_bytes(items[0].value, "big")

    def utc_timestamp(
        self,
        *,
        leap_seconds: int | None = None,
        correction_offset: int | None = None,
    ) -> datetime:
        """Convert Item 2 to UTC using packet Items 136/137 or overrides.

        Item 136 is optional on the wire, so conversion deliberately fails when
        neither the packet nor the caller supplies the accumulated leap-second
        value. Item 137 defaults to zero when absent. Explicit arguments take
        precedence over packet values, which lets applications use a trusted
        external leap-second table or post-flight correction.
        """

        timestamp = self.misp_timestamp_microseconds
        if timestamp is None:
            raise ValueError("ST 0601 Item 2 is unavailable or malformed")
        selected_leaps = self.value(136) if leap_seconds is None else leap_seconds
        if selected_leaps is None:
            raise ValueError(
                "leap_seconds is required when ST 0601 Item 136 is absent"
            )
        selected_correction = (
            self.value(137, 0) if correction_offset is None else correction_offset
        )
        return misp_timestamp_to_utc(
            timestamp,
            leap_seconds=selected_leaps,
            correction_offset=selected_correction,
        )

    @property
    def effective_fields(self) -> tuple[DecodedField, ...]:
        """Fields after applying ST 0601 preferred-representation rules."""

        return effective_uas_fields(self.fields)

    def preferred_field(
        self, semantic: ST0601Semantic | str
    ) -> ResolvedUASField | None:
        """Resolve one logical value to its normative preferred representation."""

        return resolve_preferred_uas_field(self.fields, semantic)

    def target_elevation(self) -> ResolvedTargetElevation | None:
        """Resolve Item 42's datum from current Items 25 and 78."""

        return resolve_target_elevation(self.fields)


def _coerce_st0601_semantic(semantic: ST0601Semantic | str) -> ST0601Semantic:
    if isinstance(semantic, ST0601Semantic):
        return semantic
    if not isinstance(semantic, str):
        raise TypeError("semantic must be an ST0601Semantic or string")
    try:
        return ST0601Semantic(semantic)
    except ValueError as error:
        raise ValueError(f"unknown ST 0601 semantic: {semantic!r}") from error


def resolve_preferred_uas_field(
    fields: Iterable[DecodedField], semantic: ST0601Semantic | str
) -> ResolvedUASField | None:
    """Select the representation a conforming ST 0601 decoder must use.

    Preference order combines the full-range, HAE, and extended-representation
    rules in ST 0601.19 Section 6.1. The returned ``ignored`` fields remain
    available for diagnostics and lossless round trips.
    """

    selected_semantic = _coerce_st0601_semantic(semantic)
    preference = ST0601_REPRESENTATION_PREFERENCES[selected_semantic]
    materialized = tuple(fields)
    matches_by_tag: dict[int, tuple[DecodedField, ...]] = {
        tag: tuple(field for field in materialized if field.definition.tag == tag)
        for tag in preference.tags
    }
    for tag, matches in matches_by_tag.items():
        if len(matches) > 1:
            raise ValueError(f"tag {tag} occurs {len(matches)} times")
    selected = next(
        (matches[0] for tag in preference.tags if (matches := matches_by_tag[tag])),
        None,
    )
    if selected is None:
        return None
    ignored = tuple(
        matches_by_tag[tag][0]
        for tag in preference.tags
        if matches_by_tag[tag] and tag != selected.definition.tag
    )
    return ResolvedUASField(preference, selected, ignored)


def resolve_target_elevation(
    fields: Iterable[DecodedField],
) -> ResolvedTargetElevation | None:
    """Interpret Item 42 using ST 0601.19 Section 8.42.1.

    Item 42 is MSL when only Item 25 is receiver-current, HAE when Item 78 is
    current, and HAE when both are current. Without either frame-height item,
    the numeric elevation is retained but its datum is intentionally unknown.
    Call this with a :class:`ReportOnChangeSnapshot` field set for sparse
    streams so ``present`` has the standard's receiver-current meaning.
    """

    materialized = tuple(fields)
    target_fields = tuple(
        field for field in materialized if field.definition.tag == 42
    )
    if not target_fields:
        return None
    if len(target_fields) > 1:
        raise ValueError(f"tag 42 occurs {len(target_fields)} times")
    frame_height = resolve_preferred_uas_field(
        materialized, ST0601Semantic.FRAME_CENTER_HEIGHT
    )
    datum = None
    if frame_height is not None:
        datum = VerticalDatum.HAE if frame_height.tag == 78 else VerticalDatum.MSL
    return ResolvedTargetElevation(target_fields[0], datum, frame_height)


def effective_uas_fields(fields: Iterable[DecodedField]) -> tuple[DecodedField, ...]:
    """Return fields with superseded scalar representations filtered out."""

    materialized = tuple(fields)
    ignored_ids: set[int] = set()
    for semantic in ST0601_REPRESENTATION_PREFERENCES:
        resolved = resolve_preferred_uas_field(materialized, semantic)
        if resolved is not None:
            ignored_ids.update(id(field) for field in resolved.ignored)
    return tuple(field for field in materialized if id(field) not in ignored_ids)


def _bind_sdcc_source_tags(
    local_set: LocalSet, fields: tuple[DecodedField, ...]
) -> tuple[DecodedField, ...]:
    """Validate Item 102 adjacency and attach its parent Refined Source List."""
    bound = list(fields)
    field_indexes = {id(field.item): index for index, field in enumerate(fields)}
    for item_index, item in enumerate(local_set.items):
        if item.tag != 102:
            continue
        field_index = field_indexes.get(id(item))
        if field_index is None:
            # Preserve-mode decoding records the malformed Item 102 as an issue.
            continue
        field = bound[field_index]
        if not isinstance(field.value, SDCCFLP):
            continue
        source_count = field.value.matrix_size
        if item_index < source_count:
            raise DecodeError(
                "ST 0601 Item 102 Refined Source List has fewer items than Matrix Size"
            )
        source_items = local_set.items[item_index - source_count : item_index]
        source_tags = tuple(source.tag for source in source_items)
        if any(tag not in ST0601_SDCC_SOURCE_TAGS for tag in source_tags):
            raise DecodeError(
                "ST 0601 Item 102 Refined Source List must contain only immediately "
                "preceding SDCC-eligible items"
            )
        bound[field_index] = replace(
            field,
            value=replace(field.value, source_tags=source_tags),
        )
    return tuple(bound)


def _mapped(
    tag: int,
    name: str,
    length: int,
    physical_min: int | Fraction,
    physical_max: int | Fraction,
    *,
    units: str,
    signed: bool = False,
    special_raw: int | None = None,
    special_value: SpecialValue | None = None,
) -> FieldDefinition:
    return FieldDefinition(
        tag,
        name,
        "mapped",
        length,
        units,
        signed,
        Fraction(physical_min),
        Fraction(physical_max),
        special_raw,
        special_value,
    )


def _integer(
    tag: int,
    name: str,
    length: int | None,
    *,
    units: str | None = None,
    signed: bool = False,
    minimum: int | None = None,
    maximum: int | None = None,
    maximum_length: int | None = None,
) -> FieldDefinition:
    return FieldDefinition(
        tag,
        name,
        "sint" if signed else "uint",
        length,
        units,
        integer_min=minimum,
        integer_max=maximum,
        maximum_length=maximum_length,
    )


def _imap(
    tag: int,
    name: str,
    physical_min: int,
    physical_max: int,
    *,
    units: str,
    maximum_length: int = 8,
) -> FieldDefinition:
    return FieldDefinition(
        tag,
        name,
        "imap",
        None,
        units,
        physical_min=Fraction(physical_min),
        physical_max=Fraction(physical_max),
        maximum_length=maximum_length,
    )


FIELD_DEFINITIONS: dict[int, FieldDefinition] = {
    1: FieldDefinition(1, "Checksum", "uint", 2),
    2: FieldDefinition(
        2,
        "Precision Time Stamp",
        "timestamp",
        8,
        "MISP microseconds since epoch",
    ),
    3: FieldDefinition(3, "Mission ID", "text", None, maximum_length=127),
    4: FieldDefinition(4, "Platform Tail Number", "text", None, maximum_length=127),
    5: _mapped(5, "Platform Heading Angle", 2, 0, 360, units="degrees"),
    6: _mapped(
        6,
        "Platform Pitch Angle",
        2,
        -20,
        20,
        units="degrees",
        signed=True,
        special_raw=-(2**15),
        special_value=SpecialValue.OUT_OF_RANGE,
    ),
    7: _mapped(
        7,
        "Platform Roll Angle",
        2,
        -50,
        50,
        units="degrees",
        signed=True,
        special_raw=-(2**15),
        special_value=SpecialValue.OUT_OF_RANGE,
    ),
    8: FieldDefinition(8, "Platform True Airspeed", "uint", 1, "m/s"),
    9: FieldDefinition(9, "Platform Indicated Airspeed", "uint", 1, "m/s"),
    10: FieldDefinition(10, "Platform Designation", "text", None, maximum_length=127),
    11: FieldDefinition(11, "Image Source Sensor", "text", None, maximum_length=127),
    12: FieldDefinition(12, "Image Coordinate System", "text", None, maximum_length=127),
    13: _mapped(
        13,
        "Sensor Latitude",
        4,
        -90,
        90,
        units="degrees",
        signed=True,
        special_raw=-(2**31),
        special_value=SpecialValue.RESERVED,
    ),
    14: _mapped(
        14,
        "Sensor Longitude",
        4,
        -180,
        180,
        units="degrees",
        signed=True,
        special_raw=-(2**31),
        special_value=SpecialValue.RESERVED,
    ),
    15: _mapped(15, "Sensor True Altitude", 2, -900, 19000, units="metres"),
    16: _mapped(16, "Sensor Horizontal Field of View", 2, 0, 180, units="degrees"),
    17: _mapped(17, "Sensor Vertical Field of View", 2, 0, 180, units="degrees"),
    18: _mapped(18, "Sensor Relative Azimuth Angle", 4, 0, 360, units="degrees"),
    19: _mapped(
        19,
        "Sensor Relative Elevation Angle",
        4,
        -180,
        180,
        units="degrees",
        signed=True,
        special_raw=-(2**31),
        special_value=SpecialValue.RESERVED,
    ),
    20: _mapped(20, "Sensor Relative Roll Angle", 4, 0, 360, units="degrees"),
    21: _mapped(21, "Slant Range", 4, 0, 5_000_000, units="metres"),
    22: _mapped(22, "Target Width", 2, 0, 10_000, units="metres"),
    23: _mapped(
        23,
        "Frame Center Latitude",
        4,
        -90,
        90,
        units="degrees",
        signed=True,
        special_raw=-(2**31),
        special_value=SpecialValue.OFF_EARTH,
    ),
    24: _mapped(
        24,
        "Frame Center Longitude",
        4,
        -180,
        180,
        units="degrees",
        signed=True,
        special_raw=-(2**31),
        special_value=SpecialValue.OFF_EARTH,
    ),
    25: _mapped(25, "Frame Center Elevation", 2, -900, 19000, units="metres"),
    26: _mapped(
        26,
        "Offset Corner Latitude Point 1",
        2,
        -Fraction(3, 40),
        Fraction(3, 40),
        units="degrees",
        signed=True,
        special_raw=-(2**15),
        special_value=SpecialValue.OFF_EARTH,
    ),
    27: _mapped(
        27,
        "Offset Corner Longitude Point 1",
        2,
        -Fraction(3, 40),
        Fraction(3, 40),
        units="degrees",
        signed=True,
        special_raw=-(2**15),
        special_value=SpecialValue.OFF_EARTH,
    ),
    28: _mapped(
        28,
        "Offset Corner Latitude Point 2",
        2,
        -Fraction(3, 40),
        Fraction(3, 40),
        units="degrees",
        signed=True,
        special_raw=-(2**15),
        special_value=SpecialValue.OFF_EARTH,
    ),
    29: _mapped(
        29,
        "Offset Corner Longitude Point 2",
        2,
        -Fraction(3, 40),
        Fraction(3, 40),
        units="degrees",
        signed=True,
        special_raw=-(2**15),
        special_value=SpecialValue.OFF_EARTH,
    ),
    30: _mapped(
        30,
        "Offset Corner Latitude Point 3",
        2,
        -Fraction(3, 40),
        Fraction(3, 40),
        units="degrees",
        signed=True,
        special_raw=-(2**15),
        special_value=SpecialValue.OFF_EARTH,
    ),
    31: _mapped(
        31,
        "Offset Corner Longitude Point 3",
        2,
        -Fraction(3, 40),
        Fraction(3, 40),
        units="degrees",
        signed=True,
        special_raw=-(2**15),
        special_value=SpecialValue.OFF_EARTH,
    ),
    32: _mapped(
        32,
        "Offset Corner Latitude Point 4",
        2,
        -Fraction(3, 40),
        Fraction(3, 40),
        units="degrees",
        signed=True,
        special_raw=-(2**15),
        special_value=SpecialValue.OFF_EARTH,
    ),
    33: _mapped(
        33,
        "Offset Corner Longitude Point 4",
        2,
        -Fraction(3, 40),
        Fraction(3, 40),
        units="degrees",
        signed=True,
        special_raw=-(2**15),
        special_value=SpecialValue.OFF_EARTH,
    ),
    34: _integer(34, "Icing Detected", 1, minimum=0, maximum=2),
    35: _mapped(35, "Wind Direction", 2, 0, 360, units="degrees"),
    36: _mapped(36, "Wind Speed", 1, 0, 100, units="m/s"),
    37: _mapped(37, "Static Pressure", 2, 0, 5000, units="mbar"),
    38: _mapped(38, "Density Altitude", 2, -900, 19000, units="metres"),
    39: _integer(39, "Outside Air Temperature", 1, units="Celsius", signed=True),
    40: _mapped(
        40,
        "Target Location Latitude",
        4,
        -90,
        90,
        units="degrees",
        signed=True,
        special_raw=-(2**31),
        special_value=SpecialValue.OFF_EARTH,
    ),
    41: _mapped(
        41,
        "Target Location Longitude",
        4,
        -180,
        180,
        units="degrees",
        signed=True,
        special_raw=-(2**31),
        special_value=SpecialValue.OFF_EARTH,
    ),
    42: _mapped(42, "Target Location Elevation", 2, -900, 19000, units="metres"),
    43: _mapped(43, "Target Track Gate Width", 1, 0, 510, units="pixels"),
    44: _mapped(44, "Target Track Gate Height", 1, 0, 510, units="pixels"),
    45: _mapped(45, "Target Error Estimate - CE90", 2, 0, 4095, units="metres"),
    46: _mapped(46, "Target Error Estimate - LE90", 2, 0, 4095, units="metres"),
    47: _integer(47, "Generic Flag Data", 1, minimum=0, maximum=63),
    48: FieldDefinition(48, "Security Local Set", "security", None),
    49: _mapped(49, "Differential Pressure", 2, 0, 5000, units="mbar"),
    50: _mapped(
        50,
        "Platform Angle of Attack",
        2,
        -20,
        20,
        units="degrees",
        signed=True,
        special_raw=-(2**15),
        special_value=SpecialValue.OUT_OF_RANGE,
    ),
    51: _mapped(
        51,
        "Platform Vertical Speed",
        2,
        -180,
        180,
        units="m/s",
        signed=True,
        special_raw=-(2**15),
        special_value=SpecialValue.OUT_OF_RANGE,
    ),
    52: _mapped(
        52,
        "Platform Sideslip Angle",
        2,
        -20,
        20,
        units="degrees",
        signed=True,
        special_raw=-(2**15),
        special_value=SpecialValue.OUT_OF_RANGE,
    ),
    53: _mapped(53, "Airfield Barometric Pressure", 2, 0, 5000, units="mbar"),
    54: _mapped(54, "Airfield Elevation", 2, -900, 19000, units="metres"),
    55: _mapped(55, "Relative Humidity", 1, 0, 100, units="percent"),
    56: _integer(56, "Platform Ground Speed", 1, units="m/s"),
    57: _mapped(57, "Ground Range", 4, 0, 5_000_000, units="metres"),
    58: _mapped(58, "Platform Fuel Remaining", 2, 0, 10_000, units="kilograms"),
    59: FieldDefinition(59, "Platform Call Sign", "text", None, maximum_length=127),
    60: _integer(60, "Weapon Load", 2),
    61: _integer(61, "Weapon Fired", 1),
    62: _integer(62, "Laser PRF Code", 2),
    # Section 8.63's format row says 0..7, but its authoritative enumerated
    # value table also defines 8 (Continuous Zoom) and reserves only 9..255.
    63: _integer(63, "Sensor Field of View Name", 1, minimum=0, maximum=8),
    64: _mapped(64, "Platform Magnetic Heading", 2, 0, 360, units="degrees"),
    65: FieldDefinition(65, "UAS Datalink LS Version Number", "uint", 1),
    67: _mapped(
        67,
        "Alternate Platform Latitude",
        4,
        -90,
        90,
        units="degrees",
        signed=True,
        special_raw=-(2**31),
        special_value=SpecialValue.RESERVED,
    ),
    68: _mapped(
        68,
        "Alternate Platform Longitude",
        4,
        -180,
        180,
        units="degrees",
        signed=True,
        special_raw=-(2**31),
        special_value=SpecialValue.RESERVED,
    ),
    69: _mapped(69, "Alternate Platform Altitude", 2, -900, 19000, units="metres"),
    70: FieldDefinition(70, "Alternate Platform Name", "text", None, maximum_length=127),
    71: _mapped(71, "Alternate Platform Heading", 2, 0, 360, units="degrees"),
    72: FieldDefinition(72, "Event Start Time", "timestamp", 8, "microseconds UTC"),
    73: FieldDefinition(73, "RVT Local Set", "rvt", None),
    74: FieldDefinition(74, "VMTI Local Set", "vmti", None),
    75: _mapped(75, "Sensor Ellipsoid Height", 2, -900, 19000, units="metres"),
    76: _mapped(
        76,
        "Alternate Platform Ellipsoid Height",
        2,
        -900,
        19000,
        units="metres",
    ),
    77: _integer(77, "Operational Mode", 1, minimum=0, maximum=5),
    78: _mapped(
        78,
        "Frame Center Height Above Ellipsoid",
        2,
        -900,
        19000,
        units="metres",
    ),
    79: _mapped(
        79,
        "Sensor North Velocity",
        2,
        -327,
        327,
        units="m/s",
        signed=True,
        special_raw=-(2**15),
        special_value=SpecialValue.OUT_OF_RANGE,
    ),
    80: _mapped(
        80,
        "Sensor East Velocity",
        2,
        -327,
        327,
        units="m/s",
        signed=True,
        special_raw=-(2**15),
        special_value=SpecialValue.OUT_OF_RANGE,
    ),
    81: FieldDefinition(
        81,
        "Image Horizon Pixel Pack",
        "horizon",
        None,
        maximum_length=20,
    ),
    82: _mapped(
        82,
        "Corner Latitude Point 1 (Full)",
        4,
        -90,
        90,
        units="degrees",
        signed=True,
        special_raw=-(2**31),
        special_value=SpecialValue.OFF_EARTH,
    ),
    83: _mapped(
        83,
        "Corner Longitude Point 1 (Full)",
        4,
        -180,
        180,
        units="degrees",
        signed=True,
        special_raw=-(2**31),
        special_value=SpecialValue.OFF_EARTH,
    ),
    84: _mapped(
        84,
        "Corner Latitude Point 2 (Full)",
        4,
        -90,
        90,
        units="degrees",
        signed=True,
        special_raw=-(2**31),
        special_value=SpecialValue.OFF_EARTH,
    ),
    85: _mapped(
        85,
        "Corner Longitude Point 2 (Full)",
        4,
        -180,
        180,
        units="degrees",
        signed=True,
        special_raw=-(2**31),
        special_value=SpecialValue.OFF_EARTH,
    ),
    86: _mapped(
        86,
        "Corner Latitude Point 3 (Full)",
        4,
        -90,
        90,
        units="degrees",
        signed=True,
        special_raw=-(2**31),
        special_value=SpecialValue.OFF_EARTH,
    ),
    87: _mapped(
        87,
        "Corner Longitude Point 3 (Full)",
        4,
        -180,
        180,
        units="degrees",
        signed=True,
        special_raw=-(2**31),
        special_value=SpecialValue.OFF_EARTH,
    ),
    88: _mapped(
        88,
        "Corner Latitude Point 4 (Full)",
        4,
        -90,
        90,
        units="degrees",
        signed=True,
        special_raw=-(2**31),
        special_value=SpecialValue.OFF_EARTH,
    ),
    89: _mapped(
        89,
        "Corner Longitude Point 4 (Full)",
        4,
        -180,
        180,
        units="degrees",
        signed=True,
        special_raw=-(2**31),
        special_value=SpecialValue.OFF_EARTH,
    ),
    90: _mapped(
        90,
        "Platform Pitch Angle (Full)",
        4,
        -90,
        90,
        units="degrees",
        signed=True,
        special_raw=-(2**31),
        special_value=SpecialValue.OUT_OF_RANGE,
    ),
    91: _mapped(
        91,
        "Platform Roll Angle (Full)",
        4,
        -90,
        90,
        units="degrees",
        signed=True,
        special_raw=-(2**31),
        special_value=SpecialValue.OUT_OF_RANGE,
    ),
    92: _mapped(
        92,
        "Platform Angle of Attack (Full)",
        4,
        -90,
        90,
        units="degrees",
        signed=True,
        special_raw=-(2**31),
        special_value=SpecialValue.OUT_OF_RANGE,
    ),
    93: _mapped(
        93,
        "Platform Sideslip Angle (Full)",
        4,
        -180,
        180,
        units="degrees",
        signed=True,
        special_raw=-(2**31),
        special_value=SpecialValue.OUT_OF_RANGE,
    ),
    94: FieldDefinition(94, "MIIS Core Identifier", "miis", None, maximum_length=50),
    95: FieldDefinition(95, "SAR Motion Imagery Local Set", "sar", None),
    96: _imap(96, "Target Width Extended", 0, 1_500_000, units="metres"),
    97: FieldDefinition(97, "Range Image Local Set", "range_image", None),
    98: FieldDefinition(98, "Geo-Registration Local Set", "geo_registration", None),
    99: FieldDefinition(99, "Composite Imaging Local Set", "composite_imaging", None),
    100: FieldDefinition(100, "Segment Local Set", "segment", None, multiple=True),
    101: FieldDefinition(101, "Amend Local Set", "amend", None, multiple=True),
    102: FieldDefinition(102, "SDCC-FLP", "sdcc", None, multiple=True),
    103: _imap(103, "Density Altitude Extended", -900, 40_000, units="metres"),
    104: _imap(104, "Sensor Ellipsoid Height Extended", -900, 40_000, units="metres"),
    105: _imap(
        105,
        "Alternate Platform Ellipsoid Height Extended",
        -900,
        40_000,
        units="metres",
    ),
    106: FieldDefinition(106, "Stream Designator", "text", None, maximum_length=127),
    107: FieldDefinition(107, "Operational Base", "text", None, maximum_length=127),
    108: FieldDefinition(108, "Broadcast Source", "text", None, maximum_length=127),
    109: _imap(109, "Range To Recovery Location", 0, 21_000, units="km", maximum_length=4),
    110: _integer(
        110,
        "Time Airborne",
        None,
        units="seconds",
        minimum=0,
        maximum=2**32 - 1,
        maximum_length=4,
    ),
    111: _integer(
        111,
        "Propulsion Unit Speed",
        None,
        units="RPM",
        minimum=0,
        maximum=2**32 - 1,
        maximum_length=4,
    ),
    112: _imap(112, "Platform Course Angle", 0, 360, units="degrees"),
    113: _imap(113, "Altitude AGL", -900, 40_000, units="metres", maximum_length=4),
    114: _imap(114, "Radar Altimeter", -900, 40_000, units="metres", maximum_length=4),
    115: FieldDefinition(
        115,
        "Control Command",
        "control_command",
        None,
        multiple=True,
    ),
    116: FieldDefinition(
        116,
        "Control Command Verification List",
        "command_verification",
        None,
    ),
    117: _imap(
        117,
        "Sensor Azimuth Rate",
        -1000,
        1000,
        units="degrees/second",
        maximum_length=4,
    ),
    118: _imap(
        118,
        "Sensor Elevation Rate",
        -1000,
        1000,
        units="degrees/second",
        maximum_length=4,
    ),
    119: _imap(
        119,
        "Sensor Roll Rate",
        -1000,
        1000,
        units="degrees/second",
        maximum_length=4,
    ),
    120: _imap(
        120,
        "On-board MI Storage Percent Full",
        0,
        100,
        units="percent",
        maximum_length=3,
    ),
    121: FieldDefinition(121, "Active Wavelength List", "active_wavelengths", None),
    122: FieldDefinition(122, "Country Codes", "country_codes", None),
    123: _integer(123, "Number of NAVSATs in View", 1),
    124: _integer(124, "Positioning Method Source", 1, minimum=1, maximum=255),
    125: _integer(125, "Platform Status", 1, minimum=0, maximum=12),
    126: _integer(126, "Sensor Control Mode", 1, minimum=0, maximum=6),
    127: FieldDefinition(
        127,
        "Sensor Frame Rate Pack",
        "frame_rate",
        None,
        maximum_length=16,
    ),
    128: FieldDefinition(128, "Wavelengths List", "wavelengths", None),
    129: FieldDefinition(129, "Target ID", "text", None, maximum_length=32),
    130: FieldDefinition(
        130,
        "Airbase Locations",
        "airbase_locations",
        None,
        maximum_length=24,
    ),
    131: FieldDefinition(131, "Take-off Time", "timestamp", None, maximum_length=8),
    132: _imap(132, "Transmission Frequency", 1, 99_999, units="MHz", maximum_length=4),
    133: _integer(
        133,
        "On-board MI Storage Capacity",
        None,
        units="gigabytes",
        minimum=0,
        maximum=2**32 - 1,
        maximum_length=4,
    ),
    134: _imap(134, "Zoom Percentage", 0, 100, units="percent", maximum_length=4),
    135: FieldDefinition(135, "Communications Method", "text", None, maximum_length=127),
    136: _integer(
        136,
        "Leap Seconds",
        None,
        units="seconds",
        signed=True,
        minimum=-(2**31),
        maximum=2**31 - 1,
        maximum_length=4,
    ),
    137: _integer(
        137,
        "Correction Offset",
        None,
        units="microseconds",
        signed=True,
        minimum=-(2**63),
        maximum=2**63 - 1,
        maximum_length=8,
    ),
    138: FieldDefinition(138, "Payload List", "payload_list", None),
    139: FieldDefinition(139, "Active Payloads", "active_payloads", None),
    140: FieldDefinition(140, "Weapons Stores", "weapons_stores", None),
    141: FieldDefinition(141, "Waypoint List", "waypoint_list", None),
    142: FieldDefinition(142, "View Domain", "view_domain", None),
}


def _integer_domain(length: int, signed: bool, special_raw: int | None) -> tuple[int, int]:
    bits = length * 8
    if signed:
        low = -(2 ** (bits - 1))
        high = 2 ** (bits - 1) - 1
        if special_raw == low:
            low += 1
        return low, high
    return 0, 2**bits - 1


def _validate_horizon_geometry(
    start_x: int, start_y: int, end_x: int, end_y: int, *, error_type: type[Exception]
) -> None:
    percentages = (start_x, start_y, end_x, end_y)
    if any(not 0 <= value <= 100 for value in percentages):
        raise error_type("ST 0601 Item 81 coordinates must be percentages from 0 to 100")
    if start_x not in {0, 100} and start_y not in {0, 100}:
        raise error_type("ST 0601 Item 81 start point must lie on the image border")
    if end_x not in {0, 100} and end_y not in {0, 100}:
        raise error_type("ST 0601 Item 81 end point must lie on the image border")
    if (start_x, start_y) == (end_x, end_y):
        raise error_type("ST 0601 Item 81 start and end points must differ")


def _decode_horizon_coordinate(raw: bytes, *, maximum: int) -> float | SpecialValue:
    encoded = int.from_bytes(raw, "big", signed=True)
    if encoded == -(2**31):
        return SpecialValue.ERROR
    return float(Fraction(encoded * maximum, 2**31 - 1))


def decode_image_horizon_pixel_pack(data: bytes) -> ImageHorizonPixelPack:
    """Decode the truncatable ST 0601 Item 81 defined-length pack."""
    if not isinstance(data, bytes):
        raise TypeError("Image Horizon Pixel Pack data must be bytes")
    if not 4 <= len(data) <= 20:
        raise DecodeError("ST 0601 Item 81 requires between 4 and 20 bytes")
    if (len(data) - 4) % 4:
        raise DecodeError("ST 0601 Item 81 optional coordinates use 4-byte increments")
    start_x, start_y, end_x, end_y = data[:4]
    _validate_horizon_geometry(start_x, start_y, end_x, end_y, error_type=DecodeError)
    decoded: list[float | SpecialValue | None] = [None, None, None, None]
    maxima = (90, 180, 90, 180)
    for index, offset in enumerate(range(4, len(data), 4)):
        decoded[index] = _decode_horizon_coordinate(
            data[offset : offset + 4], maximum=maxima[index]
        )
    return ImageHorizonPixelPack(
        start_x,
        start_y,
        end_x,
        end_y,
        decoded[0],
        decoded[1],
        decoded[2],
        decoded[3],
    )


def decode_sensor_frame_rate_pack(data: bytes) -> SensorFrameRatePack:
    """Decode the two-element truncatable ST 0601 Item 127 pack."""
    if not isinstance(data, bytes):
        raise TypeError("Sensor Frame Rate Pack data must be bytes")
    if not data:
        raise DecodeError("ST 0601 Item 127 requires at least one BER-OID byte")
    if len(data) > 16:
        raise DecodeError("ST 0601 Item 127 exceeds its 16-byte maximum length")
    try:
        numerator, numerator_length = decode_ber_oid(data, max_octets=16)
    except NeedMoreData as error:
        raise DecodeError("ST 0601 Item 127 has an unterminated numerator") from error
    if numerator_length == len(data):
        return SensorFrameRatePack(numerator)
    try:
        denominator, denominator_length = decode_ber_oid(
            data, numerator_length, max_octets=16 - numerator_length
        )
    except NeedMoreData as error:
        raise DecodeError("ST 0601 Item 127 has an unterminated denominator") from error
    if numerator_length + denominator_length != len(data):
        raise DecodeError("ST 0601 Item 127 must contain exactly two BER-OID integers")
    if denominator == 0:
        raise DecodeError("ST 0601 Item 127 denominator must be positive")
    return SensorFrameRatePack(numerator, denominator)


def decode_control_command(data: bytes) -> ControlCommand:
    """Decode one ST 0601 Item 115 command pack."""
    if not isinstance(data, bytes):
        raise TypeError("Control Command data must be bytes")
    try:
        command_id, id_length = decode_ber_oid(data, max_octets=max(1, len(data)))
    except (NeedMoreData, DecodeError) as error:
        raise DecodeError("ST 0601 Item 115 has an invalid Command ID") from error
    try:
        string_length, length_length = decode_ber_length(data, id_length, max_value=len(data))
    except (NeedMoreData, DecodeError) as error:
        raise DecodeError("ST 0601 Item 115 has an invalid Command String Length") from error
    string_start = id_length + length_length
    string_end = string_start + string_length
    if string_end > len(data):
        raise DecodeError("ST 0601 Item 115 Command String is truncated")
    command_bytes = data[string_start:string_end]
    try:
        command = command_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DecodeError("ST 0601 Item 115 Command String is not valid UTF-8") from error
    if len(command) > 127:
        raise DecodeError("ST 0601 Item 115 Command String exceeds 127 characters")
    try:
        _validate_text(command, tag=115)
    except ValueError as error:
        raise DecodeError(str(error)) from error
    time_bytes = data[string_end:]
    if len(time_bytes) not in {0, 8}:
        raise DecodeError("ST 0601 Item 115 Command Time must be absent or 8 bytes")
    command_time = None
    if time_bytes:
        try:
            command_time = _UNIX_EPOCH + timedelta(microseconds=int.from_bytes(time_bytes, "big"))
        except OverflowError as error:
            raise DecodeError("ST 0601 Item 115 Command Time is outside datetime range") from error
    return ControlCommand(command_id, command, command_time)


def decode_control_command_verification_list(
    data: bytes,
) -> ControlCommandVerificationList:
    """Decode the ST 0601 Item 116 BER-OID command acknowledgement list."""
    if not isinstance(data, bytes):
        raise TypeError("Control Command Verification List data must be bytes")
    if not data:
        raise DecodeError("ST 0601 Item 116 requires at least one Command ID")
    command_ids: list[int] = []
    offset = 0
    while offset < len(data):
        try:
            command_id, used = decode_ber_oid(data, offset, max_octets=len(data) - offset)
        except NeedMoreData as error:
            raise DecodeError("ST 0601 Item 116 has an unterminated Command ID") from error
        command_ids.append(command_id)
        offset += used
    return ControlCommandVerificationList(tuple(command_ids))


def _decode_ber_oid_values(data: bytes, *, item: int, name: str) -> tuple[int, ...]:
    if not data:
        raise DecodeError(f"ST 0601 Item {item} requires at least one {name}")
    values: list[int] = []
    offset = 0
    while offset < len(data):
        try:
            value, used = decode_ber_oid(data, offset, max_octets=len(data) - offset)
        except NeedMoreData as error:
            raise DecodeError(f"ST 0601 Item {item} has an unterminated {name}") from error
        values.append(value)
        offset += used
    return tuple(values)


def decode_active_wavelength_list(data: bytes) -> ActiveWavelengthList:
    """Decode the ST 0601 Item 121 BER-OID wavelength identifier list."""
    if not isinstance(data, bytes):
        raise TypeError("Active Wavelength List data must be bytes")
    wavelength_ids = _decode_ber_oid_values(data, item=121, name="Wavelength ID")
    if 0 in wavelength_ids and len(wavelength_ids) != 1:
        raise DecodeError("ST 0601 Item 121 Wavelength ID zero must be used alone")
    return ActiveWavelengthList(wavelength_ids)


def _decode_vlp_values(data: bytes, *, item: int) -> tuple[bytes, ...]:
    values: list[bytes] = []
    offset = 0
    while offset < len(data):
        try:
            length, used = decode_ber_length(data, offset, max_value=len(data))
        except NeedMoreData as error:
            raise DecodeError(f"ST 0601 Item {item} has a truncated VLP length") from error
        value_start = offset + used
        value_end = value_start + length
        if value_end > len(data):
            raise DecodeError(f"ST 0601 Item {item} has a truncated VLP value")
        values.append(data[value_start:value_end])
        offset = value_end
    return tuple(values)


def _decode_country_value(raw: bytes, *, name: str) -> str | SpecialValue:
    if not raw:
        return SpecialValue.UNKNOWN
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DecodeError(f"ST 0601 Item 122 {name} is not valid UTF-8") from error
    try:
        _validate_text(value, tag=122)
    except ValueError as error:
        raise DecodeError(str(error)) from error
    return value


def decode_country_codes(data: bytes) -> CountryCodes:
    """Decode the ST 0601 Item 122 country-code VLP."""
    if not isinstance(data, bytes):
        raise TypeError("Country Codes data must be bytes")
    components = _decode_vlp_values(data, item=122)
    if len(components) < 2:
        raise DecodeError("ST 0601 Item 122 requires Coding Method and Overflight Country")
    if len(components) > 4:
        raise DecodeError("ST 0601 Item 122 contains at most four VLP values")
    if len(components[0]) != 1:
        raise DecodeError("ST 0601 Item 122 Coding Method requires one byte")
    coding_method = components[0][0]
    if coding_method not in {*range(1, 16), 64}:
        raise DecodeError("ST 0601 Item 122 Coding Method is not allowed")
    overflight = _decode_country_value(components[1], name="Overflight Country")
    countries: list[str | SpecialValue | None] = [None, None]
    for index, raw in enumerate(components[2:], start=1):
        countries[index - 1] = _decode_country_value(
            raw,
            name=("Operator Country", "Country of Manufacture")[index - 1],
        )
    return CountryCodes(coding_method, overflight, countries[0], countries[1])


_WAVELENGTH_MAPPING = IMAPB(0, 1_000_000_000, 4)


def _decode_wavelength_record(data: bytes) -> WavelengthRecord:
    if not data:
        raise DecodeError("ST 0601 Item 128 contains an empty Wavelength Record")
    try:
        wavelength_id, id_length = decode_ber_oid(data, max_octets=max(1, len(data)))
    except (NeedMoreData, DecodeError) as error:
        raise DecodeError("ST 0601 Item 128 has an invalid Wavelength ID") from error
    if wavelength_id < 21:
        raise DecodeError("ST 0601 Item 128 custom ID must be 21 or greater")
    if len(data) < id_length + 8:
        raise DecodeError("ST 0601 Item 128 Wavelength Record is missing wavelength bounds")
    minimum = _WAVELENGTH_MAPPING.decode(data[id_length : id_length + 4])
    maximum = _WAVELENGTH_MAPPING.decode(data[id_length + 4 : id_length + 8])
    if isinstance(minimum, IMAPSpecialValue) or isinstance(maximum, IMAPSpecialValue):
        raise DecodeError("ST 0601 Item 128 wavelength bounds do not permit IMAP specials")
    if minimum > maximum:
        raise DecodeError("ST 0601 Item 128 minimum wavelength exceeds maximum wavelength")
    name_bytes = data[id_length + 8 :]
    if not name_bytes:
        raise DecodeError("ST 0601 Item 128 Wavelength Name is mandatory")
    try:
        name = name_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DecodeError("ST 0601 Item 128 Wavelength Name is not valid UTF-8") from error
    try:
        _validate_text(name, tag=128)
    except ValueError as error:
        raise DecodeError(str(error)) from error
    return WavelengthRecord(wavelength_id, minimum, maximum, name)


def decode_wavelengths_list(data: bytes) -> WavelengthsList:
    """Decode the ST 0601 Item 128 VLP of wavelength-record FLPs."""
    if not isinstance(data, bytes):
        raise TypeError("Wavelengths List data must be bytes")
    encoded_records = _decode_vlp_values(data, item=128)
    if not encoded_records:
        raise DecodeError("ST 0601 Item 128 requires at least one Wavelength Record")
    records = tuple(_decode_wavelength_record(value) for value in encoded_records)
    ids = tuple(record.wavelength_id for record in records)
    names = tuple(record.name for record in records)
    if len(set(ids)) != len(ids):
        raise DecodeError("ST 0601 Item 128 contains a duplicate Wavelength ID")
    if len(set(names)) != len(names):
        raise DecodeError("ST 0601 Item 128 contains a duplicate Wavelength Name")
    return WavelengthsList(records)


_AIRBASE_LATITUDE_MAPPING = IMAPB(-90, 90, 4)
_AIRBASE_LONGITUDE_MAPPING = IMAPB(-180, 180, 4)
_AIRBASE_HAE_MAPPING = IMAPB(-900, 9000, 3)


def _decode_airbase_location(data: bytes) -> AirbaseLocation | SpecialValue:
    if not data:
        return SpecialValue.UNKNOWN
    if len(data) not in {8, 11}:
        raise DecodeError("ST 0601 Item 130 Location must contain 8 or 11 bytes")
    latitude = _AIRBASE_LATITUDE_MAPPING.decode(data[:4])
    longitude = _AIRBASE_LONGITUDE_MAPPING.decode(data[4:8])
    hae = _AIRBASE_HAE_MAPPING.decode(data[8:]) if len(data) == 11 else None
    if any(
        isinstance(value, IMAPSpecialValue)
        for value in (latitude, longitude, hae)
        if value is not None
    ):
        raise DecodeError("ST 0601 Item 130 Location does not permit IMAP specials")
    assert isinstance(latitude, float)
    assert isinstance(longitude, float)
    assert isinstance(hae, (float, type(None)))
    return AirbaseLocation(latitude, longitude, hae)


def decode_airbase_locations(data: bytes) -> AirbaseLocations:
    """Decode the ST 0601 Item 130 take-off/recovery location VLP."""
    if not isinstance(data, bytes):
        raise TypeError("Airbase Locations data must be bytes")
    if len(data) > 24:
        raise DecodeError("ST 0601 Item 130 exceeds its 24-byte maximum length")
    encoded_locations = _decode_vlp_values(data, item=130)
    if not encoded_locations:
        raise DecodeError("ST 0601 Item 130 requires at least a take-off location")
    if len(encoded_locations) > 2:
        raise DecodeError("ST 0601 Item 130 contains at most two locations")
    takeoff = _decode_airbase_location(encoded_locations[0])
    recovery = (
        _decode_airbase_location(encoded_locations[1]) if len(encoded_locations) == 2 else None
    )
    if takeoff is SpecialValue.UNKNOWN and recovery in {None, SpecialValue.UNKNOWN}:
        raise DecodeError("ST 0601 Item 130 must be omitted when both locations are Unknown")
    if isinstance(takeoff, AirbaseLocation) and recovery == takeoff:
        raise DecodeError("ST 0601 Item 130 must truncate a recovery equal to take-off")
    return AirbaseLocations(takeoff, recovery)


def _decode_payload_record(data: bytes) -> PayloadRecord:
    if not data:
        raise DecodeError("ST 0601 Item 138 contains an empty Payload Record")
    try:
        payload_id, id_length = decode_ber_oid(data, max_octets=len(data))
    except (NeedMoreData, DecodeError) as error:
        raise DecodeError("ST 0601 Item 138 has an invalid Payload ID") from error
    try:
        payload_type, type_length = decode_ber_oid(
            data, id_length, max_octets=len(data) - id_length
        )
    except (NeedMoreData, DecodeError) as error:
        raise DecodeError("ST 0601 Item 138 has an invalid Payload Type") from error
    if payload_type > 4:
        raise DecodeError("ST 0601 Item 138 Payload Type must be between 0 and 4")
    length_offset = id_length + type_length
    try:
        name_length, name_length_size = decode_ber_length(data, length_offset, max_value=len(data))
    except (NeedMoreData, DecodeError) as error:
        raise DecodeError("ST 0601 Item 138 has an invalid Payload Name length") from error
    name_start = length_offset + name_length_size
    name_end = name_start + name_length
    if name_end != len(data):
        raise DecodeError("ST 0601 Item 138 Payload Name length does not match its record")
    if not name_length:
        raise DecodeError("ST 0601 Item 138 Payload Name is mandatory")
    try:
        name = data[name_start:name_end].decode("utf-8")
    except UnicodeDecodeError as error:
        raise DecodeError("ST 0601 Item 138 Payload Name is not valid UTF-8") from error
    try:
        _validate_text(name, tag=138)
    except ValueError as error:
        raise DecodeError(str(error)) from error
    return PayloadRecord(payload_id, payload_type, name)


def _validate_payload_records(
    total_count: int,
    records: tuple[PayloadRecord, ...],
    *,
    error_type: type[Exception],
) -> None:
    if total_count and not records:
        raise error_type("ST 0601 Item 138 requires at least one record when count is positive")
    ids = tuple(record.payload_id for record in records)
    if any(payload_id >= total_count for payload_id in ids):
        raise error_type("ST 0601 Item 138 Payload ID is outside Payload Count")
    if len(set(ids)) != len(ids):
        raise error_type("ST 0601 Item 138 Payload IDs must be unique and sequential")
    if len(records) == total_count and ids != tuple(range(total_count)):
        raise error_type("ST 0601 Item 138 complete Payload IDs must be sequential from zero")


def decode_payload_list(data: bytes) -> PayloadList:
    """Decode a complete or distributed ST 0601 Item 138 payload list."""
    if not isinstance(data, bytes):
        raise TypeError("Payload List data must be bytes")
    try:
        total_count, count_length = decode_ber_oid(data, max_octets=max(1, len(data)))
    except (NeedMoreData, DecodeError) as error:
        raise DecodeError("ST 0601 Item 138 has an invalid Payload Count") from error
    encoded_records = _decode_vlp_values(data[count_length:], item=138)
    records = tuple(_decode_payload_record(value) for value in encoded_records)
    _validate_payload_records(total_count, records, error_type=DecodeError)
    return PayloadList(total_count, records)


def decode_active_payloads(data: bytes) -> ActivePayloads:
    """Decode the little-bit-indexed ST 0601 Item 139 payload bit set."""
    if not isinstance(data, bytes):
        raise TypeError("Active Payloads data must be bytes")
    if not data:
        raise DecodeError("ST 0601 Item 139 requires at least one byte")
    payload_ids = frozenset(
        byte_index * 8 + bit_index
        for byte_index, octet in enumerate(data)
        for bit_index in range(8)
        if octet & (1 << bit_index)
    )
    return ActivePayloads(payload_ids)


def _decode_weapon_store(data: bytes) -> WeaponStore:
    if not data:
        raise DecodeError("ST 0601 Item 140 contains an empty Weapons Record")
    names = ("Station ID", "Hardpoint ID", "Carriage ID", "Store ID", "Status")
    values: list[int] = []
    offset = 0
    for name in names:
        if offset >= len(data):
            raise DecodeError(f"ST 0601 Item 140 is missing {name}")
        try:
            value, used = decode_ber_oid(data, offset, max_octets=len(data) - offset)
        except (NeedMoreData, DecodeError) as error:
            raise DecodeError(f"ST 0601 Item 140 has an invalid {name}") from error
        values.append(value)
        offset += used
    try:
        status = WeaponStatus.from_raw(values[4])
    except (TypeError, ValueError) as error:
        raise DecodeError(f"ST 0601 Item 140 {error}") from error
    try:
        type_length, length_size = decode_ber_length(data, offset, max_value=len(data))
    except (NeedMoreData, DecodeError) as error:
        raise DecodeError("ST 0601 Item 140 has an invalid Weapon Type length") from error
    type_start = offset + length_size
    type_end = type_start + type_length
    if type_end != len(data):
        raise DecodeError("ST 0601 Item 140 Weapon Type length does not match its record")
    if not type_length:
        raise DecodeError("ST 0601 Item 140 Weapon Type is mandatory")
    try:
        weapon_type = data[type_start:type_end].decode("utf-8")
    except UnicodeDecodeError as error:
        raise DecodeError("ST 0601 Item 140 Weapon Type is not valid UTF-8") from error
    try:
        _validate_text(weapon_type, tag=140)
    except ValueError as error:
        raise DecodeError(str(error)) from error
    return WeaponStore(values[0], values[1], values[2], values[3], status, weapon_type)


def decode_weapons_stores(data: bytes) -> WeaponsStores:
    """Decode the ST 0601 Item 140 VLP of weapon-store records."""
    if not isinstance(data, bytes):
        raise TypeError("Weapons Stores data must be bytes")
    encoded_records = _decode_vlp_values(data, item=140)
    if not encoded_records:
        raise DecodeError("ST 0601 Item 140 requires at least one Weapons Record")
    records = tuple(_decode_weapon_store(value) for value in encoded_records)
    addresses = tuple(record.address for record in records)
    if len(set(addresses)) != len(addresses):
        raise DecodeError("ST 0601 Item 140 contains a duplicate weapon address")
    return WeaponsStores(records)


def _decode_waypoint_record(data: bytes) -> WaypointRecord:
    if not data:
        raise DecodeError("ST 0601 Item 141 contains an empty Waypoint Record")
    try:
        waypoint_id, id_size = decode_ber_oid(data, max_octets=len(data))
    except (NeedMoreData, DecodeError) as error:
        raise DecodeError("ST 0601 Item 141 has an invalid Waypoint ID") from error
    order_end = id_size + 2
    if order_end > len(data):
        raise DecodeError("ST 0601 Item 141 is missing its Prosecution Order")
    prosecution_order = int.from_bytes(data[id_size:order_end], "big", signed=True)
    if order_end == len(data):
        return WaypointRecord(waypoint_id, prosecution_order)
    try:
        info_raw, info_size = decode_ber_oid(data, order_end, max_octets=len(data) - order_end)
    except (NeedMoreData, DecodeError) as error:
        raise DecodeError("ST 0601 Item 141 has an invalid Info Value") from error
    try:
        info = WaypointInfo.from_raw(info_raw)
    except (TypeError, ValueError) as error:
        raise DecodeError(f"ST 0601 Item 141 has {error}") from error
    location_raw = data[order_end + info_size :]
    if not location_raw:
        return WaypointRecord(waypoint_id, prosecution_order, info)
    if len(location_raw) not in {8, 11}:
        raise DecodeError("ST 0601 Item 141 Location must contain 8 or 11 bytes")
    try:
        location = _decode_airbase_location(location_raw)
    except DecodeError as error:
        raise DecodeError("ST 0601 Item 141 has an invalid Location") from error
    assert isinstance(location, AirbaseLocation)
    return WaypointRecord(waypoint_id, prosecution_order, info, location)


def _validate_waypoint_records(
    records: tuple[WaypointRecord, ...], *, error_type: type[Exception]
) -> None:
    ids = tuple(record.waypoint_id for record in records)
    if len(set(ids)) != len(ids):
        raise error_type("ST 0601 Item 141 contains a duplicate Waypoint ID")
    active_orders = tuple(
        record.prosecution_order for record in records if record.prosecution_order != 32767
    )
    if len(set(active_orders)) != len(active_orders):
        raise error_type("ST 0601 Item 141 non-cancelled Prosecution Orders must be unique")


def decode_waypoint_list(data: bytes) -> WaypointList:
    """Decode the ST 0601 Item 141 VLP of waypoint records."""
    if not isinstance(data, bytes):
        raise TypeError("Waypoint List data must be bytes")
    encoded_records = _decode_vlp_values(data, item=141)
    if not encoded_records:
        raise DecodeError("ST 0601 Item 141 requires at least one Waypoint Record")
    records = tuple(_decode_waypoint_record(value) for value in encoded_records)
    _validate_waypoint_records(records, error_type=DecodeError)
    return WaypointList(records)


_VIEW_DOMAIN_BOUNDS = ((0, 360), (-180, 180), (0, 360))
_VIEW_DOMAIN_NAMES = ("azimuth", "elevation", "roll")


def _decode_view_domain_pair(data: bytes, *, index: int) -> ViewDomainPair | SpecialValue:
    name = _VIEW_DOMAIN_NAMES[index]
    if not data:
        return SpecialValue.UNKNOWN
    if len(data) % 2 or len(data) < 2:
        raise DecodeError(f"ST 0601 Item 142 {name} Pair Length must be positive even")
    value_length = len(data) // 2
    minimum, maximum = _VIEW_DOMAIN_BOUNDS[index]
    start = IMAPB(minimum, maximum, value_length).decode(data[:value_length])
    angular_range = IMAPB(0, 360, value_length).decode(data[value_length:])
    if isinstance(start, IMAPSpecialValue) or isinstance(angular_range, IMAPSpecialValue):
        raise DecodeError(f"ST 0601 Item 142 {name} pair does not permit IMAP special values")
    return ViewDomainPair(start, angular_range)


def decode_view_domain(data: bytes) -> ViewDomain:
    """Decode the ordered, end-truncatable ST 0601 Item 142 domain pairs."""
    if not isinstance(data, bytes):
        raise TypeError("View Domain data must be bytes")
    encoded_pairs = _decode_vlp_values(data, item=142)
    if not encoded_pairs:
        raise DecodeError("ST 0601 Item 142 requires at least one domain pair")
    if len(encoded_pairs) > 3:
        raise DecodeError("ST 0601 Item 142 contains at most three domain pairs")
    values: list[ViewDomainPair | SpecialValue | None] = [
        _decode_view_domain_pair(pair, index=index) for index, pair in enumerate(encoded_pairs)
    ]
    values.extend([None] * (3 - len(values)))
    return ViewDomain(values[0], values[1], values[2])


def decode_metadata_substream_id(data: bytes) -> MetadataSubstreamID:
    """Decode an Item 143 pack for use within a Segment or Amend Local Set."""
    if not isinstance(data, bytes):
        raise TypeError("Metadata Substream ID data must be bytes")
    if not data:
        raise DecodeError("ST 0601 Item 143 requires a Local ID")
    if len(data) > 17:
        raise DecodeError("ST 0601 Item 143 exceeds its 17-byte maximum length")
    try:
        local_id, id_size = decode_ber_oid(data, max_octets=len(data))
    except (NeedMoreData, DecodeError) as error:
        raise DecodeError("ST 0601 Item 143 has an invalid Local ID") from error
    remainder = data[id_size:]
    if local_id:
        if remainder:
            raise DecodeError("ST 0601 Item 143 must omit the UUID when Local ID is nonzero")
        return MetadataSubstreamID(local_id)
    if len(remainder) != 16:
        raise DecodeError("ST 0601 Item 143 Local ID zero requires a UUID")
    return MetadataSubstreamID(0, UUID(bytes=remainder))


def _decode_metadata_branch(
    data: bytes,
    *,
    kind: Literal["segment", "amend"],
    field_decoding: FieldDecodingMode,
    depth: int,
    max_depth: int,
) -> SegmentLocalSet | AmendLocalSet:
    if depth > max_depth:
        raise DecodeError(f"ST 1607 hierarchy exceeds maximum depth {max_depth}")
    local_set = parse_local_set(data)
    if local_set.getall(1):
        raise DecodeError("nested ST 1607 Local Sets must omit the checksum item")
    identifiers = local_set.getall(143)
    if len(identifiers) != 1:
        raise DecodeError("ST 1607 Local Set requires exactly one Metadata Substream ID")
    substream_id = decode_metadata_substream_id(identifiers[0].value)
    has_segments = bool(local_set.getall(100))
    has_amends = bool(local_set.getall(101))
    if has_segments and has_amends:
        raise DecodeError("ST 1607 Local Set cannot contain both Segment and Amend children")
    if kind == "amend" and has_segments:
        raise DecodeError("ST 1607 Amend Local Set cannot contain Segment children")
    for tag, definition in FIELD_DEFINITIONS.items():
        if not definition.multiple and len(local_set.getall(tag)) > 1:
            raise DecodeError(f"ST 1607 child singleton tag {tag} occurs twice")

    fields: list[DecodedField] = []
    issues: list[FieldDecodingIssue] = []
    for item in local_set.items:
        field_definition = FIELD_DEFINITIONS.get(item.tag)
        if field_definition is None:
            continue
        try:
            if item.tag == 100:
                value: Any = _decode_metadata_branch(
                    item.value,
                    kind="segment",
                    field_decoding=field_decoding,
                    depth=depth + 1,
                    max_depth=max_depth,
                )
            elif item.tag == 101:
                value = _decode_metadata_branch(
                    item.value,
                    kind="amend",
                    field_decoding=field_decoding,
                    depth=depth + 1,
                    max_depth=max_depth,
                )
            elif kind == "amend" and not item.value:
                value = DELETE
            elif item.tag == 48:
                value = decode_security_local_set(
                    item.value,
                    standalone=False,
                    require_required=False,
                )
            else:
                value = _decode_field(item, field_definition)
                fields.append(value)
                continue
            fields.append(DecodedField(field_definition, value, item.value, item))
        except DecodeError as error:
            if field_decoding is FieldDecodingMode.STRICT:
                raise
            issues.append(
                FieldDecodingIssue(
                    item.tag,
                    field_definition.name,
                    str(error),
                    item.value,
                    item,
                )
            )
    branch_type = SegmentLocalSet if kind == "segment" else AmendLocalSet
    bound_fields = _bind_sdcc_source_tags(local_set, tuple(fields))
    return branch_type(local_set, bound_fields, substream_id, tuple(issues))


def decode_segment_local_set(
    data: bytes,
    *,
    field_decoding: FieldDecodingMode = FieldDecodingMode.STRICT,
    max_depth: int = 16,
) -> SegmentLocalSet:
    """Decode an ST 1607 Segment LS embedded as ST 0601 Item 100."""
    if not isinstance(data, bytes):
        raise TypeError("Segment Local Set data must be bytes")
    if not isinstance(field_decoding, FieldDecodingMode):
        raise TypeError("field_decoding must be a FieldDecodingMode")
    if isinstance(max_depth, bool) or not isinstance(max_depth, int):
        raise TypeError("max_depth must be an integer")
    if max_depth < 0:
        raise ValueError("max_depth cannot be negative")
    branch = _decode_metadata_branch(
        data,
        kind="segment",
        field_decoding=field_decoding,
        depth=0,
        max_depth=max_depth,
    )
    assert isinstance(branch, SegmentLocalSet)
    return branch


def decode_amend_local_set(
    data: bytes,
    *,
    field_decoding: FieldDecodingMode = FieldDecodingMode.STRICT,
    max_depth: int = 16,
) -> AmendLocalSet:
    """Decode an ST 1607 Amend LS embedded as ST 0601 Item 101."""
    if not isinstance(data, bytes):
        raise TypeError("Amend Local Set data must be bytes")
    if not isinstance(field_decoding, FieldDecodingMode):
        raise TypeError("field_decoding must be a FieldDecodingMode")
    if isinstance(max_depth, bool) or not isinstance(max_depth, int):
        raise TypeError("max_depth must be an integer")
    if max_depth < 0:
        raise ValueError("max_depth cannot be negative")
    branch = _decode_metadata_branch(
        data,
        kind="amend",
        field_decoding=field_decoding,
        depth=0,
        max_depth=max_depth,
    )
    assert isinstance(branch, AmendLocalSet)
    return branch


def _decode_field(
    item: LocalSetItem,
    definition: FieldDefinition,
    *,
    vmti_context: VMTIValidationContext | None = None,
) -> DecodedField:
    if not item.value:
        if item.tag in {1, 2, 65, 100, 101, 102}:
            raise DecodeError(f"ST 0601 mandatory tag {item.tag} requires a positive length")
        return DecodedField(definition, SpecialValue.UNKNOWN, item.value, item)
    if definition.kind == "imap" and not 1 <= len(item.value) <= (definition.maximum_length or 8):
        raise DecodeError(
            f"ST 0601 tag {item.tag} ({definition.name}) requires between 1 and "
            f"{definition.maximum_length or 8} bytes"
        )
    if definition.length is not None and len(item.value) != definition.length:
        raise DecodeError(
            f"ST 0601 tag {item.tag} ({definition.name}) requires "
            f"{definition.length} byte(s), observed {len(item.value)}"
        )
    if definition.maximum_length is not None and len(item.value) > definition.maximum_length:
        raise DecodeError(
            f"ST 0601 tag {item.tag} ({definition.name}) exceeds its "
            f"{definition.maximum_length}-byte maximum length"
        )
    if definition.kind == "text":
        if item.value == b"\x00":
            return DecodedField(definition, "", item.value, item)
        try:
            value: Any = item.value.decode("utf-8")
        except UnicodeDecodeError as error:
            raise DecodeError(f"ST 0601 tag {item.tag} is not valid UTF-8") from error
        try:
            _validate_text(value, tag=item.tag)
        except ValueError as error:
            raise DecodeError(str(error)) from error
    elif definition.kind == "timestamp":
        micros = int.from_bytes(item.value, "big")
        if definition.length is None and len(item.value) != _minimum_integer_length(
            micros,
            signed=False,
        ):
            raise DecodeError(
                f"ST 0601 tag {item.tag} ({definition.name}) must use a minimal "
                "unsigned integer encoding"
            )
        value = _UNIX_EPOCH + timedelta(microseconds=micros)
    elif definition.kind in {"uint", "sint"}:
        value = int.from_bytes(item.value, "big", signed=definition.kind == "sint")
        if definition.length is None and len(item.value) != _minimum_integer_length(
            value,
            signed=definition.kind == "sint",
        ):
            signedness = "signed" if definition.kind == "sint" else "unsigned"
            raise DecodeError(
                f"ST 0601 tag {item.tag} ({definition.name}) must use a minimal "
                f"{signedness} integer encoding"
            )
        if (definition.integer_min is not None and value < definition.integer_min) or (
            definition.integer_max is not None and value > definition.integer_max
        ):
            raise DecodeError(
                f"ST 0601 tag {item.tag} ({definition.name}) value {value} is outside "
                "its permitted range"
            )
        semantic_type = _SEMANTIC_INTEGER_TYPES.get(item.tag)
        if semantic_type is not None:
            try:
                value = semantic_type(value)
            except (TypeError, ValueError) as error:
                raise DecodeError(f"ST 0601 tag {item.tag}: {error}") from error
    elif definition.kind == "imap":
        assert definition.physical_min is not None
        assert definition.physical_max is not None
        mapping = IMAPB(definition.physical_min, definition.physical_max, len(item.value))
        value = mapping.decode(item.value)
        if isinstance(value, IMAPSpecialValue):
            raise DecodeError(
                f"ST 0601 tag {item.tag} does not permit IMAP special value {value.kind.value}"
            )
    elif definition.kind == "vmti":
        value = decode_vmti_local_set(
            item.value,
            standalone=False,
            context=vmti_context,
        )
    elif definition.kind == "miis":
        value = decode_miis_core_identifier(item.value)
    elif definition.kind == "security":
        value = decode_security_local_set(item.value, standalone=False)
    elif definition.kind == "rvt":
        value = decode_rvt_local_set(item.value, standalone=False)
    elif definition.kind == "range_image":
        value = decode_range_image_local_set(item.value, standalone=False)
    elif definition.kind == "geo_registration":
        value = decode_geo_registration_local_set(item.value)
    elif definition.kind == "composite_imaging":
        value = decode_composite_imaging_local_set(item.value)
    elif definition.kind == "sar":
        value = decode_sar_motion_imagery_local_set(item.value, standalone=False)
    elif definition.kind == "sdcc":
        value = decode_sdcc_flp(item.value, require_mode=2)
    elif definition.kind == "horizon":
        value = decode_image_horizon_pixel_pack(item.value)
    elif definition.kind == "frame_rate":
        value = decode_sensor_frame_rate_pack(item.value)
    elif definition.kind == "control_command":
        value = decode_control_command(item.value)
    elif definition.kind == "command_verification":
        value = decode_control_command_verification_list(item.value)
    elif definition.kind == "active_wavelengths":
        value = decode_active_wavelength_list(item.value)
    elif definition.kind == "country_codes":
        value = decode_country_codes(item.value)
    elif definition.kind == "wavelengths":
        value = decode_wavelengths_list(item.value)
    elif definition.kind == "airbase_locations":
        value = decode_airbase_locations(item.value)
    elif definition.kind == "payload_list":
        value = decode_payload_list(item.value)
    elif definition.kind == "active_payloads":
        value = decode_active_payloads(item.value)
    elif definition.kind == "weapons_stores":
        value = decode_weapons_stores(item.value)
    elif definition.kind == "waypoint_list":
        value = decode_waypoint_list(item.value)
    elif definition.kind == "view_domain":
        value = decode_view_domain(item.value)
    elif definition.kind == "segment":
        value = decode_segment_local_set(item.value)
    elif definition.kind == "amend":
        value = decode_amend_local_set(item.value)
    else:
        raw_value = int.from_bytes(item.value, "big", signed=definition.signed)
        if raw_value == definition.special_raw:
            assert definition.special_value is not None
            value = definition.special_value
        else:
            assert definition.physical_min is not None
            assert definition.physical_max is not None
            raw_min, raw_max = _integer_domain(
                len(item.value), definition.signed, definition.special_raw
            )
            if not raw_min <= raw_value <= raw_max:
                raise DecodeError(f"ST 0601 tag {item.tag} has reserved raw value {raw_value}")
            physical = definition.physical_min + Fraction(
                (raw_value - raw_min) * (definition.physical_max - definition.physical_min),
                raw_max - raw_min,
            )
            value = float(physical)
    return DecodedField(definition, value, item.value, item)


def _round_fraction(value: Fraction) -> int:
    """Round a fraction to nearest integer with ties to even."""
    quotient, remainder = divmod(value.numerator, value.denominator)
    doubled = remainder * 2
    if doubled < value.denominator:
        return quotient
    if doubled > value.denominator:
        return quotient + 1
    return quotient + (quotient & 1)


def _as_fraction(value: int | float | Fraction) -> Fraction:
    if isinstance(value, bool):
        raise TypeError("boolean is not a numeric ST 0601 value")
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        return Fraction(str(value))
    raise TypeError(f"expected int, float, or Fraction, got {type(value).__name__}")


def _encode_horizon_coordinate(
    value: int | float | Fraction | SpecialValue, *, name: str, maximum: int
) -> bytes:
    if isinstance(value, SpecialValue):
        if value is not SpecialValue.ERROR:
            raise ValueError(f"ST 0601 Item 81 does not define special value {value.value}")
        return (-(2**31)).to_bytes(4, "big", signed=True)
    physical = _as_fraction(value)
    if not -maximum <= physical <= maximum:
        raise ValueError(f"ST 0601 Item 81 {name} is outside [-{maximum}, {maximum}]")
    encoded = _round_fraction(physical * (2**31 - 1) / maximum)
    return encoded.to_bytes(4, "big", signed=True)


def encode_image_horizon_pixel_pack(pack: ImageHorizonPixelPack) -> bytes:
    """Encode an ST 0601 Item 81 pack using end-only truncation."""
    if not isinstance(pack, ImageHorizonPixelPack):
        raise TypeError("pack must be an ImageHorizonPixelPack")
    percentages = (pack.start_x, pack.start_y, pack.end_x, pack.end_y)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in percentages):
        raise TypeError("ST 0601 Item 81 percentage coordinates must be integers")
    _validate_horizon_geometry(*percentages, error_type=ValueError)
    optional = (
        (pack.start_latitude, "start latitude", 90),
        (pack.start_longitude, "start longitude", 180),
        (pack.end_latitude, "end latitude", 90),
        (pack.end_longitude, "end longitude", 180),
    )
    last_present = next(
        (index for index in range(len(optional) - 1, -1, -1) if optional[index][0] is not None),
        None,
    )
    encoded = bytearray(percentages)
    if last_present is None:
        return bytes(encoded)
    for value, name, maximum in optional[: last_present + 1]:
        if value is None:
            raise ValueError(
                "ST 0601 Item 81 may omit only trailing optional fields; "
                "use SpecialValue.ERROR for an interior unavailable coordinate"
            )
        encoded.extend(_encode_horizon_coordinate(value, name=name, maximum=maximum))
    return bytes(encoded)


def encode_sensor_frame_rate_pack(pack: SensorFrameRatePack) -> bytes:
    """Encode ST 0601 Item 127, truncating the default denominator of one."""
    if not isinstance(pack, SensorFrameRatePack):
        raise TypeError("pack must be a SensorFrameRatePack")
    for name, value in (("numerator", pack.numerator), ("denominator", pack.denominator)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"ST 0601 Item 127 {name} must be an integer")
    if pack.numerator < 0:
        raise ValueError("ST 0601 Item 127 numerator cannot be negative")
    if pack.denominator <= 0:
        raise ValueError("ST 0601 Item 127 denominator must be positive")
    encoded = encode_ber_oid(pack.numerator)
    if pack.denominator != 1:
        encoded += encode_ber_oid(pack.denominator)
    if len(encoded) > 16:
        raise ValueError("ST 0601 Item 127 exceeds its 16-byte maximum length")
    return encoded


def _encode_timestamp_value(value: datetime, *, item: int, name: str) -> bytes:
    if not isinstance(value, datetime):
        raise TypeError(f"ST 0601 Item {item} {name} requires datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"ST 0601 Item {item} {name} must be timezone-aware")
    delta = value.astimezone(timezone.utc) - _UNIX_EPOCH
    micros = delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
    if not 0 <= micros <= 2**64 - 1:
        raise ValueError(f"ST 0601 Item {item} {name} is outside the uint64 range")
    return micros.to_bytes(8, "big")


def encode_control_command(command: ControlCommand) -> bytes:
    """Encode one ST 0601 Item 115 command pack."""
    if not isinstance(command, ControlCommand):
        raise TypeError("command must be a ControlCommand")
    if isinstance(command.command_id, bool) or not isinstance(command.command_id, int):
        raise TypeError("ST 0601 Item 115 Command ID must be an integer")
    if command.command_id < 0:
        raise ValueError("ST 0601 Item 115 Command ID cannot be negative")
    if not isinstance(command.command, str):
        raise TypeError("ST 0601 Item 115 Command String must be str")
    if len(command.command) > 127:
        raise ValueError("ST 0601 Item 115 Command String exceeds 127 characters")
    _validate_text(command.command, tag=115)
    command_bytes = command.command.encode("utf-8")
    encoded = (
        encode_ber_oid(command.command_id) + encode_ber_length(len(command_bytes)) + command_bytes
    )
    if command.command_time is not None:
        encoded += _encode_timestamp_value(command.command_time, item=115, name="Command Time")
    return encoded


def encode_control_command_verification_list(
    acknowledgements: ControlCommandVerificationList,
) -> bytes:
    """Encode an ST 0601 Item 116 command acknowledgement list."""
    if not isinstance(acknowledgements, ControlCommandVerificationList):
        raise TypeError("acknowledgements must be a ControlCommandVerificationList")
    if not acknowledgements.command_ids:
        raise ValueError("ST 0601 Item 116 requires at least one Command ID")
    encoded = bytearray()
    for command_id in acknowledgements.command_ids:
        if isinstance(command_id, bool) or not isinstance(command_id, int):
            raise TypeError("ST 0601 Item 116 Command IDs must be integers")
        if command_id < 0:
            raise ValueError("ST 0601 Item 116 Command ID cannot be negative")
        encoded.extend(encode_ber_oid(command_id))
    return bytes(encoded)


def _encode_ber_oid_values(values: tuple[int, ...], *, item: int, name: str) -> bytes:
    if not values:
        raise ValueError(f"ST 0601 Item {item} requires at least one {name}")
    encoded = bytearray()
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"ST 0601 Item {item} {name}s must be integers")
        if value < 0:
            raise ValueError(f"ST 0601 Item {item} {name} cannot be negative")
        encoded.extend(encode_ber_oid(value))
    return bytes(encoded)


def encode_active_wavelength_list(wavelengths: ActiveWavelengthList) -> bytes:
    """Encode an ST 0601 Item 121 active wavelength identifier list."""
    if not isinstance(wavelengths, ActiveWavelengthList):
        raise TypeError("wavelengths must be an ActiveWavelengthList")
    if 0 in wavelengths.wavelength_ids and len(wavelengths.wavelength_ids) != 1:
        raise ValueError("ST 0601 Item 121 Wavelength ID zero must be used alone")
    return _encode_ber_oid_values(wavelengths.wavelength_ids, item=121, name="Wavelength ID")


def _encode_country_value(value: str | SpecialValue, *, name: str) -> bytes:
    if isinstance(value, SpecialValue):
        if value is not SpecialValue.UNKNOWN:
            raise ValueError(f"ST 0601 Item 122 {name} does not allow {value.value}")
        return b""
    if not isinstance(value, str):
        raise TypeError(f"ST 0601 Item 122 {name} requires str or SpecialValue.UNKNOWN")
    if not value:
        raise ValueError(
            f"ST 0601 Item 122 {name} empty text is ambiguous; use SpecialValue.UNKNOWN"
        )
    _validate_text(value, tag=122)
    return value.encode("utf-8")


def encode_country_codes(countries: CountryCodes) -> bytes:
    """Encode an ST 0601 Item 122 country-code VLP with end-only truncation."""
    if not isinstance(countries, CountryCodes):
        raise TypeError("countries must be a CountryCodes")
    if isinstance(countries.coding_method, bool) or not isinstance(countries.coding_method, int):
        raise TypeError("ST 0601 Item 122 Coding Method must be an integer")
    if countries.coding_method not in {*range(1, 16), 64}:
        raise ValueError("ST 0601 Item 122 Coding Method is not allowed")
    if countries.overflight is None:
        raise ValueError("ST 0601 Item 122 Overflight Country is mandatory")
    if countries.operator is None and countries.manufacture is not None:
        raise ValueError(
            "ST 0601 Item 122 may omit only trailing optional country values; "
            "use SpecialValue.UNKNOWN for an interior unknown value"
        )
    components = [
        bytes((countries.coding_method,)),
        _encode_country_value(countries.overflight, name="Overflight Country"),
    ]
    if countries.operator is not None:
        components.append(_encode_country_value(countries.operator, name="Operator Country"))
    if countries.manufacture is not None:
        components.append(
            _encode_country_value(countries.manufacture, name="Country of Manufacture")
        )
    return b"".join(encode_ber_length(len(value)) + value for value in components)


def _encode_wavelength_record(record: WavelengthRecord) -> bytes:
    if not isinstance(record, WavelengthRecord):
        raise TypeError("ST 0601 Item 128 records must be WavelengthRecord instances")
    if isinstance(record.wavelength_id, bool) or not isinstance(record.wavelength_id, int):
        raise TypeError("ST 0601 Item 128 Wavelength ID must be an integer")
    if record.wavelength_id < 21:
        raise ValueError("ST 0601 Item 128 custom ID must be 21 or greater")
    minimum = _as_fraction(record.minimum_nm)
    maximum = _as_fraction(record.maximum_nm)
    if not 0 <= minimum <= 1_000_000_000 or not 0 <= maximum <= 1_000_000_000:
        raise ValueError("ST 0601 Item 128 wavelength bound is outside [0, 1000000000]")
    if minimum > maximum:
        raise ValueError("ST 0601 Item 128 minimum wavelength exceeds maximum wavelength")
    if not isinstance(record.name, str):
        raise TypeError("ST 0601 Item 128 Wavelength Name must be str")
    if not record.name:
        raise ValueError("ST 0601 Item 128 Wavelength Name is mandatory")
    _validate_text(record.name, tag=128)
    return (
        encode_ber_oid(record.wavelength_id)
        + _WAVELENGTH_MAPPING.encode(record.minimum_nm)
        + _WAVELENGTH_MAPPING.encode(record.maximum_nm)
        + record.name.encode("utf-8")
    )


def encode_wavelengths_list(wavelengths: WavelengthsList) -> bytes:
    """Encode an ST 0601 Item 128 VLP of custom wavelength definitions."""
    if not isinstance(wavelengths, WavelengthsList):
        raise TypeError("wavelengths must be a WavelengthsList")
    if not wavelengths.records:
        raise ValueError("ST 0601 Item 128 requires at least one Wavelength Record")
    if any(not isinstance(record, WavelengthRecord) for record in wavelengths.records):
        raise TypeError("ST 0601 Item 128 records must be WavelengthRecord instances")
    ids = tuple(record.wavelength_id for record in wavelengths.records)
    names = tuple(record.name for record in wavelengths.records)
    if len(set(ids)) != len(ids):
        raise ValueError("ST 0601 Item 128 contains a duplicate Wavelength ID")
    if len(set(names)) != len(names):
        raise ValueError("ST 0601 Item 128 contains a duplicate Wavelength Name")
    records = tuple(_encode_wavelength_record(record) for record in wavelengths.records)
    return b"".join(encode_ber_length(len(record)) + record for record in records)


def _encode_airbase_location(location: AirbaseLocation | SpecialValue) -> bytes:
    if isinstance(location, SpecialValue):
        if location is not SpecialValue.UNKNOWN:
            raise ValueError(f"ST 0601 Item 130 does not allow {location.value}")
        return b""
    if not isinstance(location, AirbaseLocation):
        raise TypeError("ST 0601 Item 130 locations must be AirbaseLocation instances")
    latitude = _as_fraction(location.latitude)
    longitude = _as_fraction(location.longitude)
    if not -90 <= latitude <= 90:
        raise ValueError("ST 0601 Item 130 latitude is outside [-90, 90]")
    if not -180 <= longitude <= 180:
        raise ValueError("ST 0601 Item 130 longitude is outside [-180, 180]")
    encoded = _AIRBASE_LATITUDE_MAPPING.encode(
        location.latitude
    ) + _AIRBASE_LONGITUDE_MAPPING.encode(location.longitude)
    if location.hae is not None:
        hae = _as_fraction(location.hae)
        if not -900 <= hae <= 9000:
            raise ValueError("ST 0601 Item 130 HAE is outside [-900, 9000]")
        encoded += _AIRBASE_HAE_MAPPING.encode(location.hae)
    return encoded


def encode_airbase_locations(locations: AirbaseLocations) -> bytes:
    """Encode an ST 0601 Item 130 VLP with canonical recovery truncation."""
    if not isinstance(locations, AirbaseLocations):
        raise TypeError("locations must be an AirbaseLocations")
    recovery = locations.recovery
    if recovery == locations.takeoff:
        recovery = None
    if locations.takeoff is SpecialValue.UNKNOWN and recovery in {
        None,
        SpecialValue.UNKNOWN,
    }:
        raise ValueError("ST 0601 Item 130 must be omitted when both locations are Unknown")
    encoded_locations = [_encode_airbase_location(locations.takeoff)]
    if recovery is not None:
        encoded_locations.append(_encode_airbase_location(recovery))
    encoded = b"".join(
        encode_ber_length(len(location)) + location for location in encoded_locations
    )
    if len(encoded) > 24:
        raise ValueError("ST 0601 Item 130 exceeds its 24-byte maximum length")
    return encoded


def _encode_payload_record(record: PayloadRecord) -> bytes:
    if isinstance(record.payload_id, bool) or not isinstance(record.payload_id, int):
        raise TypeError("ST 0601 Item 138 Payload ID must be an integer")
    if record.payload_id < 0:
        raise ValueError("ST 0601 Item 138 Payload ID cannot be negative")
    if isinstance(record.payload_type, bool) or not isinstance(record.payload_type, int):
        raise TypeError("ST 0601 Item 138 Payload Type must be an integer")
    if not 0 <= record.payload_type <= 4:
        raise ValueError("ST 0601 Item 138 Payload Type must be between 0 and 4")
    if not isinstance(record.name, str):
        raise TypeError("ST 0601 Item 138 Payload Name must be str")
    if not record.name:
        raise ValueError("ST 0601 Item 138 Payload Name is mandatory")
    _validate_text(record.name, tag=138)
    name = record.name.encode("utf-8")
    return (
        encode_ber_oid(record.payload_id)
        + encode_ber_oid(record.payload_type)
        + encode_ber_length(len(name))
        + name
    )


def encode_payload_list(payloads: PayloadList) -> bytes:
    """Encode a complete or distributed ST 0601 Item 138 payload list."""
    if not isinstance(payloads, PayloadList):
        raise TypeError("payloads must be a PayloadList")
    if isinstance(payloads.total_count, bool) or not isinstance(payloads.total_count, int):
        raise TypeError("ST 0601 Item 138 Payload Count must be an integer")
    if payloads.total_count < 0:
        raise ValueError("ST 0601 Item 138 Payload Count cannot be negative")
    if any(not isinstance(record, PayloadRecord) for record in payloads.records):
        raise TypeError("ST 0601 Item 138 records must be PayloadRecord instances")
    _validate_payload_records(payloads.total_count, payloads.records, error_type=ValueError)
    records = tuple(_encode_payload_record(record) for record in payloads.records)
    return encode_ber_oid(payloads.total_count) + b"".join(
        encode_ber_length(len(record)) + record for record in records
    )


def encode_active_payloads(active: ActivePayloads) -> bytes:
    """Encode ST 0601 Item 139 using payload IDs as little-indexed bits."""
    if not isinstance(active, ActivePayloads):
        raise TypeError("active must be an ActivePayloads")
    for payload_id in active.payload_ids:
        if isinstance(payload_id, bool) or not isinstance(payload_id, int):
            raise TypeError("ST 0601 Item 139 Payload IDs must be integers")
        if payload_id < 0:
            raise ValueError("ST 0601 Item 139 Payload ID cannot be negative")
    length = max(active.payload_ids, default=0) // 8 + 1
    encoded = bytearray(length)
    for payload_id in active.payload_ids:
        encoded[payload_id // 8] |= 1 << (payload_id % 8)
    return bytes(encoded)


def _encode_weapon_store(record: WeaponStore) -> bytes:
    for name, value in zip(
        ("Station ID", "Hardpoint ID", "Carriage ID", "Store ID"),
        record.address,
        strict=True,
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"ST 0601 Item 140 {name} must be an integer")
        if value < 0:
            raise ValueError(f"ST 0601 Item 140 {name} cannot be negative")
    if not isinstance(record.status, WeaponStatus):
        raise TypeError("ST 0601 Item 140 status must be WeaponStatus")
    if isinstance(record.status.general_status, bool) or not isinstance(
        record.status.general_status, int
    ):
        raise TypeError("ST 0601 Item 140 General Status must be an integer")
    if not 0 <= record.status.general_status <= 127:
        raise ValueError("ST 0601 Item 140 General Status must be between 0 and 127")
    flags = (
        record.status.fuze_enabled,
        record.status.laser_enabled,
        record.status.target_enabled,
        record.status.weapon_armed,
    )
    if any(not isinstance(flag, bool) for flag in flags):
        raise TypeError("ST 0601 Item 140 engagement status flags must be booleans")
    if not isinstance(record.weapon_type, str):
        raise TypeError("ST 0601 Item 140 Weapon Type must be str")
    if not record.weapon_type:
        raise ValueError("ST 0601 Item 140 Weapon Type is mandatory")
    _validate_text(record.weapon_type, tag=140)
    weapon_type = record.weapon_type.encode("utf-8")
    return (
        b"".join(encode_ber_oid(value) for value in record.address)
        + encode_ber_oid(record.status.raw)
        + encode_ber_length(len(weapon_type))
        + weapon_type
    )


def encode_weapons_stores(stores: WeaponsStores) -> bytes:
    """Encode an ST 0601 Item 140 VLP of weapon-store records."""
    if not isinstance(stores, WeaponsStores):
        raise TypeError("stores must be a WeaponsStores")
    if not stores.records:
        raise ValueError("ST 0601 Item 140 requires at least one Weapons Record")
    if any(not isinstance(record, WeaponStore) for record in stores.records):
        raise TypeError("ST 0601 Item 140 records must be WeaponStore instances")
    addresses = tuple(record.address for record in stores.records)
    if len(set(addresses)) != len(addresses):
        raise ValueError("ST 0601 Item 140 contains a duplicate weapon address")
    records = tuple(_encode_weapon_store(record) for record in stores.records)
    return b"".join(encode_ber_length(len(record)) + record for record in records)


def _encode_waypoint_record(record: WaypointRecord) -> bytes:
    if isinstance(record.waypoint_id, bool) or not isinstance(record.waypoint_id, int):
        raise TypeError("ST 0601 Item 141 Waypoint ID must be an integer")
    if record.waypoint_id < 0:
        raise ValueError("ST 0601 Item 141 Waypoint ID cannot be negative")
    if isinstance(record.prosecution_order, bool) or not isinstance(record.prosecution_order, int):
        raise TypeError("ST 0601 Item 141 Prosecution Order must be an integer")
    if not -32768 <= record.prosecution_order <= 32767:
        raise ValueError("ST 0601 Item 141 Prosecution Order is outside int16")
    encoded = encode_ber_oid(record.waypoint_id) + record.prosecution_order.to_bytes(
        2, "big", signed=True
    )
    if record.info is None:
        if record.location is not None:
            raise ValueError("ST 0601 Item 141 Info Value is required before Location")
        return encoded
    if not isinstance(record.info, WaypointInfo):
        raise TypeError("ST 0601 Item 141 Info Value must be WaypointInfo")
    if not isinstance(record.info.manual, bool) or not isinstance(record.info.ad_hoc, bool):
        raise TypeError("ST 0601 Item 141 Info Value flags must be booleans")
    encoded += encode_ber_oid(record.info.raw)
    if record.location is not None:
        if not isinstance(record.location, AirbaseLocation):
            raise TypeError("ST 0601 Item 141 Location must be AirbaseLocation")
        encoded += _encode_airbase_location(record.location)
    return encoded


def encode_waypoint_list(waypoints: WaypointList) -> bytes:
    """Encode a complete or distributed ST 0601 Item 141 waypoint list."""
    if not isinstance(waypoints, WaypointList):
        raise TypeError("waypoints must be a WaypointList")
    if not waypoints.records:
        raise ValueError("ST 0601 Item 141 requires at least one Waypoint Record")
    if any(not isinstance(record, WaypointRecord) for record in waypoints.records):
        raise TypeError("ST 0601 Item 141 records must be WaypointRecord instances")
    _validate_waypoint_records(waypoints.records, error_type=ValueError)
    records = tuple(_encode_waypoint_record(record) for record in waypoints.records)
    return b"".join(encode_ber_length(len(record)) + record for record in records)


def _encode_view_domain_pair(
    pair: ViewDomainPair | SpecialValue, *, index: int, value_length: int
) -> bytes:
    name = _VIEW_DOMAIN_NAMES[index]
    if isinstance(pair, SpecialValue):
        if pair is not SpecialValue.UNKNOWN:
            raise ValueError(f"ST 0601 Item 142 {name} pair does not allow {pair.value}")
        return b""
    if not isinstance(pair, ViewDomainPair):
        raise TypeError(f"ST 0601 Item 142 {name} pair must be ViewDomainPair")
    start = _as_fraction(pair.start)
    angular_range = _as_fraction(pair.angular_range)
    minimum, maximum = _VIEW_DOMAIN_BOUNDS[index]
    if not minimum <= start <= maximum:
        raise ValueError(f"ST 0601 Item 142 {name} start is outside [{minimum}, {maximum}]")
    if not 0 <= angular_range <= 360:
        raise ValueError(f"ST 0601 Item 142 {name} angular range is outside [0, 360]")
    return IMAPB(minimum, maximum, value_length).encode(pair.start) + IMAPB(
        0, 360, value_length
    ).encode(pair.angular_range)


def encode_view_domain(domain: ViewDomain, *, value_length: int = 3) -> bytes:
    """Encode ST 0601 Item 142 with a configurable IMAPB precision."""
    if not isinstance(domain, ViewDomain):
        raise TypeError("domain must be a ViewDomain")
    if isinstance(value_length, bool) or not isinstance(value_length, int):
        raise TypeError("value_length must be an integer")
    if value_length <= 0:
        raise ValueError("value_length must be positive")
    pairs = (domain.azimuth, domain.elevation, domain.roll)
    last_index = next((index for index in range(2, -1, -1) if pairs[index] is not None), None)
    if last_index is None:
        raise ValueError("ST 0601 Item 142 requires at least one domain pair")
    if any(pair is None for pair in pairs[: last_index + 1]):
        raise ValueError(
            "ST 0601 Item 142 may omit only trailing domain pairs; "
            "use SpecialValue.UNKNOWN for an interior unknown pair"
        )
    encoded = bytearray()
    for index, pair in enumerate(pairs[: last_index + 1]):
        assert pair is not None
        encoded_pair = _encode_view_domain_pair(pair, index=index, value_length=value_length)
        encoded.extend(encode_ber_length(len(encoded_pair)))
        encoded.extend(encoded_pair)
    return bytes(encoded)


def encode_metadata_substream_id(identifier: MetadataSubstreamID) -> bytes:
    """Encode an Item 143 pack for use within a Segment or Amend Local Set."""
    if not isinstance(identifier, MetadataSubstreamID):
        raise TypeError("identifier must be a MetadataSubstreamID")
    if isinstance(identifier.local_id, bool) or not isinstance(identifier.local_id, int):
        raise TypeError("ST 0601 Item 143 Local ID must be an integer")
    if identifier.local_id < 0:
        raise ValueError("ST 0601 Item 143 Local ID cannot be negative")
    local_id = encode_ber_oid(identifier.local_id)
    if len(local_id) > 17:
        raise ValueError("ST 0601 Item 143 exceeds its 17-byte maximum length")
    if identifier.local_id:
        if identifier.universal_id is not None:
            raise ValueError("ST 0601 Item 143 must omit the UUID when Local ID is nonzero")
        return local_id
    if not isinstance(identifier.universal_id, UUID):
        raise ValueError("ST 0601 Item 143 Local ID zero requires a UUID")
    return local_id + identifier.universal_id.bytes


def _encode_metadata_branch(
    values: Mapping[int, Any], *, kind: Literal["segment", "amend"]
) -> bytes:
    if not isinstance(values, Mapping):
        raise TypeError(f"{kind.title()} Local Set values must be a mapping")
    if 1 in values:
        raise ValueError("nested ST 1607 Local Sets must omit the checksum item")
    if 143 not in values or not isinstance(values[143], MetadataSubstreamID):
        raise ValueError("ST 1607 Local Set requires exactly one Metadata Substream ID")
    if any(isinstance(tag, bool) or not isinstance(tag, int) or tag < 0 for tag in values):
        raise TypeError("ST 1607 Local Set tags must be non-negative integers")
    if 100 in values and 101 in values:
        raise ValueError("ST 1607 Local Set cannot contain both Segment and Amend children")
    if kind == "amend" and 100 in values:
        raise ValueError("ST 1607 Amend Local Set cannot contain Segment children")

    encoded_items: list[bytes] = []
    for tag, value in _ordered_local_set_entries(values):
        if tag == 143:
            encoded_value = encode_metadata_substream_id(value)
        elif value is DELETE:
            if kind != "amend":
                raise ValueError("DELETE is only valid in an Amend Local Set")
            encoded_value = b""
        elif isinstance(value, RawFieldValue):
            encoded_value = value.data
        elif tag == 100:
            if not isinstance(value, SegmentLocalSet):
                raise TypeError("ST 0601 tag 100 requires SegmentLocalSet")
            encoded_value = bytes(value.local_set)
            decode_segment_local_set(encoded_value)
        elif tag == 101:
            if not isinstance(value, AmendLocalSet):
                raise TypeError("ST 0601 tag 101 requires AmendLocalSet")
            encoded_value = bytes(value.local_set)
            decode_amend_local_set(encoded_value)
        elif tag in FIELD_DEFINITIONS:
            encoded_value = encode_field_value(tag, value)
        else:
            raise TypeError(f"untyped ST 0601 tag {tag} requires RawFieldValue")
        encoded_items.append(
            encode_ber_oid(tag)
            + encode_ber_length(len(encoded_value))
            + encoded_value
        )
    encoded = b"".join(encoded_items)
    _decode_metadata_branch(
        encoded,
        kind=kind,
        field_decoding=FieldDecodingMode.STRICT,
        depth=0,
        max_depth=16,
    )
    return encoded


def encode_segment_local_set(values: Mapping[int, Any]) -> bytes:
    """Encode an ST 1607 Segment LS value for ST 0601 Item 100."""
    return _encode_metadata_branch(values, kind="segment")


def encode_amend_local_set(values: Mapping[int, Any]) -> bytes:
    """Encode an ST 1607 Amend LS value for ST 0601 Item 101."""
    return _encode_metadata_branch(values, kind="amend")


def _minimum_integer_length(value: int, *, signed: bool) -> int:
    if signed:
        bits = value.bit_length() + 1 if value >= 0 else (~value).bit_length() + 1
    else:
        bits = value.bit_length()
    return max(1, (bits + 7) // 8)


def _validate_text(value: str, *, tag: int) -> None:
    if value and (value[0] in "\x00\t\n\r " or value[-1] in "\x00\t\n\r "):
        raise ValueError(f"ST 0601 tag {tag} violates ST 0107 trimmed UTF-8 rules")
    if any(
        ord(character) <= 0x08
        or ord(character) in {0x0B, 0x0C, 0x7F}
        or 0x0E <= ord(character) <= 0x1F
        for character in value
    ):
        raise ValueError(f"ST 0601 tag {tag} violates ST 0107 UTF-8 control rules")


def encode_field_value(tag: int, value: Any) -> bytes:
    """Encode one currently supported ST 0601 field value.

    Fixed mapped fields that define the ST 0601 ``Out of Range`` sentinel
    encode that sentinel when a numeric producer value lies outside the field's
    physical domain, as required by ST 0601.13-27. Other range violations remain
    errors; in particular, an ``Off Earth`` condition must be supplied
    explicitly because it cannot be inferred from an invalid coordinate alone.
    """
    try:
        definition = FIELD_DEFINITIONS[tag]
    except KeyError as error:
        raise ValueError(f"ST 0601 tag {tag} is not supported for typed encoding") from error
    if tag == 1:
        raise ValueError("checksum is calculated by encode_uas_local_set")

    if isinstance(value, SpecialValue):
        if value is SpecialValue.UNKNOWN:
            if tag in {2, 65, 100, 101}:
                raise ValueError(f"ST 0601 mandatory tag {tag} cannot be Unknown")
            if definition.multiple:
                raise ValueError(
                    f"ST 0601 multi-use tag {tag} cannot be a zero-length item"
                )
            return b""
        if value is not definition.special_value or definition.special_raw is None:
            raise ValueError(f"ST 0601 tag {tag} does not define special value {value.value}")
        assert definition.length is not None
        return definition.special_raw.to_bytes(
            definition.length,
            "big",
            signed=definition.signed,
        )

    if definition.kind == "text":
        if not isinstance(value, str):
            raise TypeError(f"ST 0601 tag {tag} requires str")
        if value == "":
            return b"\x00"
        _validate_text(value, tag=tag)
        encoded = value.encode("utf-8")
        if definition.maximum_length is not None and len(encoded) > definition.maximum_length:
            raise ValueError(
                f"ST 0601 tag {tag} exceeds its {definition.maximum_length}-byte maximum length"
            )
        return encoded
    if definition.kind == "timestamp":
        micros = _timestamp_microseconds(value, name="timestamp")
        length = definition.length or _minimum_integer_length(micros, signed=False)
        if definition.maximum_length is not None and length > definition.maximum_length:
            raise ValueError(
                f"ST 0601 tag {tag} timestamp exceeds its {definition.maximum_length}-byte range"
            )
        return micros.to_bytes(length, "big")
    if definition.kind in {"uint", "sint"}:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"ST 0601 tag {tag} requires int")
        if (definition.integer_min is not None and value < definition.integer_min) or (
            definition.integer_max is not None and value > definition.integer_max
        ):
            raise ValueError(f"ST 0601 tag {tag} integer {value} is outside its permitted range")
        semantic_type = _SEMANTIC_INTEGER_TYPES.get(tag)
        if semantic_type is not None:
            try:
                semantic_type(value)
            except ValueError as error:
                raise ValueError(f"ST 0601 tag {tag}: {error}") from error
        integer_length = definition.length
        if integer_length is None:
            integer_length = _minimum_integer_length(value, signed=definition.kind == "sint")
            if definition.maximum_length is not None and integer_length > definition.maximum_length:
                raise ValueError(
                    f"ST 0601 tag {tag} integer is out of its "
                    f"{definition.maximum_length}-byte range"
                )
        try:
            return value.to_bytes(
                integer_length,
                "big",
                signed=definition.kind == "sint",
            )
        except OverflowError as error:
            raise ValueError(f"ST 0601 tag {tag} integer is out of range") from error

    if definition.kind == "imap":
        if not isinstance(value, IMAPFieldValue):
            raise TypeError(f"ST 0601 tag {tag} requires IMAPFieldValue")
        maximum_length = definition.maximum_length or 8
        if (
            isinstance(value.length, bool)
            or not isinstance(value.length, int)
            or not 1 <= value.length <= maximum_length
        ):
            raise ValueError(
                f"ST 0601 tag {tag} IMAP length must be between 1 and {maximum_length} bytes"
            )
        assert definition.physical_min is not None
        assert definition.physical_max is not None
        physical = _as_fraction(value.value)
        if not definition.physical_min <= physical <= definition.physical_max:
            raise ValueError(
                f"ST 0601 tag {tag} value {float(physical)} is outside "
                f"[{float(definition.physical_min)}, {float(definition.physical_max)}]"
            )
        return IMAPB(
            definition.physical_min,
            definition.physical_max,
            value.length,
        ).encode(physical)

    if definition.kind == "vmti":
        if isinstance(value, VMTILocalSet):
            if value.standalone:
                raise ValueError("ST 0601 tag 74 requires embedded VMTI without a checksum")
            return bytes(value.local_set)
        if not isinstance(value, bytes):
            raise TypeError(f"ST 0601 tag {tag} requires VMTILocalSet or bytes")
        decode_vmti_local_set(value, standalone=False)
        return value

    if definition.kind == "miis":
        if not isinstance(value, MIISCoreIdentifier):
            raise TypeError(f"ST 0601 tag {tag} requires MIISCoreIdentifier")
        return encode_miis_core_identifier(value)

    if definition.kind == "security":
        if not isinstance(value, SecurityLocalSet):
            raise TypeError(f"ST 0601 tag {tag} requires SecurityLocalSet")
        if value.standalone:
            raise ValueError("ST 0601 tag 48 requires embedded Security Local Set data")
        return bytes(value.local_set)

    if definition.kind == "rvt":
        if not isinstance(value, RVTLocalSet):
            raise TypeError(f"ST 0601 tag {tag} requires RVTLocalSet")
        if value.standalone:
            raise ValueError("ST 0601 tag 73 requires embedded RVT without a CRC")
        encoded = bytes(value.local_set)
        decode_rvt_local_set(encoded, standalone=False)
        return encoded

    if definition.kind == "range_image":
        if not isinstance(value, RangeImageLocalSet):
            raise TypeError(f"ST 0601 tag {tag} requires RangeImageLocalSet")
        if value.standalone:
            raise ValueError("ST 0601 tag 97 requires embedded Range Image data")
        return encode_range_image_local_set(value, standalone=False, preserve=True)

    if definition.kind == "geo_registration":
        if not isinstance(value, GeoRegistrationLocalSet):
            raise TypeError(f"ST 0601 tag {tag} requires GeoRegistrationLocalSet")
        return encode_geo_registration_local_set(value, preserve=True)

    if definition.kind == "composite_imaging":
        if not isinstance(value, CompositeImagingLocalSet):
            raise TypeError(f"ST 0601 tag {tag} requires CompositeImagingLocalSet")
        return encode_composite_imaging_local_set(value, preserve=True)

    if definition.kind == "sar":
        if not isinstance(value, SARMotionImageryLocalSet):
            raise TypeError(f"ST 0601 tag {tag} requires SARMotionImageryLocalSet")
        if value.standalone:
            raise ValueError("ST 0601 tag 95 requires embedded SAR metadata")
        return encode_sar_motion_imagery_local_set(value, standalone=False, preserve=True)

    if definition.kind == "sdcc":
        if not isinstance(value, SDCCFLP):
            raise TypeError(f"ST 0601 tag {tag} requires SDCCFLP")
        if value.parse_control.mode != 2:
            raise ValueError("ST 0601 Item 102 requires ST 1010 Mode 2 Parse Control")
        return encode_sdcc_flp(value)

    if definition.kind == "horizon":
        if not isinstance(value, ImageHorizonPixelPack):
            raise TypeError(f"ST 0601 tag {tag} requires ImageHorizonPixelPack")
        return encode_image_horizon_pixel_pack(value)

    if definition.kind == "frame_rate":
        if not isinstance(value, SensorFrameRatePack):
            raise TypeError(f"ST 0601 tag {tag} requires SensorFrameRatePack")
        return encode_sensor_frame_rate_pack(value)

    if definition.kind == "control_command":
        if not isinstance(value, ControlCommand):
            raise TypeError(f"ST 0601 tag {tag} requires ControlCommand")
        return encode_control_command(value)

    if definition.kind == "command_verification":
        if not isinstance(value, ControlCommandVerificationList):
            raise TypeError(f"ST 0601 tag {tag} requires ControlCommandVerificationList")
        return encode_control_command_verification_list(value)

    if definition.kind == "active_wavelengths":
        if not isinstance(value, ActiveWavelengthList):
            raise TypeError(f"ST 0601 tag {tag} requires ActiveWavelengthList")
        return encode_active_wavelength_list(value)

    if definition.kind == "country_codes":
        if not isinstance(value, CountryCodes):
            raise TypeError(f"ST 0601 tag {tag} requires CountryCodes")
        return encode_country_codes(value)

    if definition.kind == "wavelengths":
        if not isinstance(value, WavelengthsList):
            raise TypeError(f"ST 0601 tag {tag} requires WavelengthsList")
        return encode_wavelengths_list(value)

    if definition.kind == "airbase_locations":
        if not isinstance(value, AirbaseLocations):
            raise TypeError(f"ST 0601 tag {tag} requires AirbaseLocations")
        return encode_airbase_locations(value)

    if definition.kind == "payload_list":
        if not isinstance(value, PayloadList):
            raise TypeError(f"ST 0601 tag {tag} requires PayloadList")
        return encode_payload_list(value)

    if definition.kind == "active_payloads":
        if not isinstance(value, ActivePayloads):
            raise TypeError(f"ST 0601 tag {tag} requires ActivePayloads")
        return encode_active_payloads(value)

    if definition.kind == "weapons_stores":
        if not isinstance(value, WeaponsStores):
            raise TypeError(f"ST 0601 tag {tag} requires WeaponsStores")
        return encode_weapons_stores(value)

    if definition.kind == "waypoint_list":
        if not isinstance(value, WaypointList):
            raise TypeError(f"ST 0601 tag {tag} requires WaypointList")
        return encode_waypoint_list(value)

    if definition.kind == "view_domain":
        if not isinstance(value, ViewDomain):
            raise TypeError(f"ST 0601 tag {tag} requires ViewDomain")
        return encode_view_domain(value)

    if definition.kind == "segment":
        if not isinstance(value, SegmentLocalSet):
            raise TypeError(f"ST 0601 tag {tag} requires SegmentLocalSet")
        encoded = bytes(value.local_set)
        decode_segment_local_set(encoded)
        return encoded

    if definition.kind == "amend":
        if not isinstance(value, AmendLocalSet):
            raise TypeError(f"ST 0601 tag {tag} requires AmendLocalSet")
        encoded = bytes(value.local_set)
        decode_amend_local_set(encoded)
        return encoded

    assert definition.length is not None
    assert definition.physical_min is not None
    assert definition.physical_max is not None
    physical = _as_fraction(value)
    if not definition.physical_min <= physical <= definition.physical_max:
        if definition.special_value is SpecialValue.OUT_OF_RANGE:
            assert definition.special_raw is not None
            return definition.special_raw.to_bytes(
                definition.length,
                "big",
                signed=definition.signed,
            )
        raise ValueError(
            f"ST 0601 tag {tag} value {float(physical)} is outside "
            f"[{float(definition.physical_min)}, {float(definition.physical_max)}]"
        )
    raw_min, raw_max = _integer_domain(definition.length, definition.signed, definition.special_raw)
    scaled = Fraction(raw_min) + Fraction(
        (physical - definition.physical_min) * (raw_max - raw_min),
        definition.physical_max - definition.physical_min,
    )
    raw_value = _round_fraction(scaled)
    return raw_value.to_bytes(definition.length, "big", signed=definition.signed)


def _ordered_local_set_entries(
    values: Mapping[int, Any], *, first_tags: tuple[int, ...] = ()
) -> list[tuple[int, Any]]:
    if 102 not in values:
        ordered_tags = [
            *first_tags,
            *sorted(tag for tag in values if tag not in first_tags),
        ]
        return [
            (tag, field_value)
            for tag in ordered_tags
            for field_value in _field_value_instances(tag, values[tag])
        ]

    groups = _field_value_instances(102, values[102])
    claimed_sources: set[int] = set()
    sdcc_groups: list[tuple[SDCCFLP, tuple[int, ...]]] = []
    for group in groups:
        if not isinstance(group, SDCCFLP):
            raise TypeError("ST 0601 tag 102 requires SDCCFLP")
        source_tags = group.source_tags
        if len(source_tags) != group.matrix_size:
            count_name = {
                1: "one",
                2: "two",
                3: "three",
                4: "four",
            }.get(group.matrix_size, str(group.matrix_size))
            raise ValueError(
                f"ST 0601 Item 102 Matrix Size {group.matrix_size} requires "
                f"{count_name} source tags"
            )
        for source_tag in source_tags:
            if source_tag not in ST0601_SDCC_SOURCE_TAGS:
                raise ValueError(
                    f"ST 0601 tag {source_tag} is not eligible for an Item 102 source list"
                )
            if source_tag not in values:
                raise ValueError(
                    f"ST 0601 Item 102 source tag {source_tag} is not present in values"
                )
            if source_tag in claimed_sources:
                raise ValueError(
                    f"ST 0601 source tag {source_tag} cannot belong to more than one "
                    "encoded Item 102 group"
                )
            claimed_sources.add(source_tag)
        sdcc_groups.append((group, source_tags))

    excluded = claimed_sources | {102, *first_tags}
    prelude_tags = [*first_tags, *sorted(tag for tag in values if tag not in excluded)]
    entries = [
        (tag, field_value)
        for tag in prelude_tags
        for field_value in _field_value_instances(tag, values[tag])
    ]
    for group, source_tags in sdcc_groups:
        entries.extend((tag, values[tag]) for tag in source_tags)
        entries.append((102, group))
    return entries


def _validate_metadata_birth_timestamp(
    timestamp: object,
    context: ST0601ValidationContext | None,
    *,
    error_type: type[Exception],
) -> None:
    if context is None:
        return
    if not isinstance(context, ST0601ValidationContext):
        raise TypeError("context must be an ST0601ValidationContext or None")
    if context.metadata_birth_timestamp is None:
        return
    if timestamp is None:
        raise error_type(
            "ST 0601 Precision Time Stamp must be decodable to validate metadata time of birth"
        )
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, datetime)):
        raise error_type(
            "ST 0601 Precision Time Stamp requires integer microseconds or an aware datetime"
        )
    try:
        observed = _timestamp_microseconds(timestamp, name="ST 0601 Precision Time Stamp")
    except (TypeError, ValueError) as error:
        raise error_type(str(error)) from error
    expected = _timestamp_microseconds(
        context.metadata_birth_timestamp,
        name="metadata_birth_timestamp",
    )
    if observed != expected:
        raise error_type(
            "ST 0601 Precision Time Stamp must represent the time of birth of all metadata "
            "in the Local Set"
        )


def _validate_imap_system_precision(
    lengths: Mapping[int, int],
    context: ST0601ValidationContext | None,
    *,
    error_type: type[Exception],
) -> None:
    if context is None:
        return
    if not isinstance(context, ST0601ValidationContext):
        raise TypeError("context must be an ST0601ValidationContext or None")
    for tag in context.imap_system_precisions:
        observed = lengths.get(tag)
        if observed is None:
            continue
        required = context.required_imap_length(tag)
        assert required is not None
        if observed != required:
            raise error_type(
                f"ST 0601 tag {tag} encoded length {observed} does not satisfy producer "
                f"system precision; requires {required} bytes"
            )


def _validate_field_expectations(
    uas: UASLocalSet,
    context: ST0601ValidationContext | None,
    *,
    error_type: type[Exception],
) -> None:
    if context is None:
        return
    for tag, expectation in context.field_expectations.items():
        field = uas.get(tag)
        if field is None or field.value is SpecialValue.UNKNOWN:
            raise error_type(
                f"ST 0601 tag {tag} is not present with a known value required by "
                "producer-supplied ground truth"
            )
        if not expectation.matches(field.value):
            tolerance = (
                ""
                if expectation.absolute_tolerance is None
                else f" within absolute tolerance {float(expectation.absolute_tolerance):g}"
            )
            raise error_type(
                f"ST 0601 tag {tag} decoded value {field.value!r} does not match "
                f"producer-supplied ground truth {expectation.value!r}{tolerance}"
            )


def _embedded_vmti_context(
    parent_timestamp: object,
    context: ST0601ValidationContext | None,
    *,
    error_type: type[Exception],
) -> VMTIValidationContext | None:
    if context is None or context.vmti_context is None:
        return None
    if isinstance(parent_timestamp, bool) or not isinstance(
        parent_timestamp, (int, datetime)
    ):
        raise error_type(
            "ST 0601 Precision Time Stamp must be decodable to validate embedded VMTI"
        )
    vmti_context = context.vmti_context
    if vmti_context.parent_timestamp is not None:
        supplied = _timestamp_microseconds(
            vmti_context.parent_timestamp,
            name="VMTI parent_timestamp",
        )
        observed = _timestamp_microseconds(
            parent_timestamp,
            name="ST 0601 Precision Time Stamp",
        )
        if supplied != observed:
            raise error_type(
                "VMTI parent_timestamp does not match enclosing ST 0601 Precision Time Stamp"
            )
    return replace(vmti_context, parent_timestamp=parent_timestamp)


def encode_uas_local_set(
    values: Mapping[int, Any],
    *,
    context: ST0601ValidationContext | None = None,
) -> bytes:
    """Encode supported ST 0601 fields and append the required checksum.

    Tag 2 is emitted first and the computed Tag 1 checksum is emitted last.
    Other tags are normally numeric; Item 102 groups instead preserve each
    declared Refined Source List order immediately before the SDCC-FLP.
    """
    if 1 in values:
        raise ValueError("do not provide tag 1; checksum is computed automatically")
    if 143 in values:
        raise ValueError("ST 0601 Metadata Substream ID is forbidden at the root level")
    if 100 in values and 101 in values:
        raise ValueError("ST 1607 root cannot contain both Segment and Amend children")
    if 2 not in values:
        raise ValueError("ST 0601 Precision Time Stamp (tag 2) is required")
    if 65 not in values:
        raise ValueError("ST 0601 UAS Datalink LS Version Number (tag 65) is required")
    _validate_metadata_birth_timestamp(
        values[2],
        context,
        error_type=ValueError,
    )
    _validate_imap_system_precision(
        {
            tag: value.length
            for tag, value in values.items()
            if isinstance(value, IMAPFieldValue)
        },
        context,
        error_type=ValueError,
    )
    vmti_context = (
        _embedded_vmti_context(
            values[2],
            context,
            error_type=ValueError,
        )
        if 74 in values
        else None
    )
    encoded_items = []
    for tag, field_value in _ordered_local_set_entries(values, first_tags=(2,)):
        encoded_value = encode_field_value(tag, field_value)
        if tag == 74 and vmti_context is not None:
            try:
                decode_vmti_local_set(
                    encoded_value,
                    standalone=False,
                    context=vmti_context,
                )
            except DecodeError as error:
                raise ValueError(str(error)) from error
        encoded_items.append(
            encode_ber_oid(tag) + encode_ber_length(len(encoded_value)) + encoded_value
        )
    local_value = b"".join(encoded_items) + b"\x01\x02\x00\x00"
    packet = ST0601_KEY + encode_ber_length(len(local_value)) + local_value
    checksum = running_sum_16(packet[:-2]).to_bytes(2, "big")
    result = packet[:-2] + checksum
    if context is not None and context.field_expectations:
        decoded = decode_uas_local_set(result)
        _validate_field_expectations(decoded, context, error_type=ValueError)
    return result


def update_uas_local_set(
    source: bytes | UASLocalSet,
    updates: Mapping[int, Any | RawFieldValue | UpdateAction],
    *,
    field_decoding: FieldDecodingMode = FieldDecodingMode.STRICT,
    context: ST0601ValidationContext | None = None,
) -> bytes:
    """Losslessly update selected ST 0601 items and recompute the checksum.

    Untouched items retain their exact tag, length, and value octets. Updating a
    repeated extension tag replaces all its occurrences with one canonical item.
    Unsupported tags require :class:`RawFieldValue` to make raw-wire intent
    explicit.
    """

    if 1 in updates:
        raise ValueError("do not update tag 1; checksum is computed automatically")
    uas = (
        source
        if isinstance(source, UASLocalSet)
        else decode_uas_local_set(source, field_decoding=field_decoding)
    )
    if not updates:
        decode_uas_local_set(
            uas.packet,
            field_decoding=field_decoding,
            context=context,
        )
        return uas.packet.raw

    handled: set[int] = set()
    encoded_items: list[bytes] = []
    for item in uas.local_set.items:
        if item.tag == 1:
            continue
        if item.tag not in updates:
            encoded_items.append(bytes(item))
            continue
        if item.tag in handled:
            continue
        handled.add(item.tag)
        replacement = updates[item.tag]
        if replacement is DELETE:
            continue
        encoded_items.extend(_encode_updated_items(item.tag, replacement))

    for tag in sorted(set(updates) - handled):
        replacement = updates[tag]
        if replacement is DELETE:
            continue
        encoded = _encode_updated_items(tag, replacement)
        if tag == 2:
            encoded_items[0:0] = encoded
        else:
            encoded_items.extend(encoded)

    local_value = b"".join(encoded_items) + b"\x01\x02\x00\x00"
    packet = ST0601_KEY + encode_ber_length(len(local_value)) + local_value
    result = packet[:-2] + running_sum_16(packet[:-2]).to_bytes(2, "big")
    decode_uas_local_set(result, field_decoding=field_decoding, context=context)
    return result


def _field_value_instances(tag: int, value: Any) -> tuple[Any, ...]:
    definition = FIELD_DEFINITIONS.get(tag)
    if definition is not None and definition.multiple and isinstance(value, (list, tuple)):
        if not value:
            raise ValueError(f"ST 0601 repeated tag {tag} requires at least one value")
        return tuple(value)
    return (value,)


def _encode_updated_items(tag: int, value: Any) -> tuple[bytes, ...]:
    if tag == 143:
        raise ValueError("ST 0601 Metadata Substream ID is forbidden at the root level")
    field_values: tuple[bytes, ...]
    if isinstance(value, RawFieldValue):
        field_values = (value.data,)
    elif tag in FIELD_DEFINITIONS:
        field_values = tuple(
            encode_field_value(tag, instance) for instance in _field_value_instances(tag, value)
        )
    else:
        raise TypeError(f"untyped ST 0601 tag {tag} requires RawFieldValue")
    return tuple(
        encode_ber_oid(tag) + encode_ber_length(len(encoded_value)) + encoded_value
        for encoded_value in field_values
    )


def _parse_single_packet(data: bytes) -> KLVPacket:
    parser = KLVStreamParser(key_prefix=ST0601_KEY, max_value_length=64 * 1024 * 1024)
    packets = parser.feed(data)
    packets.extend(parser.finish())
    if len(packets) != 1:
        raise DecodeError(f"expected exactly one ST 0601 packet, observed {len(packets)}")
    return packets[0]


def decode_uas_local_set(
    data: bytes | KLVPacket,
    *,
    verify_checksum: bool = True,
    require_timestamp: bool = True,
    require_version: bool = True,
    field_decoding: FieldDecodingMode = FieldDecodingMode.STRICT,
    context: ST0601ValidationContext | None = None,
) -> UASLocalSet:
    """Decode one ST 0601 Universal KLV packet and its known fields.

    ``PRESERVE`` retains an otherwise structurally valid packet when an
    individual known field cannot be decoded. The raw item remains in
    ``local_set`` and a diagnostic is added to ``issues``; checksum, required
    tags, singleton rules, and Local Set structure remain strict.
    """
    if not isinstance(field_decoding, FieldDecodingMode):
        raise TypeError("field_decoding must be a FieldDecodingMode")
    if context is not None and not isinstance(context, ST0601ValidationContext):
        raise TypeError("context must be an ST0601ValidationContext or None")
    packet = data if isinstance(data, KLVPacket) else _parse_single_packet(data)
    if packet.key != ST0601_KEY:
        raise DecodeError(
            f"unexpected Universal Key {packet.key.hex(' ')} for ST 0601 UAS Local Set"
        )
    local_set = parse_local_set(packet.value)
    if not local_set.items:
        raise DecodeError("ST 0601 Local Set is empty")
    if local_set.getall(143):
        raise DecodeError("ST 0601 Metadata Substream ID is forbidden at the root level")
    if local_set.getall(100) and local_set.getall(101):
        raise DecodeError("ST 1607 root cannot contain both Segment and Amend children")
    checksum = local_set.items[-1]
    if checksum.tag != 1 or len(checksum.value) != 2:
        raise ChecksumError("ST 0601 checksum must be the final tag with a 2-byte value")
    first = local_set.items[0]
    timestamps = local_set.getall(2)
    versions = local_set.getall(65)
    if timestamps and first.tag != 2:
        raise DecodeError("ST 0601 Precision Time Stamp (tag 2) must be the first item")
    if require_timestamp and not timestamps:
        raise DecodeError("ST 0601 Precision Time Stamp (tag 2) is required")
    if require_version and not versions:
        raise DecodeError("ST 0601 UAS Datalink LS Version Number (tag 65) is required")
    _validate_imap_system_precision(
        {
            item.tag: len(item.value)
            for item in local_set.items
            if item.tag in (context.imap_system_precisions if context else ())
        },
        context,
        error_type=DecodeError,
    )
    for tag, field_definition in FIELD_DEFINITIONS.items():
        items = local_set.getall(tag)
        if not field_definition.multiple and len(items) > 1:
            raise DecodeError(f"ST 0601 singleton tag {tag} occurs twice")
        if field_definition.multiple and any(not item.value for item in items):
            raise DecodeError(f"ST 0601 multi-use tag {tag} cannot be a zero-length item")
    if verify_checksum:
        expected = int.from_bytes(checksum.value, "big")
        observed = running_sum_16(packet.raw[:-2])
        if observed != expected:
            raise ChecksumError(
                f"ST 0601 checksum mismatch: expected 0x{expected:04X}, computed 0x{observed:04X}"
            )
    parent_timestamp: object = None
    if timestamps:
        # The normal field-decoding loop below owns strict/preserve handling
        # for a malformed timestamp. Without one there is no valid parent
        # time to propagate into the embedded VMTI validator.
        with suppress(DecodeError):
            parent_timestamp = _decode_field(
                timestamps[0],
                FIELD_DEFINITIONS[2],
            ).value
    vmti_context: VMTIValidationContext | None = None
    vmti_context_error: DecodeError | None = None
    if local_set.getall(74):
        try:
            vmti_context = _embedded_vmti_context(
                parent_timestamp,
                context,
                error_type=DecodeError,
            )
        except DecodeError as error:
            if field_decoding is FieldDecodingMode.STRICT:
                raise
            vmti_context_error = error
    fields: list[DecodedField] = []
    issues: list[FieldDecodingIssue] = []
    for item in local_set.items:
        definition = FIELD_DEFINITIONS.get(item.tag)
        if definition is None:
            continue
        try:
            if item.tag == 74 and vmti_context_error is not None:
                raise vmti_context_error
            fields.append(
                _decode_field(
                    item,
                    definition,
                    vmti_context=vmti_context if item.tag == 74 else None,
                )
            )
        except DecodeError as error:
            if field_decoding is FieldDecodingMode.STRICT:
                raise
            issues.append(
                FieldDecodingIssue(
                    item.tag,
                    definition.name,
                    str(error),
                    item.value,
                    item,
                )
            )
    bound_fields = _bind_sdcc_source_tags(local_set, tuple(fields))
    result = UASLocalSet(packet, local_set, bound_fields, tuple(issues))
    _validate_metadata_birth_timestamp(
        result.value(2),
        context,
        error_type=DecodeError,
    )
    _validate_field_expectations(result, context, error_type=DecodeError)
    return result
