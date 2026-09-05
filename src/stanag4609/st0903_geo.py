"""ST 0903.6 Location, Velocity, Acceleration, and Boundary Series codecs."""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from itertools import pairwise
from typing import TYPE_CHECKING, Literal, TypeAlias

from stanag4609.errors import DecodeError, LimitExceeded, NeedMoreData, TruncatedData
from stanag4609.imap import IMAPB, IMAPSpecialValue
from stanag4609.klv.ber import decode_ber_length, encode_ber_length

if TYPE_CHECKING:
    from stanag4609.st0903 import VTarget

IMAPValue: TypeAlias = int | float | Fraction | IMAPSpecialValue

_LATITUDE = IMAPB(-90, 90, 4)
_LONGITUDE = IMAPB(-180, 180, 4)
_HAE = IMAPB(-900, 19_000, 2)
_VECTOR = IMAPB(-900, 900, 2)
_SIGMA = IMAPB(0, 650, 2)
_RHO = IMAPB(-1, 1, 2)
DEFAULT_MAX_BOUNDARY_GEOMETRY_VERTICES = 2_048


def _validate_value(value: IMAPValue, codec: IMAPB, *, name: str) -> None:
    if isinstance(value, bool):
        raise TypeError(f"ST 0903 {name} must be numeric or an IMAP special value")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"ST 0903 {name} must be finite or an explicit IMAP special value")
    if isinstance(value, IMAPSpecialValue):
        codec.encode(value)
        return
    try:
        numeric = Fraction(str(value)) if isinstance(value, float) else Fraction(value)
    except (TypeError, ValueError, ZeroDivisionError) as error:
        raise TypeError(f"ST 0903 {name} must be numeric or an IMAP special value") from error
    if not codec.minimum <= numeric <= codec.maximum:
        raise ValueError(
            f"ST 0903 {name} must be between {float(codec.minimum)} and "
            f"{float(codec.maximum)}"
        )


def _validate_optional_groups(
    deviations: tuple[IMAPValue | None, IMAPValue | None, IMAPValue | None],
    correlations: tuple[IMAPValue | None, IMAPValue | None, IMAPValue | None],
) -> None:
    deviation_presence = tuple(value is not None for value in deviations)
    correlation_presence = tuple(value is not None for value in correlations)
    if any(deviation_presence) and not all(deviation_presence):
        raise ValueError("ST 0903 standard deviations must form a complete group")
    if any(correlation_presence) and not all(correlation_presence):
        raise ValueError("ST 0903 correlations must form a complete group")
    if all(correlation_presence) and not all(deviation_presence):
        raise ValueError("ST 0903 correlations require the standard-deviation group")


@dataclass(frozen=True, slots=True)
class Location:
    latitude: IMAPValue
    longitude: IMAPValue
    hae: IMAPValue
    sigma_east: IMAPValue | None = None
    sigma_north: IMAPValue | None = None
    sigma_up: IMAPValue | None = None
    rho_east_north: IMAPValue | None = None
    rho_east_up: IMAPValue | None = None
    rho_north_up: IMAPValue | None = None

    def __post_init__(self) -> None:
        _validate_value(self.latitude, _LATITUDE, name="Location latitude")
        _validate_value(self.longitude, _LONGITUDE, name="Location longitude")
        _validate_value(self.hae, _HAE, name="Location hae")
        _validate_uncertainty(self.standard_deviations_optional, self.correlations_optional)

    @property
    def standard_deviations_optional(
        self,
    ) -> tuple[IMAPValue | None, IMAPValue | None, IMAPValue | None]:
        return self.sigma_east, self.sigma_north, self.sigma_up

    @property
    def correlations_optional(
        self,
    ) -> tuple[IMAPValue | None, IMAPValue | None, IMAPValue | None]:
        return self.rho_east_north, self.rho_east_up, self.rho_north_up

    @property
    def standard_deviations(self) -> tuple[IMAPValue, ...]:
        return tuple(value for value in self.standard_deviations_optional if value is not None)

    @property
    def correlations(self) -> tuple[IMAPValue, ...]:
        return tuple(value for value in self.correlations_optional if value is not None)


@dataclass(frozen=True, slots=True)
class ResolvedVMTITargetLocation:
    """A VTarget position resolved into WGS-84 coordinates."""

    target_id: int
    latitude: float
    longitude: float
    hae: float | None
    source: Literal["absolute", "parent_offset"]


