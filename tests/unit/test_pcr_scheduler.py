from __future__ import annotations

from fractions import Fraction

import pytest

from stanag4609.errors import DecodeError
from stanag4609.transport.demux import ProgramClockEvent, TransportDemuxer
from stanag4609.transport.mpegts import (
    ProgramClockReference,
    parse_transport_packet,
)
from stanag4609.transport.mux import TransportMuxer
from stanag4609.transport.pcr import (
    DEFAULT_PCR_SCHEDULER_INTERVAL,
    PCR_CLOCK_RATE,
    PCR_MODULUS,
    PCRCadenceValidator,
    ProgramClockScheduler,
    pcr_from_ticks,
)
from stanag4609.transport.psi import ElementaryStreamInfo


def _muxer(*, pcr_pid: int = 0x101) -> TransportMuxer:
    return TransportMuxer(
        transport_stream_id=1,
        program_number=1,
        program_map_pid=0x100,
        pcr_pid=pcr_pid,
        streams=(ElementaryStreamInfo(0x1B, pcr_pid, ()),),
    )


def test_clock_scheduler_emits_actual_timeline_pcr_without_drift() -> None:
    muxer = _muxer()
    scheduler = ProgramClockScheduler(muxer)
    assert scheduler.interval == DEFAULT_PCR_SCHEDULER_INTERVAL == Fraction(1, 20)

    anchor = ProgramClockReference(90_000, 17)
    first = scheduler.start(anchor, at=10, discontinuity=True)
    assert first.pcr == anchor
    assert first.scheduled_at == first.emitted_at == 10
    assert first.previous_emitted_at is None
    assert first.late_by == 0
    assert first.interval_compliant
    assert first.discontinuity
    parsed = parse_transport_packet(first.packet)
    assert parsed.pid == 0x101
    assert parsed.pcr == anchor
    assert parsed.discontinuity_indicator

    assert scheduler.poll(at=Fraction(10049, 1000)) is None
    second = scheduler.poll(at=Fraction(201, 20))
    assert second is not None
    assert second.pcr.ticks == anchor.ticks + PCR_CLOCK_RATE // 20
    assert second.elapsed_since_emission == Fraction(1, 20)
    assert scheduler.next_due_at == Fraction(101, 10)
    assert scheduler.total_emissions == 2


def test_clock_scheduler_exposes_late_missed_and_noncompliant_poll() -> None:
    scheduler = ProgramClockScheduler(_muxer())
    anchor = ProgramClockReference(0, 0)
    scheduler.start(anchor, at=0)

    late = scheduler.poll(at=Fraction(11, 100))
    assert late is not None
    assert late.scheduled_at == Fraction(1, 10)
    assert late.emitted_at == Fraction(11, 100)
    assert late.late_by == Fraction(1, 100)
    assert late.missed_repetitions == 1
    assert not late.interval_compliant
    assert late.pcr.ticks == PCR_CLOCK_RATE * 11 // 100
    assert scheduler.total_missed_repetitions == 1

    recovered = scheduler.poll(at=Fraction(3, 20))
    assert recovered is not None
    assert recovered.missed_repetitions == 0
    assert recovered.interval_compliant


def test_clock_scheduler_retains_exact_fractional_cadence_across_rollover() -> None:
    scheduler = ProgramClockScheduler(_muxer(), interval=Fraction(1, 30))
    anchor = pcr_from_ticks(PCR_MODULUS - 10)
    scheduler.start(anchor, at=0)
    last = None
    for index in range(1, 301):
        last = scheduler.poll(at=Fraction(index, 30))
        assert last is not None
        assert last.late_by == 0
    assert last is not None
    assert last.pcr == pcr_from_ticks(PCR_MODULUS - 10 + PCR_CLOCK_RATE * 10)
    assert scheduler.next_due_at == Fraction(301, 30)
    assert scheduler.total_emissions == 301
    assert scheduler.total_missed_repetitions == 0


def test_scheduled_clocks_round_trip_through_demux_and_cadence_validator() -> None:
    muxer = _muxer()
    scheduler = ProgramClockScheduler(muxer, interval=Fraction(1, 10))
    emissions = [scheduler.start(ProgramClockReference(0, 0), at=0)]
    for timestamp in (Fraction(1, 10), Fraction(1, 5), Fraction(3, 10)):
        emission = scheduler.poll(at=timestamp)
        assert emission is not None
        emissions.append(emission)

    transport = b"".join(muxer.program_tables()) + b"".join(
        emission.packet for emission in emissions
    )
    validator = PCRCadenceValidator()
    issues = [
        issue
        for event in TransportDemuxer().feed(transport)
        if isinstance(event, ProgramClockEvent)
        for issue in validator.observe(event)
    ]
    assert issues == []


def test_clock_scheduler_tracks_reconfigured_pcr_pid() -> None:
    muxer = _muxer()
    scheduler = ProgramClockScheduler(muxer)
    scheduler.start(ProgramClockReference(0, 0), at=0)
    muxer.reconfigure(
        pcr_pid=0x102,
        streams=(ElementaryStreamInfo(0x1B, 0x102, ()),),
        pmt_version_number=1,
    )
    emission = scheduler.poll(at=Fraction(1, 20))
    assert emission is not None
    assert emission.pid == 0x102
    assert parse_transport_packet(emission.packet).pid == 0x102


def test_clock_scheduler_validates_configuration_time_and_lifecycle() -> None:
    muxer = _muxer()
    with pytest.raises(TypeError, match="TransportMuxer"):
        ProgramClockScheduler(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="interval"):
        ProgramClockScheduler(muxer, interval=True)
    with pytest.raises(ValueError, match="positive"):
        ProgramClockScheduler(muxer, interval=0)
    with pytest.raises(ValueError, match=r"0\.1 seconds"):
        ProgramClockScheduler(muxer, interval=Fraction(101, 1000))

    scheduler = ProgramClockScheduler(muxer)
    with pytest.raises(RuntimeError, match="started"):
        scheduler.poll(at=0)
    with pytest.raises(TypeError, match="ProgramClockReference"):
        scheduler.start(object(), at=0)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="discontinuity"):
        scheduler.start(ProgramClockReference(0, 0), at=0, discontinuity=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="finite"):
        scheduler.start(ProgramClockReference(0, 0), at=float("nan"))

    scheduler.start(ProgramClockReference(0, 0), at=1)
    with pytest.raises(RuntimeError, match="already started"):
        scheduler.start(ProgramClockReference(0, 0), at=1)
    with pytest.raises(DecodeError, match="monotonic"):
        scheduler.poll(at=0)
    scheduler.reset()
    assert scheduler.next_due_at is None
    assert scheduler.total_emissions == 0
    assert scheduler.total_missed_repetitions == 0
    assert scheduler.start(ProgramClockReference(1, 0), at=2).pcr.base == 1


def test_pcr_from_unbounded_ticks_wraps_both_directions() -> None:
    assert pcr_from_ticks(PCR_MODULUS + 301) == ProgramClockReference(1, 1)
    assert pcr_from_ticks(-1).ticks == PCR_MODULUS - 1
    with pytest.raises(TypeError, match="timeline ticks"):
        pcr_from_ticks(True)
