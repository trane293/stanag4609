from __future__ import annotations

import math
from fractions import Fraction

import pytest

from stanag4609.imap import (
    IMAPB,
    IMAPOverflowPolicy,
    IMAPSpecialKind,
    IMAPSpecialValue,
    imapa_length,
)


@pytest.mark.parametrize(
    "value, encoded",
    [
        (0.0, "00000000"),
        (10.1, "0A199999"),
        (20.2, "14333333"),
        (30.3, "1E4CCCCC"),
        (40.4, "28666666"),
        (50.5, "32800000"),
        (60.6, "3C999999"),
        (70.7, "46B33333"),
        (80.8, "50CCCCCC"),
        (90.9, "5AE66666"),
        (100.0, "64000000"),
    ],
)
def test_official_st1201_test_1_vectors(value: float, encoded: str) -> None:
    mapping = IMAPB(0.0, 100.0, imapa_length(0.0, 100.0, 1e-5))
    raw = bytes.fromhex(encoded)
    assert mapping.encode(value) == raw
    assert abs(mapping.decode(raw) - value) <= mapping.scale_reverse


@pytest.mark.parametrize(
    "value, encoded, decoded",
    [
        (0.0, "000000", 0.0),
        (10.1, "0A1999", 10.0999908447),
        (20.2, "143333", 20.1999969482),
        (30.3, "1E4CCC", 30.2999877929),
        (40.4, "286666", 40.3999938964),
        (50.5, "328000", 50.5),
        (60.6, "3C9999", 60.5999908447),
        (70.7, "46B333", 70.6999969482),
        (80.8, "50CCCC", 80.7999877929),
        (90.9, "5AE666", 90.8999938964),
        (100.0, "640000", 100.0),
    ],
)
def test_official_st1201_test_2_vectors(value: float, encoded: str, decoded: float) -> None:
    mapping = IMAPB(0.0, 100.0, 3)
    raw = bytes.fromhex(encoded)
    assert mapping.encode(value) == raw
    assert mapping.decode(raw) == pytest.approx(decoded, abs=1e-9)
    assert mapping.scale_forward == 65_536
    assert mapping.scale_reverse == Fraction(1, 65_536)
    assert mapping.zero_offset == 0


@pytest.mark.parametrize(
    "value, encoded, decoded",
    [
        (-9.9, "000000", -9.900009155273438),
        (0.225, "0A2000", 0.2249908447265625),
        (10.35, "144000", 10.349990844726562),
        (20.475, "1E6000", 20.474990844726562),
        (30.6, "288000", 30.599990844726562),
        (40.725, "32A000", 40.72499084472656),
        (0.0, "09E667", 0.0),
        (50.85, "3CC000", 50.84999084472656),
        (60.975, "46E000", 60.97499084472656),
        (71.1, "510000", 71.09999084472656),
        (81.225, "5B2000", 81.22499084472656),
        (91.35, "654000", 91.34999084472656),
        (101.475, "6F6000", 101.47499084472656),
        (110.0, "77E667", 110.0),
    ],
)
def test_official_st1201_test_3_zero_offset_vectors(
    value: float, encoded: str, decoded: float
) -> None:
    mapping = IMAPB(-9.9, 110.0, 3)
    raw = bytes.fromhex(encoded)
    assert mapping.zero_offset == Fraction(3, 5)
    assert mapping.encode(value) == raw
    assert mapping.decode(raw) == pytest.approx(decoded, abs=1e-9)


def test_official_small_range_example() -> None:
    mapping = IMAPB(0.1, 0.9, 2)
    assert mapping.encode(0.5) == bytes.fromhex("3333")
    assert mapping.decode(bytes.fromhex("3333")) == pytest.approx(0.499993896484375)


@pytest.mark.parametrize(
    "value, encoded, kind",
    [
        (-1.0, "E00000", IMAPSpecialKind.BELOW_MINIMUM),
        (101.0, "E10000", IMAPSpecialKind.ABOVE_MAXIMUM),
        (math.inf, "C80000", IMAPSpecialKind.POSITIVE_INFINITY),
        (-math.inf, "E80000", IMAPSpecialKind.NEGATIVE_INFINITY),
        (math.nan, "D00000", IMAPSpecialKind.POSITIVE_QUIET_NAN),
    ],
)
def test_official_st1201_special_value_vectors(
    value: float, encoded: str, kind: IMAPSpecialKind
) -> None:
    mapping = IMAPB(0.0, 100.0, 3)
    raw = bytes.fromhex(encoded)
    assert mapping.encode(value) == raw
    decoded = mapping.decode(raw)
    assert isinstance(decoded, IMAPSpecialValue)
    assert decoded.kind is kind
    assert decoded.raw == raw


@pytest.mark.parametrize(
    "mapping, value, encoded",
    [
        (IMAPB(0.0, 100.0, 4), math.nan, "D0000000"),
        (IMAPB(0.0, 100.0, 4), math.inf, "C8000000"),
        (IMAPB(0.0, 100.0, 4), -math.inf, "E8000000"),
        (IMAPB(0.0, 100.0, 4), -1.0, "E0000000"),
        (IMAPB(0.0, 100.0, 4), 101.0, "E1000000"),
        (IMAPB(-9.9, 110.0, 3), math.nan, "D00000"),
        (IMAPB(-9.9, 110.0, 3), math.inf, "C80000"),
        (IMAPB(-9.9, 110.0, 3), -math.inf, "E80000"),
        (IMAPB(-9.9, 110.0, 3), -100.0, "E00000"),
        (IMAPB(-9.9, 110.0, 3), 121.0, "E10000"),
    ],
)
def test_remaining_official_st1201_test_1_and_3_special_vectors(
    mapping: IMAPB, value: float, encoded: str
) -> None:
    assert mapping.encode(value) == bytes.fromhex(encoded)


