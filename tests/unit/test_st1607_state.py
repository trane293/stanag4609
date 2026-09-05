from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from stanag4609 import (
    MetadataTreeState,
    validate_st1602_composite,
    validate_st1607_mismms,
    validate_st1607_security,
)
from stanag4609.errors import DecodeError, LimitExceeded
from stanag4609.st0102 import (
    SecurityLocalSet,
    decode_security_local_set,
    encode_security_local_set,
)
from stanag4609.st0601 import (
    DELETE,
    AmendLocalSet,
    FieldDecodingMode,
    MetadataSubstreamID,
    RawFieldValue,
    SegmentLocalSet,
    SpecialValue,
    decode_amend_local_set,
    decode_segment_local_set,
    encode_amend_local_set,
    encode_segment_local_set,
    encode_uas_local_set,
    update_uas_local_set,
)
from stanag4609.st1204 import IdentifierQuality, MIISCoreIdentifier
from stanag4609.st1601 import GeoRegistrationLocalSet
from stanag4609.st1602 import CompositeImagingLocalSet

START = datetime(2024, 1, 1, tzinfo=timezone.utc)

MISMMS_BASE = {
    3: "MISSION",
    5: 10.0,
    6: 0.0,
    7: 0.0,
    10: "PLATFORM",
    11: "EO",
    12: "WGS-84",
    13: 40.0,
    14: -75.0,
    15: 1000.0,
    16: 10.0,
    17: 8.0,
    18: 0.0,
    19: -10.0,
    20: 0.0,
    21: 10000.0,
    22: 10.0,
    23: 40.0,
    24: -75.0,
    25: 100.0,
}


def _segment(identifier: int, **values: object) -> SegmentLocalSet:
    fields = {int(tag): value for tag, value in values.items()}
    fields[143] = MetadataSubstreamID(identifier)
    return decode_segment_local_set(encode_segment_local_set(fields))


def _amend(identifier: int, **values: object) -> AmendLocalSet:
    fields = {int(tag): value for tag, value in values.items()}
    fields[143] = MetadataSubstreamID(identifier)
    return decode_amend_local_set(encode_amend_local_set(fields))


def _packet(at: datetime, **values: object) -> bytes:
    fields: dict[int, object] = {2: at, 65: 19}
    fields.update({int(tag): value for tag, value in values.items()})
    return encode_uas_local_set(fields)


def _security(raw: bytes) -> SecurityLocalSet:
    return decode_security_local_set(
        raw,
        standalone=False,
        require_required=False,
    )


def _composite(
    z_order: int,
    *,
    timestamp: datetime | None = None,
) -> CompositeImagingLocalSet:
    return CompositeImagingLocalSet(
        2,
        480,
        640,
        0,
        0,
        z_order,
        timestamp=timestamp,
    )


def _miis(identifier: int) -> MIISCoreIdentifier:
    return MIISCoreIdentifier(
        1,
        sensor_quality=IdentifierQuality.PHYSICAL,
        sensor_id=UUID(int=identifier, version=4),
    )


def test_segment_union_override_and_report_on_change() -> None:
    state = MetadataTreeState()
    first = state.observe(
        _packet(
            START,
            **{"3": "ROOT", "5": 10.0, "100": _segment(1, **{"5": 20.0, "13": 30.0})},
        )
    )
    path = (MetadataSubstreamID(1),)
    assert first.effective_value(path, 3) == "ROOT"
    assert first.effective_value(path, 5) == pytest.approx(20.0, abs=0.002)
    assert first.effective_value(path, 13) == pytest.approx(30.0, abs=0.000001)

    sparse = state.observe(
        _packet(START + timedelta(seconds=10), **{"5": 15.0, "100": _segment(1)})
    )
    assert sparse.effective_value(path, 5) == pytest.approx(20.0, abs=0.002)
    assert sparse.effective_value(path, 13) == pytest.approx(30.0, abs=0.000001)

    expired = state.observe(
        _packet(START + timedelta(seconds=31), **{"100": _segment(1)})
    )
    assert expired.effective_value(path, 5) == pytest.approx(15.0, abs=0.003)
    assert expired.effective_value(path, 13) is None
    assert expired.branches[path].expired_tags == (5, 13)


