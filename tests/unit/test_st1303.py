from __future__ import annotations

import math
import struct

import pytest

from stanag4609.errors import DecodeError, LimitExceeded
from stanag4609.imap import IMAPSpecialKind, IMAPSpecialValue
from stanag4609.klv.ber import encode_ber_length
from stanag4609.st1303 import (
    MDAP,
    MDAPAlgorithm,
    MDAPElementType,
    MDAPPatch,
    decode_mdap,
    encode_mdap,
)


def _pack(body: bytes) -> bytes:
    return encode_ber_length(len(body)) + body


def test_official_boolean_array_example() -> None:
    values = (
        False,
        True,
        False,
        False,
        True,
        False,
        False,
        False,
        True,
        False,
        True,
        False,
        True,
        False,
        False,
        False,
        True,
        True,
        True,
        True,
    )
    expected = bytes.fromhex("08 02 05 04 01 03 48 A8 F0")
    pack = MDAP(
        (5, 4),
        1,
        MDAPAlgorithm.BOOLEAN,
        values,
        element_type=MDAPElementType.BOOLEAN,
    )
    assert encode_mdap(pack) == expected
    decoded = decode_mdap(expected)
    assert decoded == pack
    assert decoded.element_at(2, 2) is True
    assert decoded.element_at(3, 3) is False


def test_official_unsigned_integer_array_examples() -> None:
    first = bytes.fromhex(
        "12 02 03 03 01 04 00 0C 36 82 5E 02 90 00 00 7F 81 00 01"
    )
    decoded = decode_mdap(first)
    assert decoded.algorithm is MDAPAlgorithm.UNSIGNED_INTEGER
    assert decoded.dimensions == (3, 3)
    assert decoded.uint_bias == 0
    assert decoded.elements == (12, 54, 350, 2, 2048, 0, 127, 128, 1)
    assert encode_mdap(decoded) == first

    biased = bytes.fromhex("0B 01 05 01 04 81 02 00 28 19 0D 3C")
    decoded = decode_mdap(biased)
    assert decoded.uint_bias == 130
    assert decoded.elements == (130, 170, 155, 143, 190)
    assert encode_mdap(decoded) == biased


def test_official_signed_run_length_example() -> None:
    encoded = bytes.fromhex(
        "25 02 0A 0A 02 05 FA70 "
        "0678 00 00 04 03 "
        "0000 00 05 04 05 "
        "FC09 04 00 06 03 "
        "03D2 04 05 03 05 "
        "04EC 07 05 03 05"
    )
    decoded = decode_mdap(encoded, element_type=MDAPElementType.SIGNED_INTEGER)
    assert decoded.algorithm is MDAPAlgorithm.RUN_LENGTH
    assert decoded.rle_default == -1424
    assert len(decoded.patches) == 5
    assert decoded.element_at(0, 0) == 1656
    assert decoded.element_at(9, 4) == -1424
    assert decoded.element_at(5, 7) == 978
    assert decoded.element_at(8, 8) == 1260
    assert encode_mdap(decoded) == encoded


def test_official_overlapping_run_length_example() -> None:
    encoded = bytes.fromhex(
        "19 02 0A 0A 02 05 0000 "
        "001B 00 00 0A 03 "
        "000D 04 00 03 0A "
        "0113 00 03 0A 02"
    )
    decoded = decode_mdap(encoded, element_type=MDAPElementType.SIGNED_INTEGER)
    assert decoded.element_at(4, 1) == 13
    assert decoded.element_at(4, 3) == 275
    assert decoded.element_at(9, 8) == 0
    materialized = decoded.materialize(max_elements=100)
    assert len(materialized) == 100
    assert materialized[43] == 275
    assert encode_mdap(decoded) == encoded