def _resolved_number(value: object, *, minimum: float, maximum: float) -> float | None:
    if (
        isinstance(value, (bool, IMAPSpecialValue))
        or not isinstance(value, (int, float, Fraction))
    ):
        return None
    result = float(value)
    return result if math.isfinite(result) and minimum <= result <= maximum else None


def _wrapped_longitude(value: float) -> float:
    return (value + 180.0) % 360.0 - 180.0


def resolve_vtarget_location(
    target: VTarget,
    *,
    frame_center_latitude: object | None = None,
    frame_center_longitude: object | None = None,
) -> ResolvedVMTITargetLocation | None:
    """Resolve a VTarget's absolute or ST 0601 parent-relative coordinates.

    Absolute VTarget Item 17 takes precedence. Otherwise, embedded-VMTI Items
    10 and 11 are added to the supplied ST 0601 frame-center coordinates;
    optional Item 12 supplies WGS-84 ellipsoid height.
    """

    # Import lazily because ``st0903`` itself imports this codec module.
    from stanag4609.st0903 import VTarget

    if not isinstance(target, VTarget):
        raise TypeError("target must be a VTarget")
    absolute = target.value(17)
    if isinstance(absolute, Location):
        latitude = _resolved_number(absolute.latitude, minimum=-90, maximum=90)
        longitude = _resolved_number(absolute.longitude, minimum=-180, maximum=180)
        if latitude is not None and longitude is not None:
            return ResolvedVMTITargetLocation(
                target.target_id,
                latitude,
                _wrapped_longitude(longitude),
                _resolved_number(absolute.hae, minimum=-900, maximum=19_000),
                "absolute",
            )

    center_latitude = _resolved_number(
        frame_center_latitude,
        minimum=-90,
        maximum=90,
    )
    center_longitude = _resolved_number(
        frame_center_longitude,
        minimum=-180,
        maximum=180,
    )
    latitude_offset = _resolved_number(target.value(10), minimum=-19.2, maximum=19.2)
    longitude_offset = _resolved_number(target.value(11), minimum=-19.2, maximum=19.2)
    if None in {center_latitude, center_longitude, latitude_offset, longitude_offset}:
        return None
    assert center_latitude is not None and center_longitude is not None
    assert latitude_offset is not None and longitude_offset is not None
    latitude = center_latitude + latitude_offset
    if not -90 <= latitude <= 90:
        return None
    return ResolvedVMTITargetLocation(
        target.target_id,
        latitude,
        _wrapped_longitude(center_longitude + longitude_offset),
        _resolved_number(target.value(12), minimum=-900, maximum=19_000),
        "parent_offset",
    )


@dataclass(frozen=True, slots=True)
class Velocity:
    east: IMAPValue
    north: IMAPValue
    up: IMAPValue
    sigma_east: IMAPValue | None = None
    sigma_north: IMAPValue | None = None
    sigma_up: IMAPValue | None = None
    rho_east_north: IMAPValue | None = None
    rho_east_up: IMAPValue | None = None
    rho_north_up: IMAPValue | None = None

    def __post_init__(self) -> None:
        _validate_vector(self)


@dataclass(frozen=True, slots=True)
class Acceleration:
    east: IMAPValue
    north: IMAPValue
    up: IMAPValue
    sigma_east: IMAPValue | None = None
    sigma_north: IMAPValue | None = None
    sigma_up: IMAPValue | None = None
    rho_east_north: IMAPValue | None = None
    rho_east_up: IMAPValue | None = None
    rho_north_up: IMAPValue | None = None

    def __post_init__(self) -> None:
        _validate_vector(self)


Vector = Velocity | Acceleration
_Point = tuple[Fraction, Fraction]


def _vector_groups(
    value: Vector,
) -> tuple[
    tuple[IMAPValue, IMAPValue, IMAPValue],
    tuple[IMAPValue | None, IMAPValue | None, IMAPValue | None],
    tuple[IMAPValue | None, IMAPValue | None, IMAPValue | None],
]:
    return (
        (value.east, value.north, value.up),
        (value.sigma_east, value.sigma_north, value.sigma_up),
        (value.rho_east_north, value.rho_east_up, value.rho_north_up),
    )


def _validate_uncertainty(
    deviations: tuple[IMAPValue | None, IMAPValue | None, IMAPValue | None],
    correlations: tuple[IMAPValue | None, IMAPValue | None, IMAPValue | None],
) -> None:
    _validate_optional_groups(deviations, correlations)
    for name, value in zip(("sigEast", "sigNorth", "sigUp"), deviations, strict=True):
        if value is not None:
            _validate_value(value, _SIGMA, name=name)
    for name, value in zip(
        ("rhoEastNorth", "rhoEastUp", "rhoNorthUp"), correlations, strict=True
    ):
        if value is not None:
            _validate_value(value, _RHO, name=name)


