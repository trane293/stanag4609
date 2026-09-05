from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from stanag4609.errors import ChecksumError, DecodeError
from stanag4609.st0102 import (
    SecurityClassification,
    SecuritySpecialValue,
    decode_security_local_set,
    encode_security_local_set,
)
from stanag4609.st0601 import (
    FieldDecodingMode,
    IMAPFieldValue,
    RawFieldValue,
    SpecialValue,
    decode_uas_local_set,
    encode_uas_local_set,
    update_uas_local_set,
)
from stanag4609.st0902 import (
    MISMMSecurityContext,
    MISMMSPopulationStatus,
    MISMMSValidator,
    validate_mismms_current_state,
)
from stanag4609.st1204 import MIISCoreIdentifier

START = datetime(2024, 1, 1, tzinfo=timezone.utc)

COMPLETE_BASE = {
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


def _uas(at: datetime, **values: object):
    fields: dict[int, object] = {2: at, 65: 19}
    fields.update({int(tag): value for tag, value in values.items()})
    return decode_uas_local_set(encode_uas_local_set(fields))


def test_mismms_reporting_interval_is_stream_level_and_inclusive() -> None:
    validator = MISMMSValidator(require_security=False, require_miis=False)
    assert validator.observe(_uas(START, **{"3": "MISSION"})) == ()
    assert validator.observe(_uas(START + timedelta(seconds=30))) == ()

    issues = validator.observe(_uas(START + timedelta(seconds=31)))
    requirements = {issue.requirement for issue in issues if issue.code == "overdue"}
    assert "mission_id" in requirements
    assert "platform_heading" in requirements
    assert "security" not in requirements
    assert "miis_core_identifier" not in requirements


def test_current_state_profile_checker_accepts_complete_reconstructed_view() -> None:
    complete = _uas(START, **{str(tag): value for tag, value in COMPLETE_BASE.items()})
    assert validate_mismms_current_state(
        complete.fields,
        require_security=False,
        require_miis=False,
    ) == ()

    incomplete = _uas(
        START,
        **{
            str(tag): value
            for tag, value in COMPLETE_BASE.items()
            if tag != 13
        },
    )
    issues = validate_mismms_current_state(
        incomplete.fields,
        require_security=False,
        require_miis=False,
    )
    assert [(issue.code, issue.requirement) for issue in issues] == [
        ("missing", "sensor_latitude")
    ]


def test_current_state_rejects_preserved_malformed_st0601_population() -> None:
    complete = encode_uas_local_set(
        {2: START, 65: 19, **COMPLETE_BASE},
    )
    malformed = update_uas_local_set(
        complete,
        {5: RawFieldValue(b"\x00")},
        field_decoding=FieldDecodingMode.PRESERVE,
    )
    decoded = decode_uas_local_set(
        malformed,
        field_decoding=FieldDecodingMode.PRESERVE,
    )

    issues = validate_mismms_current_state(
        decoded,
        require_security=False,
        require_miis=False,
    )

    assert [(issue.code, issue.requirement, issue.tags) for issue in issues] == [
        ("invalid_field", "platform_heading", (5,))
    ]

    with pytest.raises(TypeError, match="FieldDecodingIssue"):
        validate_mismms_current_state(
            decoded.fields,
            field_issues=(object(),),  # type: ignore[arg-type]
            require_security=False,
            require_miis=False,
        )


def test_current_state_checker_validates_nested_security_profile() -> None:
    security = decode_security_local_set(
        encode_security_local_set(
            {1: 1, 2: 14, 3: "//USA", 12: 14, 13: "USA", 22: 12},
            standalone=False,
        ),
        standalone=False,
    )
    complete = _uas(
        START,
        **{str(tag): value for tag, value in COMPLETE_BASE.items()},
        **{"48": security},
    )
    assert validate_mismms_current_state(
        complete.fields,
        require_miis=False,
    ) == ()

    with pytest.raises(TypeError, match="booleans"):
        validate_mismms_current_state(complete.fields, require_miis=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="security_context"):
        validate_mismms_current_state(
            complete.fields,
            security_context=object(),  # type: ignore[arg-type]
        )


def test_always_required_packet_fields_are_part_of_the_profile() -> None:
    validator = MISMMSValidator(require_security=False, require_miis=False)
    validator.observe(_uas(START).packet)
    assert validator.last_seen["checksum"] == START
    assert validator.last_seen["precision_timestamp"] == START
    assert validator.last_seen["uas_local_set_version"] == START


def test_report_after_late_gap_still_records_the_cadence_violation() -> None:
    validator = MISMMSValidator(require_security=False, require_miis=False)
    validator.observe(_uas(START, **{"3": "MISSION"}))
    issues = validator.observe(_uas(START + timedelta(seconds=31), **{"3": "MISSION"}))
    assert any(
        issue.code == "overdue" and issue.requirement == "mission_id" for issue in issues
    )
    assert validator.last_seen["mission_id"] == START + timedelta(seconds=31)


def test_zero_length_item_does_not_meet_minimum_reporting_requirement() -> None:
    validator = MISMMSValidator(require_security=False, require_miis=False)
    issues = validator.observe(_uas(START, **{"3": SpecialValue.UNKNOWN}))
    assert any(issue.code == "zero_length" and issue.tags == (3,) for issue in issues)
    assert "mission_id" not in validator.last_seen


def test_alternative_groups_and_sensor_height_exclusive_or() -> None:
    validator = MISMMSValidator(require_security=False, require_miis=False)
    packet = _uas(
        START,
        **{
            "6": 0.0,
            "91": 0.0,
            "75": 1000.0,
            "104": IMAPFieldValue(1000.0, 3),
            "96": IMAPFieldValue(5.0, 3),
            "78": 100.0,
        },
    )
    issues = validator.observe(packet)
    assert validator.last_seen["platform_pitch"] == START
    assert validator.last_seen["platform_roll"] == START
    assert validator.last_seen["target_width"] == START
    assert validator.last_seen["frame_center_elevation"] == START
    assert any(issue.code == "exclusive_or" and issue.tags == (75, 104) for issue in issues)


def test_sensor_height_exclusive_or_spans_distributed_current_state() -> None:
    validator = MISMMSValidator(require_security=False, require_miis=False)
    assert validator.observe(_uas(START, **{"75": 1000.0})) == ()

    boundary = validator.observe(
        _uas(
            START + timedelta(seconds=30),
            **{"104": IMAPFieldValue(1001.0, 3)},
        )
    )
    assert any(
        issue.code == "exclusive_or"
        and issue.requirement == "sensor_altitude"
        and issue.tags == (75, 104)
        for issue in boundary
    )

    expired = MISMMSValidator(require_security=False, require_miis=False)
    expired.observe(_uas(START, **{"75": 1000.0}))
    issues = expired.observe(
        _uas(
            START + timedelta(seconds=30, microseconds=1),
            **{"104": IMAPFieldValue(1001.0, 3)},
        )
    )
    assert not any(issue.code == "exclusive_or" for issue in issues)


def test_sensor_height_zli_clears_exclusive_or_state_and_reset_forgets_it() -> None:
    validator = MISMMSValidator(require_security=False, require_miis=False)
    validator.observe(_uas(START, **{"75": 1000.0}))
    issues = validator.observe(
        _uas(
            START + timedelta(seconds=1),
            **{
                "75": SpecialValue.UNKNOWN,
                "104": IMAPFieldValue(1001.0, 3),
            },
        )
    )
    assert not any(issue.code == "exclusive_or" for issue in issues)

    validator.reset()
    assert not any(
        issue.code == "exclusive_or"
        for issue in validator.observe(
            _uas(START + timedelta(seconds=2), **{"104": IMAPFieldValue(1001.0, 3)})
        )
    )


def test_mismms_timestamp_must_be_monotonic() -> None:
    validator = MISMMSValidator(require_security=False, require_miis=False)
    validator.observe(_uas(START))
    with pytest.raises(DecodeError, match="monotonic"):
        validator.observe(_uas(START - timedelta(microseconds=1)))


def test_mismms_revalidates_predecoded_packet_checksum() -> None:
    packet = bytearray(encode_uas_local_set({2: START, 65: 19, 3: "MISSION"}))
    packet[-1] ^= 0x01
    unchecked = decode_uas_local_set(bytes(packet), verify_checksum=False)

    validator = MISMMSValidator(require_security=False, require_miis=False)
    with pytest.raises(ChecksumError, match="checksum mismatch"):
        validator.observe(unchecked)


def test_default_profile_tracks_security_and_miis_requirements() -> None:
    validator = MISMMSValidator()
    validator.observe(_uas(START))
    issues = validator.observe(_uas(START + timedelta(seconds=31)))
    overdue = {issue.requirement for issue in issues if issue.code == "overdue"}
    assert {
        "security_classification",
        "security_country_coding_method",
        "security_classifying_country",
        "security_object_country_coding_method",
        "security_object_country_codes",
        "security_metadata_version",
        "miis_core_identifier",
    } <= overdue


def test_miis_core_identifier_has_the_st1204_inclusive_thirty_second_cadence() -> None:
    core = MIISCoreIdentifier(
        version=1,
        minor_id=UUID("03dd9dee-fb48-477b-8204-b0506f6b2a33"),
    )
    validator = MISMMSValidator(require_security=False)
    assert not any(
        issue.requirement == "miis_core_identifier"
        for issue in validator.observe(_uas(START, **{"94": core}))
    )
    assert not any(
        issue.requirement == "miis_core_identifier"
        for issue in validator.observe(_uas(START + timedelta(seconds=30)))
    )
    assert any(
        issue.code == "overdue" and issue.requirement == "miis_core_identifier"
        for issue in validator.observe(
            _uas(START + timedelta(seconds=30, microseconds=1))
        )
    )


def test_typed_security_local_set_tracks_each_nested_security_item_cadence() -> None:
    security = decode_security_local_set(
        encode_security_local_set(
            {1: 1, 2: 14, 3: "//USA", 12: 14, 13: "USA", 22: 12},
            standalone=False,
        ),
        standalone=False,
    )
    validator = MISMMSValidator(require_miis=False)
    first_issues = validator.observe(_uas(START, **{"48": security}))
    assert not any(issue.requirement.startswith("security_") for issue in first_issues)
    assert validator.last_seen["security_classification"] == START
    assert validator.last_seen["security_metadata_version"] == START
    boundary_issues = validator.observe(_uas(START + timedelta(seconds=30)))
    assert not any(
        issue.requirement.startswith("security_") for issue in boundary_issues
    )
    late_issues = validator.observe(_uas(START + timedelta(seconds=31)))
    assert any(
        issue.code == "overdue"
        and issue.requirement == "security_classification"
        for issue in late_issues
    )


def test_legacy_security_set_is_valid_st0102_but_incomplete_for_st0902() -> None:
    security = decode_security_local_set(
        encode_security_local_set(
            {1: 1, 2: 1, 3: "//US", 13: "US"}, standalone=False
        ),
        standalone=False,
    )
    validator = MISMMSValidator(require_miis=False)
    assert validator.observe(_uas(START, **{"48": security})) == ()
    missing = {
        issue.requirement for issue in validator.finish() if issue.code == "missing"
    }
    assert {
        "security_object_country_coding_method",
        "security_metadata_version",
    } <= missing
    assert "security_classification" not in missing


def test_security_context_makes_only_applicable_markings_required() -> None:
    security = decode_security_local_set(
        encode_security_local_set(
            {1: 1, 2: 14, 3: "//USA", 5: "FOUO", 12: 14, 13: "USA", 22: 12},
            standalone=False,
        ),
        standalone=False,
    )
    validator = MISMMSValidator(
        require_miis=False,
        security_context=MISMMSecurityContext(
            sci_shi=True,
            caveats=True,
            releasing_instructions=True,
        ),
    )
    assert validator.observe(_uas(START, **{"48": security})) == ()
    missing = {
        issue.requirement for issue in validator.finish() if issue.code == "missing"
    }
    assert {
        "security_sci_shi_information",
        "security_releasing_instructions",
    } <= missing
    assert "security_caveats" not in missing


def test_security_context_enforces_caller_supplied_marking_policy() -> None:
    context = MISMMSecurityContext(
        expected_classification=SecurityClassification.SECRET,
        expected_classifying_country="USA",
        expected_sci_shi="SI/TK//",
        expected_caveats="FOUO",
        required_releasing_countries=frozenset({"USA", "CAN"}),
        required_object_countries=frozenset({"USA"}),
    )
    security = decode_security_local_set(
        encode_security_local_set(
            {
                1: SecurityClassification.UNCLASSIFIED,
                2: 14,
                3: "//CAN",
                4: "SI//",
                5: "RELIDO",
                6: "USA GBR",
                12: 14,
                13: "CAN;GBR",
                22: 12,
            },
            standalone=False,
        ),
        standalone=False,
    )

    issues = MISMMSValidator(
        require_miis=False,
        security_context=context,
    ).observe(_uas(START, **{"48": security}))

    assert {
        (issue.code, issue.requirement, issue.tags)
        for issue in issues
        if issue.code == "security_policy"
    } == {
        ("security_policy", "security_classification", (1,)),
        ("security_policy", "security_classifying_country", (3,)),
        ("security_policy", "security_sci_shi_information", (4,)),
        ("security_policy", "security_caveats", (5,)),
        ("security_policy", "security_releasing_instructions", (6,)),
        ("security_policy", "security_object_country_codes", (13,)),
    }


def test_current_state_accepts_matching_caller_supplied_marking_policy() -> None:
    context = MISMMSecurityContext(
        expected_classification=SecurityClassification.SECRET,
        expected_classifying_country="USA",
        expected_sci_shi="SI/TK//",
        expected_caveats="FOUO",
        required_releasing_countries=frozenset({"USA", "CAN"}),
        required_object_countries=frozenset({"USA"}),
    )
    security = decode_security_local_set(
        encode_security_local_set(
            {
                1: SecurityClassification.SECRET,
                2: 14,
                3: "//USA",
                4: "SI/TK//",
                5: "FOUO",
                6: "USA CAN GBR",
                12: 14,
                13: "USA;CAN",
                22: 12,
            },
            standalone=False,
        ),
        standalone=False,
    )
    complete = _uas(
        START,
        **{str(tag): value for tag, value in COMPLETE_BASE.items()},
        **{"48": security},
    )

    assert validate_mismms_current_state(
        complete,
        require_miis=False,
        security_context=context,
    ) == ()


def test_security_zero_lengths_do_not_refresh_nested_requirements() -> None:
    validator = MISMMSValidator(require_miis=False)
    issues = validator.observe(_uas(START, **{"48": SpecialValue.UNKNOWN}))
    assert any(
        issue.code == "zero_length"
        and issue.requirement == "security"
        and issue.tags == (48,)
        for issue in issues
    )
    assert not any(name.startswith("security_") for name in validator.last_seen)

    security = decode_security_local_set(
        encode_security_local_set(
            {
                1: 1,
                2: 14,
                3: "//USA",
                5: SecuritySpecialValue.UNKNOWN,
                12: 14,
                13: "USA",
                22: 12,
            },
            standalone=False,
        ),
        standalone=False,
    )
    contextual = MISMMSValidator(
        require_miis=False,
        security_context=MISMMSecurityContext(caveats=True),
    )
    issues = contextual.observe(_uas(START, **{"48": security}))
    assert any(
        issue.code == "zero_length"
        and issue.requirement == "security_caveats"
        and issue.tags == (5,)
        for issue in issues
    )
    assert "security_caveats" not in contextual.last_seen


def test_security_context_requires_security_profile() -> None:
    with pytest.raises(ValueError, match="require_security"):
        MISMMSValidator(
            require_security=False,
            security_context=MISMMSecurityContext(caveats=True),
        )


def test_validator_rejects_invalid_policy_configuration() -> None:
    with pytest.raises(TypeError, match="booleans"):
        MISMMSecurityContext(caveats=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="booleans"):
        MISMMSValidator(require_miis=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="security_context"):
        MISMMSValidator(security_context=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="timedelta"):
        MISMMSValidator(maximum_interval=30)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive"):
        MISMMSValidator(maximum_interval=timedelta(0))
    with pytest.raises(TypeError, match="FieldDecodingMode"):
        MISMMSValidator(field_decoding="preserve")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="expected_classification"):
        MISMMSecurityContext(expected_classification=4)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="expected_classifying_country"):
        MISMMSecurityContext(expected_classifying_country="")
    with pytest.raises(TypeError, match="required_releasing_countries"):
        MISMMSecurityContext(required_releasing_countries={"USA"})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="require_security"):
        MISMMSValidator(
            require_security=False,
            security_context=MISMMSecurityContext(
                expected_classification=SecurityClassification.SECRET
            ),
        )


