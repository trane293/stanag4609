from __future__ import annotations

from uuid import UUID

import pytest

from stanag4609.errors import DecodeError
from stanag4609.klv.ber import encode_ber_length, encode_ber_oid
from stanag4609.klv.checksum import crc16_ccitt
from stanag4609.klv.model import LocalSetItem
from stanag4609.st0601 import decode_uas_local_set, encode_uas_local_set
from stanag4609.st1303 import MDAP, MDAPAlgorithm, MDAPElementType, encode_mdap
from stanag4609.st1601 import (
    GEO_REGISTRATION_LOCAL_SET_KEY,
    GeoRegistrationLocalSet,
    HeterogeneousIMAPArray,
    RawGeoRegistrationValue,
    decode_geo_registration_local_set,
    encode_geo_registration_local_set,
)


def _row_column(tie_points: int = 2, *, rows: int = 2) -> MDAP:
    return MDAP(
        (rows, tie_points),
        2,
        MDAPAlgorithm.NATURAL,
        tuple(range(rows * tie_points)),
        MDAPElementType.UNSIGNED_INTEGER,
    )


def _lat_lon(tie_points: int = 2) -> MDAP:
    return MDAP(
        (2, tie_points),
        4,
        MDAPAlgorithm.NATURAL,
        (32.0,) * tie_points + (48.0,) * tie_points,
        MDAPElementType.IEEE,
    )


def _elevation(tie_points: int = 2) -> MDAP:
    return MDAP(
        (tie_points,),
        2,
        MDAPAlgorithm.NATURAL,
        (1500.0,) * tie_points,
        MDAPElementType.IEEE,
    )


def _pixel_uncertainty(tie_points: int = 2) -> HeterogeneousIMAPArray:
    bounds = ((0.0, 100.0), (0.0, 100.0), (-1.0, 1.0)) * 2
    return HeterogeneousIMAPArray(
        (6, tie_points),
        2,
        bounds,
        (1.0,) * (2 * tie_points)
        + (0.25,) * tie_points
        + (2.0,) * (2 * tie_points)
        + (-0.5,) * tie_points,
    )


def _geo_uncertainty(rows: int, tie_points: int = 2) -> HeterogeneousIMAPArray:
    bounds = (
        (0.0, 650.0),
        (0.0, 650.0),
        (-1.0, 1.0),
        (0.0, 1000.0),
        (-1.0, 1.0),
        (-1.0, 1.0),
    )
    return HeterogeneousIMAPArray(
        (rows, tie_points),
        2,
        bounds[:rows],
        (1.0,) * (rows * tie_points),
    )


def _encoded_item(tag: int, value: bytes) -> bytes:
    return encode_ber_oid(tag) + encode_ber_length(len(value)) + value


def test_key_and_minimal_required_round_trip() -> None:
    assert crc16_ccitt(GEO_REGISTRATION_LOCAL_SET_KEY) == 39238
    value = GeoRegistrationLocalSet(2, "registration", "1.0")
    encoded = encode_geo_registration_local_set(value)
    assert encoded == b"\x01\x01\x02\x02\x0cregistration\x03\x031.0"
    decoded = decode_geo_registration_local_set(encoded)
    assert decoded == value
    assert encode_geo_registration_local_set(decoded, preserve=True) == encoded


def test_all_primary_arrays_and_uuid_round_trip() -> None:
    identifier = UUID("12345678-1234-5678-9234-567812345678")
    value = GeoRegistrationLocalSet(
        2,
        "bundle-adjustment",
        "2026.1",
        row_column=_row_column(),
        latitude_longitude=_lat_lon(),
        second_image_name="reference.tif",
        algorithm_configuration_id=identifier,
        elevation=_elevation(),
    )
    decoded = decode_geo_registration_local_set(encode_geo_registration_local_set(value))
    assert decoded == value
    assert decoded.tie_point_count == 2
    assert decoded.algorithm_configuration_id == identifier
    assert decoded.elevation is not None
    assert decoded.elevation.elements == (1500.0, 1500.0)


