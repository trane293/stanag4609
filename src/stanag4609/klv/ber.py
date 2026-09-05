"""Strict definite-form BER lengths and BER-OID unsigned integers."""

from __future__ import annotations

from typing import TypeAlias

from stanag4609.errors import DecodeError, LimitExceeded, NeedMoreData

Buffer: TypeAlias = bytes | bytearray | memoryview


def _view(data: Buffer) -> memoryview:
    return memoryview(data).cast("B")


def encode_ber_length(value: int) -> bytes:
    """Encode a non-negative length using minimal definite-form BER."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("BER length must be an integer")
    if value < 0:
        raise ValueError("BER length cannot be negative")
    if value < 0x80:
        return bytes((value,))
    width = (value.bit_length() + 7) // 8
    if width > 0x7E:
        raise ValueError("BER length needs more than 126 value octets")
    return bytes((0x80 | width,)) + value.to_bytes(width, "big")


def decode_ber_length(
    data: Buffer,
    offset: int = 0,
    *,
    canonical: bool = True,
    max_octets: int = 8,
    max_value: int | None = None,
) -> tuple[int, int]:
    """Decode one definite BER length, returning ``(value, octets_used)``."""
    if offset < 0:
        raise ValueError("offset cannot be negative")
    if max_octets < 1 or max_octets > 0x7E:
        raise ValueError("max_octets must be between 1 and 126")
    raw = _view(data)
    available = max(0, len(raw) - offset)
    if available < 1:
        raise NeedMoreData(offset=offset, needed=1, available=available)

    first = raw[offset]
    if first < 0x80:
        value = int(first)
        used = 1
    else:
        width = first & 0x7F
        if width == 0:
            raise DecodeError(f"indefinite BER length is forbidden at offset {offset}")
        if width > max_octets:
            raise LimitExceeded(
                f"BER length uses {width} value octets; configured maximum is {max_octets}"
            )
        if available < width + 1:
            raise NeedMoreData(offset=offset, needed=width + 1, available=available)
        value_bytes = raw[offset + 1 : offset + 1 + width]
        if canonical and value_bytes[0] == 0:
            raise DecodeError(f"non-minimal BER length at offset {offset}")
        value = int.from_bytes(value_bytes, "big")
        if canonical and value < 0x80:
            raise DecodeError(f"long-form BER used for short length at offset {offset}")
        used = width + 1

    if max_value is not None and value > max_value:
        raise LimitExceeded(f"BER length {value} exceeds configured maximum {max_value}")
    return value, used


def encode_ber_oid(value: int) -> bytes:
    """Encode a non-negative integer using minimal base-128 BER-OID form."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("BER-OID value must be an integer")
    if value < 0:
        raise ValueError("BER-OID value cannot be negative")
    if value == 0:
        return b"\x00"
    groups = bytearray()
    while value:
        groups.append(value & 0x7F)
        value >>= 7
    groups.reverse()
    for index in range(len(groups) - 1):
        groups[index] |= 0x80
    return bytes(groups)


def decode_ber_oid(
    data: Buffer,
    offset: int = 0,
    *,
    canonical: bool = True,
    max_octets: int = 10,
    max_value: int | None = None,
) -> tuple[int, int]:
    """Decode one BER-OID integer, returning ``(value, octets_used)``."""
    if offset < 0:
        raise ValueError("offset cannot be negative")
    if max_octets < 1:
        raise ValueError("max_octets must be positive")
    raw = _view(data)
    available = max(0, len(raw) - offset)
    if available < 1:
        raise NeedMoreData(offset=offset, needed=1, available=available)

    value = 0
    for index in range(max_octets):
        position = offset + index
        if position >= len(raw):
            raise NeedMoreData(offset=offset, needed=index + 1, available=available)
        octet = raw[position]
        if canonical and index == 0 and octet == 0x80:
            raise DecodeError(f"non-minimal BER-OID value at offset {offset}")
        value = (value << 7) | (octet & 0x7F)
        if max_value is not None and value > max_value:
            raise LimitExceeded(
                f"BER-OID value exceeds configured maximum {max_value} at offset {offset}"
            )
        if not octet & 0x80:
            return value, index + 1
    raise LimitExceeded(
        f"BER-OID value at offset {offset} exceeds {max_octets} octets or is unterminated"
    )
