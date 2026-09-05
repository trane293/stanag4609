"""MISB ST 1010.3 standard-deviation/correlation FLP codec."""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
from typing import Literal, TypeAlias

from stanag4609.errors import DecodeError, LimitExceeded, NeedMoreData
from stanag4609.imap import IMAPB, IMAPSpecialValue
from stanag4609.klv.ber import decode_ber_oid, encode_ber_oid

SDCC_FLP_KEY = bytes.fromhex("06 0E 2B 34 02 05 01 01 0E 01 03 03 21 00 00 00")

Real: TypeAlias = int | float | Fraction


class SDCCValueFormat(Enum):
    """Runtime value representation selected by ST 1010 Mode 2."""

    IEEE = 0
    IMAP = 1


@dataclass(frozen=True, slots=True)
class RawSDCCValue:
    """An explicitly opaque SDCC element whose parent mapping is unavailable."""

    data: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes):
            raise TypeError("RawSDCCValue data must be bytes")


SDCCValue: TypeAlias = Real | RawSDCCValue


@dataclass(frozen=True, slots=True)
class SDCCParseControl:
    """Decoded ST 1010 Parse Control fields."""

    mode: Literal[1, 2]
    sparse: bool
    standard_deviation_length: int
    correlation_coefficient_length: int
    standard_deviation_format: SDCCValueFormat | None
    correlation_coefficient_format: SDCCValueFormat


@dataclass(frozen=True, slots=True)
class SDCCFLP:
    """One upper-triangular standard-deviation/correlation matrix.

    Correlations use the ST 1010 row-major upper-triangle order. In a sparse
    matrix, ``None`` represents a zero/unknown coefficient omitted from the
    wire value by the Bit Vector. ``source_tags`` is parent-set context and is
    not encoded by ST 1010 itself.
    """

    matrix_size: int
    parse_control: SDCCParseControl
    standard_deviations: tuple[SDCCValue, ...]
    correlation_coefficients: tuple[SDCCValue | None, ...]
    standard_deviation_imap_bounds: tuple[Real, Real] | None = None
    source_tags: tuple[int, ...] = ()

    @property
    def sparse(self) -> bool:
        return self.parse_control.sparse

    @property
    def correlation_pairs(self) -> tuple[tuple[int, int], ...]:
        return tuple(
            (row, column)
            for row in range(self.matrix_size)
            for column in range(row + 1, self.matrix_size)
        )

    @property
    def bit_vector(self) -> tuple[bool, ...] | None:
        if not self.sparse:
            return None
        return tuple(value is not None for value in self.correlation_coefficients)

    def correlation(self, row: int, column: int) -> SDCCValue | None:
        """Return a symmetric matrix cell, including its diagonal deviation."""
        _validate_matrix_index(row, self.matrix_size)
        _validate_matrix_index(column, self.matrix_size)
        if row == column:
            if not self.standard_deviations:
                return None
            return self.standard_deviations[row]
        if row > column:
            row, column = column, row
        index = row * (2 * self.matrix_size - row - 1) // 2 + column - row - 1
        if not self.correlation_coefficients:
            return None
        return self.correlation_coefficients[index]

    @property
    def matrix(self) -> tuple[tuple[SDCCValue | None, ...], ...]:
        return tuple(
            tuple(self.correlation(row, column) for column in range(self.matrix_size))
            for row in range(self.matrix_size)
        )


def _validate_matrix_index(index: int, size: int) -> None:
    if isinstance(index, bool) or not isinstance(index, int):
        raise TypeError("matrix index must be an integer")
    if not 0 <= index < size:
        raise IndexError(f"matrix index {index} is outside [0, {size})")


