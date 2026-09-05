from __future__ import annotations

from dataclasses import replace

import pytest

from stanag4609.transport.demux import PESStreamEvent, ProgramClockEvent, StreamKind
from stanag4609.transport.metadata import (
    MetadataSTDDescriptor,
    asynchronous_klv_stream,
    encode_metadata_au_cell,
    synchronous_klv_stream,
)
from stanag4609.transport.mpegts import parse_transport_packet
from stanag4609.transport.mux import encode_pcr_packet, encode_pes_packet, packetize_pes
from stanag4609.transport.pcr import pcr_from_ticks
from stanag4609.transport.pes import parse_pes_packet
from stanag4609.transport.psi import (
    ElementaryStreamInfo,
    KLVCarriage,
    ProgramMapTable,
)
from stanag4609.transport.std_stream import MetadataSTDStreamValidator


def _descriptor(
    *,
    buffer_bytes: int = 1_024,
    output_bits_per_second: int = 0,
) -> MetadataSTDDescriptor:
    return MetadataSTDDescriptor.from_physical(
        input_bits_per_second=1_600_000,
        buffer_bytes=buffer_bytes,
        output_bits_per_second=output_bits_per_second,
    )


def _pmt(descriptor: MetadataSTDDescriptor) -> ProgramMapTable:
    return ProgramMapTable(
        1,
        0,
        True,
        0,
        0,
        0x101,
        (),
        (
            ElementaryStreamInfo(0x1B, 0x101, ()),
            synchronous_klv_stream(0x102, metadata_std=descriptor),
        ),
        b"pmt",
    )


def _asynchronous_pmt() -> ProgramMapTable:
    return ProgramMapTable(
        1,
        0,
        True,
        0,
        0,
        0x101,
        (),
        (
            ElementaryStreamInfo(0x1B, 0x101, ()),
            asynchronous_klv_stream(0x102),
        ),
        b"async-pmt",
    )


def _clock(ticks: int, *, offset: int, discontinuity: bool = False) -> ProgramClockEvent:
    packet = parse_transport_packet(
        encode_pcr_packet(
            pid=0x101,
            pcr=pcr_from_ticks(ticks),
            discontinuity=discontinuity,
        ),
        offset=offset,
    )
    assert packet.pcr is not None
    return ProgramClockEvent(1, packet.pcr, None, discontinuity, packet)


def _pes(
    data: bytes,
    *,
    offset: int,
    pts: int,
) -> PESStreamEvent:
    raw = encode_pes_packet(
        encode_metadata_au_cell(data),
        stream_id=0xFC,
        pts=pts,
    )
    encoded, _ = packetize_pes(raw, pid=0x102)
    packets = tuple(
        parse_transport_packet(packet, offset=offset + index * 188)
        for index, packet in enumerate(encoded)
    )
    return PESStreamEvent(
        1,
        synchronous_klv_stream(0x102, metadata_std=_descriptor()),
        StreamKind.KLV,
        KLVCarriage.SYNCHRONOUS,
        parse_pes_packet(raw, offset=offset, transport_packets=packets),
    )


def _asynchronous_pes(data: bytes, *, offset: int) -> PESStreamEvent:
    raw = encode_pes_packet(data, stream_id=0xBD, data_alignment_indicator=True)
    encoded, _ = packetize_pes(raw, pid=0x102)
    packets = tuple(
        parse_transport_packet(packet, offset=offset + index * 188)
        for index, packet in enumerate(encoded)
    )
    return PESStreamEvent(
        1,
        asynchronous_klv_stream(0x102),
        StreamKind.KLV,
        KLVCarriage.ASYNCHRONOUS,
        parse_pes_packet(raw, offset=offset, transport_packets=packets),
    )


def _after(event: PESStreamEvent) -> int:
    packet = event.pes.transport_packets[-1]
    return packet.offset + len(packet.raw)


def test_stream_validator_resolves_pes_at_following_pcr_with_bounded_state() -> None:
    validator = MetadataSTDStreamValidator()
    validator.observe_pmt(_pmt(_descriptor(buffer_bytes=16 * 1_024)))
    validator.observe_clock(_clock(0, offset=0))
    event = _pes(b"metadata", offset=188, pts=45_000)

    assert validator.observe_pes(event) == ()
    assert validator.pending_pes == 1
    assert validator.pending_transport_bytes == 188
    assert validator.observe_clock(_clock(2_700_000, offset=_after(event))) == ()
    assert validator.pending_pes == 0
    assert validator.pending_transport_bytes == 0
    assert validator.observed_pes == validator.exact_pes == 1
    assert validator.finish() == ()
    assert validator.compliant