def test_amend_overlay_add_change_delete_and_expiry() -> None:
    state = MetadataTreeState()
    path = (MetadataSubstreamID(7),)
    first = state.observe(
        _packet(
            START,
            **{"3": "ROOT", "5": 10.0, "101": _amend(7, **{"3": DELETE, "5": 25.0, "13": 40.0})},
        )
    )
    assert first.effective_value(path, 3) is None
    assert first.effective_value(path, 5) == pytest.approx(25.0, abs=0.002)
    assert first.effective_value(path, 13) == pytest.approx(40.0, abs=0.000001)
    assert first.branches[path].deleted_tags == (3,)

    boundary = state.observe(
        _packet(
            START + timedelta(seconds=30),
            **{"3": "ROOT", "5": 10.0, "101": _amend(7)},
        )
    )
    assert boundary.effective_value(path, 3) is None
    expired = state.observe(
        _packet(START + timedelta(seconds=31), **{"101": _amend(7)})
    )
    assert expired.effective_value(path, 3) == "ROOT"
    assert expired.effective_value(path, 5) == pytest.approx(10.0, abs=0.003)


def test_parallel_geo_registration_amends_are_retained_and_resolved_by_msid() -> None:
    state = MetadataTreeState()
    first_path = (MetadataSubstreamID(7),)
    second_path = (MetadataSubstreamID(8),)
    first_result = GeoRegistrationLocalSet(2, "feature-match", "1.0")
    second_result = GeoRegistrationLocalSet(2, "terrain-match", "3.2")

    initial = state.observe(
        _packet(
            START,
            **{
                "13": 40.0,
                "14": -75.0,
                "101": (
                    _amend(7, **{"13": 40.1, "98": first_result}),
                    _amend(8, **{"14": -75.2, "98": second_result}),
                ),
            },
        )
    )
    assert initial.effective_geo_registration(first_path) == first_result
    assert initial.effective_geo_registration(second_path) == second_result
    assert initial.effective_value(first_path, 13) == pytest.approx(40.1, abs=0.000001)
    assert initial.effective_value(first_path, 14) == pytest.approx(-75.0, abs=0.000001)
    assert initial.effective_value(second_path, 13) == pytest.approx(40.0, abs=0.000001)
    assert initial.effective_value(second_path, 14) == pytest.approx(-75.2, abs=0.000001)

    sparse = state.observe(
        _packet(
            START + timedelta(seconds=10),
            **{"101": (_amend(7), _amend(8))},
        )
    )
    assert sparse.effective_geo_registration(first_path) == first_result
    assert sparse.effective_geo_registration(second_path) == second_result
    assert sparse.effective_geo_registration() is None

    expired = state.observe(
        _packet(
            START + timedelta(seconds=31),
            **{"101": (_amend(7), _amend(8))},
        )
    )
    assert expired.effective_geo_registration(first_path) is None
    assert expired.effective_geo_registration(second_path) is None


def test_nested_lineage_is_addressed_by_full_msid_path() -> None:
    child = _amend(2, **{"13": 42.0})
    parent = _segment(1, **{"5": 20.0, "101": child})
    snapshot = MetadataTreeState().observe(
        _packet(START, **{"5": 10.0, "100": parent})
    )
    parent_path = (MetadataSubstreamID(1),)
    child_path = (*parent_path, MetadataSubstreamID(2))
    assert snapshot.effective_value(parent_path, 5) == pytest.approx(20.0, abs=0.002)
    assert snapshot.effective_value(child_path, 5) == pytest.approx(20.0, abs=0.002)
    assert snapshot.effective_value(child_path, 13) == pytest.approx(42.0, abs=0.000001)
    assert snapshot.branches[child_path].parent_path == parent_path


def test_segment_zli_reveals_parent_and_unreported_branch_expires() -> None:
    path = (MetadataSubstreamID(1),)
    state = MetadataTreeState()
    state.observe(
        _packet(START, **{"5": 10.0, "100": _segment(1, **{"5": 20.0})})
    )
    cleared = state.observe(
        _packet(
            START + timedelta(seconds=1),
            **{"5": 10.0, "100": _segment(1, **{"5": SpecialValue.UNKNOWN})},
        )
    )
    assert cleared.effective_value(path, 5) == pytest.approx(10.0, abs=0.003)
    assert cleared.branches[path].cleared_tags == (5,)

    expired = state.observe(_packet(START + timedelta(seconds=32)))
    assert path not in expired.branches
    assert expired.expired_paths == (path,)
    with pytest.raises(KeyError):
        expired.effective_fields(path)


def test_substream_identity_cannot_move_or_change_kind() -> None:
    state = MetadataTreeState()
    state.observe(_packet(START, **{"100": _segment(1)}))
    with pytest.raises(DecodeError, match="changed kind"):
        state.observe(_packet(START + timedelta(seconds=1), **{"101": _amend(1)}))
    assert tuple(state.observe(_packet(START + timedelta(microseconds=1))).branches) == (
        (MetadataSubstreamID(1),),
    )


