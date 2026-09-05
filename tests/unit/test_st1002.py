from __future__ import annotations

import math
import struct
from datetime import datetime, timezone

import pytest

from stanag4609.errors import ChecksumError, DecodeError
from stanag4609.imap import IMAPSpecialKind, IMAPSpecialValue
from stanag4609.klv.ber import encode_ber_length, encode_ber_oid
from stanag4609.klv.checksum import crc16_ccitt
from stanag4609.st0601 import decode_uas_local_set, encode_uas_local_set
from stanag4609.st1002 import (
    RANGE_IMAGE_LOCAL_SET_KEY,
    RangeCompression,
    RangeDataType,
    RangeImageEnumerations,
    RangeImageLocalSet,
    RangeImageSource,
    RawRangeValue,
    SectionData,
    decode_range_image_local_set,
    decode_section_data,
    encode_range_image_local_set,
    encode_section_data,
    fit_range_plane,
    reverse_range_plane,
    subtract_range_plane,
)
from stanag4609.st1202 import GeneralizedTransformation, TransformationType
from stanag4609.st1303 import MDAP, MDAPAlgorithm, MDAPElementType, encode_mdap

UTC = timezone.utc
WHEN = datetime(2023, 3, 2, 12, 30, tzinfo=UTC)


def _ranges(*, dimensions: tuple[int, int] = (1, 2)) -> MDAP:
    return MDAP(
        dimensions=dimensions,
        element_size=4,
        algorithm=MDAPAlgorithm.NATURAL,
        elements=(100.0,) * math.prod(dimensions),
        element_type=MDAPElementType.IEEE,
    )


def _enumerations(
    *, compression: RangeCompression = RangeCompression.NO_COMPRESSION
) -> RangeImageEnumerations:
    return RangeImageEnumerations(
        source=RangeImageSource.RANGE_SENSOR,
        data_type=RangeDataType.PERSPECTIVE,
        compression=compression,
    )


def _planar_ranges(*, unknown_index: int | None = None) -> MDAP:
    values = tuple(
        math.nan if index == unknown_index else 2.0 * i - 3.0 * j + 10.0
        for index, (i, j) in enumerate(
            (i, j) for i in range(1, 3) for j in range(1, 4)
        )
    )
    return MDAP(
        dimensions=(2, 3),
        element_size=4,
        algorithm=MDAPAlgorithm.NATURAL,
        elements=values,
        element_type=MDAPElementType.IEEE,
    )


def test_crc16_ccitt_vectors() -> None:
    assert crc16_ccitt(b"123456789") == 0xE5CC
    assert crc16_ccitt(RANGE_IMAGE_LOCAL_SET_KEY) == 41152


def test_section_data_round_trip_without_uncertainty() -> None:
    section = SectionData(1, 1, _ranges())
    encoded = encode_section_data(section)
    decoded = decode_section_data(encoded)
    assert decoded.section_x == 1
    assert decoded.section_y == 1
    assert decoded.range_values.elements == (100.0, 100.0)
    assert decoded.uncertainty is None
    assert decoded.plane is None
    assert encode_section_data(decoded, preserve=True) == encoded


def test_section_data_with_uncertainty_and_plane() -> None:
    section = SectionData(
        2,
        1,
        _ranges(),
        uncertainty=_ranges(),
        plane=(1.0, -2.0, 3.5),
    )
    encoded = encode_section_data(section, plane_float_width=4)
    decoded = decode_section_data(encoded)
    assert decoded.uncertainty is not None
    assert decoded.uncertainty.dimensions == decoded.range_values.dimensions
    assert decoded.plane == (1.0, -2.0, 3.5)
    assert decoded.plane_widths == (4, 4, 4)


def test_plane_subtraction_fits_and_reconstructs_range_values() -> None:
    ranges = _planar_ranges()

    plane = fit_range_plane(ranges)
    fitted, adjusted = subtract_range_plane(ranges)
    reconstructed = reverse_range_plane(
        MDAP(
            dimensions=ranges.dimensions,
            element_size=4,
            algorithm=MDAPAlgorithm.NATURAL,
            elements=adjusted,
            element_type=MDAPElementType.IEEE,
        ),
        fitted,
    )

    assert plane == pytest.approx((2.0, -3.0, 10.0))
    assert fitted == pytest.approx(plane)
    assert adjusted == pytest.approx((0.0,) * 6, abs=1e-12)
    assert reconstructed == pytest.approx(ranges.elements)


