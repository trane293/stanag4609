from __future__ import annotations

from fractions import Fraction

import pytest

from stanag4609.transport.demux import PESStreamEvent, StreamKind
from stanag4609.transport.mpegts import ProgramClockReference, parse_transport_packet
from stanag4609.transport.mux import encode_pcr_packet, encode_pes_packet
from stanag4609.transport.pes import parse_pes_packet
from stanag4609.transport.psi import ElementaryStreamInfo
from stanag4609.transport.pts import (
    ST1402_MAX_PTS_INTERVAL,
    PTSCadenceValidator,
)
from stanag4609.transport.timing import PTS_CLOCK_RATE, PTS_MODULUS


def _event(
    pts: int | None,
    *,
    program_number: int = 1,
    pid: int = 0x101,
    stream_type: int = 0x1B,
    offset: int = 0,
    discontinuity: bool = False,
) -> PESStreamEvent:
    layout = ()
    if discontinuity:
        layout = (
            parse_transport_packet(
                encode_pcr_packet(
                    pid=pid,
                    pcr=ProgramClockReference(0, 0),
                    discontinuity=True,
                ),
                offset=offset,
            ),
        )
    info = ElementaryStreamInfo(stream_type, pid, ())
    pes = parse_pes_packet(
        encode_pes_packet(b"payload", stream_id=0xE0, pts=pts),
        offset=offset,
        transport_packets=layout,
    )
    return PESStreamEvent(
        program_number,
        info,
        StreamKind.VIDEO,
        None,
        pes,
    )


def test_pts_cadence_accepts_exact_boundary_and_reports_one_tick_overrun() -> None:
    validator = PTSCadenceValidator()
    assert validator.maximum_interval == ST1402_MAX_PTS_INTERVAL == Fraction(7, 10)
    assert validator.observe(_event(0, offset=0)) == ()
    assert validator.observe(_event(63_000, offset=188)) == ()

    issue = validator.observe(_event(126_001, offset=376))[0]
    assert issue.code == "interval"
    assert issue.requirement == "ST 1402.2 §7.3"
    assert issue.previous_pts == 63_000
    assert issue.observed_pts == 126_001
    assert issue.previous_source_offset == 188
    assert issue.current_source_offset == 376
    assert issue.difference == Fraction(63_001, PTS_CLOCK_RATE)
    assert issue.maximum_interval == Fraction(7, 10)
    assert "0.700011 seconds" in issue.message


def test_pts_cadence_handles_rollover_and_small_presentation_reordering() -> None:
    validator = PTSCadenceValidator()
    assert validator.observe(_event(PTS_MODULUS - 10)) == ()
    assert validator.observe(_event(20)) == ()
    assert validator.last_pts[(1, 0x101)] == PTS_MODULUS + 20

    assert validator.observe(_event(10)) == ()
    assert validator.last_pts[(1, 0x101)] == PTS_MODULUS + 10
    assert validator.observation_counts[(1, 0x101)] == 3


def test_pts_cadence_tracks_streams_independently_and_resets_discontinuities() -> None:
    validator = PTSCadenceValidator()
    validator.observe(_event(0, pid=0x101))
    validator.observe(_event(0, pid=0x102))
    assert validator.observe(_event(63_001, pid=0x101))[0].pid == 0x101
    assert validator.observe(_event(63_000, pid=0x102)) == ()

    assert validator.observe(_event(500_000, pid=0x101, discontinuity=True)) == ()
    assert validator.observation_counts[(1, 0x101)] == 1
    assert validator.observe(_event(0, pid=0x101, stream_type=0x02)) == ()
    assert validator.observation_counts[(1, 0x101)] == 1


def test_pts_cadence_ignores_untimestamped_pes_and_validates_configuration() -> None:
    validator = PTSCadenceValidator(maximum_interval=0.5)
    assert validator.observe(_event(None)) == ()
    assert validator.streams == ()
    with pytest.raises(TypeError, match="PESStreamEvent"):
        validator.observe(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="Fraction"):
        PTSCadenceValidator(maximum_interval=True)
    with pytest.raises(ValueError, match="positive"):
        PTSCadenceValidator(maximum_interval=0)
    with pytest.raises(ValueError, match="finite"):
        PTSCadenceValidator(maximum_interval=float("inf"))


def test_pts_cadence_reset_can_target_stream_program_or_all() -> None:
    validator = PTSCadenceValidator()
    validator.observe(_event(0, program_number=1, pid=0x101))
    validator.observe(_event(0, program_number=1, pid=0x102))
    validator.observe(_event(0, program_number=2, pid=0x101))

    validator.reset(program_number=1, pid=0x101)
    assert validator.streams == ((1, 0x102), (2, 0x101))
    validator.reset(program_number=1)
    assert validator.streams == ((2, 0x101),)
    validator.reset()
    assert validator.streams == ()

    with pytest.raises(ValueError, match="requires program_number"):
        validator.reset(pid=0x101)
    with pytest.raises(ValueError, match="program_number"):
        validator.reset(program_number=0)
    with pytest.raises(ValueError, match="pid"):
        validator.reset(program_number=1, pid=0x2000)
