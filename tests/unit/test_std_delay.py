from __future__ import annotations

from fractions import Fraction

import pytest

from stanag4609.transport.demux import PESStreamEvent, ProgramClockEvent, StreamKind
from stanag4609.transport.mpegts import ProgramClockReference, TransportPacket
from stanag4609.transport.mux import encode_pes_packet
from stanag4609.transport.pcr import PCR_CLOCK_RATE, PCR_MODULUS
from stanag4609.transport.pes import parse_pes_packet
from stanag4609.transport.psi import ElementaryStreamInfo, KLVCarriage
from stanag4609.transport.std import ST1402_MAX_METADATA_DELAY, MetadataDelayValidator
from stanag4609.transport.timing import PTS_CLOCK_RATE, PTS_MODULUS


def _packet(offset: int, *, pid: int, discontinuity: bool = False) -> TransportPacket:
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
        discontinuity_indicator=discontinuity,
        pcr=None,
        opcr=None,
    )


def _clock(
    ticks: int,
    *,
    offset: int,
    program_number: int = 1,
    discontinuity: bool = False,
) -> ProgramClockEvent:
    wrapped = ticks % PCR_MODULUS
    base, extension = divmod(wrapped, 300)
    source = _packet(offset, pid=0x101, discontinuity=discontinuity)
    pcr = ProgramClockReference(base, extension)
    return ProgramClockEvent(program_number, pcr, None, discontinuity, source)


def _metadata(
    pts: int | None,
    *,
    offset: int,
    program_number: int = 1,
    carriage: KLVCarriage = KLVCarriage.SYNCHRONOUS,
) -> PESStreamEvent:
    packet = _packet(offset, pid=0x102)
    pes = parse_pes_packet(
        encode_pes_packet(b"metadata", stream_id=0xFC, pts=pts),
        offset=offset,
        transport_packets=(packet,),
    )
    return PESStreamEvent(
        program_number,
        ElementaryStreamInfo(0x15, 0x102, ()),
        StreamKind.KLV,
        carriage,
        pes,
    )


def test_metadata_delay_accepts_exact_boundary_and_reports_one_tick_over() -> None:
    validator = MetadataDelayValidator()
    assert validator.maximum_delay == ST1402_MAX_METADATA_DELAY == Fraction(1)
    validator.observe_clock(_clock(PCR_CLOCK_RATE // 10, offset=188))

    assert validator.observe_pes(_metadata(99_000, offset=188)) == ()
    issue = validator.observe_pes(_metadata(99_001, offset=188))[0]
    assert issue.code == "excessive_delay"
    assert issue.requirement == "ST 1402.2 ST 1402-12"
    assert issue.minimum_delay == Fraction(90_001, PTS_CLOCK_RATE)
    assert issue.maximum_delay == Fraction(90_001, PTS_CLOCK_RATE)
    assert issue.source_offset == 188
    assert validator.compliant_pes == 1
    assert validator.violating_pes == 1


def test_metadata_delay_uses_conservative_pcr_brackets() -> None:
    validator = MetadataDelayValidator()
    validator.observe_clock(_clock(0, offset=0))
    validator.observe_clock(_clock(PCR_CLOCK_RATE // 10, offset=376))

    assert validator.observe_pes(_metadata(81_000, offset=188)) == ()
    issue = validator.observe_pes(_metadata(99_001, offset=188))[0]
    assert issue.minimum_delay == Fraction(90_001, PTS_CLOCK_RATE)
    assert issue.maximum_delay == Fraction(99_001, PTS_CLOCK_RATE)

    assert validator.observe_pes(_metadata(94_500, offset=188)) == ()
    assert validator.compliant_pes == 1
    assert validator.indeterminate_pes == 1
    assert validator.violating_pes == 1


def test_metadata_delay_reports_metadata_that_is_provably_late() -> None:
    validator = MetadataDelayValidator()
    validator.observe_clock(_clock(PCR_CLOCK_RATE, offset=0))
    validator.observe_clock(_clock(PCR_CLOCK_RATE * 11 // 10, offset=376))
    issue = validator.observe_pes(_metadata(81_000, offset=188))[0]
    assert issue.code == "late"
    assert issue.maximum_delay == Fraction(-9_000, PTS_CLOCK_RATE)


def test_metadata_delay_resolves_pending_pes_and_bounds_unverifiable_state() -> None:
    validator = MetadataDelayValidator(max_pending_pes=1)
    validator.observe_clock(_clock(0, offset=0))
    assert validator.observe_pes(_metadata(81_000, offset=188)) == ()
    assert validator.pending_pes == 1
    assert validator.observe_pes(_metadata(81_000, offset=376)) == ()
    assert validator.pending_pes == 1
    assert validator.unverifiable_pes == 1

    assert validator.observe_clock(_clock(PCR_CLOCK_RATE // 10, offset=564)) == ()
    assert validator.pending_pes == 0
    assert validator.compliant_pes == 1


def test_metadata_delay_handles_rollover_discontinuity_and_irrelevant_pes() -> None:
    validator = MetadataDelayValidator()
    start = PCR_MODULUS - PCR_CLOCK_RATE // 20
    validator.observe_clock(_clock(start, offset=0))
    validator.observe_clock(_clock(PCR_CLOCK_RATE // 20, offset=376))
    pts = (PTS_MODULUS - PTS_CLOCK_RATE // 20 + PTS_CLOCK_RATE) % PTS_MODULUS
    assert validator.observe_pes(_metadata(pts, offset=188)) == ()

    validator.observe_pes(_metadata(0, offset=564))
    assert validator.pending_pes == 1
    validator.observe_clock(_clock(0, offset=752, discontinuity=True))
    assert validator.pending_pes == 0
    assert validator.unverifiable_pes == 1

    assert validator.observe_pes(
        _metadata(None, offset=940, carriage=KLVCarriage.SYNCHRONOUS)
    ) == ()
    assert validator.observe_pes(
        _metadata(0, offset=1128, carriage=KLVCarriage.ASYNCHRONOUS)
    ) == ()


def test_metadata_delay_validates_api_and_configuration() -> None:
    with pytest.raises(TypeError, match="Fraction"):
        MetadataDelayValidator(maximum_delay=True)
    with pytest.raises(ValueError, match="positive"):
        MetadataDelayValidator(maximum_delay=0)
    with pytest.raises(ValueError, match="finite"):
        MetadataDelayValidator(maximum_delay=float("inf"))
    with pytest.raises(ValueError, match="max_clock_points"):
        MetadataDelayValidator(max_clock_points=1)
    with pytest.raises(ValueError, match="max_pending_pes"):
        MetadataDelayValidator(max_pending_pes=0)

    validator = MetadataDelayValidator()
    with pytest.raises(TypeError, match="ProgramClockEvent"):
        validator.observe_clock(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="PESStreamEvent"):
        validator.observe_pes(object())  # type: ignore[arg-type]

    validator.observe_clock(_clock(0, offset=0, program_number=2))
    validator.reset(program_number=2)
    assert validator.programs == ()
    with pytest.raises(ValueError, match="program_number"):
        validator.reset(program_number=0)