def test_plane_subtraction_masks_unknown_ranges_without_moving_them() -> None:
    ranges = _planar_ranges(unknown_index=1)

    plane, adjusted = subtract_range_plane(ranges)
    reconstructed = reverse_range_plane(
        MDAP(
            dimensions=ranges.dimensions,
            element_size=4,
            algorithm=MDAPAlgorithm.NATURAL,
            elements=adjusted,
            element_type=MDAPElementType.IEEE,
        ),
        plane,
    )

    assert plane == pytest.approx((2.0, -3.0, 10.0))
    assert math.isnan(adjusted[1])
    assert math.isnan(reconstructed[1])
    for index in {0, 2, 3, 4, 5}:
        assert reconstructed[index] == pytest.approx(ranges.elements[index])


def test_plane_subtraction_preserves_imap_unknown_value() -> None:
    special = IMAPSpecialValue(IMAPSpecialKind.POSITIVE_QUIET_NAN, b"\xd0\x00")
    source = _planar_ranges()
    ranges = MDAP(
        dimensions=source.dimensions,
        element_size=2,
        algorithm=MDAPAlgorithm.IMAP,
        elements=(*source.elements[:4], special, *source.elements[5:]),
        element_type=MDAPElementType.IMAP,
        imap_bounds=(-100.0, 100.0),
        imap_parameter_size=4,
    )

    plane, adjusted = subtract_range_plane(ranges)
    assert plane == pytest.approx((2.0, -3.0, 10.0))
    assert adjusted[4] is special
    assert reverse_range_plane(ranges, plane)[4] is special


def test_range_mdap_rejects_non_qnan_imap_special_value() -> None:
    special = IMAPSpecialValue(IMAPSpecialKind.BELOW_MINIMUM, b"\xc0\x00")
    ranges = MDAP(
        dimensions=(1, 1),
        element_size=2,
        algorithm=MDAPAlgorithm.IMAP,
        elements=(special,),
        element_type=MDAPElementType.IMAP,
        imap_bounds=(0.0, 100.0),
        imap_parameter_size=4,
    )

    with pytest.raises(ValueError, match="positive quiet NaN"):
        encode_section_data(SectionData(1, 1, ranges))
    encoded_ranges = encode_mdap(ranges)
    raw = b"".join(
        encode_ber_length(len(element)) + element
        for element in (encode_ber_oid(1), encode_ber_oid(1), encoded_ranges, b"")
    )
    with pytest.raises(DecodeError, match="positive quiet NaN"):
        decode_section_data(raw)


def test_plane_fit_rejects_singular_geometry_and_invalid_parameters() -> None:
    one_axis = MDAP(
        dimensions=(1, 3),
        element_size=4,
        algorithm=MDAPAlgorithm.NATURAL,
        elements=(1.0, 2.0, 3.0),
        element_type=MDAPElementType.IEEE,
    )
    with pytest.raises(ValueError, match="unique ST 1002 range plane"):
        fit_range_plane(one_axis)
    with pytest.raises(ValueError, match="three finite"):
        subtract_range_plane(_planar_ranges(), (1.0, math.inf, 2.0))


def test_section_data_rejects_bad_layouts() -> None:
    encoded = encode_section_data(SectionData(1, 1, _ranges()))
    with pytest.raises(DecodeError, match="four or seven"):
        decode_section_data(encoded + b"\x00")
    with pytest.raises(ValueError, match="dimensions"):
        encode_section_data(
            SectionData(1, 1, _ranges(), uncertainty=_ranges(dimensions=(2, 2)))
        )
    with pytest.raises(ValueError, match="all three"):
        SectionData(1, 1, _ranges(), plane=(1.0, 2.0))  # type: ignore[arg-type]


def test_section_data_rejects_invalid_coordinate_and_mdap_kind() -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        encode_section_data(SectionData(0, 1, _ranges()))
    boolean = MDAP(
        (1, 1), 1, MDAPAlgorithm.BOOLEAN, (True,), MDAPElementType.BOOLEAN
    )
    with pytest.raises(ValueError, match="Natural or IMAP"):
        encode_section_data(SectionData(1, 1, boolean))