def test_tree_limits_time_order_and_reset_are_atomic() -> None:
    state = MetadataTreeState(max_branches=1)
    state.observe(_packet(START, **{"100": _segment(1)}))
    with pytest.raises(LimitExceeded, match="branch limit"):
        state.observe(_packet(START + timedelta(seconds=2), **{"100": _segment(2)}))
    assert state.observe(_packet(START + timedelta(seconds=1))).timestamp > START
    with pytest.raises(DecodeError, match="monotonic"):
        state.observe(_packet(START))
    state.reset()
    assert state.observe(_packet(START - timedelta(days=1))).timestamp < START

    with pytest.raises(TypeError, match="timedelta"):
        MetadataTreeState(refresh_period=30)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive"):
        MetadataTreeState(refresh_period=timedelta(0))
    with pytest.raises(ValueError, match="30 seconds"):
        MetadataTreeState(refresh_period=timedelta(seconds=31))
    with pytest.raises(ValueError, match="positive integer"):
        MetadataTreeState(max_branches=0)
    with pytest.raises(ValueError, match="positive integer"):
        MetadataTreeState(max_fields_per_branch=0)
    with pytest.raises(TypeError, match="FieldDecodingMode"):
        MetadataTreeState(field_decoding="preserve")  # type: ignore[arg-type]

    fields = MetadataTreeState(max_fields_per_branch=1)
    with pytest.raises(LimitExceeded, match="field limit"):
        fields.observe(
            _packet(START, **{"100": _segment(1, **{"5": 10.0, "13": 20.0})})
        )
    assert not fields.observe(_packet(START - timedelta(days=1))).branches


def test_malformed_branch_field_does_not_replace_current_value() -> None:
    good = _segment(1, **{"5": 20.0})
    raw = b"\x05\x01\x00\x81\x0f\x01\x01"
    malformed_packet = update_uas_local_set(
        _packet(START + timedelta(seconds=1), **{"100": _segment(1)}),
        {100: RawFieldValue(raw)},
        field_decoding=FieldDecodingMode.PRESERVE,
    )
    state = MetadataTreeState()
    state.observe(_packet(START, **{"100": good}))
    snapshot = state.observe(malformed_packet)
    path = (MetadataSubstreamID(1),)
    assert snapshot.effective_value(path, 5) == pytest.approx(20.0, abs=0.002)
    assert snapshot.root.issues[0].tag == 100


def test_child_country_security_overlays_inherited_root_security() -> None:
    root = decode_security_local_set(
        encode_security_local_set(
            {1: 1, 2: 14, 3: "//USA", 12: 14, 13: "USA", 22: 12},
            standalone=False,
        ),
        standalone=False,
    )
    child = _security(bytes.fromhex("0C 01 0E 0D 06 00 43 00 41 00 4E"))
    snapshot = MetadataTreeState().observe(
        _packet(START, **{"48": root, "100": _segment(1, **{"48": child})})
    )
    path = (MetadataSubstreamID(1),)
    effective = snapshot.effective_security(path)
    assert effective is not None
    assert effective[1].value == 1
    assert effective[13].value == "CAN"
    assert validate_st1607_security(snapshot) == ()


def test_child_security_policy_reports_shape_and_inheritance_violations() -> None:
    extra = _security(
        bytes.fromhex("0C 01 0E 0D 06 00 43 00 41 00 4E 0E 01 58")
    )
    missing = _security(bytes.fromhex("0C 01 0D"))
    snapshot = MetadataTreeState().observe(
        _packet(
            START,
            **{
                "100": (
                    _segment(1, **{"48": extra}),
                    _segment(2, **{"48": missing}),
                )
            },
        )
    )
    issues = validate_st1607_security(snapshot)
    assert [(issue.code, issue.tags) for issue in issues] == [
        ("unexpected_child_security_item", (14,)),
        ("incomplete_child_country_security", (13,)),
    ]

    deleted = MetadataTreeState().observe(
        _packet(START, **{"101": _amend(9, **{"48": DELETE})})
    )
    assert validate_st1607_security(deleted)[0].code == "root_security_deleted"
    assert deleted.effective_security((MetadataSubstreamID(9),)) is None
    with pytest.raises(TypeError, match="MetadataTreeSnapshot"):
        validate_st1607_security(object())  # type: ignore[arg-type]


def test_segment_leaf_union_can_complete_mismms_across_substream_levels() -> None:
    root_values = {
        str(tag): value for tag, value in MISMMS_BASE.items() if tag != 13
    }
    child = _segment(2, **{"13": 40.0})
    parent = _segment(1, **{"100": child})
    snapshot = MetadataTreeState().observe(
        _packet(START, **root_values, **{"100": parent})
    )
    assert validate_st1607_mismms(
        snapshot,
        require_security=False,
        require_miis=False,
    ) == ()


