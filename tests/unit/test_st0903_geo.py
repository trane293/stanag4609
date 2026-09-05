from __future__ import annotations

from fractions import Fraction

import pytest

from stanag4609.errors import DecodeError, LimitExceeded, TruncatedData
from stanag4609.imap import IMAPB, IMAPSpecialKind, IMAPSpecialValue
from stanag4609.st0903 import VTargetData, decode_vmti_local_set, encode_vmti_local_set
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


def _location(latitude: float, longitude: float, *, hae: float = 100) -> Location:
    return Location(latitude, longitude, hae)


def test_location_required_group_uses_fixed_st0903_imap_layout() -> None:
    location = Location(45, 90, 1_000)
    encoded = encode_location(location)
    assert encoded == b"".join(
        (
            IMAPB(-90, 90, 4).encode(45),
            IMAPB(-180, 180, 4).encode(90),
            IMAPB(-900, 19_000, 2).encode(1_000),
        )
    )
    assert len(encoded) == 10
    decoded = decode_location(encoded)
    assert float(decoded.latitude) == pytest.approx(45, abs=1e-5)
    assert float(decoded.longitude) == pytest.approx(90, abs=1e-5)
    assert float(decoded.hae) == pytest.approx(1_000, abs=1)
    assert encode_location(decoded) == encoded


def test_location_truncation_groups_are_all_or_nothing() -> None:
    standard = Location(0, 0, 0, 1, 2, 3)
    full = Location(0, 0, 0, 1, 2, 3, -0.5, 0, 0.5)
    assert len(encode_location(standard)) == 16
    assert len(encode_location(full)) == 22
    assert decode_location(encode_location(standard)) == standard
    decoded = decode_location(encode_location(full))
    assert tuple(float(value) for value in decoded.standard_deviations) == pytest.approx((1, 2, 3))
    assert tuple(float(value) for value in decoded.correlations) == pytest.approx((-0.5, 0, 0.5))


@pytest.mark.parametrize("length", [0, 9, 11, 15, 17, 21, 23])
def test_location_rejects_non_group_truncation_lengths(length: int) -> None:
    with pytest.raises(DecodeError, match="10, 16, or 22"):
        decode_location(bytes(length))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sigma_east": 1},
        {"sigma_east": 1, "sigma_north": 1, "sigma_up": 1, "rho_east_north": 0},
    ],
)
def test_location_rejects_partial_optional_groups(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="complete group"):
        Location(0, 0, 0, **kwargs)


def test_velocity_and_acceleration_share_group_truncation_but_remain_typed() -> None:
    velocity = Velocity(10, -20, 3, 1, 2, 3, -1, 0, 1)
    acceleration = Acceleration(-10, 20, -3, 3, 2, 1)
    assert len(encode_velocity(velocity)) == 18
    assert len(encode_acceleration(acceleration)) == 12
    assert decode_velocity(encode_velocity(velocity)) == velocity
    assert decode_acceleration(encode_acceleration(acceleration)) == acceleration


def test_motion_vector_required_group_roundtrips_without_uncertainty() -> None:
    velocity = Velocity(10, -20, 3)
    assert len(encode_velocity(velocity)) == 6
    assert decode_velocity(encode_velocity(velocity)) == velocity


@pytest.mark.parametrize(
    ("decoder", "length"),
    [
        (decode_velocity, 5),
        (decode_velocity, 7),
        (decode_acceleration, 11),
        (decode_acceleration, 19),
    ],
)
def test_motion_vectors_reject_non_group_lengths(decoder: object, length: int) -> None:
    with pytest.raises(DecodeError, match="6, 12, or 18"):
        decoder(bytes(length))  # type: ignore[operator]


def test_location_series_and_boundary_series_use_ber_length_elements() -> None:
    first = _location(1, 2)
    second = _location(3, 4)
    encoded = encode_boundary_series((first, second))
    assert encoded[0] == 10
    assert encoded[11] == 10
    assert decode_boundary_series(encoded) == (first, second)
    assert decode_location_series(encoded) == (first, second)
    assert encode_location_series((first, second)) == encoded


def test_boundary_requires_two_locations_but_history_series_allows_one() -> None:
    location = _location(1, 2)
    single = encode_location_series((location,))
    assert decode_location_series(single) == (location,)
    with pytest.raises(ValueError, match="at least two"):
        encode_boundary_series((location,))
    with pytest.raises(DecodeError, match="at least two"):
        decode_boundary_series(single)
    with pytest.raises(ValueError, match="at least one"):
        encode_location_series(())
    with pytest.raises(DecodeError, match="at least one"):
        decode_location_series(b"")


