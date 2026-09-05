from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from stanag4609.errors import DecodeError
from stanag4609.klv.ber import encode_ber_oid
from stanag4609.klv.local_set import parse_local_set
from stanag4609.st0903_geo import Acceleration, Location, Velocity
from stanag4609.st0903_tracker import (
    VTrackerLocalSet,
    decode_vtracker_local_set,
    encode_vtracker_local_set,
)
from stanag4609.st0903_vocab import RawVMTIValue


def _item(tag: int, value: bytes) -> bytes:
    return encode_ber_oid(tag) + bytes((len(value),)) + value


def _tracker() -> VTrackerLocalSet:
    return VTrackerLocalSet(
        track_id=UUID("f81d4fae-7dec-11d0-a765-00a0c91e6bf6"),
        first_observation_time=datetime(2001, 4, 19, 4, 25, 21, tzinfo=timezone.utc),
        latest_observation_time=datetime(2001, 4, 19, 4, 25, 22, tzinfo=timezone.utc),
        track_boundary_series=(Location(49, -123, 10), Location(50, -122, 20)),
        confidence_level=50,
        track_history_series=(Location(49.1, -122.9, 11), Location(49.2, -122.8, 12)),
        velocity=Velocity(12, -3, 0),
        acceleration=Acceleration(1, 0, -1),
        algorithm_id=3,
    )


def test_vtracker_roundtrips_all_st0903_6_fields_and_official_values() -> None:
    tracker = _tracker()
    encoded = encode_vtracker_local_set(tracker)
    items = {item.tag: item.value for item in parse_local_set(encoded).items}

    assert items[1] == bytes.fromhex("F81D4FAE7DEC11D0A76500A0C91E6BF6")
    assert items[3] == bytes.fromhex("0003824430F6CE40")
    assert items[7] == b"\x32"
    assert items[12] == b"\x03"
    assert encode_vtracker_local_set(decode_vtracker_local_set(encoded)) == encoded


def test_vtracker_preserves_unknown_nested_items_on_request() -> None:
    raw = _item(1, UUID(int=1).bytes) + _item(13, b"future")
    decoded = decode_vtracker_local_set(raw)
    assert decoded.extensions == {13: RawVMTIValue(b"future")}
    assert encode_vtracker_local_set(decoded, preserve=True) == raw
    assert encode_vtracker_local_set(decoded) == raw


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (_item(1, bytes(15)), "trackId must contain 16 bytes"),
        (_item(3, bytes(7)), "firstObsvTime must contain 8 bytes"),
        (_item(3, bytes.fromhex("FFFFFFFFFFFFFFFF")), "outside datetime range"),
        (_item(7, b"\x00\x32"), "confidenceLevel must contain 1 byte"),
        (_item(7, b"\x65"), "confidenceLevel must be between 0 and 100"),
        (_item(12, b"\x00\x03"), "minimal unsigned encoding"),
        (_item(1, bytes(16)) + _item(1, bytes(16)), "occurs twice"),
        (bytes.fromhex("81000100"), "one-byte UINT tags"),
    ],
)
def test_vtracker_rejects_malformed_local_sets(raw: bytes, message: str) -> None:
    with pytest.raises(DecodeError, match=message):
        decode_vtracker_local_set(raw)


def test_vtracker_model_rejects_invalid_time_geometry_and_motion_dependencies() -> None:
    aware = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(TypeError, match="track_id"):
        VTrackerLocalSet(track_id=bytes(16))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timezone-aware"):
        VTrackerLocalSet(first_observation_time=datetime(2026, 1, 1))
    with pytest.raises(TypeError, match="must be a datetime"):
        VTrackerLocalSet(first_observation_time=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unsigned 64-bit"):
        VTrackerLocalSet(first_observation_time=datetime(1969, 1, 1, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match=r"latestObsvTime.*before"):
        VTrackerLocalSet(
            first_observation_time=aware,
            latest_observation_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
    with pytest.raises(ValueError, match="at least 2"):
        VTrackerLocalSet(track_boundary_series=(Location(0, 0, 0),))
    with pytest.raises(TypeError, match="trackHistorySeries"):
        VTrackerLocalSet(track_history_series=(object(),))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="trackBoundarySeries"):
        VTrackerLocalSet(track_boundary_series=[])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="associated trackHistorySeries"):
        VTrackerLocalSet(velocity=Velocity(1, 2, 3))
    with pytest.raises(TypeError, match="velocity"):
        VTrackerLocalSet(velocity=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="acceleration"):
        VTrackerLocalSet(acceleration=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="confidenceLevel"):
        VTrackerLocalSet(confidence_level=101)


def test_vtracker_encoder_requires_typed_input_and_future_extensions() -> None:
    with pytest.raises(TypeError, match="VTrackerLocalSet"):
        encode_vtracker_local_set(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="after Item 12"):
        encode_vtracker_local_set(
            VTrackerLocalSet(extensions={2: RawVMTIValue(b"deprecated")})
        )