def test_heterogeneous_pixel_uncertainty_round_trip() -> None:
    value = GeoRegistrationLocalSet(
        2,
        "points",
        "1",
        row_column=_row_column(rows=4),
        pixel_uncertainty=_pixel_uncertainty(),
    )
    decoded = decode_geo_registration_local_set(encode_geo_registration_local_set(value))
    assert decoded.pixel_uncertainty is not None
    assert decoded.pixel_uncertainty.element_at(0, 0) == pytest.approx(1.0, abs=0.01)
    assert decoded.pixel_uncertainty.element_at(2, 0) == pytest.approx(0.25, abs=0.01)
    assert decoded.pixel_uncertainty.element_at(5, 1) == pytest.approx(-0.5, abs=0.01)


@pytest.mark.parametrize("rows", [3, 6])
def test_geographic_uncertainty_shapes(rows: int) -> None:
    all_bounds = (
        (0.0, 650.0),
        (0.0, 650.0),
        (-1.0, 1.0),
        (0.0, 1000.0),
        (-1.0, 1.0),
        (-1.0, 1.0),
    )
    uncertainty = HeterogeneousIMAPArray(
        (rows, 1), 2, all_bounds[:rows], (1.0,) * rows
    )
    value = GeoRegistrationLocalSet(
        2,
        "geo",
        "1",
        latitude_longitude=_lat_lon(1),
        elevation=_elevation(1) if rows == 6 else None,
        geo_uncertainty=uncertainty,
    )
    decoded = decode_geo_registration_local_set(encode_geo_registration_local_set(value))
    assert decoded.geo_uncertainty is not None
    assert decoded.geo_uncertainty.dimensions == (rows, 1)


