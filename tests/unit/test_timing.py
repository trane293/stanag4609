from __future__ import annotations

import pytest

from stanag4609.errors import DecodeError
from stanag4609.transport.timing import PTS_MODULUS, PTSTimeline, unwrap_pts


def test_unwrap_pts_uses_the_epoch_nearest_a_reference() -> None:
    assert unwrap_pts(10, reference=PTS_MODULUS - 5) == PTS_MODULUS + 10
    assert unwrap_pts(PTS_MODULUS - 5, reference=10) == -5
    assert unwrap_pts(1234) == 1234


def test_unwrap_pts_rejects_an_exact_half_epoch_ambiguity() -> None:
    with pytest.raises(DecodeError, match="half-epoch"):
        unwrap_pts(0, reference=PTS_MODULUS // 2)


@pytest.mark.parametrize("value", [True, -1, PTS_MODULUS])
def test_unwrap_pts_rejects_invalid_raw_values(value: int) -> None:
    with pytest.raises(ValueError, match="unsigned 33-bit"):
        unwrap_pts(value)


@pytest.mark.parametrize("reference", [True, 1.5])
def test_unwrap_pts_rejects_invalid_references(reference: object) -> None:
    with pytest.raises(TypeError, match="reference"):
        unwrap_pts(0, reference=reference)  # type: ignore[arg-type]


def test_pts_timeline_tracks_wraparound_and_small_reordering() -> None:
    timeline = PTSTimeline()
    assert timeline.reference is None
    assert timeline.observe(PTS_MODULUS - 20) == PTS_MODULUS - 20
    assert timeline.observe(5) == PTS_MODULUS + 5
    assert timeline.reference == PTS_MODULUS + 5

    # A late timestamp maps behind the watermark but does not move it backward.
    assert timeline.observe(PTS_MODULUS - 3) == PTS_MODULUS - 3
    assert timeline.reference == PTS_MODULUS + 5


def test_pts_timeline_can_map_without_observing_and_reset() -> None:
    timeline = PTSTimeline(90_000)
    assert timeline.near(45_000) == 45_000
    assert timeline.reference == 90_000
    timeline.reset()
    assert timeline.reference is None
    assert timeline.near(7) == 7


@pytest.mark.parametrize("reference", [True, 1.5])
def test_pts_timeline_rejects_invalid_initial_reference(reference: object) -> None:
    with pytest.raises(TypeError, match="reference"):
        PTSTimeline(reference)  # type: ignore[arg-type]
