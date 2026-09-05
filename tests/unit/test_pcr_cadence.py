from __future__ import annotations

from fractions import Fraction

import pytest

from stanag4609.errors import DecodeError
from stanag4609.transport.demux import ProgramClockEvent
from stanag4609.transport.mpegts import ProgramClockReference, TransportPacket
from stanag4609.transport.pcr import (
    PCR_CLOCK_RATE,
    PCR_MODULUS,
    ST1402_MAX_PCR_INTERVAL,
    PCRCadenceValidator,
    unwrap_pcr_ticks,
)


def _event(
    ticks: int,
    *,
    program_number: int = 1,
    pid: int = 0x101,
    offset: int = 0,
    discontinuity: bool = False,
) -> ProgramClockEvent:
    wrapped = ticks % PCR_MODULUS
    base, extension = divmod(wrapped, 300)
    source = TransportPacket(
        raw=b"",
        offset=offset,
        pid=pid,
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
    return ProgramClockEvent(
        program_number,
        source.pcr,
        None,
        discontinuity,
        source,
    )


def test_pcr_interval_boundary_is_inclusive_and_one_tick_late_fails() -> None:
    validator = PCRCadenceValidator()
    assert validator.maximum_interval == ST1402_MAX_PCR_INTERVAL == Fraction(1, 10)
    assert validator.observe(_event(0, offset=0)) == ()
    assert validator.observe(_event(PCR_CLOCK_RATE // 10, offset=188)) == ()

    issues = validator.observe(
        _event((PCR_CLOCK_RATE // 10) * 2 + 1, offset=376)
    )
    assert len(issues) == 1
    issue = issues[0]
    assert issue.code == "interval"
    assert issue.program_number == 1
    assert issue.pid == 0x101
    assert issue.elapsed == Fraction(PCR_CLOCK_RATE // 10 + 1, PCR_CLOCK_RATE)
    assert issue.previous_source_offset == 188
    assert issue.current_source_offset == 376
    assert "100 milliseconds" in issue.message


def test_pcr_cadence_is_program_aware_and_supports_shared_clock_pid() -> None:
    validator = PCRCadenceValidator()
    assert validator.observe(_event(0, program_number=1)) == ()
    assert validator.observe(_event(0, program_number=2)) == ()
    assert validator.observe(_event(PCR_CLOCK_RATE // 20, program_number=1)) == ()
    assert validator.observe(_event(PCR_CLOCK_RATE // 5, program_number=2))[0].program_number == 2
    assert validator.programs == (1, 2)
    assert validator.last_pcr_ticks[1] == PCR_CLOCK_RATE // 20
    assert validator.last_pcr_ticks[2] == PCR_CLOCK_RATE // 5


def test_pcr_cadence_unwraps_clock_rollover() -> None:
    validator = PCRCadenceValidator()
    start = PCR_MODULUS - PCR_CLOCK_RATE // 20
    validator.observe(_event(start))
    assert validator.observe(_event(PCR_CLOCK_RATE // 20)) == ()
    assert validator.last_pcr_ticks[1] == PCR_MODULUS + PCR_CLOCK_RATE // 20
    assert unwrap_pcr_ticks(0, reference=PCR_MODULUS - 1) == PCR_MODULUS


def test_discontinuity_and_pcr_pid_change_reanchor_without_false_gap() -> None:
    validator = PCRCadenceValidator()
    validator.observe(_event(PCR_CLOCK_RATE))
    assert validator.observe(_event(10, discontinuity=True)) == ()
    assert validator.last_pcr_ticks[1] == 10
    assert validator.observe(_event(20, pid=0x102)) == ()
    assert validator.last_pcr_ticks[1] == 20


def test_unflagged_pcr_regression_is_diagnostic_and_recovers() -> None:
    validator = PCRCadenceValidator()
    validator.observe(_event(1_000, offset=0))
    issue = validator.observe(_event(900, offset=188))[0]
    assert issue.code == "regression"
    assert issue.elapsed == Fraction(-100, PCR_CLOCK_RATE)
    assert "without a discontinuity" in issue.message
    assert validator.last_pcr_ticks[1] == 900
    assert validator.observe(_event(1_000, offset=376)) == ()


def test_pcr_cadence_configuration_reset_and_input_validation() -> None:
    with pytest.raises(TypeError, match="maximum_interval"):
        PCRCadenceValidator(maximum_interval=True)
    with pytest.raises(ValueError, match="positive"):
        PCRCadenceValidator(maximum_interval=0)
    with pytest.raises(ValueError, match="finite"):
        PCRCadenceValidator(maximum_interval=float("inf"))
    with pytest.raises(TypeError, match="ProgramClockEvent"):
        PCRCadenceValidator().observe(object())  # type: ignore[arg-type]

    validator = PCRCadenceValidator(maximum_interval=0.2)
    validator.observe(_event(0, program_number=1))
    validator.observe(_event(0, program_number=2))
    validator.reset(program_number=1)
    assert validator.programs == (2,)
    validator.reset()
    assert validator.programs == ()
    with pytest.raises(ValueError, match="program_number"):
        validator.reset(program_number=0)


def test_pcr_unwrap_rejects_invalid_and_ambiguous_values() -> None:
    with pytest.raises(ValueError, match="PCR ticks"):
        unwrap_pcr_ticks(-1)
    with pytest.raises(TypeError, match="reference"):
        unwrap_pcr_ticks(0, reference=True)
    with pytest.raises(DecodeError, match="half-epoch"):
        unwrap_pcr_ticks(0, reference=PCR_MODULUS // 2)
