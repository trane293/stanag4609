"""Checksums used by MISB KLV structures."""

from __future__ import annotations

from typing import TypeAlias

Buffer: TypeAlias = bytes | bytearray | memoryview


def running_sum_16(data: Buffer) -> int:
    """Return the big-endian 16-bit word sum, modulo 2**16.

    For an ST 0601 packet, pass every byte from the Universal Key through the
    Checksum item's length octet, excluding the two checksum value octets.
    """
    raw = memoryview(data).cast("B")
    total = 0
    paired_end = len(raw) - (len(raw) % 2)
    for index in range(0, paired_end, 2):
        total += (raw[index] << 8) | raw[index + 1]
    if paired_end < len(raw):
        total += raw[-1] << 8
    return total & 0xFFFF


def mpeg2_crc32(data: Buffer) -> int:
    """Return the non-reflected MPEG-2 CRC-32 remainder."""
    crc = 0xFFFFFFFF
    for octet in memoryview(data).cast("B"):
        crc ^= octet << 24
        for _ in range(8):
            crc = (
                ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
                if crc & 0x80000000
                else (crc << 1) & 0xFFFFFFFF
            )
    return crc


def crc16_ccitt(data: Buffer) -> int:
    """Return the MISB CRC-16-CCITT remainder (polynomial 0x1021).

    MISB's KLV checksum profile uses the augmented CCITT initial remainder
    ``0x1D0F``. Appending the big-endian result makes the remainder zero.
    """
    crc = 0x1D0F
    for octet in memoryview(data).cast("B"):
        crc ^= octet << 8
        for _ in range(8):
            crc = (
                ((crc << 1) ^ 0x1021) & 0xFFFF
                if crc & 0x8000
                else (crc << 1) & 0xFFFF
            )
    return crc
