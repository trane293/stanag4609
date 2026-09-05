from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from stanag4609 import PayloadTableState
from stanag4609.errors import DecodeError, LimitExceeded
from stanag4609.st0601 import (
    ActivePayloads,
    PayloadList,
    PayloadRecord,
    SpecialValue,
    encode_uas_local_set,
)

START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _packet(at: datetime, **values: object) -> bytes:
    fields: dict[int, object] = {2: at, 65: 19}
    fields.update({int(tag): value for tag, value in values.items()})
    return encode_uas_local_set(fields)


def _payloads(count: int, *ids: int) -> PayloadList:
    return PayloadList(
        count,
        tuple(
            PayloadRecord(identifier, identifier % 5, f"Payload {identifier}")
            for identifier in ids
        ),
    )


def test_distributed_payload_records_merge_and_expire_independently() -> None:
    state = PayloadTableState()
    first = state.observe(_packet(START, **{"138": _payloads(3, 0)}))
    assert not first.complete
    assert first.missing_ids == (1, 2)
    second = state.observe(
        _packet(START + timedelta(seconds=10), **{"138": _payloads(3, 1, 2)})
    )
    assert second.complete
    assert second.missing_ids == ()
    assert tuple(second.records) == (0, 1, 2)

    boundary = state.observe(_packet(START + timedelta(seconds=30)))
    assert tuple(boundary.records) == (0, 1, 2)
    expired = state.observe(_packet(START + timedelta(seconds=31)))
    assert tuple(expired.records) == (1, 2)
    assert expired.expired_ids == (0,)
    assert not expired.complete

    table_expired = state.observe(_packet(START + timedelta(seconds=41)))
    assert table_expired.total_count is None
    assert table_expired.table_expired


def test_active_payloads_resolve_only_current_payload_ids() -> None:
    state = PayloadTableState()
    current = state.observe(
        _packet(
            START,
            **{"138": _payloads(2, 0, 1), "139": ActivePayloads(frozenset({0, 1}))},
        )
    )
    assert current.active_ids == frozenset({0, 1})
    assert current.issues == ()

    invalid = state.observe(
        _packet(
            START + timedelta(seconds=1),
            **{"139": ActivePayloads(frozenset({0, 2}))},
        )
    )
    assert invalid.issues[0].code == "undefined_active_payload"
    assert invalid.issues[0].payload_ids == (2,)


def test_payload_reference_is_checked_after_individual_record_expiry() -> None:
    state = PayloadTableState()
    state.observe(_packet(START, **{"138": _payloads(1, 0)}))
    snapshot = state.observe(
        _packet(
            START + timedelta(seconds=31),
            **{"139": ActivePayloads(frozenset({0}))},
        )
    )
    assert snapshot.expired_ids == (0,)
    assert snapshot.issues[0].payload_ids == (0,)


def test_changed_payload_count_starts_a_new_table_generation() -> None:
    state = PayloadTableState()
    state.observe(_packet(START, **{"138": _payloads(2, 0, 1)}))
    changed = state.observe(
        _packet(START + timedelta(seconds=1), **{"138": _payloads(1, 0)})
    )
    assert changed.total_count == 1
    assert tuple(changed.records) == (0,)
    assert changed.table_restarted
    assert changed.issues[0].code == "payload_count_changed"


def test_payload_and_active_zli_clear_state_immediately() -> None:
    state = PayloadTableState()
    state.observe(
        _packet(
            START,
            **{"138": _payloads(1, 0), "139": ActivePayloads(frozenset({0}))},
        )
    )
    snapshot = state.observe(
        _packet(
            START + timedelta(seconds=1),
            **{"138": SpecialValue.UNKNOWN, "139": SpecialValue.UNKNOWN},
        )
    )
    assert snapshot.total_count is None
    assert not snapshot.records
    assert snapshot.active_ids is None
    assert snapshot.table_cleared
    assert snapshot.active_cleared


def test_payload_active_selection_expires_after_inclusive_window() -> None:
    state = PayloadTableState()
    state.observe(_packet(START, **{"139": ActivePayloads(frozenset())}))
    assert not state.observe(_packet(START + timedelta(seconds=30))).active_expired
    expired = state.observe(_packet(START + timedelta(seconds=31)))
    assert expired.active_ids is None
    assert expired.active_expired


def test_payload_table_limit_rejection_is_atomic() -> None:
    state = PayloadTableState(max_payload_records=1)
    state.observe(_packet(START, **{"138": _payloads(1, 0)}))
    with pytest.raises(LimitExceeded, match="Payload Count"):
        state.observe(
            _packet(START + timedelta(seconds=2), **{"138": _payloads(2, 0)})
        )
    assert state.observe(_packet(START + timedelta(seconds=1))).total_count == 1


def test_payload_state_configuration_and_reset() -> None:
    state = PayloadTableState()
    assert state.refresh_period == timedelta(seconds=30)
    assert state.max_payload_records == 4096
    state.observe(_packet(START))
    state.reset()
    assert state.observe(_packet(START - timedelta(days=1))).timestamp < START

    with pytest.raises(TypeError, match="timedelta"):
        PayloadTableState(refresh_period=30)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive"):
        PayloadTableState(refresh_period=timedelta(0))
    with pytest.raises(ValueError, match="30 seconds"):
        PayloadTableState(refresh_period=timedelta(seconds=31))
    with pytest.raises(ValueError, match="positive integer"):
        PayloadTableState(max_payload_records=0)
    with pytest.raises(TypeError, match="FieldDecodingMode"):
        PayloadTableState(field_decoding="preserve")  # type: ignore[arg-type]

    with pytest.raises(LimitExceeded, match="Active Payload ID"):
        PayloadTableState(max_payload_records=1).observe(
            _packet(START, **{"139": ActivePayloads(frozenset({1}))})
        )

    ordered = PayloadTableState()
    ordered.observe(_packet(START))
    with pytest.raises(DecodeError, match="monotonic"):
        ordered.observe(_packet(START - timedelta(microseconds=1)))