def test_negative_zero_maps_as_normal_positive_zero() -> None:
    mapping = IMAPB(-1, 1, 2)
    assert mapping.encode(-0.0) == mapping.encode(0.0)
    assert mapping.decode(mapping.encode(-0.0)) == 0.0


def test_negative_nan_maps_to_negative_quiet_nan() -> None:
    mapping = IMAPB(0, 100, 3)
    assert mapping.encode(math.copysign(math.nan, -1.0)) == bytes.fromhex("F00000")


def test_parent_unspecified_overflow_can_use_st1201_default_bounds() -> None:
    mapping = IMAPB(-9.9, 110, 3)
    assert mapping.decode(
        bytes.fromhex("E00000"), overflow_policy=IMAPOverflowPolicy.CLAMP
    ) == pytest.approx(-9.9)
    assert mapping.decode(
        bytes.fromhex("E10000"), overflow_policy=IMAPOverflowPolicy.CLAMP
    ) == pytest.approx(110)


@pytest.mark.parametrize(
    "mapping, raw",
    [
        (IMAPB(-90, 90, 4), bytes.fromhex("4071D894")),
        (IMAPB(-180, 180, 4), bytes.fromhex("19BDBFE7")),
        (IMAPB(-900, 9000, 3), bytes.fromhex("089800")),
        (IMAPB(-9.9, 110, 3), bytes.fromhex("000000")),
        (IMAPB(0.1, 0.9, 2), bytes.fromhex("1335")),
    ],
)
def test_normal_decode_encode_preserves_quantized_code_word(
    mapping: IMAPB, raw: bytes
) -> None:
    decoded = mapping.decode(raw)
    assert isinstance(decoded, float)
    assert mapping.encode(decoded) == raw


def test_reserved_and_user_defined_patterns_are_preserved() -> None:
    mapping = IMAPB(0, 100, 3)
    reserved = mapping.decode(bytes.fromhex("800001"))
    user = mapping.decode(bytes.fromhex("C01234"))
    assert isinstance(reserved, IMAPSpecialValue)
    assert reserved.kind is IMAPSpecialKind.RESERVED
    assert isinstance(user, IMAPSpecialValue)
    assert user.kind is IMAPSpecialKind.USER_DEFINED
    assert mapping.encode(user) == bytes.fromhex("C01234")


@pytest.mark.parametrize(
    "encoded, kind",
    [
        ("C80000", IMAPSpecialKind.POSITIVE_INFINITY),
        ("E80000", IMAPSpecialKind.NEGATIVE_INFINITY),
        ("D00000", IMAPSpecialKind.POSITIVE_QUIET_NAN),
        ("F00000", IMAPSpecialKind.NEGATIVE_QUIET_NAN),
        ("D80000", IMAPSpecialKind.POSITIVE_SIGNALING_NAN),
        ("F80000", IMAPSpecialKind.NEGATIVE_SIGNALING_NAN),
        ("C00001", IMAPSpecialKind.USER_DEFINED),
        ("E20000", IMAPSpecialKind.RESERVED),
        ("E70001", IMAPSpecialKind.RESERVED),
        ("800001", IMAPSpecialKind.RESERVED),
    ],
)
def test_all_st1201_special_families_are_classified_and_lossless(
    encoded: str, kind: IMAPSpecialKind
) -> None:
    mapping = IMAPB(0, 100, 3)
    raw = bytes.fromhex(encoded)
    decoded = mapping.decode(raw)
    assert isinstance(decoded, IMAPSpecialValue)
    assert decoded.kind is kind
    assert mapping.encode(decoded) == raw


def test_power_of_two_maximum_is_the_only_normal_10_prefix() -> None:
    mapping = IMAPB(0, 128, 3)
    assert mapping.encode(128) == bytes.fromhex("800000")
    assert mapping.decode(bytes.fromhex("800000")) == 128.0


@pytest.mark.parametrize(
    "minimum, maximum, length",
    [(0, 0, 2), (1, 0, 2), (0, 1, 0)],
)
def test_mapping_parameters_are_validated(minimum: int, maximum: int, length: int) -> None:
    with pytest.raises(ValueError):
        IMAPB(minimum, maximum, length)


@pytest.mark.parametrize("length", [True, 1.5, "2"])
def test_mapping_length_must_be_an_integer_byte_count(length: object) -> None:
    with pytest.raises(TypeError, match="length must be an integer"):
        IMAPB(0, 100, length)  # type: ignore[arg-type]


def test_decode_rejects_unknown_overflow_policy() -> None:
    mapping = IMAPB(0, 100, 2)
    with pytest.raises(TypeError, match="overflow_policy"):
        mapping.decode(b"\x00\x00", overflow_policy="clamp")  # type: ignore[arg-type]


def test_encoded_length_and_special_instance_are_validated() -> None:
    mapping = IMAPB(0, 100, 2)
    with pytest.raises(ValueError, match="length"):
        mapping.decode(b"\x00")
    with pytest.raises(ValueError, match="length"):
        mapping.encode(
            IMAPSpecialValue(IMAPSpecialKind.RESERVED, b"\x80\x00\x01")
        )


@pytest.mark.parametrize(
    "minimum, maximum, precision, expected",
    [(-900, 19_000, 0.5, 3), (-1, 1, 0.0001, 2), (0, 100, 0.00001, 4)],
)
def test_imapa_computes_official_lengths(
    minimum: float, maximum: float, precision: float, expected: int
) -> None:
    assert imapa_length(minimum, maximum, precision) == expected


def test_imapa_parameters_are_validated() -> None:
    with pytest.raises(ValueError):
        imapa_length(0, 1, 0)
    with pytest.raises(ValueError):
        imapa_length(0, 1, 1)
