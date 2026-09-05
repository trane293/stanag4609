from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from stanag4609 import WaypointListState
from stanag4609.errors import DecodeError, LimitExceeded
from stanag4609.st0601 import (
    FieldDecodingMode,
    RawFieldValue,
    SpecialValue,
    WaypointList,
    WaypointRecord,
    decode_uas_local_set,
    encode_uas_local_set,
    update_uas_local_set,
)

START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _packet(at: datetime, **values: object) -> bytes:
    fields: dict[int, object] = {2: at, 65: 19}
    fields.update({int(tag): value for tag, value in values.items()})
    return encode_uas_local_set(fields)


def _waypoints(*records: tuple[int, int]) -> WaypointList:
    return WaypointList(
        tuple(WaypointRecord(identifier, order) for identifier, order in records)
    )


def test_distributed_waypoints_merge_and_expire_independently() -> None:
    state = WaypointListState()
    first = state.observe(_packet(START, **{"141": _waypoints((1, 0))}))
    assert tuple(first.records) == (1,)
    assert first.current_ids == (1,)

    second = state.observe(
        _packet(START + timedelta(seconds=10), **{"141": _waypoints((2, 1))})
    )
    assert tuple(second.records) == (1, 2)
    assert second.planned_ids == (2,)

    boundary = state.observe(_packet(START + timedelta(seconds=30)))
    assert tuple(boundary.records) == (1, 2)
    expired = state.observe(_packet(START + timedelta(seconds=31)))
    assert tuple(expired.records) == (2,)
    assert expired.expired_ids == (1,)


def test_waypoint_updates_replace_by_id_and_expose_transient_order_conflicts() -> None:
    state = WaypointListState()
    state.observe(_packet(START, **{"141": _waypoints((1, 0), (2, 1))}))
    transient = state.observe(
        _packet(START + timedelta(seconds=1), **{"141": _waypoints((2, 0))})
    )
    assert transient.records[2].prosecution_order == 0
    assert transient.order_conflicts == {0: (1, 2)}
    assert transient.current_ids == (1, 2)

    resolved = state.observe(
        _packet(START + timedelta(seconds=2), **{"141": _waypoints((1, 2))})
    )
    assert not resolved.order_conflicts
    assert resolved.current_ids == (2,)
    assert resolved.planned_ids == (1,)


def test_cancelled_and_historical_waypoint_views_follow_prosecution_order() -> None:
    snapshot = WaypointListState().observe(
        _packet(
            START,
            **{"141": _waypoints((1, -1), (2, -5), (3, 32767), (4, 32767))},
        )
    )
    assert snapshot.historical_ids == (2, 1)
    assert snapshot.cancelled_ids == (3, 4)
    assert 32767 not in snapshot.order_conflicts


def test_new_historical_waypoint_must_extend_negative_order() -> None:
    state = WaypointListState()
    state.observe(_packet(START, **{"141": _waypoints((1, -5))}))
    invalid = state.observe(
        _packet(START + timedelta(seconds=1), **{"141": _waypoints((2, -2))})
    )
    assert invalid.issues[0].code == "historical_order_not_decreasing"
    assert invalid.issues[0].waypoint_ids == (2,)

    valid = state.observe(
        _packet(START + timedelta(seconds=2), **{"141": _waypoints((3, -6))})
    )
    assert valid.issues == ()


def test_waypoint_zli_and_malformed_update_have_distinct_lifecycle_effects() -> None:
    state = WaypointListState()
    state.observe(_packet(START, **{"141": _waypoints((1, 0))}))
    malformed = update_uas_local_set(
        _packet(START + timedelta(seconds=1)),
        {141: RawFieldValue(b"\x01")},
        field_decoding=FieldDecodingMode.PRESERVE,
    )
    preserved = state.observe(malformed)
    assert tuple(preserved.records) == (1,)
    assert preserved.updated_ids == ()
    assert preserved.field_issues[0].tag == 141

    cleared = state.observe(
        _packet(START + timedelta(seconds=2), **{"141": SpecialValue.UNKNOWN})
    )
    assert not cleared.records
    assert cleared.cleared


def test_waypoint_state_limit_rejection_is_atomic() -> None:
    state = WaypointListState(max_waypoint_records=1)
    state.observe(_packet(START, **{"141": _waypoints((1, 0))}))
    with pytest.raises(LimitExceeded, match="waypoint list"):
        state.observe(
            _packet(START + timedelta(seconds=2), **{"141": _waypoints((2, 1))})
        )
    assert tuple(
        state.observe(_packet(START + timedelta(seconds=1))).records
    ) == (1,)


def test_waypoint_state_configuration_decoded_input_and_reset() -> None:
    state = WaypointListState(refresh_period=timedelta(seconds=10))
    assert state.refresh_period == timedelta(seconds=10)
    assert state.max_waypoint_records == 4096
    assert state.field_decoding is FieldDecodingMode.PRESERVE
    state.observe(decode_uas_local_set(_packet(START)))
    state.reset()
    assert state.observe(_packet(START - timedelta(days=1))).timestamp < START

    with pytest.raises(TypeError, match="timedelta"):
        WaypointListState(refresh_period=30)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive"):
        WaypointListState(refresh_period=timedelta(0))
    with pytest.raises(ValueError, match="30 seconds"):
        WaypointListState(refresh_period=timedelta(seconds=31))
    with pytest.raises(ValueError, match="positive integer"):
        WaypointListState(max_waypoint_records=0)
    with pytest.raises(TypeError, match="FieldDecodingMode"):
        WaypointListState(field_decoding="preserve")  # type: ignore[arg-type]

    ordered = WaypointListState()
    ordered.observe(_packet(START))
    with pytest.raises(DecodeError, match="monotonic"):
        ordered.observe(_packet(START - timedelta(microseconds=1)))
