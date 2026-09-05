from __future__ import annotations

from fractions import Fraction

import pytest

from stanag4609.errors import DecodeError
from stanag4609.transport.demux import PATEvent, PMTEvent, TransportDemuxer
from stanag4609.transport.mpegts import TransportStreamParser
from stanag4609.transport.mux import ProgramTableScheduler, TransportMuxer
from stanag4609.transport.psi import ElementaryStreamInfo, PSICadenceValidator


def _muxer() -> TransportMuxer:
    return TransportMuxer(
        transport_stream_id=1,
        program_number=1,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=(ElementaryStreamInfo(0x1B, 0x101, ()),),
    )


def test_scheduler_emits_recommended_eight_hz_without_drift() -> None:
    muxer = _muxer()
    scheduler = ProgramTableScheduler(muxer)
    assert scheduler.interval == Fraction(1, 8)
    assert scheduler.next_due_at is None

    first = scheduler.poll(at=100)
    assert first is not None
    assert first.scheduled_at == Fraction(100)
    assert first.emitted_at == Fraction(100)
    assert first.previous_emitted_at is None
    assert first.late_by == 0
    assert first.missed_repetitions == 0
    assert first.interval_compliant
    parsed = TransportStreamParser().feed(b"".join(first.packets))
    assert [packet.pid for packet in parsed] == [0, 0x100]
    assert scheduler.next_due_at == Fraction(801, 8)

    assert scheduler.poll(at=100.124) is None
    second = scheduler.poll(at=100.125)
    assert second is not None
    assert second.scheduled_at == Fraction(801, 8)
    assert second.late_by == 0
    assert second.elapsed_since_emission == Fraction(1, 8)
    assert scheduler.total_emissions == 2
    assert muxer.continuity_counters[0] == 2
    assert muxer.continuity_counters[0x100] == 2


def test_scheduled_tables_round_trip_through_demux_and_cadence_validator() -> None:
    scheduler = ProgramTableScheduler(_muxer())
    demuxer = TransportDemuxer()
    validator = PSICadenceValidator()
    validator.start(at=0)

    for timestamp in (Fraction(0), Fraction(1, 8), Fraction(1, 4), Fraction(3, 8)):
        emission = scheduler.poll(at=timestamp)
        assert emission is not None
        events = demuxer.feed(b"".join(emission.packets))
        issues = []
        for event in events:
            if isinstance(event, PATEvent):
                issues.extend(validator.observe_pat(event.table, at=timestamp))
            elif isinstance(event, PMTEvent):
                issues.extend(validator.observe_pmt(event.table, at=timestamp))
        assert issues == []

    assert validator.check(at=Fraction(1, 2) - Fraction(1, 1000)) == ()


def test_late_polling_reports_skipped_slots_and_st1402_violation() -> None:
    scheduler = ProgramTableScheduler(_muxer())
    assert scheduler.poll(at=0) is not None

    late = scheduler.poll(at=0.49)
    assert late is not None
    assert late.scheduled_at == Fraction(3, 8)
    assert late.emitted_at == Fraction(49, 100)
    assert late.late_by == Fraction(23, 200)
    assert late.missed_repetitions == 2
    assert late.elapsed_since_emission == Fraction(49, 100)
    assert not late.interval_compliant
    assert scheduler.total_missed_repetitions == 2
    assert scheduler.next_due_at == Fraction(1, 2)

    recovered = scheduler.poll(at=0.5)
    assert recovered is not None
    assert recovered.missed_repetitions == 0
    assert recovered.interval_compliant


def test_compliant_late_poll_need_not_meet_recommended_slot() -> None:
    scheduler = ProgramTableScheduler(_muxer())
    scheduler.poll(at=0)
    emission = scheduler.poll(at=0.2)
    assert emission is not None
    assert emission.late_by == Fraction(3, 40)
    assert emission.missed_repetitions == 0
    assert emission.interval_compliant


def test_scheduler_reset_reanchors_time_without_rewinding_mux_continuity() -> None:
    muxer = _muxer()
    scheduler = ProgramTableScheduler(muxer, interval=0.2)
    scheduler.poll(at=10)
    assert scheduler.next_due_at == Fraction(51, 5)
    scheduler.reset()
    assert scheduler.next_due_at is None
    assert scheduler.total_emissions == 0
    assert scheduler.total_missed_repetitions == 0

    scheduler.poll(at=20)
    assert muxer.continuity_counters[0] == 2


def test_scheduler_rejects_noncompliant_configuration_and_time() -> None:
    muxer = _muxer()
    with pytest.raises(TypeError, match="TransportMuxer"):
        ProgramTableScheduler(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="interval"):
        ProgramTableScheduler(muxer, interval=True)
    with pytest.raises(ValueError, match="positive"):
        ProgramTableScheduler(muxer, interval=0)
    with pytest.raises(ValueError, match="less than"):
        ProgramTableScheduler(muxer, interval=0.25)

    scheduler = ProgramTableScheduler(muxer)
    with pytest.raises(ValueError, match="finite"):
        scheduler.poll(at=float("nan"))
    scheduler.poll(at=1)
    with pytest.raises(DecodeError, match="monotonic"):
        scheduler.poll(at=0)