def test_stream_validator_preserves_main_buffer_occupancy_across_pcr_windows() -> None:
    validator = MetadataSTDStreamValidator()
    validator.observe_pmt(_pmt(_descriptor()))
    validator.observe_clock(_clock(0, offset=0))

    first = _pes(b"a" * 600, offset=188, pts=270_000)
    validator.observe_pes(first)
    first_clock_offset = _after(first)
    assert validator.observe_clock(_clock(2_700_000, offset=first_clock_offset)) == ()

    second = _pes(b"b" * 600, offset=first_clock_offset + 188, pts=270_000)
    validator.observe_pes(second)
    issues = validator.observe_clock(_clock(5_400_000, offset=_after(second)))

    assert any(issue.code == "main_buffer_overflow" for issue in issues)
    assert validator.exact_pes == 2
    assert validator.violations == 1
    validator.finish()
    assert not validator.compliant


def test_stream_validator_resolves_configured_asynchronous_pes_live() -> None:
    descriptor = _descriptor(output_bits_per_second=400)
    validator = MetadataSTDStreamValidator(
        asynchronous_descriptors={(1, 0x102): descriptor}
    )
    validator.observe_pmt(_asynchronous_pmt())
    validator.observe_clock(_clock(0, offset=0))
    event = _asynchronous_pes(b"x" * 1_200, offset=188)

    assert validator.observe_pes(event) == ()
    issues = validator.observe_clock(_clock(27_000_000, offset=_after(event)))

    assert any(issue.code == "main_buffer_overflow" for issue in issues)
    assert any(issue.code == "excessive_delay" for issue in issues)
    assert validator.observed_pes == validator.exact_pes == 1
    validator.finish()
    assert not validator.compliant


def test_stream_validator_does_not_invent_asynchronous_std_parameters() -> None:
    validator = MetadataSTDStreamValidator()
    validator.observe_pmt(_asynchronous_pmt())
    validator.observe_clock(_clock(0, offset=0))

    validator.observe_pes(_asynchronous_pes(b"metadata", offset=188))

    assert validator.observed_pes == 1
    assert validator.unverifiable_pes == 1
    assert not validator.compliant


def test_stream_validator_bounds_pending_pes_without_claiming_exact_coverage() -> None:
    validator = MetadataSTDStreamValidator(max_pending_pes=1)
    validator.observe_pmt(_pmt(_descriptor()))
    validator.observe_clock(_clock(0, offset=0))
    first = _pes(b"first", offset=188, pts=45_000)
    second = _pes(b"second", offset=_after(first), pts=54_000)

    validator.observe_pes(first)
    validator.observe_pes(second)

    assert validator.pending_pes == 1
    assert validator.unverifiable_pes == 1
    validator.observe_clock(_clock(2_700_000, offset=_after(second)))
    assert validator.unverifiable_pes == 2
    assert validator.exact_pes == 0
    assert not validator.compliant


def test_stream_validator_resets_pending_state_at_discontinuity() -> None:
    validator = MetadataSTDStreamValidator()
    validator.observe_pmt(_pmt(_descriptor()))
    validator.observe_clock(_clock(0, offset=0))
    validator.observe_pes(_pes(b"pending", offset=188, pts=45_000))

    assert validator.observe_clock(
        _clock(9_000_000, offset=376, discontinuity=True)
    ) == ()
    assert validator.pending_pes == 0
    assert validator.unverifiable_pes == 1


def test_stream_validator_rejects_invalid_limits() -> None:
    for name in (
        "max_pending_pes",
        "max_pending_transport_bytes",
        "max_pending_timeline_events",
        "max_pending_removal_groups",
    ):
        try:
            MetadataSTDStreamValidator(**{name: 0})
        except ValueError as error:
            assert name in str(error)
        else:  # pragma: no cover - assertion helper
            raise AssertionError(f"{name} accepted zero")
    with pytest.raises(ValueError, match="at least two"):
        MetadataSTDStreamValidator(max_clock_points=1)
    with pytest.raises(TypeError, match="mapping"):
        MetadataSTDStreamValidator(asynchronous_descriptors=())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="program, PID"):
        MetadataSTDStreamValidator(
            asynchronous_descriptors={(1,): _descriptor()}  # type: ignore[dict-item]
        )
    with pytest.raises(ValueError, match="program number"):
        MetadataSTDStreamValidator(
            asynchronous_descriptors={(0, 0x102): _descriptor()}
        )
    with pytest.raises(ValueError, match="PID"):
        MetadataSTDStreamValidator(
            asynchronous_descriptors={(1, 0x2000): _descriptor()}
        )
    with pytest.raises(TypeError, match="MetadataSTDDescriptor"):
        MetadataSTDStreamValidator(
            asynchronous_descriptors={(1, 0x102): object()}  # type: ignore[dict-item]
        )