def decode_sdcc_parse_control(data: bytes) -> SDCCParseControl:
    """Decode one complete Mode 1 or Mode 2 Parse Control value."""
    if not isinstance(data, bytes):
        raise TypeError("SDCC Parse Control data must be bytes")
    if not data:
        raise DecodeError("ST 1010 Parse Control is missing")
    first = data[0]
    if not first & 0x80:
        if len(data) != 1:
            raise DecodeError("ST 1010 Mode 1 Parse Control must contain one byte")
        return SDCCParseControl(
            mode=1,
            sparse=bool(first & 0x08),
            standard_deviation_length=(first >> 4) & 0x07,
            correlation_coefficient_length=first & 0x07,
            standard_deviation_format=None,
            correlation_coefficient_format=SDCCValueFormat.IMAP,
        )

    if len(data) != 2:
        raise DecodeError("ST 1010 Mode 2 Parse Control must contain two bytes")
    second = data[1]
    if first & 0x40 or second & 0x60:
        raise DecodeError("ST 1010 Mode 2 Parse Control reserved bits must be zero")
    if second & 0x80:
        raise DecodeError("ST 1010 Mode 2 Parse Control second byte must be final")
    return SDCCParseControl(
        mode=2,
        sparse=bool(first & 0x20),
        standard_deviation_length=second & 0x0F,
        correlation_coefficient_length=first & 0x0F,
        standard_deviation_format=SDCCValueFormat.IMAP if second & 0x10 else SDCCValueFormat.IEEE,
        correlation_coefficient_format=SDCCValueFormat.IMAP
        if first & 0x10
        else SDCCValueFormat.IEEE,
    )


def encode_sdcc_parse_control(control: SDCCParseControl) -> bytes:
    """Encode canonical ST 1010 Parse Control bytes."""
    if not isinstance(control, SDCCParseControl):
        raise TypeError("control must be an SDCCParseControl")
    if isinstance(control.mode, bool) or not isinstance(control.mode, int):
        raise TypeError("ST 1010 Parse Control mode must be an integer")
    if not isinstance(control.sparse, bool):
        raise TypeError("ST 1010 Parse Control sparse must be a boolean")
    if isinstance(control.standard_deviation_length, bool) or not isinstance(
        control.standard_deviation_length, int
    ):
        raise TypeError("ST 1010 Slen must be an integer")
    if isinstance(control.correlation_coefficient_length, bool) or not isinstance(
        control.correlation_coefficient_length, int
    ):
        raise TypeError("ST 1010 Clen must be an integer")
    if control.mode == 1:
        if not 0 <= control.standard_deviation_length <= 7:
            raise ValueError("ST 1010 Mode 1 Slen must be between 0 and 7")
        if not 0 <= control.correlation_coefficient_length <= 7:
            raise ValueError("ST 1010 Mode 1 Clen must be between 0 and 7")
        if control.correlation_coefficient_format is not SDCCValueFormat.IMAP:
            raise ValueError("ST 1010 Mode 1 correlations must use IMAP")
        if control.standard_deviation_format is not None and not isinstance(
            control.standard_deviation_format, SDCCValueFormat
        ):
            raise ValueError("ST 1010 Mode 1 has an invalid parent-defined format")
        return bytes(
            (
                control.standard_deviation_length << 4
                | int(control.sparse) << 3
                | control.correlation_coefficient_length,
            )
        )
    if control.mode != 2:
        raise ValueError("ST 1010 Parse Control mode must be 1 or 2")
    if not 0 <= control.standard_deviation_length <= 15:
        raise ValueError("ST 1010 Mode 2 Slen must be between 0 and 15")
    if not 0 <= control.correlation_coefficient_length <= 15:
        raise ValueError("ST 1010 Mode 2 Clen must be between 0 and 15")
    if not isinstance(control.standard_deviation_format, SDCCValueFormat):
        raise ValueError("ST 1010 Mode 2 requires a standard-deviation format")
    if not isinstance(control.correlation_coefficient_format, SDCCValueFormat):
        raise ValueError("ST 1010 Mode 2 requires a correlation format")
    first = (
        0x80
        | int(control.sparse) << 5
        | control.correlation_coefficient_format.value << 4
        | control.correlation_coefficient_length
    )
    second = control.standard_deviation_format.value << 4 | control.standard_deviation_length
    return bytes((first, second))


