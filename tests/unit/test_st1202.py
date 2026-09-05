from __future__ import annotations

import math
import struct

import pytest

from stanag4609.errors import DecodeError
from stanag4609.st1010 import SDCCFLP, SDCCParseControl, SDCCValueFormat
from stanag4609.st1202 import (
    GENERALIZED_TRANSFORMATION_KEY,
    GeneralizedTransformation,
    RawTransformationValue,
    TransformationType,
    apply_transformation_sequence,
    decode_generalized_transformation,
    encode_generalized_transformation,
)


def _mode2_sdcc() -> SDCCFLP:
    return SDCCFLP(
        matrix_size=8,
        parse_control=SDCCParseControl(
            mode=2,
            sparse=False,
            standard_deviation_length=4,
            correlation_coefficient_length=0,
            standard_deviation_format=SDCCValueFormat.IEEE,
            correlation_coefficient_format=SDCCValueFormat.IMAP,
        ),
        standard_deviations=(1.0,) * 8,
        correlation_coefficients=(),
    )


def test_key_matches_st1202_3() -> None:
    assert bytes.fromhex(
        "06 0E 2B 34 02 0B 01 01 0E 01 03 05 05 00 00 00"
    ) == GENERALIZED_TRANSFORMATION_KEY


def test_identity_is_minimal_and_round_trips() -> None:
    encoded = encode_generalized_transformation(
        GeneralizedTransformation(document_version=3)
    )
    assert encoded == b"\x0a\x01\x03"
    decoded = decode_generalized_transformation(encoded)
    assert decoded.document_version == 3
    assert decoded.transformation_type is TransformationType.NO_DEFINED_TRANSFORMATION
    assert decoded.coefficients == (0.0,) * 8
    assert decoded.transform(12.5, -2.0) == (12.5, -2.0)


def test_all_float_widths_and_unknown_items_are_lossless() -> None:
    wire = (
        b"\x01\x02" + struct.pack(">e", 0.5)
        + b"\x02\x04" + struct.pack(">f", 0.25)
        + b"\x03\x08" + struct.pack(">d", -4.0)
        + b"\x0a\x01\x03\x0b\x01\x01\x63\x02\xaa\xbb"
    )
    value = decode_generalized_transformation(wire)
    assert value.coefficients[:3] == (0.5, 0.25, -4.0)
    assert tuple(field.width for field in value.fields[:3]) == (2, 4, 8)
    assert value.unknown_items == ((99, b"\xaa\xbb"),)
    assert encode_generalized_transformation(value, preserve=True) == wire


def test_projective_forward_and_inverse() -> None:
    value = GeneralizedTransformation(
        a=0.5,
        b=0.25,
        c=3.0,
        d=-0.125,
        e=0.25,
        f=4.0,
        g=0.001,
        h=-0.002,
        document_version=3,
        transformation_type=TransformationType.CHIPPING,
    )
    output = value.transform(20.0, 30.0)
    recovered = value.inverse_transform(*output)
    assert recovered == pytest.approx((20.0, 30.0))


def test_formula_defined_chipping_transformation() -> None:
    value = GeneralizedTransformation.for_chipping(
        scale_factor=2.0,
        center_line=100.0,
        center_sample=200.0,
        chip_height=40.0,
        chip_width=60.0,
    )
    assert value.transformation_type is TransformationType.CHIPPING
    assert value.coefficients == pytest.approx((0.5, 0.0, 90.0, 0.0, 0.5, 185.0, 0.0, 0.0))
    assert value.transform(20.0, 30.0) == pytest.approx((100.0, 200.0))
    decoded = decode_generalized_transformation(encode_generalized_transformation(value))
    assert decoded.transformation_type is TransformationType.CHIPPING
    assert decoded.coefficients == value.coefficients


def test_formula_defined_digital_zoom_transformation() -> None:
    value = GeneralizedTransformation.for_digital_zoom(
        scale_factor=2.0,
        image_height=480.0,
        image_width=640.0,
    )
    assert value.transformation_type is TransformationType.CHIPPING
    assert value.coefficients == pytest.approx((0.5, 0.0, 120.0, 0.0, 0.5, 160.0, 0.0, 0.0))
    assert value.transform(240.0, 320.0) == pytest.approx((240.0, 320.0))