def _validate_vector(value: Vector) -> None:
    components, deviations, correlations = _vector_groups(value)
    for name, component in zip(("east", "north", "up"), components, strict=True):
        _validate_value(component, _VECTOR, name=name)
    _validate_uncertainty(deviations, correlations)


def _encode_values(values: tuple[IMAPValue, ...], codecs: tuple[IMAPB, ...]) -> bytes:
    return b"".join(codec.encode(value) for codec, value in zip(codecs, values, strict=True))


def _present_group(
    values: tuple[IMAPValue | None, IMAPValue | None, IMAPValue | None],
) -> tuple[IMAPValue, ...]:
    return tuple(value for value in values if value is not None)


def encode_location(value: Location) -> bytes:
    """Encode a 10-, 16-, or 22-byte Location DLP."""
    if not isinstance(value, Location):
        raise TypeError("value must be a Location")
    output = bytearray(
        _encode_values((value.latitude, value.longitude, value.hae), (_LATITUDE, _LONGITUDE, _HAE))
    )
    deviations = _present_group(value.standard_deviations_optional)
    correlations = _present_group(value.correlations_optional)
    if deviations:
        output.extend(_encode_values(deviations, (_SIGMA, _SIGMA, _SIGMA)))
    if correlations:
        output.extend(_encode_values(correlations, (_RHO, _RHO, _RHO)))
    return bytes(output)


def decode_location(data: bytes) -> Location:
    """Decode a Location DLP and enforce group-only truncation."""
    if len(data) not in {10, 16, 22}:
        raise DecodeError("ST 0903 Location DLP must contain 10, 16, or 22 bytes")
    base = (
        _LATITUDE.decode(data[0:4]),
        _LONGITUDE.decode(data[4:8]),
        _HAE.decode(data[8:10]),
    )
    deviations = (
        tuple(_SIGMA.decode(data[index : index + 2]) for index in range(10, 16, 2))
        if len(data) >= 16
        else ()
    )
    correlations = (
        tuple(_RHO.decode(data[index : index + 2]) for index in range(16, 22, 2))
        if len(data) == 22
        else ()
    )
    return Location(*base, *deviations, *correlations)


def _encode_vector(value: Vector) -> bytes:
    components, optional_deviations, optional_correlations = _vector_groups(value)
    output = bytearray(_encode_values(components, (_VECTOR, _VECTOR, _VECTOR)))
    deviations = _present_group(optional_deviations)
    correlations = _present_group(optional_correlations)
    if deviations:
        output.extend(_encode_values(deviations, (_SIGMA, _SIGMA, _SIGMA)))
    if correlations:
        output.extend(_encode_values(correlations, (_RHO, _RHO, _RHO)))
    return bytes(output)


def _decode_vector(data: bytes, value_type: type[Vector]) -> Vector:
    if len(data) not in {6, 12, 18}:
        raise DecodeError("ST 0903 vector DLP must contain 6, 12, or 18 bytes")
    components = tuple(_VECTOR.decode(data[index : index + 2]) for index in range(0, 6, 2))
    deviations = (
        tuple(_SIGMA.decode(data[index : index + 2]) for index in range(6, 12, 2))
        if len(data) >= 12
        else ()
    )
    correlations = (
        tuple(_RHO.decode(data[index : index + 2]) for index in range(12, 18, 2))
        if len(data) == 18
        else ()
    )
    return value_type(*components, *deviations, *correlations)


def encode_velocity(value: Velocity) -> bytes:
    if not isinstance(value, Velocity):
        raise TypeError("value must be a Velocity")
    return _encode_vector(value)


def decode_velocity(data: bytes) -> Velocity:
    return _decode_vector(data, Velocity)  # type: ignore[return-value]


def encode_acceleration(value: Acceleration) -> bytes:
    if not isinstance(value, Acceleration):
        raise TypeError("value must be an Acceleration")
    return _encode_vector(value)


def decode_acceleration(data: bytes) -> Acceleration:
    return _decode_vector(data, Acceleration)  # type: ignore[return-value]


