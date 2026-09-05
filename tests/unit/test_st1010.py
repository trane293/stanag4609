from __future__ import annotations

from fractions import Fraction

import pytest

from stanag4609.errors import DecodeError, LimitExceeded
from stanag4609.st1010 import (
    SDCC_FLP_KEY,
    SDCCFLP,
    RawSDCCValue,
    SDCCParseControl,
    SDCCValueFormat,
    decode_sdcc_flp,
    decode_sdcc_parse_control,
    encode_sdcc_flp,
    encode_sdcc_parse_control,
)

FULL_MODE_2 = bytes.fromhex("03 92 043F800000 40000000 404000000000 4000 8000")

SPARSE_MODE_2 = bytes.fromhex("03 B2 04 A03F800000 40000000 404000002000 6000")


def test_sdcc_flp_universal_key_is_the_registered_st1010_key() -> None:
    assert bytes.fromhex("060E2B34020501010E01030321000000") == SDCC_FLP_KEY


def test_official_mode_1_parse_control_example() -> None:
    control = decode_sdcc_parse_control(bytes.fromhex("4B"))
    assert control == SDCCParseControl(
        mode=1,
        sparse=True,
        standard_deviation_length=4,
        correlation_coefficient_length=3,
        standard_deviation_format=None,
        correlation_coefficient_format=SDCCValueFormat.IMAP,
    )
    assert encode_sdcc_parse_control(control) == bytes.fromhex("4B")


def test_official_mode_2_parse_control_example() -> None:
    control = decode_sdcc_parse_control(bytes.fromhex("B308"))
    assert control == SDCCParseControl(
        mode=2,
        sparse=True,
        standard_deviation_length=8,
        correlation_coefficient_length=3,
        standard_deviation_format=SDCCValueFormat.IEEE,
        correlation_coefficient_format=SDCCValueFormat.IMAP,
    )
    assert encode_sdcc_parse_control(control) == bytes.fromhex("B308")


def test_full_mode_2_matrix_decodes_and_round_trips() -> None:
    decoded = decode_sdcc_flp(FULL_MODE_2, require_mode=2)
    assert decoded.matrix_size == 3
    assert decoded.standard_deviations == (1.0, 2.0, 3.0)
    assert decoded.correlation_coefficients == pytest.approx((-1.0, 0.0, 1.0))
    assert decoded.correlation_pairs == ((0, 1), (0, 2), (1, 2))
    assert decoded.correlation(2, 0) == pytest.approx(0.0)
    assert decoded.matrix == (
        (1.0, -1.0, 0.0),
        (-1.0, 2.0, 1.0),
        (0.0, 1.0, 3.0),
    )
    assert encode_sdcc_flp(decoded) == FULL_MODE_2


def test_sparse_matrix_retains_omitted_cells_and_msb_first_bit_vector() -> None:
    decoded = decode_sdcc_flp(SPARSE_MODE_2, require_mode=2)
    assert decoded.sparse is True
    assert decoded.bit_vector == (True, False, True)
    assert decoded.correlation_coefficients[0] == pytest.approx(-0.5)
    assert decoded.correlation_coefficients[1] is None
    assert decoded.correlation_coefficients[2] == pytest.approx(0.5)
    assert decoded.correlation(0, 2) is None
    assert encode_sdcc_flp(decoded) == SPARSE_MODE_2


def test_user_constructed_ieee_and_imap_values_encode() -> None:
    value = SDCCFLP(
        matrix_size=2,
        parse_control=SDCCParseControl(
            mode=2,
            sparse=False,
            standard_deviation_length=8,
            correlation_coefficient_length=3,
            standard_deviation_format=SDCCValueFormat.IEEE,
            correlation_coefficient_format=SDCCValueFormat.IMAP,
        ),
        standard_deviations=(Fraction(1, 2), 2),
        correlation_coefficients=(Fraction(1, 4),),
    )
    encoded = encode_sdcc_flp(value)
    decoded = decode_sdcc_flp(encoded, require_mode=2)
    assert decoded.standard_deviations == (0.5, 2.0)
    assert decoded.correlation_coefficients == pytest.approx((0.25,))


def test_mode_1_parent_format_can_be_supplied_or_preserved_raw() -> None:
    raw = bytes.fromhex("01 40 3F800000")
    undecoded = decode_sdcc_flp(raw)
    assert undecoded.standard_deviations == (RawSDCCValue(bytes.fromhex("3F800000")),)
    assert encode_sdcc_flp(undecoded) == raw

    decoded = decode_sdcc_flp(
        raw,
        mode1_standard_deviation_format=SDCCValueFormat.IEEE,
    )
    assert decoded.standard_deviations == (1.0,)
    assert encode_sdcc_flp(decoded) == raw


