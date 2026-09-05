"""MISB ST 1201 integer mapping helpers.

The mapping calculations use :class:`fractions.Fraction` internally.  This
avoids platform-dependent rounding at code-word boundaries while retaining a
convenient floating-point API for decoded application values.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
from typing import TypeAlias

Real: TypeAlias = int | float | Fraction


class IMAPSpecialKind(Enum):
    """Semantic classes assigned to ST 1201 special code words."""

    BELOW_MINIMUM = "below_minimum"
    ABOVE_MAXIMUM = "above_maximum"
    POSITIVE_INFINITY = "positive_infinity"
    NEGATIVE_INFINITY = "negative_infinity"
    POSITIVE_QUIET_NAN = "positive_quiet_nan"
    NEGATIVE_QUIET_NAN = "negative_quiet_nan"
    POSITIVE_SIGNALING_NAN = "positive_signaling_nan"
    NEGATIVE_SIGNALING_NAN = "negative_signaling_nan"
    USER_DEFINED = "user_defined"
    RESERVED = "reserved"


@dataclass(frozen=True, slots=True)
class IMAPSpecialValue:
    """A non-numeric IMAP code word, including its original representation."""

    kind: IMAPSpecialKind
    raw: bytes


def _as_fraction(value: Real, *, name: str) -> Fraction:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
        return Fraction(str(value))
    return Fraction(value)


def _power_of_two(exponent: int) -> Fraction:
    if exponent >= 0:
        return Fraction(1 << exponent)
    return Fraction(1, 1 << -exponent)


def _floor_log2(value: Fraction) -> int:
    """Return floor(log2(value)) exactly for a positive rational."""

    if value <= 0:
        raise ValueError("logarithm input must be positive")
    exponent = value.numerator.bit_length() - value.denominator.bit_length()
    if _power_of_two(exponent) > value:
        exponent -= 1
    return exponent


def _ceil_log2(value: Fraction) -> int:
    floor = _floor_log2(value)
    return floor if value == _power_of_two(floor) else floor + 1


def _is_power_of_two(value: Fraction) -> bool:
    floor = _floor_log2(value)
    return value == _power_of_two(floor)


def imapa_length(minimum: Real, maximum: Real, precision: Real) -> int:
    """Calculate the byte length selected by the ST 1201 IMAPA process."""

    lower = _as_fraction(minimum, name="minimum")
    upper = _as_fraction(maximum, name="maximum")
    requested = _as_fraction(precision, name="precision")
    interval = upper - lower
    if interval <= 0:
        raise ValueError("maximum must be greater than minimum")
    if requested <= 0 or requested >= interval:
        raise ValueError("precision must be positive and smaller than the interval")

    bit_length = _ceil_log2(interval) - _floor_log2(requested) + 1
    return (bit_length + 7) // 8


class IMAPB:
    """Fixed-length ST 1201 IMAPB encoder and decoder."""

    __slots__ = (
        "_interval_is_power_of_two",
        "_maximum",
        "_minimum",
        "_scale_forward",
        "_scale_reverse",
        "_zero_offset",
        "length",
    )

    def __init__(self, minimum: Real, maximum: Real, length: int) -> None:
        lower = _as_fraction(minimum, name="minimum")
        upper = _as_fraction(maximum, name="maximum")
        if upper <= lower:
            raise ValueError("maximum must be greater than minimum")
        if length <= 0:
            raise ValueError("length must be positive")

        interval = upper - lower
        binary_point = _ceil_log2(interval)
        data_power = (8 * length) - 1
        scale_forward = _power_of_two(data_power - binary_point)
        scaled_minimum = scale_forward * lower
        zero_offset = Fraction(0)
        if lower < 0 < upper:
            zero_offset = scaled_minimum - math.floor(scaled_minimum)

        self._minimum = lower
        self._maximum = upper
        self.length = length
        self._scale_forward = scale_forward
        self._scale_reverse = 1 / scale_forward
        self._zero_offset = zero_offset
        self._interval_is_power_of_two = _is_power_of_two(interval)

    @property
    def minimum(self) -> Fraction:
        return self._minimum

    @property
    def maximum(self) -> Fraction:
        return self._maximum

    @property
    def scale_forward(self) -> Fraction:
        return self._scale_forward

    @property
    def scale_reverse(self) -> Fraction:
        return self._scale_reverse

    @property
    def zero_offset(self) -> Fraction:
        return self._zero_offset

    def _special_bytes(self, first_octet: int) -> bytes:
        return bytes([first_octet]) + bytes(self.length - 1)

    def encode(self, value: Real | IMAPSpecialValue) -> bytes:
        """Encode a number or losslessly re-emit an explicit special value."""

        if isinstance(value, IMAPSpecialValue):
            if len(value.raw) != self.length:
                raise ValueError("special value length does not match the mapping length")
            return value.raw

        if isinstance(value, float) and not math.isfinite(value):
            if math.isnan(value):
                return self._special_bytes(0xD0)
            return self._special_bytes(0xC8 if value > 0 else 0xE8)

        inverse_candidate: bytes | None = None
        if isinstance(value, float):
            # Make encode a left inverse of decode despite binary64 rounding and
            # the small normal-domain overhang produced by a non-zero offset.
            candidate = round(
                self._scale_forward * (Fraction(value) - self._minimum)
                + self._zero_offset
            )
            if 0 <= candidate < 1 << (self.length * 8):
                candidate_bytes = candidate.to_bytes(self.length, "big")
                decoded_candidate = self.decode(candidate_bytes)
                if not isinstance(decoded_candidate, IMAPSpecialValue) and (
                    decoded_candidate == value
                ):
                    inverse_candidate = candidate_bytes

        numeric = _as_fraction(value, name="value")
        if numeric < self._minimum:
            if inverse_candidate == bytes(self.length):
                return inverse_candidate
            return self._special_bytes(0xE0)
        if numeric > self._maximum:
            return self._special_bytes(0xE1)
        if inverse_candidate is not None:
            return inverse_candidate

        code_word = int(
            self._scale_forward * (numeric - self._minimum) + self._zero_offset
        )
        return code_word.to_bytes(self.length, "big")

    def decode(self, data: bytes) -> float | IMAPSpecialValue:
        """Decode a fixed-length IMAP value, preserving all special bit patterns."""

        if len(data) != self.length:
            raise ValueError("encoded value length does not match the mapping length")

        code_word = int.from_bytes(data, "big")
        total_bits = self.length * 8
        prefix = code_word >> (total_bits - 2)
        if prefix == 0b11:
            return IMAPSpecialValue(self._classify_special(data), data)
        if prefix == 0b10:
            normal_maximum = 1 << (total_bits - 1)
            if code_word != normal_maximum or not self._interval_is_power_of_two:
                return IMAPSpecialValue(IMAPSpecialKind.RESERVED, data)

        decoded = (
            self._scale_reverse * (code_word - self._zero_offset) + self._minimum
        )
        return float(decoded)

    @staticmethod
    def _classify_special(data: bytes) -> IMAPSpecialKind:
        first = data[0]
        trailing_is_zero = not any(data[1:])
        if first == 0xE0 and trailing_is_zero:
            return IMAPSpecialKind.BELOW_MINIMUM
        if first == 0xE1 and trailing_is_zero:
            return IMAPSpecialKind.ABOVE_MAXIMUM
        if first == 0xC8 and trailing_is_zero:
            return IMAPSpecialKind.POSITIVE_INFINITY
        if first == 0xE8 and trailing_is_zero:
            return IMAPSpecialKind.NEGATIVE_INFINITY
        family = first & 0xF8
        if family == 0xC0:
            return IMAPSpecialKind.USER_DEFINED
        if family == 0xD0:
            return IMAPSpecialKind.POSITIVE_QUIET_NAN
        if family == 0xD8:
            return IMAPSpecialKind.POSITIVE_SIGNALING_NAN
        if family == 0xF0:
            return IMAPSpecialKind.NEGATIVE_QUIET_NAN
        if family == 0xF8:
            return IMAPSpecialKind.NEGATIVE_SIGNALING_NAN
        return IMAPSpecialKind.RESERVED