def decode_location_series(
    data: bytes,
    *,
    max_locations: int = 100_000,
) -> tuple[Location, ...]:
    """Decode a BER-length Series of one or more Location DLPs."""
    if isinstance(max_locations, bool) or not isinstance(max_locations, int) or max_locations < 1:
        raise ValueError("max_locations must be a positive integer")
    locations: list[Location] = []
    cursor = 0
    while cursor < len(data):
        if len(locations) >= max_locations:
            raise DecodeError(
                f"ST 0903 Location Series exceeds configured maximum {max_locations}"
            )
        try:
            length, used = decode_ber_length(data, cursor, max_value=22)
        except NeedMoreData as error:
            raise TruncatedData("truncated ST 0903 Location Series length") from error
        cursor += used
        end = cursor + length
        if end > len(data):
            raise TruncatedData("truncated ST 0903 Location Series element")
        locations.append(decode_location(data[cursor:end]))
        cursor = end
    if not locations:
        raise DecodeError("ST 0903 Location Series requires at least one Location")
    return tuple(locations)


def encode_location_series(values: tuple[Location, ...]) -> bytes:
    """Encode one or more Location DLPs as a BER-length Series."""
    if not isinstance(values, tuple) or any(not isinstance(value, Location) for value in values):
        raise TypeError("values must be a tuple of Location values")
    if not values:
        raise ValueError("ST 0903 Location Series requires at least one Location")
    output = bytearray()
    for value in values:
        encoded = encode_location(value)
        output.extend(encode_ber_length(len(encoded)))
        output.extend(encoded)
    return bytes(output)


def decode_boundary_series(
    data: bytes,
    *,
    max_locations: int = 100_000,
    max_geometry_vertices: int = DEFAULT_MAX_BOUNDARY_GEOMETRY_VERTICES,
) -> tuple[Location, ...]:
    """Decode a Boundary Series, which requires at least two Location vertices."""
    locations = decode_location_series(data, max_locations=max_locations)
    if len(locations) < 2:
        raise DecodeError("ST 0903 Boundary Series requires at least two Locations")
    try:
        validate_boundary_geometry(
            locations,
            max_vertices=max_geometry_vertices,
        )
    except LimitExceeded:
        raise
    except ValueError as error:
        raise DecodeError(str(error)) from error
    return locations


def encode_boundary_series(
    values: tuple[Location, ...],
    *,
    max_geometry_vertices: int = DEFAULT_MAX_BOUNDARY_GEOMETRY_VERTICES,
) -> bytes:
    """Encode a Boundary Series with at least two Location vertices."""
    if not isinstance(values, tuple) or any(not isinstance(value, Location) for value in values):
        raise TypeError("values must be a tuple of Location values")
    if len(values) < 2:
        raise ValueError("ST 0903 Boundary Series requires at least two Locations")
    validate_boundary_geometry(values, max_vertices=max_geometry_vertices)
    return encode_location_series(values)


def validate_boundary_geometry(
    values: tuple[Location, ...],
    *,
    max_vertices: int = DEFAULT_MAX_BOUNDARY_GEOMETRY_VERTICES,
) -> None:
    """Validate ST 0903 BoundarySeries normalized-contour semantics.

    Height is intentionally ignored: the standard defines the normalized
    contour by projecting every vertex onto the zero-HAE ellipsoid. Adjacent
    coincident points and retraced stranded branches are accepted. Intersections
    are split into a planar graph; exactly one independent cycle corresponds to
    the required single interior area.

    Geometry containing an explicit IMAP special latitude or longitude cannot
    be resolved numerically and is left structurally valid.
    """

    if not isinstance(values, tuple) or any(not isinstance(value, Location) for value in values):
        raise TypeError("values must be a tuple of Location values")
    if (
        isinstance(max_vertices, bool)
        or not isinstance(max_vertices, int)
        or max_vertices < 2
    ):
        raise ValueError("max_vertices must be an integer of at least two")
    if len(values) < 2:
        raise ValueError("ST 0903 Boundary Series requires at least two Locations")
    if len(values) > max_vertices:
        raise LimitExceeded(
            "ST 0903 Boundary Series exceeds configured geometry-validation "
            f"maximum {max_vertices}"
        )
    points = _normalized_boundary_points(values)
    if points is None:
        return
    if len(points) == 2:
        first, second = points
        if first[0] == second[0] or first[1] == second[1]:
            raise ValueError(
                "ST 0903 two-vertex Boundary Series requires opposite corners "
                "with distinct latitude and longitude"
            )
        return
    if _bounded_area_count(points) != 1:
        raise ValueError(
            "ST 0903 Boundary Series normalized contour must contain exactly "
            "one interior area"
        )