def test_raw_packet_field_errors_are_preserved_as_profile_diagnostics() -> None:
    packet = update_uas_local_set(
        encode_uas_local_set({2: START, 65: 19}),
        {5: RawFieldValue(b"\x00")},
        field_decoding=FieldDecodingMode.PRESERVE,
    )
    issues = MISMMSValidator(
        require_security=False,
        require_miis=False,
    ).observe(packet)
    assert any(
        issue.code == "invalid_field" and issue.requirement == "platform_heading"
        for issue in issues
    )

    with pytest.raises(DecodeError):
        MISMMSValidator(
            require_security=False,
            require_miis=False,
            field_decoding=FieldDecodingMode.STRICT,
        ).observe(packet)


def test_malformed_nested_security_text_cannot_meet_mismms_profile() -> None:
    malformed_security = encode_security_local_set(
        {1: 1, 2: 14, 3: "//USA", 12: 14, 13: "USA", 22: 12},
        standalone=False,
    ).replace(b"\x0d\x06\x00U\x00S\x00A", b"\x0d\x08\x00U\x00S\x00A\x00 ")
    packet = update_uas_local_set(
        encode_uas_local_set({2: START, 65: 19}),
        {48: RawFieldValue(malformed_security)},
        field_decoding=FieldDecodingMode.PRESERVE,
    )

    issues = MISMMSValidator(require_miis=False).observe(packet)

    assert any(
        issue.code == "invalid_field" and issue.requirement == "security_classification"
        for issue in issues
    )


