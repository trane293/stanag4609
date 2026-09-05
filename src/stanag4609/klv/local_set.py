"""Lossless Local Set parsing and encoding."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypeAlias

from stanag4609.errors import NeedMoreData, TruncatedData
from stanag4609.klv.ber import (
    decode_ber_length,
    decode_ber_oid,
    encode_ber_length,
    encode_ber_oid,
)
from stanag4609.klv.model import LocalSet, LocalSetItem

Buffer: TypeAlias = bytes | bytearray | memoryview


def parse_local_set(
    data: Buffer,
    *,
    canonical: bool = True,
    max_item_length: int = 64 * 1024 * 1024,
) -> LocalSet:
    """Parse all Local Set items while preserving byte-for-byte encodings."""
    raw = bytes(data)
    items: list[LocalSetItem] = []
    cursor = 0
    while cursor < len(raw):
        start = cursor
        try:
            tag, tag_used = decode_ber_oid(raw, cursor, canonical=canonical)
            cursor += tag_used
            length, length_used = decode_ber_length(
                raw,
                cursor,
                canonical=canonical,
                max_value=max_item_length,
            )
        except NeedMoreData as error:
            raise TruncatedData(f"truncated Local Set item starting at offset {start}") from error
        length_start = cursor
        cursor += length_used
        end = cursor + length
        if end > len(raw):
            raise TruncatedData(
                f"Local Set tag {tag} at offset {start} declares {length} value byte(s), "
                f"only {len(raw) - cursor} remain"
            )
        items.append(
            LocalSetItem(
                tag=tag,
                value=raw[cursor:end],
                tag_octets=raw[start:length_start],
                length_octets=raw[length_start:cursor],
                offset=start,
            )
        )
        cursor = end
    return LocalSet(tuple(items), raw)


def encode_local_set(items: Iterable[LocalSetItem], *, canonical: bool = True) -> bytes:
    """Encode Local Set items canonically or reproduce their preserved bytes."""
    if canonical:
        return b"".join(
            encode_ber_oid(item.tag) + encode_ber_length(len(item.value)) + item.value
            for item in items
        )
    return b"".join(bytes(item) for item in items)