def test_stream_validator_validates_event_types_and_ignores_other_pes() -> None:
    validator = MetadataSTDStreamValidator()
    with pytest.raises(TypeError, match="ProgramMapTable"):
        validator.observe_pmt(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="PESStreamEvent"):
        validator.observe_pes(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ProgramClockEvent"):
        validator.observe_clock(object())  # type: ignore[arg-type]

    event = _pes(b"metadata", offset=188, pts=45_000)
    assert validator.observe_pes(replace(event, kind=StreamKind.VIDEO)) == ()
    assert validator.observed_pes == 0

    validator.finish()
    with pytest.raises(RuntimeError, match="finished"):
        validator.finish()
    with pytest.raises(RuntimeError, match="finished"):
        validator.observe_pes(event)


def test_stream_validator_marks_missing_context_and_oversized_pes_unverifiable() -> None:
    event = _pes(b"metadata", offset=188, pts=45_000)
    no_config = MetadataSTDStreamValidator()
    no_config.observe_pes(event)
    assert no_config.unverifiable_pes == 1

    no_clock = MetadataSTDStreamValidator()
    no_clock.observe_pmt(_pmt(_descriptor()))
    no_clock.observe_pes(event)
    assert no_clock.unverifiable_pes == 1

    oversized = MetadataSTDStreamValidator(max_pending_transport_bytes=100)
    oversized.observe_pmt(_pmt(_descriptor()))
    oversized.observe_clock(_clock(0, offset=0))
    oversized.observe_pes(event)
    assert oversized.unverifiable_pes == 1


def test_stream_validator_descriptor_change_disables_exact_epoch() -> None:
    validator = MetadataSTDStreamValidator()
    validator.observe_pmt(_pmt(_descriptor()))
    validator.observe_pmt(_pmt(_descriptor(buffer_bytes=2 * 1_024)))
    validator.observe_clock(_clock(0, offset=0))
    validator.observe_pes(_pes(b"metadata", offset=188, pts=45_000))

    assert validator.unverifiable_pes == 1
    assert not validator.compliant


def test_stream_validator_turns_model_resource_limit_into_coverage_gap() -> None:
    validator = MetadataSTDStreamValidator(max_pending_timeline_events=1)
    validator.observe_pmt(_pmt(_descriptor()))
    validator.observe_clock(_clock(0, offset=0))
    event = _pes(b"metadata", offset=188, pts=45_000)
    validator.observe_pes(event)
    validator.observe_clock(_clock(2_700_000, offset=_after(event)))

    assert validator.exact_pes == 0
    assert validator.unverifiable_pes == 1
    assert not validator.compliant


def test_stream_validator_demotes_prior_window_when_state_limit_abandons_epoch() -> None:
    validator = MetadataSTDStreamValidator(max_pending_timeline_events=50)
    validator.observe_pmt(_pmt(_descriptor()))
    validator.observe_clock(_clock(0, offset=0))
    first = _pes(b"a", offset=188, pts=270_000)
    validator.observe_pes(first)
    clock_offset = _after(first)
    validator.observe_clock(_clock(2_700_000, offset=clock_offset))
    assert validator.exact_pes == 1

    second = _pes(b"b" * 30, offset=clock_offset + 188, pts=270_000)
    validator.observe_pes(second)
    validator.observe_clock(_clock(5_400_000, offset=_after(second)))

    assert validator.exact_pes == 0
    assert validator.unverifiable_pes == 2


def test_stream_validator_resets_on_clock_regression_and_trims_stale_bracket() -> None:
    regression = MetadataSTDStreamValidator()
    regression.observe_clock(_clock(2_700_000, offset=0))
    regression.observe_clock(_clock(2_700_000, offset=188))
    assert regression.observe_clock(_clock(5_400_000, offset=376)) == ()

    validator = MetadataSTDStreamValidator(max_clock_points=2)
    validator.observe_pmt(_pmt(_descriptor()))
    validator.observe_clock(_clock(0, offset=0))
    event = _pes(b"x" * 300, offset=188, pts=45_000)
    packets = event.pes.transport_packets
    delayed = replace(
        event,
        pes=replace(
            event.pes,
            transport_packets=(packets[0], replace(packets[1], offset=10_000)),
        ),
    )
    validator.observe_pes(delayed)
    validator.observe_clock(_clock(900_000, offset=376))
    validator.observe_clock(_clock(1_800_000, offset=564))

    assert validator.pending_pes == 0
    assert validator.unverifiable_pes == 1