def test_amend_cannot_supply_mismms_item_missing_from_root() -> None:
    root_values = {
        str(tag): value for tag, value in MISMMS_BASE.items() if tag != 13
    }
    snapshot = MetadataTreeState().observe(
        _packet(START, **root_values, **{"101": _amend(1, **{"13": 40.0})})
    )
    issues = validate_st1607_mismms(
        snapshot,
        require_security=False,
        require_miis=False,
    )
    assert [(issue.code, issue.requirement, issue.path, issue.tags) for issue in issues] == [
        ("mismms_missing", "ST 1607-05", (), (13,))
    ]
    with pytest.raises(TypeError, match="MetadataTreeSnapshot"):
        validate_st1607_mismms(object())  # type: ignore[arg-type]


def test_composite_z_order_is_unique_across_retained_sibling_segments() -> None:
    state = MetadataTreeState()
    first = state.observe(
        _packet(
            START,
            **{"100": _segment(1, **{"94": _miis(1), "99": _composite(4)})},
        )
    )
    assert validate_st1602_composite(first) == ()
    snapshot = state.observe(
        _packet(
            START + timedelta(seconds=1),
            **{
                "100": _segment(
                    2,
                    **{"94": _miis(2), "99": _composite(4)},
                )
            },
        )
    )

    issues = validate_st1602_composite(snapshot)
    assert [issue.code for issue in issues] == [
        "duplicate_composite_z_order",
        "duplicate_composite_z_order",
    ]
    assert [issue.path for issue in issues] == [
        (MetadataSubstreamID(1),),
        (MetadataSubstreamID(2),),
    ]
    assert all(issue.requirement == "ST 1602-04" for issue in issues)
    assert all(issue.tags == (99,) for issue in issues)


def test_multiple_composite_sensors_require_direct_miis_in_every_child() -> None:
    state = MetadataTreeState()
    first = state.observe(
        _packet(START, **{"100": _segment(1, **{"99": _composite(1)})})
    )
    assert validate_st1602_composite(first) == ()

    second = state.observe(
        _packet(
            START + timedelta(seconds=1),
            **{
                "94": _miis(99),
                "100": _segment(
                    2,
                    **{"94": _miis(2), "99": _composite(2)},
                ),
            },
        )
    )
    issues = validate_st1602_composite(second)
    assert [(issue.code, issue.path, issue.tags) for issue in issues] == [
        ("missing_composite_sensor_miis", (MetadataSubstreamID(1),), (94,))
    ]
    assert issues[0].requirement == "ST 1602.1-10"
    assert "direct" in issues[0].message


def test_effective_composite_timestamp_inherits_parent_or_uses_explicit_value() -> None:
    explicit = START + timedelta(milliseconds=20)
    nested_parent_timestamp = START + timedelta(milliseconds=10)
    snapshot = MetadataTreeState().observe(
        _packet(
            START,
            **{
                "100": (
                    _segment(1, **{"99": _composite(1)}),
                    _segment(2, **{"99": _composite(2, timestamp=explicit)}),
                    _segment(3),
                    _segment(
                        4,
                        **{
                            "2": nested_parent_timestamp,
                            "100": _segment(5, **{"99": _composite(3)}),
                        },
                    ),
                )
            },
        )
    )

    assert snapshot.effective_composite_timestamp((MetadataSubstreamID(1),)) == START
    assert snapshot.effective_composite_timestamp((MetadataSubstreamID(2),)) == explicit
    assert snapshot.effective_composite_timestamp(
        (MetadataSubstreamID(4), MetadataSubstreamID(5))
    ) == nested_parent_timestamp
    with pytest.raises(ValueError, match="does not carry Item 99"):
        snapshot.effective_composite_timestamp((MetadataSubstreamID(3),))
    with pytest.raises(KeyError):
        snapshot.effective_composite_timestamp((MetadataSubstreamID(99),))


def test_composite_z_order_scope_is_one_parent_and_expires_with_segment() -> None:
    first_parent = _segment(
        1,
        **{"100": _segment(11, **{"99": _composite(1)})},
    )
    second_parent = _segment(
        2,
        **{"100": _segment(12, **{"94": _miis(12), "99": _composite(1)})},
    )
    state = MetadataTreeState()
    distinct_composites = state.observe(
        _packet(START, **{"100": (first_parent, second_parent)})
    )
    issues = validate_st1602_composite(distinct_composites)
    assert [(issue.code, issue.path) for issue in issues] == [
        (
            "missing_composite_sensor_miis",
            (MetadataSubstreamID(1), MetadataSubstreamID(11)),
        )
    ]

    expired = state.observe(_packet(START + timedelta(seconds=31)))
    assert validate_st1602_composite(expired) == ()
    with pytest.raises(TypeError, match="MetadataTreeSnapshot"):
        validate_st1602_composite(object())  # type: ignore[arg-type]
