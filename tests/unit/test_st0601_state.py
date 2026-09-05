from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from stanag4609 import ControlCommandState, ReportOnChangeState
from stanag4609.errors import DecodeError, LimitExceeded
from stanag4609.klv.ber import encode_ber_length, encode_ber_oid
from stanag4609.klv.model import KLVPacket
from stanag4609.st0601 import (
    ST0601_KEY,
    ControlCommand,
    ControlCommandVerificationList,
    FieldDecodingMode,
    IMAPFieldValue,
    RawFieldValue,
    SpecialValue,
    ST0601Semantic,
    decode_uas_local_set,
    encode_uas_local_set,
    update_uas_local_set,
)

START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _packet(at: datetime, **values: object) -> bytes:
    fields: dict[int, object] = {2: at, 65: 19}
    fields.update({int(tag): value for tag, value in values.items()})
    return encode_uas_local_set(fields)


def test_report_on_change_carries_values_through_inclusive_refresh_period() -> None:
    state = ReportOnChangeState()
    first = state.observe(_packet(START, **{"3": "MISSION", "5": 180.0}))
    assert first.value(3) == "MISSION"
    assert first.updated_tags == (1, 2, 3, 5, 65)

    boundary = state.observe(_packet(START + timedelta(seconds=30)))
    assert boundary.value(3) == "MISSION"
    assert boundary.expired_tags == ()

    expired = state.observe(_packet(START + timedelta(seconds=31)))
    assert expired.value(3) is None
    assert expired.value(5) is None
    assert expired.expired_tags == (3, 5)


def test_report_on_change_snapshot_exposes_preferred_effective_representation() -> None:
    snapshot = ReportOnChangeState().observe(
        _packet(
            START,
            **{
                "15": 100.0,
                "75": 200.0,
                "104": IMAPFieldValue(300.0, 3),
            },
        )
    )

    resolved = snapshot.preferred_field(ST0601Semantic.SENSOR_HEIGHT)

    assert resolved is not None
    assert resolved.tag == 104
    assert {field.definition.tag for field in snapshot.effective_fields}.isdisjoint({15, 75})


def test_report_on_change_zli_clears_single_use_value_immediately() -> None:
    state = ReportOnChangeState()
    state.observe(_packet(START, **{"3": "MISSION"}))
    update = state.observe(
        _packet(START + timedelta(seconds=1), **{"3": SpecialValue.UNKNOWN})
    )
    assert update.value(3) is None
    assert update.cleared_tags == (3,)
    assert 3 not in state.last_seen


def test_late_refresh_reports_expiry_and_reestablishes_current_value() -> None:
    state = ReportOnChangeState()
    state.observe(_packet(START, **{"3": "MISSION-A"}))
    update = state.observe(
        _packet(START + timedelta(seconds=31), **{"3": "MISSION-B"})
    )
    assert update.expired_tags == (3,)
    assert 3 in update.updated_tags
    assert update.value(3) == "MISSION-B"


def test_malformed_update_does_not_replace_last_valid_value() -> None:
    state = ReportOnChangeState()
    first = state.observe(_packet(START, **{"5": 90.0}))
    malformed = update_uas_local_set(
        _packet(START + timedelta(seconds=1)),
        {5: RawFieldValue(b"\x00")},
        field_decoding=FieldDecodingMode.PRESERVE,
    )
    update = state.observe(malformed)
    assert update.value(5) == first.value(5)
    assert update.updated_tags == (1, 2, 65)
    assert update.issues[0].tag == 5


def test_report_on_change_accepts_decoded_packets_and_can_reset() -> None:
    state = ReportOnChangeState(refresh_period=timedelta(seconds=10))
    assert state.refresh_period == timedelta(seconds=10)
    assert state.field_decoding is FieldDecodingMode.PRESERVE
    assert state.max_items_per_tag == 1024
    state.observe(decode_uas_local_set(_packet(START, **{"3": "MISSION"})))
    assert state.last_seen[3] == START
    state.reset()
    assert not state.last_seen
    assert state.observe(_packet(START - timedelta(days=1))).timestamp < START


def test_report_on_change_rejects_invalid_configuration_and_time_order() -> None:
    with pytest.raises(TypeError, match="timedelta"):
        ReportOnChangeState(refresh_period=30)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive"):
        ReportOnChangeState(refresh_period=timedelta(0))
    with pytest.raises(ValueError, match="30 seconds"):
        ReportOnChangeState(refresh_period=timedelta(seconds=31))
    with pytest.raises(TypeError, match="FieldDecodingMode"):
        ReportOnChangeState(field_decoding="preserve")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive integer"):
        ReportOnChangeState(max_items_per_tag=0)

    state = ReportOnChangeState()
    state.observe(_packet(START))
    with pytest.raises(DecodeError, match="monotonic"):
        state.observe(_packet(START - timedelta(microseconds=1)))


def test_report_on_change_bounds_multiple_item_state() -> None:
    packet = _packet(
        START + timedelta(seconds=2),
        **{
            "115": (
                ControlCommand(1, "Fly"),
                ControlCommand(2, "Track"),
            )
        },
    )
    state = ReportOnChangeState(max_items_per_tag=1)
    state.observe(_packet(START, **{"3": "MISSION"}))
    with pytest.raises(LimitExceeded, match="tag 115"):
        state.observe(packet)
    assert state.observe(_packet(START + timedelta(seconds=1))).value(3) == "MISSION"