def _decode_ieee(data: bytes, *, coefficient: bool) -> float:
    formats = {2: ">e", 4: ">f", 8: ">d"}
    try:
        format_code = formats[len(data)]
    except KeyError as error:
        raise DecodeError("ST 1010 IEEE values must contain 2, 4, or 8 bytes") from error
    value = float(struct.unpack(format_code, data)[0])
    if not math.isfinite(value):
        raise DecodeError("ST 1010 IEEE values must be finite")
    if coefficient and not -1.0 <= value <= 1.0:
        raise DecodeError("ST 1010 correlation coefficient is outside [-1, 1]")
    if not coefficient and value < 0:
        raise DecodeError("ST 1010 standard deviation must be non-negative")
    return value


def _decode_value(
    data: bytes,
    value_format: SDCCValueFormat | None,
    *,
    coefficient: bool,
    standard_deviation_bounds: tuple[Real, Real] | None,
) -> SDCCValue:
    if value_format is None:
        return RawSDCCValue(data)
    if value_format is SDCCValueFormat.IEEE:
        return _decode_ieee(data, coefficient=coefficient)
    bounds: tuple[Real, Real] | None = (-1, 1) if coefficient else standard_deviation_bounds
    if bounds is None:
        return RawSDCCValue(data)
    try:
        decoded = IMAPB(bounds[0], bounds[1], len(data)).decode(data)
    except ValueError as error:
        raise DecodeError(f"invalid ST 1010 IMAP mapping: {error}") from error
    if isinstance(decoded, IMAPSpecialValue):
        raise DecodeError(f"ST 1010 values do not permit IMAP special value {decoded.kind.value}")
    if coefficient and not -1.0 <= decoded <= 1.0:
        raise DecodeError("ST 1010 correlation coefficient is outside [-1, 1]")
    if not coefficient and decoded < 0:
        raise DecodeError("ST 1010 standard deviation must be non-negative")
    return decoded