@pytest.mark.parametrize("element_size", [2, 4, 8])
def test_natural_ieee_array_round_trip(element_size: int) -> None:
    pack = MDAP(
        dimensions=(2, 2),
        element_size=element_size,
        algorithm=MDAPAlgorithm.NATURAL,
        elements=(1.25, -2.5, math.inf, math.nan),
        element_type=MDAPElementType.IEEE,
    )
    decoded = decode_mdap(encode_mdap(pack), element_type=MDAPElementType.IEEE)
    assert decoded.elements[:3] == (1.25, -2.5, math.inf)
    assert math.isnan(decoded.elements[3])
    assert decoded.element_at(0, 1) == -2.5


@pytest.mark.parametrize("parameter_size", [4, 8])
def test_imap_array_round_trip_and_special_identity(parameter_size: int) -> None:
    special = IMAPSpecialValue(IMAPSpecialKind.POSITIVE_QUIET_NAN, b"\xD0\x00")
    pack = MDAP(
        dimensions=(3,),
        element_size=2,
        algorithm=MDAPAlgorithm.IMAP,
        elements=(0.0, 100.0, special),
        element_type=MDAPElementType.IMAP,
        imap_bounds=(0.0, 100.0),
        imap_parameter_size=parameter_size,
    )
    encoded = encode_mdap(pack)
    decoded = decode_mdap(encoded)
    assert decoded.imap_bounds == (0.0, 100.0)
    assert decoded.elements[0] == pytest.approx(0.0)
    assert decoded.elements[1] == pytest.approx(100.0, abs=0.01)
    assert decoded.elements[2] == special
    assert encode_mdap(decoded) == encoded


def test_imap_uses_the_quantized_wire_bounds_for_element_mapping() -> None:
    pack = MDAP(
        (2,),
        2,
        MDAPAlgorithm.IMAP,
        (0.1, 1.1),
        MDAPElementType.IMAP,
        imap_bounds=(0.1, 1.1),
        imap_parameter_size=4,
    )
    decoded = decode_mdap(encode_mdap(pack))
    assert decoded.imap_bounds != pack.imap_bounds
    assert decoded.elements[0] == pytest.approx(0.1, abs=0.001)
    assert encode_mdap(decoded) == encode_mdap(pack)


def test_empty_natural_array_signal_round_trip() -> None:
    pack = MDAP((20, 30), 0, MDAPAlgorithm.NATURAL)
    encoded = encode_mdap(pack)
    assert decode_mdap(encoded) == pack
    assert decode_mdap(encoded).materialize() == ()
    with pytest.raises(LookupError, match="contains no elements"):
        decode_mdap(encoded).element_at(0, 0)


@pytest.mark.parametrize(
    ("algorithm", "element_type"),
    [
        (MDAPAlgorithm.NATURAL, MDAPElementType.RAW),
        (MDAPAlgorithm.IMAP, MDAPElementType.IMAP),
        (MDAPAlgorithm.BOOLEAN, MDAPElementType.BOOLEAN),
        (MDAPAlgorithm.UNSIGNED_INTEGER, MDAPElementType.UNSIGNED_INTEGER),
        (MDAPAlgorithm.RUN_LENGTH, MDAPElementType.RAW),
    ],
)
def test_zero_ebytes_empty_signal_is_supported_for_every_table_3_apa(
    algorithm: MDAPAlgorithm, element_type: MDAPElementType
) -> None:
    pack = MDAP((20, 30), 0, algorithm, element_type=element_type)
    encoded = encode_mdap(pack)
    decoded = decode_mdap(encoded)
    assert decoded.dimensions == pack.dimensions
    assert decoded.element_size == 0
    assert decoded.algorithm is algorithm
    assert decoded.elements == ()
    assert decoded.patches == ()
    assert decoded.materialize() == ()
    assert encode_mdap(decoded) == encoded
    with pytest.raises(LookupError, match="contains no elements"):
        decoded.element_at(0, 0)


