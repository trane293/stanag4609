from __future__ import annotations

import pytest

from stanag4609.errors import DecodeError, TruncatedData
from stanag4609.klv.local_set import encode_local_set, parse_local_set


def test_local_set_preserves_unknown_duplicate_and_noncanonical_items() -> None:
    raw = bytes.fromhex("01 01 AA 81 00 81 01 BB")
    parsed = parse_local_set(raw, canonical=False)
    assert [item.tag for item in parsed.items] == [1, 128]
    assert parsed.items[1].tag_octets == b"\x81\x00"
    assert bytes(parsed) == raw
    assert encode_local_set(parsed.items, canonical=False) == raw
    assert encode_local_set(parsed.items, canonical=True) == bytes.fromhex("01 01 AA 81 00 01 BB")


def test_local_set_getall_and_getone_handle_duplicates() -> None:
    parsed = parse_local_set(bytes.fromhex("02 01 AA 02 01 BB"))
    assert [item.value for item in parsed.getall(2)] == [b"\xaa", b"\xbb"]
    with pytest.raises(ValueError):
        parsed.getone(2)
    assert parsed.getone(99) is None


@pytest.mark.parametrize("raw", [b"\x81", b"\x01", b"\x01\x02\xaa"])
def test_local_set_rejects_truncation(raw: bytes) -> None:
    with pytest.raises(TruncatedData):
        parse_local_set(raw)


def test_local_set_rejects_nonminimal_ber_by_default() -> None:
    with pytest.raises(DecodeError):
        parse_local_set(bytes.fromhex("01 81 01 AA"))