def test_standalone_range_image_crc_and_required_items() -> None:
    packet = encode_range_image_local_set(
        RangeImageLocalSet(
            timestamp=WHEN,
            document_version=3,
            enumerations=_enumerations(),
            sections=(SectionData(1, 1, _ranges()),),
        )
    )
    assert packet.startswith(RANGE_IMAGE_LOCAL_SET_KEY)
    assert crc16_ccitt(packet) == 0
    decoded = decode_range_image_local_set(packet)
    assert decoded.timestamp == WHEN
    assert decoded.sections[0].range_values.dimensions == (1, 2)
    assert decoded.standalone is True


def test_embedded_range_image_omits_crc_and_nests_st1202() -> None:
    transform = GeneralizedTransformation(
        document_version=3,
        transformation_type=TransformationType.CHILD_PARENT,
        c=4.0,
    )
    encoded = encode_range_image_local_set(
        RangeImageLocalSet(WHEN, 3, _enumerations(), transformation=transform),
        standalone=False,
    )
    decoded = decode_range_image_local_set(encoded, standalone=False)
    assert decoded.transformation == transform
    assert not decoded.local_set.getall(21)


def test_st0601_item_97_bridge() -> None:
    embedded = encode_range_image_local_set(
        RangeImageLocalSet(WHEN, 3, _enumerations()), standalone=False
    )
    range_image = decode_range_image_local_set(embedded, standalone=False)
    packet = encode_uas_local_set({2: WHEN, 65: 19, 97: range_image})
    decoded = decode_uas_local_set(packet)
    assert decoded.value(97) == range_image
    assert decoded.value(97).effective_sprm_coordinates(
        image_rows=1080, image_columns=1920
    ) == (540.0, 960.0)


def test_bad_crc_and_embedded_crc_are_rejected() -> None:
    packet = encode_range_image_local_set(RangeImageLocalSet(WHEN, 3, _enumerations()))
    with pytest.raises(ChecksumError, match="mismatch"):
        decode_range_image_local_set(packet[:-1] + bytes((packet[-1] ^ 1,)))
    value = packet[17:]  # one-byte outer length in this compact fixture
    with pytest.raises(DecodeError, match="forbids"):
        decode_range_image_local_set(value, standalone=False)


def test_sprm_and_signed_leap_seconds_round_trip() -> None:
    value = RangeImageLocalSet(
        WHEN,
        3,
        _enumerations(),
        sprm=1250.5,
        sprm_uncertainty=2.25,
        sprm_row=10.5,
        sprm_column=20.5,
        leap_seconds=-37,
    )
    decoded = decode_range_image_local_set(
        encode_range_image_local_set(value, float_width=4), standalone=True
    )
    assert (decoded.sprm, decoded.sprm_uncertainty) == (1250.5, 2.25)
    assert (decoded.sprm_row, decoded.sprm_column) == (10.5, 20.5)
    assert decoded.leap_seconds == -37


@pytest.mark.parametrize(
    ("sprm_row", "sprm_column", "expected"),
    [
        (None, None, (540.0, 960.0)),
        (10.5, None, (10.5, 960.0)),
        (None, 20.5, (540.0, 20.5)),
        (10.5, 20.5, (10.5, 20.5)),
    ],
)
def test_effective_sprm_coordinates_apply_independent_image_center_defaults(
    sprm_row: float | None,
    sprm_column: float | None,
    expected: tuple[float, float],
) -> None:
    value = RangeImageLocalSet(
        WHEN,
        3,
        _enumerations(),
        sprm_row=sprm_row,
        sprm_column=sprm_column,
    )

    assert value.effective_sprm_coordinates(
        image_rows=1080, image_columns=1920
    ) == expected