def test_formula_defined_csm_pixel_to_image_transformation() -> None:
    value = GeneralizedTransformation.for_csm_pixel_to_image(
        pixel_size_x=0.01,
        pixel_size_y=0.02,
        image_height=480.0,
        image_width=640.0,
    )
    assert value.transformation_type is TransformationType.DEFAULT_PIXEL_TO_IMAGE
    assert value.coefficients == pytest.approx(
        (1.0, 0.01, -3.2, -0.02, 1.0, 4.8, 0.0, 0.0)
    )
    assert value.transform(240.0, 320.0) == pytest.approx((0.0, 0.0), abs=1e-12)


@pytest.mark.parametrize(
    ("factory", "arguments", "message"),
    [
        (
            GeneralizedTransformation.for_chipping,
            {
                "scale_factor": 0.0,
                "center_line": 1.0,
                "center_sample": 1.0,
                "chip_height": 1.0,
                "chip_width": 1.0,
            },
            "scale_factor",
        ),
        (
            GeneralizedTransformation.for_digital_zoom,
            {"scale_factor": 1.0, "image_height": math.inf, "image_width": 1.0},
            "image_height",
        ),
        (
            GeneralizedTransformation.for_csm_pixel_to_image,
            {
                "pixel_size_x": True,
                "pixel_size_y": 1.0,
                "image_height": 1.0,
                "image_width": 1.0,
            },
            "pixel_size_x",
        ),
    ],
)
def test_formula_defined_transformation_inputs_are_positive_finite(
    factory: object,
    arguments: dict[str, object],
    message: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        factory(**arguments)  # type: ignore[operator]


def test_transformation_sequence_applies_mandatory_image_to_ground_order() -> None:
    transformations = (
        GeneralizedTransformation(
            c=10.0, transformation_type=TransformationType.CHIPPING
        ),
        GeneralizedTransformation(
            f=20.0, transformation_type=TransformationType.CHILD_PARENT
        ),
        GeneralizedTransformation(
            b=2.0,
            transformation_type=TransformationType.DEFAULT_PIXEL_TO_IMAGE,
        ),
        GeneralizedTransformation(
            d=0.5, transformation_type=TransformationType.OPTICAL
        ),
    )
    transformed = apply_transformation_sequence(transformations, 1.0, 2.0)
    assert transformed == pytest.approx((55.0, 49.5))
    assert apply_transformation_sequence(
        transformations,
        *transformed,
        inverse=True,
    ) == pytest.approx((1.0, 2.0))


def test_transformation_sequence_rejects_duplicate_or_out_of_order_types() -> None:
    chipping = GeneralizedTransformation(
        transformation_type=TransformationType.CHIPPING
    )
    optical = GeneralizedTransformation(
        transformation_type=TransformationType.OPTICAL
    )
    with pytest.raises(ValueError, match="order"):
        apply_transformation_sequence((optical, chipping), 0.0, 0.0)
    with pytest.raises(ValueError, match="unique"):
        apply_transformation_sequence((chipping, chipping), 0.0, 0.0)
    with pytest.raises(ValueError, match="defined production type"):
        apply_transformation_sequence((GeneralizedTransformation(),), 0.0, 0.0)
    with pytest.raises(TypeError, match="GeneralizedTransformation"):
        apply_transformation_sequence((object(),), 0.0, 0.0)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="inverse"):
        apply_transformation_sequence((), 0.0, 0.0, inverse=1)  # type: ignore[arg-type]
    assert apply_transformation_sequence((), 1.0, 2.0) == (1.0, 2.0)


def test_transform_rejects_point_at_infinity() -> None:
    value = GeneralizedTransformation(g=1.0, document_version=3)
    with pytest.raises(ZeroDivisionError, match="infinity"):
        value.transform(-1.0, 4.0)


def test_inverse_rejects_singular_transformation() -> None:
    value = GeneralizedTransformation(a=1.0, e=1.0, document_version=3)
    with pytest.raises(ValueError, match="singular"):
        value.inverse_transform(1.0, 1.0)


def test_mode2_sdcc_round_trip_has_parent_source_context() -> None:
    encoded = encode_generalized_transformation(
        GeneralizedTransformation(document_version=3, uncertainty=_mode2_sdcc())
    )
    decoded = decode_generalized_transformation(encoded)
    assert decoded.uncertainty is not None
    assert decoded.uncertainty.parse_control.mode == 2
    assert decoded.uncertainty.source_tags == tuple(range(1, 9))


