"""Lossless immutable KLV data models."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class KLVPacket:
    """A Universal KLV packet with its original length encoding."""

    key: bytes
    value: bytes
    length_octets: bytes
    offset: int = 0

    @property
    def raw(self) -> bytes:
        return self.key + self.length_octets + self.value

    def __bytes__(self) -> bytes:
        return self.raw

@dataclass(frozen=True, slots=True)
class LocalSetItem:
    """One Local Set item, preserving the exact tag and length octets."""

    tag: int
    value: bytes
    tag_octets: bytes
    length_octets: bytes
    offset: int

    @property
    def raw(self) -> bytes:
        return self.tag_octets + self.length_octets + self.value

    def __bytes__(self) -> bytes:
        return self.raw


@dataclass(frozen=True, slots=True)
class LocalSet:
    """Ordered Local Set items; duplicate and unknown tags are preserved."""

    items: tuple[LocalSetItem, ...]
    raw: bytes

    def getall(self, tag: int) -> tuple[LocalSetItem, ...]:
        return tuple(item for item in self.items if item.tag == tag)

    def getone(self, tag: int) -> LocalSetItem | None:
        matches = self.getall(tag)
        if not matches:
            return None
        if len(matches) > 1:
            raise ValueError(f"tag {tag} occurs {len(matches)} times")
        return matches[0]

    def __bytes__(self) -> bytes:
        return self.raw