def test_two_location_boundary_requires_opposite_box_corners() -> None:
    opposite = (_location(1, 2), _location(3, 4))
    assert decode_boundary_series(encode_boundary_series(opposite)) == opposite

    same_latitude = (_location(1, 2), _location(1, 4))
    with pytest.raises(ValueError, match="opposite corners"):
        encode_boundary_series(same_latitude)
    with pytest.raises(DecodeError, match="opposite corners"):
        decode_boundary_series(encode_location_series(same_latitude))

    same_meridian_across_wrap = (_location(1, 180), _location(3, -180))
    with pytest.raises(ValueError, match="opposite corners"):
        validate_boundary_geometry(same_meridian_across_wrap)


def test_boundary_requires_exactly_one_normalized_interior_area() -> None:
    square = (
        _location(0, 0),
        _location(0, 2),
        _location(2, 2),
        _location(2, 0),
    )
    assert decode_boundary_series(encode_boundary_series(square)) == square

    bow_tie = (
        _location(0, 0),
        _location(2, 2),
        _location(0, 2),
        _location(2, 0),
    )
    with pytest.raises(ValueError, match="exactly one interior area"):
        encode_boundary_series(bow_tie)
    with pytest.raises(DecodeError, match="exactly one interior area"):
        decode_boundary_series(encode_location_series(bow_tie))

    collinear = (_location(0, 0), _location(0, 1), _location(0, 2))
    with pytest.raises(ValueError, match="exactly one interior area"):
        validate_boundary_geometry(collinear)


def test_boundary_allows_coincident_vertices_and_retraced_stranded_points() -> None:
    with_coincident = (
        _location(0, 0, hae=100),
        _location(0, 0, hae=200),
        _location(0, 2),
        _location(2, 2),
        _location(2, 0),
    )
    validate_boundary_geometry(with_coincident)

    with_strand = (
        _location(0, 0),
        _location(0, 2),
        _location(1, 2),
        _location(1, 3),
        _location(1, 2),
        _location(2, 2),
        _location(2, 0),
    )
    validate_boundary_geometry(with_strand)


def test_boundary_rejects_two_loops_sharing_one_vertex() -> None:
    figure_eight = (
        _location(0, 0),
        _location(-1, -1),
        _location(1, -1),
        _location(0, 0),
        _location(-1, 1),
        _location(1, 1),
    )
    with pytest.raises(ValueError, match="exactly one interior area"):
        validate_boundary_geometry(figure_eight)


def test_boundary_geometry_unwraps_the_antimeridian_locally() -> None:
    boundary = (
        _location(10, 179),
        _location(10, -179),
        _location(11, -179),
        _location(11, 179),
    )
    validate_boundary_geometry(boundary)
    assert decode_boundary_series(encode_boundary_series(boundary)) == boundary


def test_boundary_with_explicit_invalid_coordinate_remains_structurally_decodable() -> None:
    invalid = IMAPSpecialValue(IMAPSpecialKind.USER_DEFINED, b"\xc1\x00\x00\x00")
    boundary = (Location(invalid, 1, 0), _location(2, 3))
    validate_boundary_geometry(boundary)
    assert decode_boundary_series(encode_boundary_series(boundary))[0].latitude == invalid


def test_boundary_geometry_validation_has_an_explicit_resource_budget() -> None:
    boundary = (_location(0, 0), _location(0, 1), _location(1, 0))
    with pytest.raises(LimitExceeded, match="configured geometry-validation maximum 2"):
        validate_boundary_geometry(boundary, max_vertices=2)
    with pytest.raises(LimitExceeded, match="configured geometry-validation maximum 2"):
        decode_boundary_series(
            encode_location_series(boundary),
            max_geometry_vertices=2,
        )
    with pytest.raises(ValueError, match="at least two"):
        validate_boundary_geometry(boundary, max_vertices=True)


def test_location_series_rejects_truncated_or_excessive_elements() -> None:
    with pytest.raises(TruncatedData, match="Location Series length"):
        decode_location_series(b"\x81")
    with pytest.raises(TruncatedData, match="Location Series"):
        decode_location_series(bytes.fromhex("0A 00"))
    encoded = encode_location_series((_location(1, 2), _location(3, 4)))
    with pytest.raises(DecodeError, match="configured maximum 1"):
        decode_location_series(encoded, max_locations=1)