def test_raw_and_fixed_integer_element_types() -> None:
    raw = MDAP(
        (2,),
        2,
        MDAPAlgorithm.NATURAL,
        (b"ab", b"cd"),
        element_type=MDAPElementType.RAW,
    )
    assert decode_mdap(encode_mdap(raw)).elements == (b"ab", b"cd")

    unsigned = MDAP(
        (2,),
        2,
        MDAPAlgorithm.NATURAL,
        (0, 65535),
        element_type=MDAPElementType.UNSIGNED_INTEGER,
    )
    assert decode_mdap(
        encode_mdap(unsigned), element_type=MDAPElementType.UNSIGNED_INTEGER
    ).elements == (0, 65535)

    signed = MDAP(
        (2,),
        2,
        MDAPAlgorithm.NATURAL,
        (-32768, 32767),
        element_type=MDAPElementType.SIGNED_INTEGER,
    )
    assert decode_mdap(
        encode_mdap(signed), element_type=MDAPElementType.SIGNED_INTEGER
    ).elements == (-32768, 32767)


def test_three_dimensional_natural_array_uses_row_major_offsets() -> None:
    values = tuple(range(2 * 3 * 4))
    pack = MDAP(
        (2, 3, 4),
        1,
        MDAPAlgorithm.NATURAL,
        values,
        element_type=MDAPElementType.UNSIGNED_INTEGER,
    )
    decoded = decode_mdap(
        encode_mdap(pack), element_type=MDAPElementType.UNSIGNED_INTEGER
    )
    assert decoded.element_at(0, 0, 0) == 0
    assert decoded.element_at(0, 2, 3) == 11
    assert decoded.element_at(1, 0, 0) == 12
    assert decoded.element_at(1, 2, 3) == 23


def test_rle_patch_model_and_row_major_access() -> None:
    pack = MDAP(
        (3, 4),
        1,
        MDAPAlgorithm.RUN_LENGTH,
        element_type=MDAPElementType.UNSIGNED_INTEGER,
        rle_default=0,
        patches=(
            MDAPPatch(1, (0, 0), (3, 2)),
            MDAPPatch(2, (1, 1), (1, 3)),
        ),
    )
    decoded = decode_mdap(encode_mdap(pack), element_type=MDAPElementType.UNSIGNED_INTEGER)
    assert decoded.materialize() == (1, 1, 0, 0, 1, 2, 2, 2, 1, 1, 0, 0)
    assert decoded.element_count == 12
    assert decoded.ndim == 2


@pytest.mark.parametrize(
    ("encoded", "message"),
    [
        (b"", "BER length"),
        (bytes.fromhex("02 01"), "length"),
        (bytes.fromhex("04 00 01 01 01"), "NDim"),
        (bytes.fromhex("05 01 00 01 01 00"), "dimension"),
        (bytes.fromhex("04 01 01 01 00"), "APA"),
        (bytes.fromhex("05 01 01 01 06 00"), "APA"),
        (bytes.fromhex("05 01 02 01 01 00"), "expected 2"),
        (bytes.fromhex("05 01 01 01 03 81"), "padding"),
        (bytes.fromhex("05 01 02 01 04 00"), "BER-OID element"),
        (bytes.fromhex("04 01 01 01 05"), "default"),
    ],
)
def test_decoder_rejects_malformed_packs(encoded: bytes, message: str) -> None:
    with pytest.raises(DecodeError, match=message):
        decode_mdap(encoded)


def test_decoder_resource_limits() -> None:
    with pytest.raises(LimitExceeded, match="dimensions"):
        decode_mdap(bytes.fromhex("06 02 01 01 01 01 00"), max_dimensions=1)
    with pytest.raises(LimitExceeded, match="elements"):
        decode_mdap(bytes.fromhex("05 01 81 00 00 01"), max_elements=127)
    with pytest.raises(LimitExceeded, match="BER length"):
        decode_mdap(bytes.fromhex("05 01 01 00 01 00"), max_pack_length=4)