@pytest.mark.parametrize(
    ("image_rows", "image_columns", "error_type"),
    [
        (True, 1920, TypeError),
        (1080.0, 1920, TypeError),
        (1080, False, TypeError),
        (1080, 1920.0, TypeError),
        (0, 1920, ValueError),
        (1080, -1, ValueError),
    ],
)
def test_effective_sprm_coordinates_reject_invalid_image_dimensions(
    image_rows: object,
    image_columns: object,
    error_type: type[Exception],
) -> None:
    value = RangeImageLocalSet(WHEN, 3, _enumerations())

    with pytest.raises(error_type, match=r"image_(rows|columns)"):
        value.effective_sprm_coordinates(
            image_rows=image_rows,  # type: ignore[arg-type]
            image_columns=image_columns,  # type: ignore[arg-type]
        )


def test_simple_section_layout_and_count_are_enforced() -> None:
    with pytest.raises(ValueError, match="simple Section layout"):
        encode_range_image_local_set(
            RangeImageLocalSet(WHEN, 3, _enumerations(), sections_x=2, sections_y=2),
            standalone=False,
        )
    with pytest.raises(ValueError, match="Section Data count"):
        encode_range_image_local_set(
            RangeImageLocalSet(
                WHEN,
                3,
                _enumerations(),
                sections_x=2,
                sections=(SectionData(1, 1, _ranges()),),
            ),
            standalone=False,
        )


def test_duplicate_section_coordinates_and_out_of_range_are_rejected() -> None:
    duplicate = (SectionData(1, 1, _ranges()),) * 2
    with pytest.raises(ValueError, match="duplicate ST 1002 Section"):
        encode_range_image_local_set(
            RangeImageLocalSet(WHEN, 3, _enumerations(), sections_x=2, sections=duplicate),
            standalone=False,
        )
    with pytest.raises(ValueError, match="outside"):
        encode_range_image_local_set(
            RangeImageLocalSet(
                WHEN,
                3,
                _enumerations(),
                sections=(SectionData(2, 1, _ranges()),),
            ),
            standalone=False,
        )


def test_planar_fit_requires_plane_for_every_section() -> None:
    with pytest.raises(ValueError, match="Planar Fit"):
        encode_range_image_local_set(
            RangeImageLocalSet(
                WHEN,
                3,
                _enumerations(compression=RangeCompression.PLANAR_FIT),
                sections=(SectionData(1, 1, _ranges()),),
            ),
            standalone=False,
        )


def test_reserved_enumerations_and_noncanonical_uints_are_rejected() -> None:
    base = b"\x01\x08" + (0).to_bytes(8, "big") + b"\x0b\x01\x03"
    with pytest.raises(DecodeError, match="reserved bit"):
        decode_range_image_local_set(base + b"\x0c\x01\x80", standalone=False)
    with pytest.raises(DecodeError, match="reserved data type"):
        decode_range_image_local_set(base + b"\x0c\x01\x10", standalone=False)
    with pytest.raises(DecodeError, match="minimal"):
        decode_range_image_local_set(
            b"\x01\x08" + (0).to_bytes(8, "big") + b"\x0b\x02\x00\x03\x0c\x01\x00",
            standalone=False,
        )


def test_missing_first_timestamp_and_duplicate_singleton_are_rejected() -> None:
    with pytest.raises(DecodeError, match="Precision Time Stamp"):
        decode_range_image_local_set(b"\x0b\x01\x03\x0c\x01\x00", standalone=False)
    wire = (
        b"\x01\x08" + (0).to_bytes(8, "big")
        + b"\x0b\x01\x03\x0c\x01\x00\x0c\x01\x00"
    )
    with pytest.raises(DecodeError, match="duplicate"):
        decode_range_image_local_set(wire, standalone=False)


def test_unknown_item_is_preserved() -> None:
    value = RangeImageLocalSet(
        WHEN,
        3,
        _enumerations(),
        extensions={
            30: RawRangeValue(b"future"),
            50: RawRangeValue(b"before-retired-range"),
            55: RawRangeValue(b"after-retired-range"),
        },
    )
    encoded = encode_range_image_local_set(value, standalone=False)
    decoded = decode_range_image_local_set(encoded, standalone=False)
    assert decoded.extensions[30].data == b"future"
    assert decoded.extensions[50].data == b"before-retired-range"
    assert decoded.extensions[55].data == b"after-retired-range"
    assert encode_range_image_local_set(decoded, standalone=False, preserve=True) == encoded