def test_multi_use_st0601_item_cannot_be_a_zli() -> None:
    with pytest.raises(ValueError, match=r"multi-use.*zero-length"):
        _packet(START, **{"115": SpecialValue.UNKNOWN})

    base = decode_uas_local_set(_packet(START))
    local_value = (
        b"".join(bytes(item) for item in base.local_set.items if item.tag != 1)
        + encode_ber_oid(115)
        + encode_ber_length(0)
        + b"\x01\x02\x00\x00"
    )
    packet = KLVPacket(ST0601_KEY, local_value, encode_ber_length(len(local_value)))
    with pytest.raises(DecodeError, match=r"multi-use.*zero-length"):
        decode_uas_local_set(packet, verify_checksum=False)


def test_control_command_state_tracks_repeat_and_acknowledgement_lifecycle() -> None:
    state = ControlCommandState()

    issued = state.observe(_packet(START, **{"115": ControlCommand(5, "Fly")}))
    assert issued.outstanding_commands == {5: ControlCommand(5, "Fly")}
    assert issued.issued_at == {5: START}
    assert issued.updated_ids == (5,)
    assert issued.newly_acknowledged_ids == ()
    assert issued.issues == ()

    repeated = state.observe(
        _packet(
            START + timedelta(seconds=1),
            **{"115": ControlCommand(5, "Fly", START)},
        )
    )
    assert repeated.outstanding_commands == {5: ControlCommand(5, "Fly")}
    assert repeated.issued_at == {5: START}
    assert repeated.issues == ()

    acknowledged = state.observe(
        _packet(
            START + timedelta(seconds=2),
            **{"116": ControlCommandVerificationList((5,))},
        )
    )
    assert acknowledged.outstanding_commands == {}
    assert acknowledged.acknowledged_ids == (5,)
    assert acknowledged.newly_acknowledged_ids == (5,)

    repeated_after_ack = state.observe(
        _packet(
            START + timedelta(seconds=3),
            **{"115": ControlCommand(5, "Fly", START)},
        )
    )
    assert repeated_after_ack.outstanding_commands == {}
    assert [issue.code for issue in repeated_after_ack.issues] == [
        "command_after_acknowledgement"
    ]


def test_control_command_state_validates_identity_time_and_increasing_new_ids() -> None:
    state = ControlCommandState()
    state.observe(_packet(START, **{"115": ControlCommand(5, "Fly")}))

    changed = state.observe(
        _packet(
            START + timedelta(seconds=1),
            **{
                "115": (
                    ControlCommand(5, "Land"),
                    ControlCommand(4, "Orbit"),
                )
            },
        )
    )
    assert {issue.code for issue in changed.issues} == {
        "command_changed",
        "non_increasing_command_id",
    }
    assert changed.outstanding_commands[5].command == "Fly"

    changed_time = state.observe(
        _packet(
            START + timedelta(seconds=2),
            **{"115": ControlCommand(5, "Fly", START + timedelta(seconds=1))},
        )
    )
    assert [issue.code for issue in changed_time.issues] == ["command_time_changed"]


def test_control_command_state_rejects_duplicate_and_unknown_acknowledgements() -> None:
    state = ControlCommandState()
    state.observe(_packet(START, **{"115": ControlCommand(3, "Track")}))

    snapshot = state.observe(
        _packet(
            START + timedelta(seconds=1),
            **{"116": ControlCommandVerificationList((3, 3, 7))},
        )
    )

    assert snapshot.acknowledged_ids == (3,)
    assert snapshot.newly_acknowledged_ids == (3,)
    assert {issue.code for issue in snapshot.issues} == {
        "duplicate_acknowledgement",
        "unknown_acknowledgement",
    }
    assert next(
        issue for issue in snapshot.issues if issue.code == "unknown_acknowledgement"
    ).command_ids == (7,)


def test_control_command_state_rejects_duplicate_id_in_one_packet() -> None:
    snapshot = ControlCommandState().observe(
        _packet(
            START,
            **{
                "115": (
                    ControlCommand(1, "Fly"),
                    ControlCommand(1, "Fly"),
                )
            },
        )
    )

    assert snapshot.outstanding_commands == {1: ControlCommand(1, "Fly")}
    assert [issue.code for issue in snapshot.issues] == ["duplicate_command_in_packet"]


def test_control_command_state_is_bounded_atomic_resettable_and_monotonic() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        ControlCommandState(max_command_history=0)
    with pytest.raises(TypeError, match="FieldDecodingMode"):
        ControlCommandState(field_decoding="strict")  # type: ignore[arg-type]

    state = ControlCommandState(max_command_history=1)
    state.observe(_packet(START, **{"115": ControlCommand(1, "Fly")}))
    with pytest.raises(LimitExceeded, match="command history"):
        state.observe(
            _packet(
                START + timedelta(seconds=1),
                **{"115": ControlCommand(2, "Land")},
            )
        )
    assert state.observe(
        _packet(START + timedelta(microseconds=1))
    ).outstanding_commands == {1: ControlCommand(1, "Fly")}

    with pytest.raises(DecodeError, match="monotonic"):
        state.observe(_packet(START - timedelta(microseconds=1)))

    state.reset()
    reset = state.observe(
        _packet(START - timedelta(days=1), **{"115": ControlCommand(1, "Again")})
    )
    assert reset.issued_at == {1: START - timedelta(days=1)}