def test_decoder_validates_context_and_algorithm_shapes() -> None:
    with pytest.raises(TypeError, match="must be bytes"):
        decode_mdap(bytearray())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="MDAPElementType"):
        decode_mdap(_pack(bytes.fromhex("01 01 00 01")), element_type="raw")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="contextual"):
        decode_mdap(
            _pack(bytes.fromhex("01 01 01 01 00")),
            element_type=MDAPElementType.BOOLEAN,
        )
    with pytest.raises(DecodeError, match="IEEE elements require"):
        decode_mdap(
            _pack(bytes.fromhex("01 01 03 01 000000")),
            element_type=MDAPElementType.IEEE,
        )
    with pytest.raises(LimitExceeded, match="EBytes"):
        decode_mdap(_pack(bytes.fromhex("01 01 02 01 0000")), max_element_size=1)


def test_decoder_rejects_malformed_imap_arrays() -> None:
    with pytest.raises(DecodeError, match="zero signal"):
        decode_mdap(_pack(bytes.fromhex("01 01 00 02 00")))
    with pytest.raises(DecodeError, match="APAS"):
        decode_mdap(_pack(bytes.fromhex("01 01 01 02 0000")))
    equal_bounds = struct.pack(">ff", 1.0, 1.0)
    with pytest.raises(DecodeError, match="finite and increasing"):
        decode_mdap(_pack(bytes.fromhex("01 01 01 02") + equal_bounds + b"\x00"))


def test_decoder_rejects_malformed_compact_arrays() -> None:
    with pytest.raises(DecodeError, match="Boolean APA requires"):
        decode_mdap(_pack(bytes.fromhex("01 01 02 03 0000")))
    with pytest.raises(DecodeError, match="Boolean array has"):
        decode_mdap(_pack(bytes.fromhex("01 01 01 03")))
    with pytest.raises(DecodeError, match="Unsigned Integer APA requires"):
        decode_mdap(_pack(bytes.fromhex("01 01 02 04 00 00")))
    with pytest.raises(DecodeError, match="trailing bytes"):
        decode_mdap(_pack(bytes.fromhex("01 01 01 04 00 00 00")))
    with pytest.raises(DecodeError, match="bias must equal"):
        decode_mdap(_pack(bytes.fromhex("01 01 01 04 01 01")))
    with pytest.raises(DecodeError, match="zero signal"):
        decode_mdap(_pack(bytes.fromhex("01 01 00 05 00")))
    with pytest.raises(ValueError, match="contextual"):
        decode_mdap(
            _pack(bytes.fromhex("01 01 01 05 00")),
            element_type=MDAPElementType.BOOLEAN,
        )


def test_decoder_bounds_rle_patches_and_validates_geometry() -> None:
    one_patch = _pack(bytes.fromhex("02 03 04 01 05 00 01 00 00 01 01"))
    with pytest.raises(LimitExceeded, match="patches"):
        decode_mdap(one_patch, max_patches=0)
    with pytest.raises(DecodeError, match="patch value"):
        decode_mdap(_pack(bytes.fromhex("01 01 02 05 0000 FF")))
    with pytest.raises(DecodeError, match="coordinate"):
        decode_mdap(_pack(bytes.fromhex("01 01 01 05 00 01")))
    outside = _pack(bytes.fromhex("02 03 04 01 05 00 01 02 00 02 01"))
    with pytest.raises(DecodeError, match="outside"):
        decode_mdap(outside, element_type=MDAPElementType.UNSIGNED_INTEGER)


