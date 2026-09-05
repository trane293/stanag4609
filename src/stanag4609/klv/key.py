"""SMPTE ST 336 Universal Label parsing and KLV-key validation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from stanag4609.errors import DecodeError

SMPTE_UL_PREFIX = b"\x06\x0e\x2b\x34"
SMPTE_UL_LENGTH = 16


class UniversalLabelCategory(IntEnum):
    """Registry Category Designators assigned by SMPTE ST 336 Table 3."""

    DICTIONARY = 0x01
    GROUP = 0x02
    WRAPPER = 0x03
    LABEL = 0x04
    REGISTERED_PRIVATE = 0x05


@dataclass(frozen=True, slots=True)
class UniversalLabel:
    """A structurally valid 16-byte SMPTE-administered Universal Label."""

    raw: bytes
    category_designator: int
    registry_designator: int
    structure_designator: int
    version_number: int
    item_designator: bytes

    @property
    def category(self) -> UniversalLabelCategory | None:
        """Return the known ST 336 category, or ``None`` for future categories."""
        try:
            return UniversalLabelCategory(self.category_designator)
        except ValueError:
            return None

    @property
    def is_klv_key(self) -> bool:
        """Whether ST 336 permits this UL in the Key field of a KLV triplet."""
        return self.category != UniversalLabelCategory.LABEL

    def __bytes__(self) -> bytes:
        return self.raw


def _validate_single_octet_subidentifier(value: int, *, name: str) -> None:
    if not 0x01 <= value <= 0x7F:
        raise DecodeError(
            f"SMPTE UL {name} must be a single-byte BER OID subidentifier "
            f"from 0x01 through 0x7f, observed 0x{value:02x}"
        )


def _significant_item_designator(octets: bytes) -> bytes:
    if not octets or octets[0] == 0:
        raise DecodeError("SMPTE UL Item Designator must contain at least one subidentifier")

    component_open = False
    terminated = False
    significant_length = len(octets)
    for index, octet in enumerate(octets):
        if terminated:
            if octet != 0:
                raise DecodeError(
                    "SMPTE UL has a non-zero octet after the Item Designator terminator "
                    f"at byte {9 + index}"
                )
            continue

        if not component_open and octet == 0:
            terminated = True
            significant_length = index
            continue
        if not component_open and octet == 0x80:
            raise DecodeError(
                f"SMPTE UL has a non-minimal BER OID subidentifier at byte {9 + index}"
            )
        component_open = bool(octet & 0x80)

    if component_open:
        raise DecodeError("SMPTE UL Item Designator ends with an incomplete BER OID subidentifier")
    return octets[:significant_length]


def parse_universal_label(data: bytes | bytearray | memoryview) -> UniversalLabel:
    """Parse and structurally validate one SMPTE ST 336 Universal Label."""
    raw = bytes(data)
    if len(raw) != SMPTE_UL_LENGTH:
        raise DecodeError(
            f"SMPTE Universal Label must be exactly 16 bytes, observed {len(raw)}"
        )
    if not raw.startswith(SMPTE_UL_PREFIX):
        raise DecodeError(
            f"invalid SMPTE Universal Label prefix: expected {SMPTE_UL_PREFIX.hex(' ')}, "
            f"observed {raw[:4].hex(' ')}"
        )

    field_names = (
        "Category Designator",
        "Registry Designator",
        "Structure Designator",
        "Version Number",
    )
    for value, name in zip(raw[4:8], field_names, strict=True):
        _validate_single_octet_subidentifier(value, name=name)

    item_designator = _significant_item_designator(raw[8:])
    return UniversalLabel(
        raw=raw,
        category_designator=raw[4],
        registry_designator=raw[5],
        structure_designator=raw[6],
        version_number=raw[7],
        item_designator=item_designator,
    )


def validate_klv_key(data: bytes | bytearray | memoryview) -> UniversalLabel:
    """Validate that *data* is a Universal Label permitted as a KLV Key."""
    label = parse_universal_label(data)
    if not label.is_klv_key:
        raise DecodeError("a category 0x04 SMPTE Label cannot be used as a KLV key")
    return label


__all__ = [
    "SMPTE_UL_LENGTH",
    "SMPTE_UL_PREFIX",
    "UniversalLabel",
    "UniversalLabelCategory",
    "parse_universal_label",
    "validate_klv_key",
]
