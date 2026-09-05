"""MPEG 33-bit presentation-timestamp timeline helpers."""

from __future__ import annotations

from stanag4609.errors import DecodeError

PTS_CLOCK_RATE = 90_000
PTS_MODULUS = 1 << 33


def _validate_raw_pts(pts: int) -> None:
    if isinstance(pts, bool) or not isinstance(pts, int) or not 0 <= pts < PTS_MODULUS:
        raise ValueError("PTS must be an unsigned 33-bit integer")


def _validate_reference(reference: int) -> None:
    if isinstance(reference, bool) or not isinstance(reference, int):
        raise TypeError("PTS reference must be an integer")


def unwrap_pts(pts: int, *, reference: int | None = None) -> int:
    """Map a raw 33-bit PTS into the epoch nearest ``reference``.

    The result is an unbounded signed integer measured in 90 kHz ticks.  With
    no reference the first epoch is used.  A point exactly half an epoch away
    is rejected because either adjacent epoch is equally plausible.
    """
    _validate_raw_pts(pts)
    if reference is None:
        return pts
    _validate_reference(reference)
    epoch = reference // PTS_MODULUS
    candidates = tuple((epoch + offset) * PTS_MODULUS + pts for offset in (-1, 0, 1))
    distances = tuple(abs(candidate - reference) for candidate in candidates)
    minimum = min(distances)
    nearest = tuple(
        candidate
        for candidate, distance in zip(candidates, distances, strict=True)
        if distance == minimum
    )
    if len(nearest) != 1:
        raise DecodeError("PTS is exactly half-epoch from its reference")
    return nearest[0]


class PTSTimeline:
    """Track a forward watermark while mapping reordered 33-bit timestamps."""

    __slots__ = ("_reference",)

    def __init__(self, reference: int | None = None) -> None:
        if reference is not None:
            _validate_reference(reference)
        self._reference = reference

    @property
    def reference(self) -> int | None:
        """Largest unwrapped timestamp observed, or ``None`` before observation."""
        return self._reference

    def near(self, pts: int) -> int:
        """Map ``pts`` near the watermark without changing timeline state."""
        return unwrap_pts(pts, reference=self._reference)

    def observe(self, pts: int) -> int:
        """Map ``pts`` and advance the watermark when the result is newer."""
        value = self.near(pts)
        if self._reference is None or value > self._reference:
            self._reference = value
        return value

    def reset(self) -> None:
        """Forget the current epoch and watermark."""
        self._reference = None