@pytest.mark.parametrize("tag", [2, 10, 51, 54])
def test_retired_local_set_tags_are_rejected_on_decode_and_encode(tag: int) -> None:
    mandatory = b"\x01\x08" + (0).to_bytes(8, "big") + b"\x0b\x01\x03\x0c\x01\x00"
    retired_item = encode_ber_oid(tag) + b"\x01x"

    with pytest.raises(DecodeError, match=rf"retired ST 1002 tag {tag}"):
        decode_range_image_local_set(mandatory + retired_item, standalone=False)
    with pytest.raises(ValueError, match=rf"tag {tag}.*extension slot"):
        encode_range_image_local_set(
            RangeImageLocalSet(
                WHEN,
                3,
                _enumerations(),
                extensions={tag: RawRangeValue(b"retired")},
            ),
            standalone=False,
        )


def test_uncertainty_must_be_nonnegative() -> None:
    bad = MDAP(
        (1, 1),
        4,
        MDAPAlgorithm.NATURAL,
        (-1.0,),
        MDAPElementType.IEEE,
    )
    with pytest.raises(ValueError, match="non-negative"):
        encode_section_data(SectionData(1, 1, _ranges(dimensions=(1, 1)), bad))


def test_positive_qnan_uncertainty_is_allowed() -> None:
    special = IMAPSpecialValue(IMAPSpecialKind.POSITIVE_QUIET_NAN, b"\xd0\x00")
    uncertainty = MDAP(
        (1, 1),
        2,
        MDAPAlgorithm.IMAP,
        (special,),
        MDAPElementType.IMAP,
        imap_bounds=(0.0, 10.0),
        imap_parameter_size=4,
    )
    encoded = encode_section_data(
        SectionData(1, 1, _ranges(dimensions=(1, 1)), uncertainty)
    )
    decoded = decode_section_data(encoded)
    assert decoded.uncertainty is not None
    assert decoded.uncertainty.elements == (special,)


def test_float_domains_and_types_are_validated() -> None:
    with pytest.raises(ValueError, match="finite"):
        encode_range_image_local_set(
            RangeImageLocalSet(WHEN, 3, _enumerations(), sprm=math.inf),
            standalone=False,
        )
    with pytest.raises(ValueError, match="float_width"):
        encode_range_image_local_set(
            RangeImageLocalSet(WHEN, 3, _enumerations()),
            standalone=False,
            float_width=2,
        )
    with pytest.raises(TypeError):
        decode_range_image_local_set(bytearray(), standalone=False)  # type: ignore[arg-type]


def test_decode_rejects_mismatched_section_count() -> None:
    section = encode_section_data(SectionData(1, 1, _ranges()))
    wire = (
        b"\x01\x08" + (0).to_bytes(8, "big")
        + b"\x0b\x01\x03\x0c\x01\x00\x11\x01\x02"
        + b"\x14" + bytes((len(section),)) + section
    )
    with pytest.raises(DecodeError, match="Section Data count"):
        decode_range_image_local_set(wire, standalone=False)


def test_section_decode_rejects_negative_uncertainty() -> None:
    ranges = encode_mdap(_ranges(dimensions=(1, 1)))
    bad_uncertainty = MDAP(
        (1, 1), 4, MDAPAlgorithm.NATURAL, (-1.0,), MDAPElementType.IEEE
    )
    uncertainty = encode_mdap(bad_uncertainty)
    raw = b"".join(
        encode_ber_length(len(element)) + element
        for element in (encode_ber_oid(1), encode_ber_oid(1), ranges, uncertainty)
    )
    with pytest.raises(DecodeError, match="non-negative"):
        decode_section_data(raw)


def test_explicit_nan_sprm_decodes_but_infinity_does_not() -> None:
    prefix = b"\x01\x08" + (0).to_bytes(8, "big") + b"\x0b\x01\x03\x0c\x01\x00"
    decoded = decode_range_image_local_set(
        prefix + b"\x0d\x04" + struct.pack(">f", math.nan), standalone=False
    )
    assert math.isnan(decoded.sprm)
    with pytest.raises(DecodeError, match="infinite"):
        decode_range_image_local_set(
            prefix + b"\x0d\x04" + struct.pack(">f", math.inf), standalone=False
        )