def decode_sdcc_flp(
    data: bytes,
    *,
    mode1_standard_deviation_format: SDCCValueFormat | None = None,
    standard_deviation_imap_bounds: tuple[Real, Real] | None = None,
    require_mode: Literal[1, 2] | None = None,
    max_matrix_size: int = 1024,
) -> SDCCFLP:
    """Decode one bounded ST 1010 SDCC-FLP value.

    Mode 1 leaves standard-deviation atoms opaque unless the parent-defined
    representation is supplied. The same applies to IMAP deviations whose
    parent-defined bounds are unavailable.
    """
    if not isinstance(data, bytes):
        raise TypeError("SDCC-FLP data must be bytes")
    if isinstance(max_matrix_size, bool) or not isinstance(max_matrix_size, int):
        raise TypeError("max_matrix_size must be an integer")
    if max_matrix_size < 1:
        raise ValueError("max_matrix_size must be positive")
    if require_mode is not None and (
        isinstance(require_mode, bool) or not isinstance(require_mode, int)
    ):
        raise TypeError("require_mode must be an integer or None")
    if require_mode not in {None, 1, 2}:
        raise ValueError("require_mode must be 1, 2, or None")
    if mode1_standard_deviation_format is not None and not isinstance(
        mode1_standard_deviation_format, SDCCValueFormat
    ):
        raise TypeError("mode1_standard_deviation_format must be an SDCCValueFormat")
    if standard_deviation_imap_bounds is not None:
        if not isinstance(standard_deviation_imap_bounds, tuple) or len(
            standard_deviation_imap_bounds
        ) != 2:
            raise TypeError("standard_deviation_imap_bounds must be a two-value tuple")
        try:
            IMAPB(
                standard_deviation_imap_bounds[0],
                standard_deviation_imap_bounds[1],
                1,
            )
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid standard_deviation_imap_bounds: {error}") from error
    if not data:
        raise DecodeError("ST 1010 SDCC-FLP is empty")
    try:
        matrix_size, size_length = decode_ber_oid(data, max_octets=10)
    except NeedMoreData as error:
        raise DecodeError("ST 1010 Matrix Size is truncated") from error
    if matrix_size < 1:
        raise DecodeError("ST 1010 matrix size must be positive")
    if matrix_size > max_matrix_size:
        raise LimitExceeded(
            f"ST 1010 matrix size {matrix_size} exceeds configured maximum {max_matrix_size}"
        )
    if size_length >= len(data):
        raise DecodeError("ST 1010 Parse Control is missing")
    control_length = 2 if data[size_length] & 0x80 else 1
    control_end = size_length + control_length
    if control_end > len(data):
        raise DecodeError("ST 1010 Parse Control is truncated")
    control = decode_sdcc_parse_control(data[size_length:control_end])
    if require_mode is not None and control.mode != require_mode:
        raise DecodeError(f"ST 1010 Mode {require_mode} Parse Control is required")
    if control.mode == 1 and mode1_standard_deviation_format is not None:
        control = SDCCParseControl(
            mode=1,
            sparse=control.sparse,
            standard_deviation_length=control.standard_deviation_length,
            correlation_coefficient_length=control.correlation_coefficient_length,
            standard_deviation_format=mode1_standard_deviation_format,
            correlation_coefficient_format=SDCCValueFormat.IMAP,
        )

    slen = control.standard_deviation_length
    clen = control.correlation_coefficient_length
    if not slen and not clen:
        raise DecodeError("ST 1010 requires one or both conditional value arrays")
    if control.sparse and not clen:
        raise DecodeError("ST 1010 sparse flag is invalid when correlation length is zero")
    correlation_count = matrix_size * (matrix_size - 1) // 2
    if control.sparse and not correlation_count:
        raise DecodeError("ST 1010 sparse mode requires at least one correlation cell")

    offset = control_end
    bit_vector: tuple[bool, ...] | None = None
    if control.sparse:
        vector_length = (correlation_count + 7) // 8
        vector_end = offset + vector_length
        if vector_end > len(data):
            raise DecodeError("ST 1010 sparse Bit Vector is truncated")
        vector = data[offset:vector_end]
        padding = vector_length * 8 - correlation_count
        if padding and vector[-1] & ((1 << padding) - 1):
            raise DecodeError("ST 1010 sparse Bit Vector padding bits must be zero")
        bit_vector = tuple(
            bool(vector[index // 8] & (1 << (7 - index % 8))) for index in range(correlation_count)
        )
        if all(bit_vector):
            raise DecodeError("ST 1010 sparse Bit Vector cannot contain all ones")
        offset = vector_end

    standard_count = matrix_size if slen else 0
    encoded_correlation_count = (
        sum(bit_vector) if bit_vector is not None else correlation_count if clen else 0
    )
    expected_length = offset + standard_count * slen + encoded_correlation_count * clen
    if len(data) != expected_length:
        raise DecodeError(
            f"ST 1010 SDCC-FLP length is {len(data)} bytes; expected {expected_length}"
        )

    standard_deviations: list[SDCCValue] = []
    for _ in range(standard_count):
        raw = data[offset : offset + slen]
        standard_deviations.append(
            _decode_value(
                raw,
                control.standard_deviation_format,
                coefficient=False,
                standard_deviation_bounds=standard_deviation_imap_bounds,
            )
        )
        offset += slen

    correlations: list[SDCCValue | None] = []
    for index in range(correlation_count if clen else 0):
        if bit_vector is not None and not bit_vector[index]:
            correlations.append(None)
            continue
        raw = data[offset : offset + clen]
        correlations.append(
            _decode_value(
                raw,
                control.correlation_coefficient_format,
                coefficient=True,
                standard_deviation_bounds=None,
            )
        )
        offset += clen
    return SDCCFLP(
        matrix_size,
        control,
        tuple(standard_deviations),
        tuple(correlations),
        standard_deviation_imap_bounds,
    )


def _numeric(value: Real, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Fraction)):
        raise TypeError(f"{name} must be numeric or RawSDCCValue")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _encode_value(
    value: SDCCValue,
    length: int,
    value_format: SDCCValueFormat | None,
    *,
    coefficient: bool,
    standard_deviation_bounds: tuple[Real, Real] | None,
) -> bytes:
    if isinstance(value, RawSDCCValue):
        if len(value.data) != length:
            raise ValueError(f"RawSDCCValue has {len(value.data)} bytes; expected {length}")
        return value.data
    numeric = _numeric(value, name="ST 1010 value")
    if coefficient and not -1 <= numeric <= 1:
        raise ValueError("ST 1010 correlation coefficient is outside range [-1, 1]")
    if not coefficient and numeric < 0:
        raise ValueError("ST 1010 standard deviation must be non-negative")
    if value_format is None:
        raise ValueError("opaque Mode 1 values require RawSDCCValue")
    if value_format is SDCCValueFormat.IEEE:
        formats = {2: ">e", 4: ">f", 8: ">d"}
        try:
            return struct.pack(formats[length], numeric)
        except KeyError as error:
            raise ValueError("ST 1010 IEEE values must contain 2, 4, or 8 bytes") from error
        except (OverflowError, struct.error) as error:
            raise ValueError("ST 1010 value cannot be represented in its IEEE format") from error
    bounds: tuple[Real, Real] | None = (-1, 1) if coefficient else standard_deviation_bounds
    if bounds is None:
        raise ValueError("ST 1010 IMAP deviations require parent-supplied bounds")
    return IMAPB(bounds[0], bounds[1], length).encode(value)


def _encode_bit_vector(bits: tuple[bool, ...]) -> bytes:
    encoded = bytearray((len(bits) + 7) // 8)
    for index, present in enumerate(bits):
        if present:
            encoded[index // 8] |= 1 << (7 - index % 8)
    return bytes(encoded)


def encode_sdcc_flp(value: SDCCFLP) -> bytes:
    """Encode and structurally validate one ST 1010 SDCC-FLP value."""
    if not isinstance(value, SDCCFLP):
        raise TypeError("value must be an SDCCFLP")
    if isinstance(value.matrix_size, bool) or not isinstance(value.matrix_size, int):
        raise TypeError("ST 1010 matrix size must be an integer")
    if value.matrix_size < 1:
        raise ValueError("ST 1010 matrix size must be positive")
    control = value.parse_control
    encoded_control = encode_sdcc_parse_control(control)
    slen = control.standard_deviation_length
    clen = control.correlation_coefficient_length
    if not slen and not clen:
        raise ValueError("ST 1010 requires one or both conditional value arrays")
    if control.sparse and not clen:
        raise ValueError("ST 1010 sparse flag is invalid when correlation length is zero")

    if slen:
        if len(value.standard_deviations) != value.matrix_size:
            raise ValueError(
                f"ST 1010 matrix requires {value.matrix_size} standard deviation values"
            )
    elif value.standard_deviations:
        raise ValueError("ST 1010 Slen zero forbids standard deviation values")

    count = value.matrix_size * (value.matrix_size - 1) // 2
    bit_vector = b""
    if clen:
        if len(value.correlation_coefficients) != count:
            raise ValueError(f"ST 1010 matrix requires {count} correlation coefficients")
        present = tuple(item is not None for item in value.correlation_coefficients)
        if control.sparse:
            if not count:
                raise ValueError("ST 1010 sparse mode requires a correlation cell")
            if all(present):
                raise ValueError("ST 1010 sparse Bit Vector cannot contain all ones")
            bit_vector = _encode_bit_vector(present)
        elif not all(present):
            raise ValueError("ST 1010 full matrix cannot omit correlation coefficients")
    elif value.correlation_coefficients:
        raise ValueError("ST 1010 Clen zero forbids correlation coefficients")

    encoded_standards = b"".join(
        _encode_value(
            item,
            slen,
            control.standard_deviation_format,
            coefficient=False,
            standard_deviation_bounds=value.standard_deviation_imap_bounds,
        )
        for item in value.standard_deviations
    )
    encoded_correlations = b"".join(
        _encode_value(
            item,
            clen,
            control.correlation_coefficient_format,
            coefficient=True,
            standard_deviation_bounds=None,
        )
        for item in value.correlation_coefficients
        if item is not None
    )
    return (
        encode_ber_oid(value.matrix_size)
        + encoded_control
        + bit_vector
        + encoded_standards
        + encoded_correlations
    )