def test_tie_point_counts_must_match() -> None:
    with pytest.raises(ValueError, match="same number"):
        encode_geo_registration_local_set(
            GeoRegistrationLocalSet(
                2,
                "bad",
                "1",
                row_column=_row_column(2),
                latitude_longitude=_lat_lon(3),
            )
        )


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (
            GeoRegistrationLocalSet(2, "bad", "1", pixel_uncertainty=_pixel_uncertainty()),
            "requires a four-row Item 4",
        ),
        (
            GeoRegistrationLocalSet(
                2,
                "bad",
                "1",
                row_column=_row_column(rows=2),
                pixel_uncertainty=_pixel_uncertainty(),
            ),
            "requires a four-row Item 4",
        ),
        (
            GeoRegistrationLocalSet(2, "bad", "1", geo_uncertainty=_geo_uncertainty(3)),
            "requires Item 5",
        ),
        (
            GeoRegistrationLocalSet(
                2,
                "bad",
                "1",
                latitude_longitude=_lat_lon(),
                geo_uncertainty=_geo_uncertainty(6),
            ),
            "six-row Item 10 requires Item 8",
        ),
        (
            GeoRegistrationLocalSet(
                2,
                "bad",
                "1",
                latitude_longitude=_lat_lon(),
                elevation=_elevation(),
                geo_uncertainty=_geo_uncertainty(3),
            ),
            "three-row Item 10 cannot accompany Item 8",
        ),
    ],
)
def test_uncertainty_arrays_require_matching_source_geometry(
    value: GeoRegistrationLocalSet,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        encode_geo_registration_local_set(value)


def test_decoder_rejects_uncertainty_with_incompatible_source_geometry() -> None:
    def items(encoded: bytes) -> tuple[LocalSetItem, ...]:
        decoded = decode_geo_registration_local_set(encoded)
        assert decoded.local_set is not None
        return decoded.local_set.items

    pixel = encode_geo_registration_local_set(
        GeoRegistrationLocalSet(
            2,
            "pixel",
            "1",
            row_column=_row_column(rows=4),
            pixel_uncertainty=_pixel_uncertainty(),
        )
    )
    pixel_items = items(pixel)
    pixel_without_item4 = b"".join(
        item.raw for item in pixel_items if item.tag != 4
    )
    pixel_with_two_row_item4 = b"".join(
        _encoded_item(4, encode_mdap(_row_column(rows=2)))
        if item.tag == 4
        else item.raw
        for item in pixel_items
    )

    geographic_three = encode_geo_registration_local_set(
        GeoRegistrationLocalSet(
            2,
            "geographic",
            "1",
            latitude_longitude=_lat_lon(),
            geo_uncertainty=_geo_uncertainty(3),
        )
    )
    geographic_three_items = items(geographic_three)
    geographic_without_item5 = b"".join(
        item.raw for item in geographic_three_items if item.tag != 5
    )
    geographic_three_with_item8 = geographic_three + _encoded_item(
        8, encode_mdap(_elevation())
    )

    geographic_six = encode_geo_registration_local_set(
        GeoRegistrationLocalSet(
            2,
            "geographic",
            "1",
            latitude_longitude=_lat_lon(),
            elevation=_elevation(),
            geo_uncertainty=_geo_uncertainty(6),
        )
    )
    geographic_without_item8 = b"".join(
        item.raw for item in items(geographic_six) if item.tag != 8
    )

    for encoded, message in (
        (pixel_without_item4, "requires a four-row Item 4"),
        (pixel_with_two_row_item4, "requires a four-row Item 4"),
        (geographic_without_item5, "requires Item 5"),
        (geographic_without_item8, "six-row Item 10 requires Item 8"),
        (geographic_three_with_item8, "three-row Item 10 cannot accompany Item 8"),
    ):
        with pytest.raises(DecodeError, match=message):
            decode_geo_registration_local_set(encoded)


def test_array_shape_and_context_are_strict() -> None:
    wrong = MDAP(
        (3, 1), 2, MDAPAlgorithm.NATURAL, (1, 2, 3), MDAPElementType.UNSIGNED_INTEGER
    )
    with pytest.raises(ValueError, match="first dimension"):
        encode_geo_registration_local_set(
            GeoRegistrationLocalSet(2, "bad", "1", row_column=wrong)
        )
    with pytest.raises(ValueError, match="row bounds"):
        HeterogeneousIMAPArray((2, 1), 2, ((0.0, 1.0),), (0.0, 0.0))


def test_required_items_duplicates_and_version_encoding() -> None:
    with pytest.raises(DecodeError, match="requires"):
        decode_geo_registration_local_set(b"\x01\x01\x02")
    with pytest.raises(DecodeError, match="duplicate"):
        decode_geo_registration_local_set(
            b"\x01\x01\x02\x01\x01\x02\x02\x01a\x03\x011"
        )
    with pytest.raises(DecodeError, match="minimal"):
        decode_geo_registration_local_set(b"\x01\x02\x00\x02\x02\x01a\x03\x011")


def test_utf8_and_uuid_validation() -> None:
    with pytest.raises(DecodeError, match="UTF-8"):
        decode_geo_registration_local_set(b"\x01\x01\x02\x02\x01\xff\x03\x011")
    with pytest.raises(DecodeError, match="16 bytes"):
        decode_geo_registration_local_set(
            b"\x01\x01\x02\x02\x01a\x03\x011\x07\x01x"
        )
    with pytest.raises(ValueError, match="trimmed"):
        encode_geo_registration_local_set(GeoRegistrationLocalSet(2, " bad ", "1"))


def test_unknown_extension_is_lossless() -> None:
    value = GeoRegistrationLocalSet(
        2,
        "future",
        "1",
        extensions={50: RawGeoRegistrationValue(b"opaque")},
    )
    encoded = encode_geo_registration_local_set(value)
    decoded = decode_geo_registration_local_set(encoded)
    assert decoded.extensions[50].data == b"opaque"
    assert encode_geo_registration_local_set(decoded, preserve=True) == encoded


def test_st0601_item_98_bridge() -> None:
    value = decode_geo_registration_local_set(
        encode_geo_registration_local_set(GeoRegistrationLocalSet(2, "geo", "1"))
    )
    from datetime import datetime, timezone

    packet = encode_uas_local_set(
        {2: datetime(2023, 3, 2, tzinfo=timezone.utc), 65: 19, 98: value}
    )
    assert decode_uas_local_set(packet).value(98) == value


def test_input_and_mapped_index_validation() -> None:
    with pytest.raises(TypeError):
        decode_geo_registration_local_set(bytearray())  # type: ignore[arg-type]
    with pytest.raises(IndexError):
        _pixel_uncertainty().element_at(6, 0)
    with pytest.raises(ValueError, match="dimensions"):
        HeterogeneousIMAPArray((2, 0), 2, ((0.0, 1.0),) * 2, ())