def test_standard_deviation_imap_uses_parent_supplied_bounds() -> None:
    raw = bytes.fromhex("02 80 12 0000 4000")
    decoded = decode_sdcc_flp(raw, standard_deviation_imap_bounds=(0, 4))
    assert decoded.standard_deviations == (0.0, 2.0)
    assert encode_sdcc_flp(decoded) == raw


@pytest.mark.parametrize("padding_bit", [0x10, 0x08, 0x04, 0x02, 0x01])
def test_sparse_vector_padding_must_be_zero(padding_bit: int) -> None:
    data = bytearray(SPARSE_MODE_2)
    data[3] |= padding_bit
    with pytest.raises(DecodeError, match="padding"):
        decode_sdcc_flp(bytes(data), require_mode=2)


@pytest.mark.parametrize(
    "raw, message",
    [
        ("03 B2 04 E0" + "3F800000" * 3 + "0000" * 3, "all ones"),
        ("01 A0 04", "correlation length is zero"),
        ("01 80 00", "one or both"),
        ("01 D0 04 3F800000", "reserved"),
        ("01 90 84 3F800000", "final"),
        ("02 90 04 3F800000", "length"),
    ],
)
def test_malformed_flps_are_rejected(raw: str, message: str) -> None:
    with pytest.raises(DecodeError, match=message):
        decode_sdcc_flp(bytes.fromhex(raw))


def test_decoder_bounds_matrix_allocation() -> None:
    with pytest.raises(LimitExceeded, match="matrix size"):
        decode_sdcc_flp(bytes.fromhex("8100 8004"), max_matrix_size=127)


def test_encoder_rejects_invalid_matrix_shapes_and_domains() -> None:
    control = SDCCParseControl(
        mode=2,
        sparse=False,
        standard_deviation_length=4,
        correlation_coefficient_length=4,
        standard_deviation_format=SDCCValueFormat.IEEE,
        correlation_coefficient_format=SDCCValueFormat.IEEE,
    )
    with pytest.raises(ValueError, match="3 standard deviation"):
        encode_sdcc_flp(SDCCFLP(3, control, (1.0, 2.0), (0.0, 0.0, 0.0)))
    with pytest.raises(ValueError, match="range"):
        encode_sdcc_flp(SDCCFLP(2, control, (1.0, 2.0), (1.1,)))
    with pytest.raises(ValueError, match="non-negative"):
        encode_sdcc_flp(SDCCFLP(2, control, (-1.0, 2.0), (0.0,)))


def test_ieee_lengths_are_limited_to_interchange_formats() -> None:
    with pytest.raises(DecodeError, match=r"IEEE.*2, 4, or 8"):
        decode_sdcc_flp(bytes.fromhex("01 80 03 000000"), require_mode=2)


def test_correlation_only_and_deviation_only_matrices() -> None:
    correlation_only = decode_sdcc_flp(bytes.fromhex("02 84 00 3F000000"), require_mode=2)
    assert correlation_only.standard_deviations == ()
    assert correlation_only.correlation_coefficients == (0.5,)
    assert correlation_only.correlation(0, 0) is None
    assert correlation_only.bit_vector is None
    assert encode_sdcc_flp(correlation_only) == bytes.fromhex("02 84 00 3F000000")

    deviation_only = decode_sdcc_flp(bytes.fromhex("02 80 02 3C00 4000"), require_mode=2)
    assert deviation_only.standard_deviations == (1.0, 2.0)
    assert deviation_only.correlation_coefficients == ()
    assert deviation_only.correlation(0, 1) is None
    assert encode_sdcc_flp(deviation_only) == bytes.fromhex("02 80 02 3C00 4000")


def test_sparse_matrix_may_omit_every_correlation_when_deviations_exist() -> None:
    raw = bytes.fromhex("03 B2 04 00" + "3F800000" * 3)
    decoded = decode_sdcc_flp(raw, require_mode=2)
    assert decoded.correlation_coefficients == (None, None, None)
    assert encode_sdcc_flp(decoded) == raw


def test_matrix_accessor_validates_indices() -> None:
    value = decode_sdcc_flp(FULL_MODE_2)
    with pytest.raises(TypeError, match="integer"):
        value.correlation(True, 0)
    with pytest.raises(IndexError, match="outside"):
        value.correlation(-1, 0)
    with pytest.raises(IndexError, match="outside"):
        value.correlation(0, 3)