@pytest.mark.parametrize("maximum", [0, True, 1.5])
def test_location_series_validates_resource_limit(maximum: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        decode_location_series(b"", max_locations=maximum)  # type: ignore[arg-type]


def test_location_series_encoder_requires_typed_tuple() -> None:
    location = _location(1, 2)
    with pytest.raises(TypeError, match="tuple of Location"):
        encode_location_series([location])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="tuple of Location"):
        encode_location_series((object(),))  # type: ignore[arg-type]


def test_geospatial_models_validate_ranges_types_and_group_dependencies() -> None:
    with pytest.raises(ValueError, match="latitude"):
        Location(91, 0, 0)
    with pytest.raises(TypeError, match="east"):
        Velocity(True, 0, 0)
    with pytest.raises(ValueError, match="rhoEastNorth"):
        Acceleration(0, 0, 0, 1, 1, 1, 2, 0, 0)
    with pytest.raises(ValueError, match="require the standard-deviation"):
        Location(0, 0, 0, None, None, None, 0, 0, 0)
    with pytest.raises(ValueError, match="finite"):
        Location(float("inf"), 0, 0)
    with pytest.raises(TypeError, match="latitude"):
        Location("north", 0, 0)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="Location"):
        encode_location(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="Velocity"):
        encode_velocity(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="Acceleration"):
        encode_acceleration(object())  # type: ignore[arg-type]


def test_fraction_inputs_preserve_exact_mapping_intent() -> None:
    location = Location(Fraction(1, 3), Fraction(-2, 3), Fraction(100, 3))
    assert encode_location(decode_location(encode_location(location))) == encode_location(location)


def test_explicit_imap_special_value_preserves_its_code_word() -> None:
    special = IMAPSpecialValue(IMAPSpecialKind.POSITIVE_QUIET_NAN, b"\xd0\x00\x00\x00")
    location = Location(special, 0, 0)
    assert encode_location(location)[:4] == special.raw
    with pytest.raises(ValueError, match="mapping length"):
        Location(
            IMAPSpecialValue(IMAPSpecialKind.POSITIVE_QUIET_NAN, b"\xd0\x00"),
            0,
            0,
        )


def test_vtarget_location_resolver_prefers_absolute_location() -> None:
    vmti = decode_vmti_local_set(
        encode_vmti_local_set(
            {4: 6},
            targets=(
                VTargetData(
                    7,
                    {
                        10: 1,
                        11: 2,
                        12: 500,
                        17: Location(49, -123, 125),
                    },
                ),
            ),
        ),
        standalone=False,
    )

    resolved = resolve_vtarget_location(
        vmti.targets[0],
        frame_center_latitude=40,
        frame_center_longitude=-70,
    )

    assert resolved == ResolvedVMTITargetLocation(
        target_id=7,
        latitude=pytest.approx(49, abs=1e-5),
        longitude=pytest.approx(-123, abs=1e-5),
        hae=pytest.approx(125, abs=1),
        source="absolute",
    )


def test_vtarget_location_resolver_applies_parent_offsets_and_wraps_longitude() -> None:
    vmti = decode_vmti_local_set(
        encode_vmti_local_set(
            {4: 6},
            targets=(VTargetData(9, {10: 0.25, 11: 0.2, 12: 1_000}),),
        ),
        standalone=False,
    )

    resolved = resolve_vtarget_location(
        vmti.targets[0],
        frame_center_latitude=89.5,
        frame_center_longitude=179.9,
    )

    assert resolved is not None
    assert resolved.target_id == 9
    assert resolved.latitude == pytest.approx(89.75, abs=1e-5)
    assert resolved.longitude == pytest.approx(-179.9, abs=1e-5)
    assert resolved.hae == pytest.approx(1_000, abs=1)
    assert resolved.source == "parent_offset"


def test_vtarget_location_resolver_requires_a_complete_numeric_location() -> None:
    vmti = decode_vmti_local_set(
        encode_vmti_local_set(
            {4: 6},
            targets=(VTargetData(1, {10: 0.1}),),
        ),
        standalone=False,
    )

    assert resolve_vtarget_location(vmti.targets[0]) is None
    assert (
        resolve_vtarget_location(
            vmti.targets[0],
            frame_center_latitude=90,
            frame_center_longitude=0,
        )
        is None
    )
    with pytest.raises(TypeError, match="VTarget"):
        resolve_vtarget_location(object())  # type: ignore[arg-type]
