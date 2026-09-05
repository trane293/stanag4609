from __future__ import annotations

from fractions import Fraction

import pytest

from stanag4609.transport.demux import PATEvent, PMTEvent, ProgramClockEvent
from stanag4609.transport.mpegts import ProgramClockReference, TransportPacket
from stanag4609.transport.mux import build_pat_section, build_pmt_section
from stanag4609.transport.pcr import PCR_CLOCK_RATE, PCR_MODULUS
from stanag4609.transport.psi import (
    ElementaryStreamInfo,
    ProgramAssociation,
    parse_pat,
    parse_pmt,
)
from stanag4609.transport.psi_timing import (
    ST1402_MAX_PSI_INTERVAL,
    PCRBracketedPSICadenceValidator,
)


def _source(*, pid: int, offset: int) -> TransportPacket:
    return TransportPacket(
        raw=b"",
        offset=offset,
        pid=pid,
        transport_error_indicator=False,
        payload_unit_start=True,
        transport_priority=False,
        scrambling_control=0,
        adaptation_field_control=1,
        continuity_counter=0,
        adaptation_field=b"",
        payload=b"",
        discontinuity_indicator=False,
        pcr=None,
        opcr=None,
    )


def _pat(*, offset: int = 0) -> PATEvent:
    table = parse_pat(
        build_pat_section(
            transport_stream_id=1,
            programs=(ProgramAssociation(1, 0x100),),
        )
    )
    return PATEvent(table, source=_source(pid=0, offset=offset))


def _pmt(*, offset: int = 0) -> PMTEvent:
    table = parse_pmt(
        build_pmt_section(
            program_number=1,
            pcr_pid=0x101,
            streams=(ElementaryStreamInfo(0x1B, 0x101, ()),),
        )
    )
    return PMTEvent(table, 0x100, _source(pid=0x100, offset=offset))


def _clock(
    seconds: Fraction,
    *,
    offset: int,
    discontinuity: bool = False,
) -> ProgramClockEvent:
    ticks = int(seconds * PCR_CLOCK_RATE) % PCR_MODULUS
    base, extension = divmod(ticks, 300)
    source = TransportPacket(
        raw=b"",
        offset=offset,
        pid=0x101,
        transport_error_indicator=False,
        payload_unit_start=False,
        transport_priority=False,
        scrambling_control=0,
        adaptation_field_control=2,
        continuity_counter=0,
        adaptation_field=b"",
        payload=b"",
        discontinuity_indicator=discontinuity,
        pcr=ProgramClockReference(base, extension),
        opcr=None,
    )
    return ProgramClockEvent(1, source.pcr, None, discontinuity, source)


def test_reports_only_a_pcr_proven_pat_blackout_at_the_strict_boundary() -> None:
    validator = PCRBracketedPSICadenceValidator()
    assert validator.maximum_interval == ST1402_MAX_PSI_INTERVAL == Fraction(1, 4)

    validator.observe(_clock(Fraction(0), offset=0))
    validator.observe(_pat(offset=188))
    assert validator.observe(_clock(Fraction(1, 10), offset=376)) == ()

    issues = validator.observe(_clock(Fraction(7, 20), offset=564))
    validator.observe(_pat(offset=752))
    assert validator.observe(_clock(Fraction(2, 5), offset=940)) == ()

    assert len(issues) == 1
    issue = issues[0]
    assert issue.table == "PAT"
    assert issue.code == "interval"
    assert issue.program_number == 1
    assert issue.previous_source_offset == 188
    assert issue.current_source_offset == 564
    assert issue.minimum_interval == Fraction(1, 4)
    assert issue.maximum_interval == Fraction(7, 20)
    assert "has not recurred" in issue.message
    assert "more than four times per second" in issue.message


def test_does_not_report_a_gap_whose_pcr_brackets_are_ambiguous() -> None:
    validator = PCRBracketedPSICadenceValidator()
    validator.observe(_clock(Fraction(0), offset=0))
    validator.observe(_pmt(offset=188))
    validator.observe(_clock(Fraction(1, 10), offset=376))
    validator.observe(_clock(Fraction(3, 10), offset=564))
    validator.observe(_pmt(offset=752))

    assert validator.observe(_clock(Fraction(2, 5), offset=940)) == ()


def test_reports_pmt_cadence_and_reanchors_on_discontinuity() -> None:
    validator = PCRBracketedPSICadenceValidator()
    validator.observe(_clock(Fraction(0), offset=0))
    validator.observe(_pmt(offset=188))
    validator.observe(_clock(Fraction(1, 10), offset=376))
    validator.observe(_clock(Fraction(2), offset=564, discontinuity=True))
    validator.observe(_pmt(offset=752))
    assert validator.observe(_clock(Fraction(21, 10), offset=940)) == ()

    issues = validator.observe(_clock(Fraction(12, 5), offset=1128))
    validator.observe(_pmt(offset=1316))
    assert validator.observe(_clock(Fraction(5, 2), offset=1504)) == ()
    issue = issues[0]
    assert issue.table == "PMT"
    assert issue.minimum_interval == Fraction(3, 10)


def test_continuing_blackout_is_reported_once_until_the_table_recovers() -> None:
    validator = PCRBracketedPSICadenceValidator()
    validator.observe(_clock(Fraction(0), offset=0))
    validator.observe(_pat(offset=188))
    validator.observe(_clock(Fraction(1, 10), offset=376))

    first = validator.observe(_clock(Fraction(7, 20), offset=564))
    assert len(first) == 1
    assert validator.observe(_clock(Fraction(1, 2), offset=752)) == ()

    validator.observe(_pat(offset=940))
    assert validator.observe(_clock(Fraction(3, 5), offset=1128)) == ()
    assert validator.observe(_clock(Fraction(17, 20), offset=1316))[0].table == "PAT"


def test_unbracketed_and_overflowing_observations_are_counted_but_not_guessed() -> None:
    validator = PCRBracketedPSICadenceValidator(max_pending_per_program=1)
    validator.observe(_pat(offset=0))
    validator.observe(_clock(Fraction(0), offset=188))
    assert validator.unverifiable_observations == 1

    validator.observe(_pat(offset=376))
    validator.observe(_pat(offset=564))
    assert validator.dropped_observations == 1
    assert validator.observe(_clock(Fraction(1, 10), offset=752)) == ()


def test_rejects_invalid_configuration_and_event_types() -> None:
    with pytest.raises(ValueError, match="maximum_interval"):
        PCRBracketedPSICadenceValidator(maximum_interval=0)
    with pytest.raises(ValueError, match="max_pending"):
        PCRBracketedPSICadenceValidator(max_pending_per_program=0)
    validator = PCRBracketedPSICadenceValidator()
    with pytest.raises(TypeError, match="PATEvent, PMTEvent, or ProgramClockEvent"):
        validator.observe(object())
