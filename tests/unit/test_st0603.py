from __future__ import annotations

from collections.abc import Callable

import pytest

from stanag4609.st0603 import (
    nano_precision_to_precision_timestamp,
    precision_to_nano_precision_timestamp,
)


@pytest.mark.parametrize(
    ("nanoseconds", "microseconds"),
    [
        (31_276, 31),
        (9_572_831, 9_573),
        (9_572_632, 9_573),
        (499, 0),
        (500, 1),
        (2**64 - 1, 18_446_744_073_709_552),
    ],
)
def test_nano_precision_to_precision_uses_st0603_half_up_mapping(
    nanoseconds: int,
    microseconds: int,
) -> None:
    assert nano_precision_to_precision_timestamp(nanoseconds) == microseconds


@pytest.mark.parametrize(
    ("microseconds", "nanoseconds"),
    [
        (31, 31_000),
        (9_573, 9_573_000),
        (0, 0),
        ((2**64 - 1) // 1_000, ((2**64 - 1) // 1_000) * 1_000),
    ],
)
def test_precision_to_nano_precision_uses_st0603_exact_mapping(
    microseconds: int,
    nanoseconds: int,
) -> None:
    assert precision_to_nano_precision_timestamp(microseconds) == nanoseconds


@pytest.mark.parametrize("value", [-1, 2**64, True, 1.0, "1"])
@pytest.mark.parametrize(
    "convert",
    [
        nano_precision_to_precision_timestamp,
        precision_to_nano_precision_timestamp,
    ],
)
def test_st0603_timestamp_conversions_require_uint64_input(
    convert: Callable[[int], int], value: object
) -> None:
    with pytest.raises((TypeError, ValueError), match="unsigned 64-bit integer"):
        convert(value)  # type: ignore[arg-type]


def test_precision_to_nano_precision_rejects_uint64_output_overflow() -> None:
    with pytest.raises(ValueError, match="outside the unsigned 64-bit range"):
        precision_to_nano_precision_timestamp((2**64 - 1) // 1_000 + 1)