@pytest.mark.parametrize(
    "data, message",
    [
        (b"", "missing"),
        (b"\x40\x00", "one byte"),
        (b"\x80", "two bytes"),
        (bytes.fromhex("C000"), "reserved"),
        (bytes.fromhex("8080"), "final"),
    ],
)
def test_parse_control_decoder_rejects_invalid_framing(data: bytes, message: str) -> None:
    with pytest.raises(DecodeError, match=message):
        decode_sdcc_parse_control(data)


def test_parse_control_public_type_checks() -> None:
    with pytest.raises(TypeError, match="bytes"):
        decode_sdcc_parse_control(bytearray(b"\0"))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="SDCCParseControl"):
        encode_sdcc_parse_control(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="mode must be an integer"):
        encode_sdcc_parse_control(
            SDCCParseControl(True, False, 0, 0, None, SDCCValueFormat.IMAP)
        )
    with pytest.raises(TypeError, match="sparse must be a boolean"):
        encode_sdcc_parse_control(
            SDCCParseControl(1, 1, 0, 0, None, SDCCValueFormat.IMAP)  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="Slen must be an integer"):
        encode_sdcc_parse_control(
            SDCCParseControl(1, False, 1.5, 0, None, SDCCValueFormat.IMAP)  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="Clen must be an integer"):
        encode_sdcc_parse_control(
            SDCCParseControl(1, False, 0, True, None, SDCCValueFormat.IMAP)
        )


