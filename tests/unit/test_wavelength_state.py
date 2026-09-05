from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from stanag4609 import WavelengthTableState
from stanag4609.errors import DecodeError, LimitExceeded
from stanag4609.st0601 import (
    ActiveWavelengthList,
    SpecialValue,
    WavelengthRecord,
    WavelengthsList,
    encode_uas_local_set,
)

START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _packet(at: datetime, **values: object) -> bytes:
    fields: dict[int, object] = {2: at, 65: 19}
    fields.update({int(tag): value for tag, value in values.items()})
    return encode_uas_local_set(fields)


def _record(identifier: int, name: str) -> WavelengthRecord:
    return WavelengthRecord(identifier, 1_000.0, 2_000.0, name)


def test_distributed_custom_wavelength_records_expire_independently() -> None:
    state = WavelengthTableState()
    state.observe(_packet(START, **{"128": WavelengthsList((_record(21, "A"),))}))
    second = state.observe(
        _packet(
            START + timedelta(seconds=10),
            **{"128": WavelengthsList((_record(22, "B"),))},
        )
    )
    assert tuple(second.custom_records) == (21, 22)

    boundary = state.observe(_packet(START + timedelta(seconds=30)))
    assert tuple(boundary.custom_records) == (21, 22)
    expired = state.observe(_packet(START + timedelta(seconds=31)))
    assert tuple(expired.custom_records) == (22,)
    assert expired.expired_ids == (21,)


def test_active_wavelengths_accept_predefined_and_current_custom_ids() -> None:
    state = WavelengthTableState()
    custom = WavelengthsList((_record(21, "CUSTOM"),))
    snapshot = state.observe(
        _packet(
            START,
            **{"121": ActiveWavelengthList((1, 3, 21)), "128": custom},
        )
    )
    assert snapshot.active_ids == (1, 3, 21)
    assert snapshot.known_ids == (0, 1, 2, 3, 4, 5, 6, 21)
    assert snapshot.issues == ()


def test_active_wavelength_reference_is_checked_after_table_expiry() -> None:
    state = WavelengthTableState()
    state.observe(_packet(START, **{"128": WavelengthsList((_record(21, "A"),))}))
    snapshot = state.observe(
        _packet(
            START + timedelta(seconds=31),
            **{"121": ActiveWavelengthList((21,))},
        )
    )
    assert snapshot.expired_ids == (21,)
    assert snapshot.issues[0].code == "undefined_active_id"
    assert snapshot.issues[0].wavelength_ids == (21,)


def test_reserved_active_wavelength_id_is_diagnosed() -> None:
    snapshot = WavelengthTableState().observe(
        _packet(START, **{"121": ActiveWavelengthList((7,))})
    )
    assert snapshot.issues[0].code == "reserved_active_id"
    assert snapshot.issues[0].wavelength_ids == (7,)


def test_custom_wavelength_names_remain_unique_across_distributed_packets() -> None:
    state = WavelengthTableState()
    state.observe(_packet(START, **{"128": WavelengthsList((_record(21, "SAME"),))}))
    snapshot = state.observe(
        _packet(
            START + timedelta(seconds=1),
            **{"128": WavelengthsList((_record(22, "SAME"),))},
        )
    )
    assert snapshot.issues[0].code == "duplicate_custom_name"
    assert snapshot.issues[0].wavelength_ids == (21, 22)


def test_wavelength_zli_and_refresh_expiry_clear_receiver_state() -> None:
    state = WavelengthTableState()
    state.observe(
        _packet(
            START,
            **{
                "121": ActiveWavelengthList((21,)),
                "128": WavelengthsList((_record(21, "A"),)),
            },
        )
    )
    cleared = state.observe(
        _packet(START + timedelta(seconds=1), **{"128": SpecialValue.UNKNOWN})
    )
    assert not cleared.custom_records
    assert cleared.cleared_custom_table

    expired = state.observe(_packet(START + timedelta(seconds=31)))
    assert expired.active_ids is None
    assert expired.active_expired

    active_state = WavelengthTableState()
    active_state.observe(_packet(START, **{"121": ActiveWavelengthList((1,))}))
    active_cleared = active_state.observe(
        _packet(START + timedelta(seconds=1), **{"121": SpecialValue.UNKNOWN})
    )
    assert active_cleared.active_ids is None
    assert active_cleared.active_cleared


def test_wavelength_table_is_bounded_and_rejects_time_reversal() -> None:
    state = WavelengthTableState(max_custom_records=1)
    state.observe(_packet(START, **{"128": WavelengthsList((_record(21, "A"),))}))
    with pytest.raises(LimitExceeded, match="custom wavelength"):
        state.observe(
            _packet(
                START + timedelta(seconds=2),
                **{"128": WavelengthsList((_record(22, "B"),))},
            )
        )
    assert tuple(state.observe(_packet(START + timedelta(seconds=1))).custom_records) == (21,)

    with pytest.raises(DecodeError, match="monotonic"):
        state.observe(_packet(START))


def test_wavelength_state_rejects_invalid_configuration() -> None:
    state = WavelengthTableState()
    assert state.refresh_period == timedelta(seconds=30)
    assert state.max_custom_records == 4096
    assert state.field_decoding.value == "preserve"
    state.observe(_packet(START))
    state.reset()
    assert state.observe(_packet(START - timedelta(days=1))).timestamp < START

    with pytest.raises(TypeError, match="timedelta"):
        WavelengthTableState(refresh_period=30)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive"):
        WavelengthTableState(refresh_period=timedelta(0))
    with pytest.raises(ValueError, match="30 seconds"):
        WavelengthTableState(refresh_period=timedelta(seconds=31))
    with pytest.raises(ValueError, match="positive integer"):
        WavelengthTableState(max_custom_records=0)
    with pytest.raises(TypeError, match="FieldDecodingMode"):
        WavelengthTableState(field_decoding="preserve")  # type: ignore[arg-type]
