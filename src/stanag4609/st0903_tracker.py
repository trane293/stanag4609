"""MISB ST 0903.6 VTracker Local Set codec."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import UUID

from stanag4609.errors import DecodeError
from stanag4609.klv.model import LocalSet
from stanag4609.st0903_geo import (
    Acceleration,
    Location,
    Velocity,
    decode_acceleration,
    decode_boundary_series,
    decode_location_series,
    decode_velocity,
    encode_acceleration,
    encode_boundary_series,
    encode_location_series,
    encode_velocity,
)
from stanag4609.st0903_vocab import (
    RawVMTIValue,
    _decode_uint,
    _encode_extensions,
    _extensions,
    _item,
    _parse_nested,
    _uint,
    _validate_uint,
)

_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_KNOWN_TAGS = frozenset({1, 3, 4, 5, 7, 9, 10, 11, 12})


def _timestamp_microseconds(value: datetime, *, name: str) -> int:
    if not isinstance(value, datetime):
        raise TypeError(f"ST 0903 VTracker {name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"ST 0903 VTracker {name} must be timezone-aware")
    delta = value.astimezone(timezone.utc) - _UNIX_EPOCH
    micros = delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
    if not 0 <= micros <= 2**64 - 1:
        raise ValueError(f"ST 0903 VTracker {name} is outside the unsigned 64-bit range")
    return micros


def _decode_timestamp(data: bytes, *, name: str) -> datetime:
    if len(data) != 8:
        raise DecodeError(f"ST 0903 VTracker {name} must contain 8 bytes")
    micros = int.from_bytes(data, "big")
    try:
        return _UNIX_EPOCH + timedelta(microseconds=micros)
    except OverflowError as error:
        raise DecodeError(f"ST 0903 VTracker {name} is outside datetime range") from error


def _validate_location_tuple(
    values: tuple[Location, ...],
    *,
    name: str,
    minimum: int,
) -> None:
    if not isinstance(values, tuple) or any(not isinstance(value, Location) for value in values):
        raise TypeError(f"ST 0903 VTracker {name} must be a tuple of Location values")
    if values and len(values) < minimum:
        raise ValueError(f"ST 0903 VTracker {name} requires at least {minimum} Locations")


@dataclass(frozen=True, slots=True)
class VTrackerLocalSet:
    """Spatial and temporal track state nested in a VTarget."""

    track_id: UUID | None = None
    first_observation_time: datetime | None = None
    latest_observation_time: datetime | None = None
    track_boundary_series: tuple[Location, ...] = ()
    confidence_level: int | None = None
    track_history_series: tuple[Location, ...] = ()
    velocity: Velocity | None = None
    acceleration: Acceleration | None = None
    algorithm_id: int | None = None
    extensions: Mapping[int, RawVMTIValue] = field(default_factory=dict)
    local_set: LocalSet | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.track_id is not None and not isinstance(self.track_id, UUID):
            raise TypeError("ST 0903 VTracker track_id must be a UUID or None")
        first_micros = (
            _timestamp_microseconds(self.first_observation_time, name="firstObsvTime")
            if self.first_observation_time is not None
            else None
        )
        latest_micros = (
            _timestamp_microseconds(self.latest_observation_time, name="latestObsvTime")
            if self.latest_observation_time is not None
            else None
        )
        if first_micros is not None and latest_micros is not None and latest_micros < first_micros:
            raise ValueError("ST 0903 VTracker latestObsvTime cannot be before firstObsvTime")
        _validate_location_tuple(
            self.track_boundary_series,
            name="trackBoundarySeries",
            minimum=2,
        )
        _validate_location_tuple(
            self.track_history_series,
            name="trackHistorySeries",
            minimum=1,
        )
        if self.confidence_level is not None:
            _validate_uint(
                self.confidence_level,
                name="ST 0903 VTracker confidenceLevel",
                maximum=100,
            )
        if self.velocity is not None and not isinstance(self.velocity, Velocity):
            raise TypeError("ST 0903 VTracker velocity must be a Velocity or None")
        if self.acceleration is not None and not isinstance(self.acceleration, Acceleration):
            raise TypeError("ST 0903 VTracker acceleration must be an Acceleration or None")
        if (self.velocity is not None or self.acceleration is not None) and not (
            self.track_history_series
        ):
            raise ValueError(
                "ST 0903 VTracker velocity and acceleration require an associated "
                "trackHistorySeries Location"
            )
        if self.algorithm_id is not None:
            _validate_uint(self.algorithm_id, name="ST 0903 VTracker algorithmId")


def decode_vtracker_local_set(data: bytes) -> VTrackerLocalSet:
    """Decode one embedded ST 0903.6 VTracker Local Set."""
    local_set = _parse_nested(data, name="VTracker", known=_KNOWN_TAGS)
    items = {item.tag: item.value for item in local_set.items}
    try:
        return VTrackerLocalSet(
            _decode_track_id(items),
            _decode_timestamp(items[3], name="firstObsvTime") if 3 in items else None,
            _decode_timestamp(items[4], name="latestObsvTime") if 4 in items else None,
            decode_boundary_series(items[5]) if 5 in items else (),
            _decode_confidence(items[7]) if 7 in items else None,
            decode_location_series(items[9]) if 9 in items else (),
            decode_velocity(items[10]) if 10 in items else None,
            decode_acceleration(items[11]) if 11 in items else None,
            _decode_uint(items[12], name="VTracker algorithmId") if 12 in items else None,
            _extensions(local_set, _KNOWN_TAGS),
            local_set,
        )
    except (TypeError, ValueError) as error:
        raise DecodeError(str(error)) from error


def _decode_track_id(items: dict[int, bytes]) -> UUID | None:
    data = items.get(1)
    if data is None:
        return None
    if len(data) != 16:
        raise DecodeError("ST 0903 VTracker trackId must contain 16 bytes")
    return UUID(bytes=data)


def _decode_confidence(data: bytes) -> int:
    if len(data) != 1:
        raise DecodeError("ST 0903 VTracker confidenceLevel must contain 1 byte")
    value = data[0]
    if value > 100:
        raise DecodeError("ST 0903 VTracker confidenceLevel must be between 0 and 100")
    return value


def encode_vtracker_local_set(value: VTrackerLocalSet, *, preserve: bool = False) -> bytes:
    """Encode one embedded ST 0903.6 VTracker Local Set."""
    if not isinstance(value, VTrackerLocalSet):
        raise TypeError("value must be a VTrackerLocalSet")
    if preserve and value.local_set is not None:
        return value.local_set.raw
    output = bytearray()
    if value.track_id is not None:
        output.extend(_item(1, value.track_id.bytes))
    if value.first_observation_time is not None:
        micros = _timestamp_microseconds(value.first_observation_time, name="firstObsvTime")
        output.extend(_item(3, micros.to_bytes(8, "big")))
    if value.latest_observation_time is not None:
        micros = _timestamp_microseconds(value.latest_observation_time, name="latestObsvTime")
        output.extend(_item(4, micros.to_bytes(8, "big")))
    if value.track_boundary_series:
        output.extend(_item(5, encode_boundary_series(value.track_boundary_series)))
    if value.confidence_level is not None:
        output.extend(_item(7, bytes((value.confidence_level,))))
    if value.track_history_series:
        output.extend(_item(9, encode_location_series(value.track_history_series)))
    if value.velocity is not None:
        output.extend(_item(10, encode_velocity(value.velocity)))
    if value.acceleration is not None:
        output.extend(_item(11, encode_acceleration(value.acceleration)))
    if value.algorithm_id is not None:
        output.extend(_item(12, _uint(value.algorithm_id, name="VTracker algorithmId")))
    output.extend(_encode_extensions(value.extensions, after=12))
    return bytes(output)