def _numeric_coordinate(value: IMAPValue) -> Fraction | None:
    if isinstance(value, IMAPSpecialValue):
        return None
    return Fraction(str(value)) if isinstance(value, float) else Fraction(value)


def _normalized_boundary_points(values: tuple[Location, ...]) -> tuple[_Point, ...] | None:
    points: list[_Point] = []
    previous_longitude: Fraction | None = None
    for location in values:
        latitude = _numeric_coordinate(location.latitude)
        longitude = _numeric_coordinate(location.longitude)
        if latitude is None or longitude is None:
            return None
        if previous_longitude is not None:
            while longitude - previous_longitude > 180:
                longitude -= 360
            while longitude - previous_longitude < -180:
                longitude += 360
        points.append((longitude, latitude))
        previous_longitude = longitude
    return tuple(points)


def _cross(first: _Point, second: _Point) -> Fraction:
    return first[0] * second[1] - first[1] * second[0]


def _subtract(first: _Point, second: _Point) -> _Point:
    return first[0] - second[0], first[1] - second[1]


def _point_at(start: _Point, direction: _Point, parameter: Fraction) -> _Point:
    return (
        start[0] + parameter * direction[0],
        start[1] + parameter * direction[1],
    )


def _segment_parameter(point: _Point, start: _Point, direction: _Point) -> Fraction:
    if direction[0]:
        return (point[0] - start[0]) / direction[0]
    return (point[1] - start[1]) / direction[1]


def _bounded_area_count(points: tuple[_Point, ...]) -> int:
    segments = tuple(
        (start, end)
        for start, end in zip(points, (*points[1:], points[0]), strict=True)
        if start != end
    )
    if not segments:
        return 0
    bounds = tuple(
        (
            min(start[0], end[0]),
            max(start[0], end[0]),
            min(start[1], end[1]),
            max(start[1], end[1]),
        )
        for start, end in segments
    )
    splits: list[dict[Fraction, _Point]] = [
        {Fraction(0): start, Fraction(1): end} for start, end in segments
    ]
    for first_index, (first_start, first_end) in enumerate(segments):
        first_direction = _subtract(first_end, first_start)
        first_bounds = bounds[first_index]
        for second_index in range(first_index + 1, len(segments)):
            second_bounds = bounds[second_index]
            if (
                first_bounds[1] < second_bounds[0]
                or second_bounds[1] < first_bounds[0]
                or first_bounds[3] < second_bounds[2]
                or second_bounds[3] < first_bounds[2]
            ):
                continue
            second_start, second_end = segments[second_index]
            second_direction = _subtract(second_end, second_start)
            offset = _subtract(second_start, first_start)
            denominator = _cross(first_direction, second_direction)
            if denominator:
                first_parameter = _cross(offset, second_direction) / denominator
                second_parameter = _cross(offset, first_direction) / denominator
                if (
                    0 <= first_parameter <= 1
                    and 0 <= second_parameter <= 1
                ):
                    intersection = _point_at(
                        first_start,
                        first_direction,
                        first_parameter,
                    )
                    splits[first_index][first_parameter] = intersection
                    splits[second_index][second_parameter] = intersection
                continue
            if _cross(offset, first_direction):
                continue
            for point in (second_start, second_end):
                parameter = _segment_parameter(point, first_start, first_direction)
                if 0 <= parameter <= 1:
                    splits[first_index][parameter] = point
            for point in (first_start, first_end):
                parameter = _segment_parameter(point, second_start, second_direction)
                if 0 <= parameter <= 1:
                    splits[second_index][parameter] = point

    edges: set[frozenset[_Point]] = set()
    for segment_splits in splits:
        ordered = [segment_splits[key] for key in sorted(segment_splits)]
        edges.update(
            frozenset((start, end))
            for start, end in pairwise(ordered)
            if start != end
        )
    vertices = {point for edge in edges for point in edge}
    adjacency: dict[_Point, set[_Point]] = {point: set() for point in vertices}
    for edge in edges:
        first, second = tuple(edge)
        adjacency[first].add(second)
        adjacency[second].add(first)
    components = 0
    remaining = set(vertices)
    while remaining:
        components += 1
        pending = [remaining.pop()]
        while pending:
            point = pending.pop()
            connected = adjacency[point] & remaining
            remaining.difference_update(connected)
            pending.extend(connected)
    return len(edges) - len(vertices) + components
