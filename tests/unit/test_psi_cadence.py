from __future__ import annotations

from fractions import Fraction

import pytest

from stanag4609.errors import DecodeError
from stanag4609.transport.psi import (
    ProgramAssociation,
    ProgramAssociationTable,
    ProgramMapTable,
    PSICadenceValidator,
)


def _pat(
    *programs: tuple[int, int],
    current: bool = True,
    section: int = 0,
    last_section: int = 0,
) -> ProgramAssociationTable:
    return ProgramAssociationTable(
        transport_stream_id=1,
        version_number=0,
        current_next_indicator=current,
        section_number=section,
        last_section_number=last_section,
        programs=tuple(ProgramAssociation(*program) for program in programs),
        raw=b"",
    )


def _pmt(
    program_number: int,
    *,
    current: bool = True,
    section: int = 0,
    last_section: int = 0,
) -> ProgramMapTable:
    return ProgramMapTable(
        program_number=program_number,
        version_number=0,
        current_next_indicator=current,
        section_number=section,
        last_section_number=last_section,
        pcr_pid=0x101,
        descriptors=(),
        streams=(),
        raw=b"",
    )


def test_strict_st1402_cadence_boundary_and_recommended_interval() -> None:
    validator = PSICadenceValidator()
    assert validator.maximum_interval == Fraction(1, 4)
    assert validator.recommended_interval == Fraction(1, 8)

    assert validator.observe_pat(_pat((1, 0x100)), at=0) == ()
    assert validator.observe_pmt(_pmt(1), at=0) == ()
    assert validator.observe_pat(_pat((1, 0x100)), at=0.249) == ()
    assert validator.observe_pmt(_pmt(1), at=0.249) == ()

    pat_issues = validator.observe_pat(_pat((1, 0x100)), at=0.499)
    pmt_issues = validator.observe_pmt(_pmt(1), at=0.499)
    assert [(issue.code, issue.table, issue.program_number) for issue in pat_issues] == [
        ("interval", "PAT", None)
    ]
    assert [(issue.code, issue.table, issue.program_number) for issue in pmt_issues] == [
        ("interval", "PMT", 1)
    ]
    assert pat_issues[0].elapsed == Fraction(1, 4)
    assert "more than 4 times per second" in pat_issues[0].message


def test_check_reports_missing_pat_and_each_active_program_pmt() -> None:
    validator = PSICadenceValidator()
    validator.start(at=10)
    assert validator.check(at=10.249) == ()

    initial = validator.check(at=10.25)
    assert [(issue.table, issue.program_number) for issue in initial] == [("PAT", None)]
    assert initial[0].previous_at == Fraction(10)

    validator.observe_pat(_pat((1, 0x100), (2, 0x200)), at=10.3)
    due = validator.check(at=10.55)
    assert [(issue.table, issue.program_number) for issue in due] == [
        ("PAT", None),
        ("PMT", 1),
        ("PMT", 2),
    ]
    assert all(issue.code == "missing" for issue in due[1:])


def test_pat_reconfiguration_adds_and_removes_pmt_deadlines() -> None:
    validator = PSICadenceValidator()
    validator.observe_pat(_pat((1, 0x100), (2, 0x200)), at=0)
    validator.observe_pmt(_pmt(1), at=0.1)
    validator.observe_pmt(_pmt(2), at=0.1)

    validator.observe_pat(_pat((2, 0x200), (3, 0x300)), at=0.2)
    assert validator.expected_programs == (2, 3)
    issues = validator.check(at=0.45)
    assert [(issue.table, issue.program_number) for issue in issues] == [
        ("PAT", None),
        ("PMT", 2),
        ("PMT", 3),
    ]
    assert issues[1].code == "interval"
    assert issues[2].code == "missing"


def test_next_tables_and_unannounced_pmts_do_not_satisfy_current_cadence() -> None:
    validator = PSICadenceValidator()
    validator.observe_pat(_pat((1, 0x100)), at=0)
    validator.observe_pmt(_pmt(1), at=0)

    assert validator.observe_pat(_pat((1, 0x100), current=False), at=0.1) == ()
    assert validator.observe_pmt(_pmt(1, current=False), at=0.1) == ()
    assert validator.observe_pmt(_pmt(99), at=0.1) == ()

    issues = validator.check(at=0.25)
    assert [(issue.table, issue.program_number) for issue in issues] == [
        ("PAT", None),
        ("PMT", 1),
    ]


def test_multisection_tables_count_only_after_a_complete_cycle() -> None:
    validator = PSICadenceValidator()
    first = _pat((1, 0x100), section=0, last_section=1)
    second = _pat((2, 0x200), section=1, last_section=1)
    assert validator.observe_pat(first, at=0) == ()
    assert validator.expected_programs == ()
    assert validator.observe_pat(second, at=0.01) == ()
    assert validator.expected_programs == (1, 2)
    assert validator.last_pat_at == Fraction(1, 100)

    assert validator.observe_pmt(_pmt(1), at=0.02) == ()
    assert validator.last_pmt_at[1] == Fraction(1, 50)

    validator.observe_pat(first, at=0.20)
    issues = validator.observe_pat(second, at=0.26)
    assert [(issue.table, issue.elapsed) for issue in issues] == [
        ("PAT", Fraction(1, 4))
    ]


def test_invalid_section_sequence_is_rejected_by_cadence_monitor() -> None:
    validator = PSICadenceValidator()
    with pytest.raises(DecodeError, match="section_number"):
        validator.observe_pat(_pat(section=2, last_section=1), at=0)

    validator.observe_pat(_pat((1, 0x100)), at=0.1)
    with pytest.raises(DecodeError, match=r"PMT.*zero"):
        validator.observe_pmt(_pmt(1, section=1, last_section=1), at=0.2)


def test_custom_interval_reset_and_immutable_observation_state() -> None:
    validator = PSICadenceValidator(
        maximum_interval=0.2,
        recommended_interval=0.1,
    )
    validator.observe_pat(_pat((7, 0x700)), at=1)
    validator.observe_pmt(_pmt(7), at=1.1)
    assert validator.last_pmt_at[7] == Fraction(11, 10)
    with pytest.raises(TypeError):
        validator.last_pmt_at[7] = Fraction(2)  # type: ignore[index]

    validator.reset()
    assert validator.expected_programs == ()
    assert validator.last_pat_at is None
    assert dict(validator.last_pmt_at) == {}
    assert validator.check(at=100) == ()


def test_cadence_validator_rejects_invalid_configuration_and_time_order() -> None:
    with pytest.raises(TypeError, match="maximum_interval"):
        PSICadenceValidator(maximum_interval=True)
    with pytest.raises(ValueError, match="positive"):
        PSICadenceValidator(maximum_interval=0)
    with pytest.raises(ValueError, match="recommended_interval"):
        PSICadenceValidator(maximum_interval=0.1, recommended_interval=0.2)

    validator = PSICadenceValidator()
    with pytest.raises(TypeError, match="ProgramAssociationTable"):
        validator.observe_pat(object(), at=0)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ProgramMapTable"):
        validator.observe_pmt(object(), at=0)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="timestamp"):
        validator.start(at=True)
    with pytest.raises(ValueError, match="finite"):
        validator.start(at=float("inf"))

    validator.start(at=1)
    with pytest.raises(DecodeError, match="already started"):
        validator.start(at=2)
    with pytest.raises(DecodeError, match="monotonic"):
        validator.check(at=0)