@pytest.mark.parametrize(
    ("pack", "error", "message"),
    [
        (MDAP((), 1, MDAPAlgorithm.NATURAL), ValueError, "dimension"),
        (MDAP((1, 0), 1, MDAPAlgorithm.NATURAL), ValueError, "positive"),
        (MDAP((2,), 1, MDAPAlgorithm.NATURAL, (b"x",)), ValueError, "2 elements"),
        (MDAP((1,), 0, MDAPAlgorithm.NATURAL, (b"",)), ValueError, "zero signal"),
        (
            MDAP(
                (1,),
                1,
                MDAPAlgorithm.BOOLEAN,
                (1,),
                element_type=MDAPElementType.BOOLEAN,
            ),
            TypeError,
            "boolean",
        ),
        (
            MDAP(
                (1,),
                2,
                MDAPAlgorithm.BOOLEAN,
                (True,),
                element_type=MDAPElementType.BOOLEAN,
            ),
            ValueError,
            "EBytes 1",
        ),
        (
            MDAP(
                (1,),
                2,
                MDAPAlgorithm.UNSIGNED_INTEGER,
                (1,),
                element_type=MDAPElementType.UNSIGNED_INTEGER,
                uint_bias=0,
            ),
            ValueError,
            "EBytes 1",
        ),
        (
            MDAP(
                (1,),
                1,
                MDAPAlgorithm.UNSIGNED_INTEGER,
                (1,),
                element_type=MDAPElementType.UNSIGNED_INTEGER,
                uint_bias=2,
            ),
            ValueError,
            "bias",
        ),
        (
            MDAP(
                (1,),
                1,
                MDAPAlgorithm.IMAP,
                (0.0,),
                element_type=MDAPElementType.IMAP,
            ),
            ValueError,
            "bounds",
        ),
        (
            MDAP(
                (1,),
                1,
                MDAPAlgorithm.RUN_LENGTH,
                element_type=MDAPElementType.UNSIGNED_INTEGER,
            ),
            ValueError,
            "default",
        ),
    ],
)
def test_encoder_rejects_invalid_models(
    pack: MDAP, error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        encode_mdap(pack)


def test_encoder_validates_model_and_resource_limits() -> None:
    with pytest.raises(TypeError, match="pack must"):
        encode_mdap(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="algorithm"):
        encode_mdap(MDAP((1,), 0, "natural"))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="element_type"):
        encode_mdap(MDAP((1,), 0, MDAPAlgorithm.NATURAL, element_type="raw"))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="dimension sizes"):
        encode_mdap(MDAP((True,), 0, MDAPAlgorithm.NATURAL))
    with pytest.raises(TypeError, match="EBytes"):
        encode_mdap(MDAP((1,), True, MDAPAlgorithm.NATURAL))
    with pytest.raises(ValueError, match="non-negative"):
        encode_mdap(MDAP((1,), -1, MDAPAlgorithm.NATURAL))
    with pytest.raises(LimitExceeded, match="dimensions"):
        encode_mdap(MDAP((1, 1), 0, MDAPAlgorithm.NATURAL), max_dimensions=1)
    with pytest.raises(LimitExceeded, match="elements"):
        encode_mdap(MDAP((11, 10), 0, MDAPAlgorithm.NATURAL), max_elements=100)
    with pytest.raises(LimitExceeded, match="EBytes"):
        encode_mdap(MDAP((1,), 2, MDAPAlgorithm.NATURAL, (b"xx",)), max_element_size=1)
    with pytest.raises(LimitExceeded, match="pack length"):
        encode_mdap(MDAP((1,), 1, MDAPAlgorithm.NATURAL, (b"x",)), max_pack_length=4)


