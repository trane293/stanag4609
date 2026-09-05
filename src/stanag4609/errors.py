"""Library exception hierarchy."""

from __future__ import annotations


class Stanag4609Error(Exception):
    """Base class for all package-defined exceptions."""


class DecodeError(Stanag4609Error, ValueError):
    """Input is complete enough to inspect but is not valid."""


class NeedMoreData(Stanag4609Error):
    """A bounded decoder needs additional bytes."""

    def __init__(self, *, offset: int, needed: int, available: int) -> None:
        self.offset = offset
        self.needed = needed
        self.available = available
        super().__init__(
            f"need {needed} byte(s) at offset {offset}, only {available} available"
        )


class TruncatedData(DecodeError):
    """The input ended in the middle of a structure."""


class LimitExceeded(DecodeError):
    """A declared size exceeds a configured safety limit."""


class ChecksumError(DecodeError):
    """A checksum is absent, misplaced, malformed, or incorrect."""