def test_partial_sdcc_sources_are_ordered_immediately_before_pack() -> None:
    sdcc = SDCCFLP(
        matrix_size=2,
        parse_control=SDCCParseControl(
            mode=2,
            sparse=False,
            standard_deviation_length=4,
            correlation_coefficient_length=0,
            standard_deviation_format=SDCCValueFormat.IEEE,
            correlation_coefficient_format=SDCCValueFormat.IMAP,
        ),
        standard_deviations=(1.0, 2.0),
        correlation_coefficients=(),
        source_tags=(3, 1),
    )
    encoded = encode_generalized_transformation(
        GeneralizedTransformation(a=1.0, b=9.0, c=3.0, document_version=3, uncertainty=sdcc),
        float_width=4,
    )
    decoded = decode_generalized_transformation(encoded)
    assert decoded.local_set is not None
    assert tuple(item.tag for item in decoded.local_set.items) == (
        2,
        3,
        1,
        9,
        10,
    )
    assert decoded.uncertainty is not None
    assert decoded.uncertainty.source_tags == (3, 1)


def test_mode1_sdcc_is_rejected() -> None:
    # matrix size 1, Mode 1 parse control, one four-byte opaque deviation
    wire = b"\x09\x06\x01\x40\x00\x00\x00\x00\x0a\x01\x03"
    with pytest.raises(DecodeError, match="Mode 2"):
        decode_generalized_transformation(wire)


@pytest.mark.parametrize("length", [0, 1, 3, 5, 6, 7, 9])
def test_invalid_float_lengths_are_rejected(length: int) -> None:
    wire = b"\x01" + bytes((length,)) + b"\x00" * length + b"\x0a\x01\x03"
    with pytest.raises(DecodeError, match="2, 4, or 8"):
        decode_generalized_transformation(wire)


def test_nan_and_infinity_are_rejected() -> None:
    for number in (math.nan, math.inf, -math.inf):
        wire = b"\x01\x08" + struct.pack(">d", number) + b"\x0a\x01\x03"
        with pytest.raises(DecodeError, match="finite"):
            decode_generalized_transformation(wire)


def test_missing_or_duplicate_version_is_rejected() -> None:
    with pytest.raises(DecodeError, match="Document Version"):
        decode_generalized_transformation(b"\x01\x02\x00\x00")
    with pytest.raises(DecodeError, match="duplicate"):
        decode_generalized_transformation(b"\x0a\x01\x03\x0a\x01\x03")


def test_uints_must_be_minimal_and_known_enumeration() -> None:
    with pytest.raises(DecodeError, match="minimal"):
        decode_generalized_transformation(b"\x0a\x02\x00\x03")
    with pytest.raises(DecodeError, match="enumeration"):
        decode_generalized_transformation(b"\x0a\x01\x03\x0b\x01\x05")


def test_model_and_encoder_validation() -> None:
    with pytest.raises(ValueError, match="Document Version"):
        GeneralizedTransformation(document_version=0)
    with pytest.raises(TypeError, match="TransformationType"):
        GeneralizedTransformation(document_version=3, transformation_type=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="float_width"):
        encode_generalized_transformation(
            GeneralizedTransformation(document_version=3), float_width=3
        )
    with pytest.raises(TypeError, match="RawTransformationValue"):
        encode_generalized_transformation(
            GeneralizedTransformation(
                document_version=3, extensions={99: b"bad"}  # type: ignore[dict-item]
            )
        )


def test_canonical_encoding_defaults_and_extensions() -> None:
    encoded = encode_generalized_transformation(
        GeneralizedTransformation(
            c=2.5,
            document_version=3,
            transformation_type=TransformationType.OPTICAL,
            extensions={99: RawTransformationValue(b"x")},
        ),
        float_width=4,
    )
    assert encoded == b"\x03\x04" + struct.pack(">f", 2.5) + b"\x0a\x01\x03\x0b\x01\x04\x63\x01x"
    assert decode_generalized_transformation(encoded).c == 2.5


def test_wrong_input_types_are_rejected() -> None:
    with pytest.raises(TypeError):
        decode_generalized_transformation(bytearray(b"\x0a\x01\x03"))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        encode_generalized_transformation(object())  # type: ignore[arg-type]
