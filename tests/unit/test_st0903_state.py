from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from stanag4609.errors import LimitExceeded
from stanag4609.st0903 import (
    DetectionStatus,
    VTargetData,
    decode_vmti_local_set,
    encode_vmti_local_set,
)
from stanag4609.st0903_state import VMTILifecycleState

_START = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _vmti(*targets: tuple[int, DetectionStatus | None], offset: int = 0):
    encoded_targets = tuple(
        VTargetData(
            target_id,
            (
                {1: 1, 23: status}
                if status in {DetectionStatus.ACTIVE_MOVING, DetectionStatus.ACTIVE_STOPPED}
                else {23: status}
                if status is not None
                else {5: 80}
            ),
        )
        for target_id, status in targets
    )
    return decode_vmti_local_set(
        encode_vmti_local_set(
            {2: _START + timedelta(seconds=offset), 4: 6, 8: 1},
            targets=encoded_targets,
            standalone=False,
        ),
        standalone=False,
    )


def test_vmti_lifecycle_tracks_valid_states_without_dropping_unreported_targets() -> None:
    state = VMTILifecycleState(assume_stream_start=True)
    for offset, status in enumerate(
        (
            DetectionStatus.ACTIVE_MOVING,
            DetectionStatus.ACTIVE_COASTING,
            DetectionStatus.ACTIVE_STOPPED,
            DetectionStatus.INACTIVE,
            DetectionStatus.ACTIVE_MOVING,
            DetectionStatus.DROPPED,
        )
    ):
        snapshot = state.observe(_vmti((7, status), offset=offset))
        assert snapshot.issues == ()

    empty = state.observe(_vmti(offset=6))
    assert empty.current_target_ids == ()
    assert empty.targets[7].observations == 6
    assert empty.targets[7].first_timestamp == _START
    assert empty.targets[7].last_timestamp == _START + timedelta(seconds=5)
    assert empty.targets[7].retired
    assert empty.retired_target_ids == frozenset({7})


def test_vmti_lifecycle_reports_initial_transition_and_reuse_policy() -> None:
    state = VMTILifecycleState(assume_stream_start=True)
    initial = state.observe(_vmti((8, DetectionStatus.INACTIVE)))
    assert [issue.code for issue in initial.issues] == ["invalid_initial_status"]

    transition = state.observe(_vmti((8, DetectionStatus.ACTIVE_COASTING)))
    assert [issue.code for issue in transition.issues] == ["invalid_status_transition"]
    assert transition.issues[0].previous_status is DetectionStatus.INACTIVE

    state.observe(_vmti((8, DetectionStatus.DROPPED)))
    reused = state.observe(_vmti((8, DetectionStatus.ACTIVE_MOVING)))
    assert [issue.code for issue in reused.issues] == ["retired_target_id_reused"]
    assert reused.targets[8].retired


def test_vmti_lifecycle_live_join_allows_nonactive_first_observation() -> None:
    state = VMTILifecycleState()
    snapshot = state.observe(_vmti((9, DetectionStatus.INACTIVE)))
    assert snapshot.issues == ()


def test_vmti_lifecycle_reports_missing_status_and_is_bounded_atomically() -> None:
    state = VMTILifecycleState(max_target_ids=1)
    missing = state.observe(_vmti((1, None)))
    assert missing.issues[0].code == "missing_detection_status"
    assert missing.targets[1].status is None

    with pytest.raises(LimitExceeded, match="1 target identifiers"):
        state.observe(_vmti((1, DetectionStatus.INACTIVE), (2, DetectionStatus.INACTIVE)))
    unchanged = state.snapshot()
    assert unchanged.generation == 1
    assert tuple(unchanged.targets) == (1,)

    state.reset()
    assert state.snapshot().generation == 0
    assert not state.snapshot().targets


def test_vmti_lifecycle_validates_configuration_and_input() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        VMTILifecycleState(max_target_ids=0)
    with pytest.raises(TypeError, match="boolean"):
        VMTILifecycleState(assume_stream_start=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="VMTILocalSet"):
        VMTILifecycleState().observe(object())  # type: ignore[arg-type]