def test_finish_reports_requirements_never_seen_in_a_finite_stream() -> None:
    validator = MISMMSValidator(require_security=False, require_miis=False)
    validator.observe(_uas(START, **{"3": "MISSION"}))
    missing = {
        issue.requirement for issue in validator.finish() if issue.code == "missing"
    }
    assert "platform_heading" in missing
    assert "mission_id" not in missing
    assert "checksum" not in missing


def test_finish_without_observations_reports_the_selected_profile() -> None:
    validator = MISMMSValidator(require_security=False, require_miis=False)
    assert validator.maximum_interval == timedelta(seconds=30)
    assert validator.security_context == MISMMSecurityContext()
    assert validator.field_decoding is FieldDecodingMode.PRESERVE

    missing = {issue.requirement for issue in validator.finish()}
    assert "checksum" in missing
    assert "security" not in missing

    with pytest.raises(TypeError, match="datetime"):
        validator.finish("2024-01-01")  # type: ignore[arg-type]


def test_finish_reports_trailing_overdue_requirements_at_requested_time() -> None:
    validator = MISMMSValidator(require_security=False, require_miis=False)
    validator.observe(_uas(START, **{"3": "MISSION"}))
    issues = validator.finish(START + timedelta(seconds=31))
    assert any(
        issue.code == "overdue" and issue.requirement == "mission_id" for issue in issues
    )

    with pytest.raises(DecodeError, match="before the last observation"):
        validator.finish(START - timedelta(microseconds=1))