@pytest.mark.parametrize(
    "control, message",
    [
        (SDCCParseControl(1, False, 8, 0, None, SDCCValueFormat.IMAP), "Slen"),
        (SDCCParseControl(1, False, 0, 8, None, SDCCValueFormat.IMAP), "Clen"),
        (
            SDCCParseControl(1, False, 0, 1, None, SDCCValueFormat.IEEE),
            "must use IMAP",
        ),
        (SDCCParseControl(3, False, 1, 0, None, SDCCValueFormat.IMAP), "mode"),
        (SDCCParseControl(2, False, 16, 0, SDCCValueFormat.IEEE, SDCCValueFormat.IMAP), "Slen"),
        (SDCCParseControl(2, False, 0, 16, SDCCValueFormat.IEEE, SDCCValueFormat.IMAP), "Clen"),
        (SDCCParseControl(2, False, 1, 0, None, SDCCValueFormat.IMAP), "format"),
    ],
)
def test_parse_control_encoder_rejects_invalid_domains(
    control: SDCCParseControl, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        encode_sdcc_parse_control(control)


@pytest.mark.parametrize(
    "data, kwargs, exception, message",
    [
        (bytearray(b"\x01\x40"), {}, TypeError, "bytes"),
        (b"\x01\x40", {"max_matrix_size": True}, TypeError, "integer"),
        (b"\x01\x40", {"max_matrix_size": 0}, ValueError, "positive"),
        (b"\x01\x40", {"require_mode": 3}, ValueError, "require_mode"),
        (b"\x01\x40", {"require_mode": True}, TypeError, "integer"),
        (
            b"\x01\x40",
            {"mode1_standard_deviation_format": "IEEE"},
            TypeError,
            "SDCCValueFormat",
        ),
        (
            b"\x01\x40",
            {"standard_deviation_imap_bounds": [0, 1]},
            TypeError,
            "two-value tuple",
        ),
        (
            b"\x01\x40",
            {"standard_deviation_imap_bounds": (1, 1)},
            ValueError,
            "invalid standard_deviation",
        ),
        (b"", {}, DecodeError, "empty"),
        (b"\x81", {}, DecodeError, "Matrix Size"),
        (b"\x01", {}, DecodeError, "Parse Control is missing"),
        (bytes.fromhex("01 80"), {}, DecodeError, "Parse Control is truncated"),
        (bytes.fromhex("01 80 04 3F800000"), {"require_mode": 1}, DecodeError, "Mode 1"),
        (bytes.fromhex("02 A1 04"), {}, DecodeError, "Bit Vector is truncated"),
        (bytes.fromhex("01 A1 04 00"), {}, DecodeError, "at least one correlation"),
    ],
)
def test_flp_decoder_rejects_invalid_envelopes(
    data: object,
    kwargs: dict[str, object],
    exception: type[Exception],
    message: str,
) -> None:
    with pytest.raises(exception, match=message):
        decode_sdcc_flp(data, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "raw, message",
    [
        ("01 80 04 BF800000", "non-negative"),
        ("01 80 04 7F800000", "finite"),
        ("02 84 00 3F8CCCCD", "outside"),
        ("02 91 00 E0", "special"),
    ],
)
def test_decoder_rejects_invalid_numeric_values(raw: str, message: str) -> None:
    with pytest.raises(DecodeError, match=message):
        decode_sdcc_flp(bytes.fromhex(raw), require_mode=2)


def test_imap_deviations_without_parent_bounds_remain_lossless_raw_values() -> None:
    raw = bytes.fromhex("01 80 12 1234")
    decoded = decode_sdcc_flp(raw)
    assert decoded.standard_deviations == (RawSDCCValue(bytes.fromhex("1234")),)
    assert encode_sdcc_flp(decoded) == raw


def test_raw_value_type_and_length_are_explicit() -> None:
    with pytest.raises(TypeError, match="bytes"):
        RawSDCCValue(bytearray(b"x"))  # type: ignore[arg-type]
    control = SDCCParseControl(1, False, 2, 0, None, SDCCValueFormat.IMAP)
    with pytest.raises(ValueError, match="expected 2"):
        encode_sdcc_flp(SDCCFLP(1, control, (RawSDCCValue(b"x"),), ()))


def test_flp_encoder_validates_public_types_and_empty_shapes() -> None:
    with pytest.raises(TypeError, match="SDCCFLP"):
        encode_sdcc_flp(object())  # type: ignore[arg-type]
    empty = SDCCParseControl(2, False, 0, 0, SDCCValueFormat.IEEE, SDCCValueFormat.IMAP)
    with pytest.raises(TypeError, match="integer"):
        encode_sdcc_flp(SDCCFLP(True, empty, (), ()))
    with pytest.raises(ValueError, match="positive"):
        encode_sdcc_flp(SDCCFLP(0, empty, (), ()))
    with pytest.raises(ValueError, match="one or both"):
        encode_sdcc_flp(SDCCFLP(1, empty, (), ()))


def test_flp_encoder_rejects_shape_and_sparse_control_mismatches() -> None:
    deviation_only = SDCCParseControl(2, False, 4, 0, SDCCValueFormat.IEEE, SDCCValueFormat.IMAP)
    with pytest.raises(ValueError, match="Clen zero"):
        encode_sdcc_flp(SDCCFLP(2, deviation_only, (1.0, 2.0), (0.0,)))
    correlation_only = SDCCParseControl(2, False, 0, 2, SDCCValueFormat.IEEE, SDCCValueFormat.IMAP)
    with pytest.raises(ValueError, match="Slen zero"):
        encode_sdcc_flp(SDCCFLP(2, correlation_only, (1.0,), (0.0,)))
    with pytest.raises(ValueError, match="requires 1 correlation"):
        encode_sdcc_flp(SDCCFLP(2, correlation_only, (), ()))
    with pytest.raises(ValueError, match="full matrix"):
        encode_sdcc_flp(SDCCFLP(2, correlation_only, (), (None,)))
    sparse_without_correlations = SDCCParseControl(
        2, True, 4, 0, SDCCValueFormat.IEEE, SDCCValueFormat.IMAP
    )
    with pytest.raises(ValueError, match="correlation length is zero"):
        encode_sdcc_flp(SDCCFLP(1, sparse_without_correlations, (1.0,), ()))
    sparse_scalar = SDCCParseControl(2, True, 4, 2, SDCCValueFormat.IEEE, SDCCValueFormat.IMAP)
    with pytest.raises(ValueError, match="correlation cell"):
        encode_sdcc_flp(SDCCFLP(1, sparse_scalar, (1.0,), ()))


def test_flp_encoder_requires_bounds_for_numeric_imap_deviations() -> None:
    control = SDCCParseControl(2, False, 2, 0, SDCCValueFormat.IMAP, SDCCValueFormat.IMAP)
    with pytest.raises(ValueError, match="parent-supplied bounds"):
        encode_sdcc_flp(SDCCFLP(1, control, (1.0,), ()))


def test_flp_encoder_rejects_unrepresentable_and_nonfinite_ieee_values() -> None:
    half = SDCCParseControl(2, False, 2, 0, SDCCValueFormat.IEEE, SDCCValueFormat.IMAP)
    with pytest.raises(ValueError, match="finite"):
        encode_sdcc_flp(SDCCFLP(1, half, (float("nan"),), ()))
    with pytest.raises(ValueError, match="represented"):
        encode_sdcc_flp(SDCCFLP(1, half, (1e100,), ()))
