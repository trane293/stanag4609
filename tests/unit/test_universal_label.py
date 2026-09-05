from __future__ import annotations

import pytest

from stanag4609.errors import DecodeError
from stanag4609.klv.key import (
    SMPTE_UL_PREFIX,
    UniversalLabelCategory,
    parse_universal_label,
    validate_klv_key,
)


def test_parse_universal_label_exposes_st336_fields() -> None:
    raw = bytes.fromhex("06 0E 2B 34 02 0B 01 01 0E 01 03 01 01 00 00 00")

    label = parse_universal_label(raw)

    assert label.raw == raw
    assert label.category == UniversalLabelCategory.GROUP
    assert label.category_designator == 0x02
    assert label.registry_designator == 0x0B
    assert label.structure_designator == 0x01
    assert label.version_number == 0x01
    assert label.item_designator == bytes.fromhex("0E 01 03 01 01")
    assert label.is_klv_key
    assert bytes(label) == raw


def test_unknown_category_is_preserved_for_forward_compatible_processing() -> None:
    raw = bytes.fromhex("06 0E 2B 34 06 01 01 01 0E 01 01 00 00 00 00 00")

    label = validate_klv_key(raw)

    assert label.category is None
    assert label.category_designator == 0x06
    assert label.is_klv_key


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"short", "exactly 16 bytes"),
        (bytes.fromhex("06 0E 2B 35 02 0B 01 01 0E 01 03 01 01 00 00 00"), "prefix"),
        (
            bytes.fromhex("06 0E 2B 34 02 0B 01 01 0E 01 00 01 00 00 00 00"),
            "non-zero octet after the Item Designator terminator",
        ),
        (
            bytes.fromhex("06 0E 2B 34 02 0B 01 01 0E 01 03 01 01 01 01 81"),
            "incomplete BER OID",
        ),
        (
            bytes.fromhex("06 0E 2B 34 02 0B 01 01 80 01 03 01 01 00 00 00"),
            "non-minimal BER OID",
        ),
    ],
)
def test_parse_universal_label_rejects_malformed_labels(raw: bytes, message: str) -> None:
    with pytest.raises(DecodeError, match=message):
        parse_universal_label(raw)


def test_smpte_label_is_valid_ul_but_not_a_klv_key() -> None:
    raw = SMPTE_UL_PREFIX + bytes.fromhex("04 01 01 01 0E 01 01 01 01 00 00 00")
    label = parse_universal_label(raw)

    assert label.category == UniversalLabelCategory.LABEL
    assert not label.is_klv_key
    with pytest.raises(DecodeError, match=r"SMPTE Label.*cannot be used as a KLV key"):
        validate_klv_key(raw)