@pytest.mark.parametrize(
    ("pack", "message"),
    [
        (
            MDAP((1,), 0, MDAPAlgorithm.NATURAL, imap_bounds=(0.0, 1.0)),
            "IMAP parameters",
        ),
        (MDAP((1,), 0, MDAPAlgorithm.NATURAL, uint_bias=0), "unsigned bias"),
        (MDAP((1,), 0, MDAPAlgorithm.NATURAL, rle_default=b""), "RLE parameters"),
    ],
)
def test_encoder_rejects_parameters_for_another_algorithm(pack: MDAP, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        encode_mdap(pack)


@pytest.mark.parametrize(
    "pack",
    [
        MDAP(
            (1,),
            0,
            MDAPAlgorithm.IMAP,
            element_type=MDAPElementType.IMAP,
            imap_bounds=(0.0, 1.0),
            imap_parameter_size=4,
        ),
        MDAP(
            (1,),
            0,
            MDAPAlgorithm.UNSIGNED_INTEGER,
            element_type=MDAPElementType.UNSIGNED_INTEGER,
            uint_bias=0,
        ),
        MDAP(
            (1,),
            0,
            MDAPAlgorithm.RUN_LENGTH,
            rle_default=b"",
        ),
    ],
)
def test_zero_ebytes_signal_rejects_algorithm_parameters(pack: MDAP) -> None:
    with pytest.raises(ValueError, match="zero signal"):
        encode_mdap(pack)


def test_encoder_validates_natural_element_representations() -> None:
    with pytest.raises(ValueError, match="contextual"):
        encode_mdap(
            MDAP((1,), 1, MDAPAlgorithm.NATURAL, (True,), MDAPElementType.BOOLEAN)
        )
    with pytest.raises(TypeError, match="raw element"):
        encode_mdap(MDAP((1,), 1, MDAPAlgorithm.NATURAL, (1,)))
    with pytest.raises(ValueError, match="requires 2 bytes"):
        encode_mdap(MDAP((1,), 2, MDAPAlgorithm.NATURAL, (b"x",)))
    with pytest.raises(TypeError, match="IEEE element"):
        encode_mdap(
            MDAP((1,), 4, MDAPAlgorithm.NATURAL, (b"xxxx",), MDAPElementType.IEEE)
        )
    with pytest.raises(ValueError, match="IEEE elements require"):
        encode_mdap(MDAP((1,), 3, MDAPAlgorithm.NATURAL, (1.0,), MDAPElementType.IEEE))
    with pytest.raises(ValueError, match="outside"):
        encode_mdap(MDAP((1,), 4, MDAPAlgorithm.NATURAL, (1e100,), MDAPElementType.IEEE))
    with pytest.raises(TypeError, match="integer element"):
        encode_mdap(
            MDAP(
                (1,),
                1,
                MDAPAlgorithm.NATURAL,
                (True,),
                MDAPElementType.UNSIGNED_INTEGER,
            )
        )
    with pytest.raises(ValueError, match="does not fit"):
        encode_mdap(
            MDAP(
                (1,),
                1,
                MDAPAlgorithm.NATURAL,
                (256,),
                MDAPElementType.UNSIGNED_INTEGER,
            )
        )


def test_encoder_validates_imap_and_compact_parameters() -> None:
    imap = dict(
        dimensions=(1,),
        element_size=1,
        algorithm=MDAPAlgorithm.IMAP,
        elements=(0.0,),
        element_type=MDAPElementType.IMAP,
        imap_bounds=(0.0, 1.0),
        imap_parameter_size=4,
    )
    with pytest.raises(ValueError, match="IMAP element type"):
        encode_mdap(MDAP(**{**imap, "element_type": MDAPElementType.RAW}))
    with pytest.raises(ValueError, match="EBytes"):
        encode_mdap(MDAP(**{**imap, "element_size": 0}))
    with pytest.raises(ValueError, match="requires 2 elements"):
        encode_mdap(MDAP(**{**imap, "dimensions": (2,)}))
    with pytest.raises(ValueError, match="parameter size"):
        encode_mdap(MDAP(**{**imap, "imap_parameter_size": 2}))
    with pytest.raises(ValueError, match="finite and increasing"):
        encode_mdap(MDAP(**{**imap, "imap_bounds": (math.nan, 1.0)}))
    with pytest.raises(ValueError, match="finite and increasing"):
        encode_mdap(MDAP(**{**imap, "imap_bounds": (0, 10**1000)}))
    with pytest.raises(TypeError, match="numeric"):
        encode_mdap(MDAP(**{**imap, "elements": (b"x",)}))

    with pytest.raises(ValueError, match="Boolean element type"):
        encode_mdap(MDAP((1,), 1, MDAPAlgorithm.BOOLEAN, (True,)))
    with pytest.raises(ValueError, match="requires 2 elements"):
        encode_mdap(
            MDAP((2,), 1, MDAPAlgorithm.BOOLEAN, (True,), MDAPElementType.BOOLEAN)
        )
    with pytest.raises(ValueError, match="unsigned element type"):
        encode_mdap(MDAP((1,), 1, MDAPAlgorithm.UNSIGNED_INTEGER, (0,), uint_bias=0))
    with pytest.raises(TypeError, match="bias"):
        encode_mdap(
            MDAP(
                (1,),
                1,
                MDAPAlgorithm.UNSIGNED_INTEGER,
                (0,),
                MDAPElementType.UNSIGNED_INTEGER,
                uint_bias=True,
            )
        )
    with pytest.raises(ValueError, match="non-negative"):
        encode_mdap(
            MDAP(
                (1,),
                1,
                MDAPAlgorithm.UNSIGNED_INTEGER,
                (0,),
                MDAPElementType.UNSIGNED_INTEGER,
                uint_bias=-1,
            )
        )
    with pytest.raises(TypeError, match="element must"):
        encode_mdap(
            MDAP(
                (1,),
                1,
                MDAPAlgorithm.UNSIGNED_INTEGER,
                (False,),
                MDAPElementType.UNSIGNED_INTEGER,
                uint_bias=0,
            )
        )


def test_encoder_validates_rle_model_details() -> None:
    base = dict(
        dimensions=(1,),
        element_size=1,
        algorithm=MDAPAlgorithm.RUN_LENGTH,
        element_type=MDAPElementType.UNSIGNED_INTEGER,
        rle_default=0,
    )
    with pytest.raises(ValueError, match="zero signal"):
        encode_mdap(MDAP(**{**base, "element_size": 0}))
    with pytest.raises(ValueError, match="contextual"):
        encode_mdap(MDAP(**{**base, "element_type": MDAPElementType.BOOLEAN}))
    with pytest.raises(ValueError, match="dense elements"):
        encode_mdap(MDAP(**base, elements=(0,)))
    with pytest.raises(TypeError, match="MDAPPatch"):
        encode_mdap(MDAP(**base, patches=(object(),)))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="coordinates"):
        encode_mdap(MDAP(**base, patches=(MDAPPatch(1, (True,), (1,)),)))
    with pytest.raises(LimitExceeded, match="patches"):
        encode_mdap(MDAP(**base, patches=(MDAPPatch(1, (0,), (1,)),)), max_patches=0)


