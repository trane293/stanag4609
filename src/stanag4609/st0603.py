"""MISB ST 0603.5 MISP timestamp representations and conversions."""

from __future__ import annotations

UINT64_MAX = 2**64 - 1


def _uint64(value: int, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an unsigned 64-bit integer")
    if not 0 <= value <= UINT64_MAX:
        raise ValueError(f"{name} must be an unsigned 64-bit integer")
    return value


def nano_precision_to_precision_timestamp(nanoseconds: int) -> int:
    """Convert an ST 0603 Nano Precision Time Stamp to microseconds.

    ST 0603.5 section 7.3 requires integer half-up rounding: add 500
    nanoseconds, then divide by 1000. The conversion is lossy.
    """

    value = _uint64(nanoseconds, name="Nano Precision Time Stamp")
    return (value + 500) // 1_000


def precision_to_nano_precision_timestamp(microseconds: int) -> int:
    """Convert an ST 0603 Precision Time Stamp to exact nanoseconds.

    The result remains a timestamp representation and therefore must fit the
    unsigned 64-bit domain defined by ST 0603.5.
    """

    value = _uint64(microseconds, name="Precision Time Stamp")
    converted = value * 1_000
    if converted > UINT64_MAX:
        raise ValueError(
            "converted Nano Precision Time Stamp is outside the unsigned 64-bit range"
        )
    return converted


__all__ = [
    "nano_precision_to_precision_timestamp",
    "precision_to_nano_precision_timestamp",
]
