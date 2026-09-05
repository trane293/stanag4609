from __future__ import annotations

import pytest

from stanag4609.st1402 import validate_st1402_metadata_program
from stanag4609.transport.metadata import (
    asynchronous_klv_stream,
    synchronous_klv_stream,
)
from stanag4609.transport.psi import (
    Descriptor,
    ElementaryStreamInfo,
    KLVCarriage,
    ProgramMapTable,
)


def _pmt(
    *streams: ElementaryStreamInfo,
    descriptors: tuple[Descriptor, ...] = (),
) -> ProgramMapTable:
    return ProgramMapTable(1, 0, True, 0, 0, 0x101, descriptors, streams, b"")


VIDEO = ElementaryStreamInfo(0x1B, 0x101, ())


def test_valid_synchronous_and_asynchronous_programs_have_no_issues() -> None:
    synchronous = synchronous_klv_stream(
        0x102,
        metadata_input_leak_rate=1_000,
        metadata_buffer_size=200_000,
    )
    asynchronous = asynchronous_klv_stream(0x103)
    assert validate_st1402_metadata_program(_pmt(VIDEO, synchronous)) == ()
    assert validate_st1402_metadata_program(_pmt(VIDEO, asynchronous)) == ()
    assert validate_st1402_metadata_program(
        _pmt(VIDEO, synchronous, asynchronous),
        expected_carriage={
            0x102: KLVCarriage.SYNCHRONOUS,
            0x103: KLVCarriage.ASYNCHRONOUS,
        },
    ) == ()


def test_explicit_async_intent_reports_missing_registration_and_video() -> None:
    pmt = _pmt(ElementaryStreamInfo(0x06, 0x120, ()))
    issues = validate_st1402_metadata_program(
        pmt,
        expected_carriage={0x120: KLVCarriage.ASYNCHRONOUS},
    )
    assert {issue.requirement for issue in issues} == {
        "ST 1402-24",
        "ST 1402-25",
        "ST 1402-03",
    }
    assert all(issue.elementary_pid == 0x120 for issue in issues)


def test_sync_descriptor_placement_count_and_format_are_validated() -> None:
    misplaced_metadata = Descriptor(0x26, b"\x01\x00\xffKLVA\x00\x0f")
    malformed_std = Descriptor(0x27, b"\x00" * 9)
    stream = ElementaryStreamInfo(0x15, 0x102, (malformed_std, malformed_std))
    issues = validate_st1402_metadata_program(
        _pmt(VIDEO, stream, descriptors=(misplaced_metadata,)),
        expected_carriage={0x102: KLVCarriage.SYNCHRONOUS},
    )
    assert {issue.code for issue in issues} == {
        "metadata_descriptor",
        "metadata_descriptor_location",
        "metadata_format_identifier",
        "metadata_std_descriptor_count",
        "metadata_std_descriptor_format",
    }

    malformed_metadata = Descriptor(0x26, b"\x01\x00\xffKLVA\x04\x00")
    valid_std = synchronous_klv_stream(
        0x102,
        metadata_input_leak_rate=1_000,
        metadata_buffer_size=200_000,
    ).descriptors[1]
    malformed_stream = ElementaryStreamInfo(
        0x15,
        0x102,
        (malformed_metadata, valid_std),
    )
    malformed_issues = validate_st1402_metadata_program(
        _pmt(VIDEO, malformed_stream),
        expected_carriage={0x102: KLVCarriage.SYNCHRONOUS},
    )
    assert {issue.code for issue in malformed_issues} == {
        "metadata_descriptor_format",
        "metadata_format_identifier",
    }


def test_wrong_stream_types_and_missing_declared_pid_are_reported() -> None:
    pmt = _pmt(VIDEO, ElementaryStreamInfo(0x06, 0x102, (Descriptor(0x05, b"KLVA"),)))
    sync_issues = validate_st1402_metadata_program(
        pmt,
        expected_carriage={0x102: KLVCarriage.SYNCHRONOUS},
    )
    assert any(issue.code == "stream_type" for issue in sync_issues)

    missing = validate_st1402_metadata_program(
        pmt,
        expected_carriage={0x777: KLVCarriage.ASYNCHRONOUS},
    )
    assert [(issue.code, issue.elementary_pid) for issue in missing] == [
        ("missing_stream", 0x777)
    ]


def test_auto_discovery_includes_sync_candidates_but_not_unmarked_private_data() -> None:
    private = ElementaryStreamInfo(0x06, 0x120, ())
    broken_sync = ElementaryStreamInfo(0x15, 0x121, ())
    issues = validate_st1402_metadata_program(_pmt(VIDEO, private, broken_sync))
    assert {issue.elementary_pid for issue in issues} == {0x121}
    assert {issue.requirement for issue in issues} == {
        "ST 1402-15",
        "ST 1402.1-26",
        "ST 1402-17",
    }


def test_program_validator_rejects_invalid_api_inputs() -> None:
    with pytest.raises(TypeError, match="ProgramMapTable"):
        validate_st1402_metadata_program(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="mapping"):
        validate_st1402_metadata_program(_pmt(VIDEO), expected_carriage=())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="13 bits"):
        validate_st1402_metadata_program(
            _pmt(VIDEO),
            expected_carriage={0x2000: KLVCarriage.ASYNCHRONOUS},
        )
    with pytest.raises(TypeError, match="KLVCarriage"):
        validate_st1402_metadata_program(
            _pmt(VIDEO),
            expected_carriage={0x102: "async"},  # type: ignore[dict-item]
        )
