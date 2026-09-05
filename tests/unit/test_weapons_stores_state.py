from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from stanag4609 import WeaponsStoresState
from stanag4609.errors import DecodeError, LimitExceeded
from stanag4609.st0601 import (
    FieldDecodingMode,
    RawFieldValue,
    SpecialValue,
    WeaponsStores,
    WeaponStatus,
    WeaponStore,
    decode_uas_local_set,
    encode_uas_local_set,
    update_uas_local_set,
)

START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _packet(at: datetime, **values: object) -> bytes:
    fields: dict[int, object] = {2: at, 65: 19}
    fields.update({int(tag): value for tag, value in values.items()})
    return encode_uas_local_set(fields)


def _store(
    address: tuple[int, int, int, int],
    weapon_type: str,
    *,
    general_status: int = 3,
) -> WeaponStore:
    return WeaponStore(
        *address,
        WeaponStatus(general_status, fuze_enabled=True),
        weapon_type,
    )


def test_distributed_weapon_records_merge_and_expire_independently() -> None:
    first_address = (1, 1, 1, 3)
    second_address = (1, 1, 2, 2)
    state = WeaponsStoresState()
    first = state.observe(
        _packet(START, **{"140": WeaponsStores((_store(first_address, "Harpoon"),))})
    )
    assert tuple(first.records) == (first_address,)
    assert first.updated_addresses == (first_address,)

    second = state.observe(
        _packet(
            START + timedelta(seconds=10),
            **{"140": WeaponsStores((_store(second_address, "Hellfire"),))},
        )
    )
    assert tuple(second.records) == (first_address, second_address)

    boundary = state.observe(_packet(START + timedelta(seconds=30)))
    assert tuple(boundary.records) == (first_address, second_address)
    expired = state.observe(_packet(START + timedelta(seconds=31)))
    assert tuple(expired.records) == (second_address,)
    assert expired.expired_addresses == (first_address,)


def test_same_physical_address_replaces_status_and_refreshes_record() -> None:
    address = (1, 2, 1, 1)
    state = WeaponsStoresState()
    state.observe(
        _packet(
            START,
            **{"140": WeaponsStores((_store(address, "GBU-15", general_status=3),))},
        )
    )
    changed = state.observe(
        _packet(
            START + timedelta(seconds=20),
            **{"140": WeaponsStores((_store(address, "GBU-15", general_status=4),))},
        )
    )
    assert len(changed.records) == 1
    assert changed.records[address].status.general_status == 4
    assert changed.updated_addresses == (address,)
    assert address in state.observe(_packet(START + timedelta(seconds=50))).records


def test_weapons_stores_zli_clears_all_records_immediately() -> None:
    address = (1, 1, 1, 1)
    state = WeaponsStoresState()
    state.observe(
        _packet(START, **{"140": WeaponsStores((_store(address, "Hellfire"),))})
    )
    cleared = state.observe(
        _packet(START + timedelta(seconds=1), **{"140": SpecialValue.UNKNOWN})
    )
    assert not cleared.records
    assert cleared.cleared


def test_malformed_weapons_update_preserves_last_valid_records() -> None:
    address = (1, 1, 1, 1)
    state = WeaponsStoresState()
    state.observe(
        _packet(START, **{"140": WeaponsStores((_store(address, "Hellfire"),))})
    )
    malformed = update_uas_local_set(
        _packet(START + timedelta(seconds=1)),
        {140: RawFieldValue(b"\x01")},
        field_decoding=FieldDecodingMode.PRESERVE,
    )
    snapshot = state.observe(malformed)
    assert tuple(snapshot.records) == (address,)
    assert snapshot.updated_addresses == ()
    assert snapshot.field_issues[0].tag == 140


def test_weapons_stores_limit_rejection_is_atomic() -> None:
    first_address = (1, 1, 1, 1)
    second_address = (1, 1, 1, 2)
    state = WeaponsStoresState(max_weapon_records=1)
    state.observe(
        _packet(START, **{"140": WeaponsStores((_store(first_address, "A"),))})
    )
    with pytest.raises(LimitExceeded, match="weapon-store table"):
        state.observe(
            _packet(
                START + timedelta(seconds=2),
                **{"140": WeaponsStores((_store(second_address, "B"),))},
            )
        )
    assert tuple(
        state.observe(_packet(START + timedelta(seconds=1))).records
    ) == (first_address,)


def test_weapons_stores_state_configuration_decoded_input_and_reset() -> None:
    state = WeaponsStoresState(refresh_period=timedelta(seconds=10))
    assert state.refresh_period == timedelta(seconds=10)
    assert state.max_weapon_records == 4096
    assert state.field_decoding is FieldDecodingMode.PRESERVE
    state.observe(decode_uas_local_set(_packet(START)))
    state.reset()
    assert state.observe(_packet(START - timedelta(days=1))).timestamp < START

    with pytest.raises(TypeError, match="timedelta"):
        WeaponsStoresState(refresh_period=30)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive"):
        WeaponsStoresState(refresh_period=timedelta(0))
    with pytest.raises(ValueError, match="30 seconds"):
        WeaponsStoresState(refresh_period=timedelta(seconds=31))
    with pytest.raises(ValueError, match="positive integer"):
        WeaponsStoresState(max_weapon_records=0)
    with pytest.raises(TypeError, match="FieldDecodingMode"):
        WeaponsStoresState(field_decoding="preserve")  # type: ignore[arg-type]

    ordered = WeaponsStoresState()
    ordered.observe(_packet(START))
    with pytest.raises(DecodeError, match="monotonic"):
        ordered.observe(_packet(START - timedelta(microseconds=1)))