def test_coverage_reports_current_missing_and_overdue_population() -> None:
    validator = MISMMSValidator(require_security=False, require_miis=False)
    validator.observe(_uas(START, **{"3": "MISSION"}))

    initial = {item.requirement: item for item in validator.coverage()}
    assert initial["mission_id"].status is MISMMSPopulationStatus.CURRENT
    assert initial["mission_id"].last_seen == START
    assert initial["mission_id"].age_seconds == 0
    assert initial["platform_heading"].status is MISMMSPopulationStatus.MISSING
    assert initial["platform_heading"].last_seen is None

    late = {
        item.requirement: item
        for item in validator.coverage(START + timedelta(seconds=31))
    }
    assert late["mission_id"].status is MISMMSPopulationStatus.OVERDUE
    assert late["mission_id"].age_seconds == 31
    assert late["mission_id"].tags == (3,)
    assert late["mission_id"].to_dict() == {
        "requirement": "mission_id",
        "tags": [3],
        "status": "overdue",
        "last_seen": START.isoformat(),
        "age_seconds": 31.0,
        "tag_paths": [[3]],
    }

    secured = MISMMSValidator(require_miis=False)
    security = decode_security_local_set(
        encode_security_local_set(
            {1: 1, 2: 14, 3: "//USA", 12: 14, 13: "USA", 22: 12},
            standalone=False,
        ),
        standalone=False,
    )
    secured.observe(_uas(START, **{"48": security}))
    nested = {
        item.requirement: item for item in secured.coverage()
    }["security_classification"]
    assert nested.parent_tag == 48
    assert nested.tags == (1,)
    assert nested.tag_paths == ((48, 1),)
    assert nested.to_dict()["tag_paths"] == [[48, 1]]

    with pytest.raises(TypeError, match="datetime"):
        validator.coverage("later")  # type: ignore[arg-type]
    with pytest.raises(DecodeError, match="before the last observation"):
        validator.coverage(START - timedelta(microseconds=1))


def test_reset_allows_reusing_validator_for_a_new_stream() -> None:
    validator = MISMMSValidator(require_security=False, require_miis=False)
    validator.observe(_uas(START, **{"3": "MISSION"}))
    validator.reset()
    assert not validator.last_seen
    assert validator.observe(_uas(START - timedelta(days=1))) == ()