def test_api_limit_arguments_and_unvalidated_rle_access() -> None:
    for kwargs in ({"max_dimensions": True}, {"max_dimensions": 0}, {"max_patches": -1}):
        with pytest.raises((TypeError, ValueError)):
            decode_mdap(b"", **kwargs)  # type: ignore[arg-type]
    empty_rle = MDAP((1,), 1, MDAPAlgorithm.RUN_LENGTH)
    with pytest.raises(LookupError, match="no default"):
        empty_rle.element_at(0)
    with pytest.raises(TypeError, match="integer"):
        empty_rle.materialize(max_elements=True)
    with pytest.raises(ValueError, match="non-negative"):
        empty_rle.materialize(max_elements=-1)


def test_rle_rejects_invalid_patch_geometry() -> None:
    base = dict(
        dimensions=(3, 4),
        element_size=1,
        algorithm=MDAPAlgorithm.RUN_LENGTH,
        element_type=MDAPElementType.UNSIGNED_INTEGER,
        rle_default=0,
    )
    with pytest.raises(ValueError, match="dimensionality"):
        encode_mdap(MDAP(**base, patches=(MDAPPatch(1, (0,), (1,)),)))
    with pytest.raises(ValueError, match="positive"):
        encode_mdap(MDAP(**base, patches=(MDAPPatch(1, (0, 0), (0, 1)),)))
    with pytest.raises(ValueError, match="outside"):
        encode_mdap(MDAP(**base, patches=(MDAPPatch(1, (2, 0), (2, 1)),)))


def test_access_and_materialization_are_bounded() -> None:
    pack = MDAP((100, 100), 0, MDAPAlgorithm.NATURAL)
    with pytest.raises(IndexError, match="outside"):
        pack.element_at(100, 0)
    with pytest.raises(TypeError, match="integer"):
        pack.element_at(True, 0)
    with pytest.raises(ValueError, match="indices"):
        pack.element_at(0)
    with pytest.raises(LimitExceeded, match="materialization"):
        pack.materialize(max_elements=9999)
