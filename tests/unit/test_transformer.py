from __future__ import annotations

from fractions import Fraction

import pytest

from stanag4609.errors import DecodeError
from stanag4609.st0601 import UASLocalSet, encode_uas_local_set, update_uas_local_set
from stanag4609.st0903 import DetectionStatus, VTargetData, encode_vmti_local_set
from stanag4609.transport.demux import (
    PATEvent,
    PESStreamEvent,
    PMTEvent,
    StreamKind,
    TransportDemuxer,
)
from stanag4609.transport.metadata import asynchronous_klv_stream, synchronous_klv_stream
from stanag4609.transport.metadata_stream import MetadataStreamDecoder
from stanag4609.transport.mpegts import ProgramClockReference
from stanag4609.transport.mux import (
    TransportMuxer,
    build_pat_section,
    encode_pes_packet,
)
from stanag4609.transport.processor import MetadataDecision, TimedKLVPacket
from stanag4609.transport.psi import (
    Descriptor,
    ElementaryStreamInfo,
    KLVCarriage,
    ProgramAssociation,
    find_klv_streams,
)
from stanag4609.transport.transformer import (
    LiveTransportTransformer,
    TransformerResetReport,
)


def _streams() -> tuple[ElementaryStreamInfo, ...]:
    return (
        ElementaryStreamInfo(0x1B, 0x101, (Descriptor(0x52, b"video"),)),
        synchronous_klv_stream(
            0x102,
            metadata_input_leak_rate=1_000,
            metadata_buffer_size=200_000,
            metadata_service_id=7,
        ),
        ElementaryStreamInfo(0x03, 0x103, (Descriptor(0x52, b"audio-1"),)),
        ElementaryStreamInfo(0x03, 0x104, (Descriptor(0x52, b"audio-2"),)),
    )


def _source() -> tuple[bytes, dict[int, bytes]]:
    muxer = TransportMuxer(
        transport_stream_id=9,
        program_number=3,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=_streams(),
        descriptors=(Descriptor(0x40, b"program"),),
        version_number=2,
    )
    pes = {
        0x101: encode_pes_packet(b"video-access-unit", stream_id=0xE0, pts=90_000),
        0x103: encode_pes_packet(b"left-audio", stream_id=0xC0, pts=90_000),
        0x104: encode_pes_packet(b"right-audio", stream_id=0xC1, pts=90_000),
    }
    uas = encode_uas_local_set({2: 1_700_000_000_000_000, 65: 19})
    output = b"".join(muxer.program_tables())
    for pid in (0x101, 0x103, 0x104):
        output += b"".join(muxer.mux_pes(pid, pes[pid]))
    output += b"".join(
        muxer.mux_sync_klv(
            0x102,
            uas,
            pts=90_000,
            metadata_service_id=7,
            random_access=True,
        )
    )
    return output, pes


def _truck_vmti() -> bytes:
    return encode_vmti_local_set(
        {4: 6, 8: 1920, 9: 1080},
        targets=(
            VTargetData(
                42,
                {
                    1: 409_600,
                    2: 400_000,
                    3: 420_000,
                    5: 97,
                    19: 872,
                    20: 1137,
                    23: DetectionStatus.ACTIVE_MOVING,
                },
            ),
        ),
    )


def _psi_packet(section: bytes, *, pid: int, continuity_counter: int = 0) -> bytes:
    payload = b"\x00" + section
    adaptation_length = 183 - len(payload)
    return (
        bytes(
            (
                0x47,
                0x40 | (pid >> 8),
                pid & 0xFF,
                0x30 | continuity_counter,
                adaptation_length,
            )
        )
        + (b"\x00" if adaptation_length else b"")
        + b"\xff" * max(0, adaptation_length - 1)
        + payload
    )


def _multi_program_source() -> tuple[bytes, dict[int, bytes]]:
    first = TransportMuxer(
        transport_stream_id=9,
        program_number=3,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=(
            ElementaryStreamInfo(0x1B, 0x101, ()),
            asynchronous_klv_stream(0x102),
        ),
        version_number=1,
    )
    second_streams = (
        ElementaryStreamInfo(0x1B, 0x111, ()),
        asynchronous_klv_stream(0x112),
    )
    second = TransportMuxer(
        transport_stream_id=9,
        program_number=4,
        program_map_pid=0x110,
        pcr_pid=0x111,
        streams=second_streams,
        version_number=1,
    )
    first_tables = first.program_tables()
    second_tables = second.program_tables()
    payloads = {
        0x101: encode_pes_packet(b"program-three", stream_id=0xE0, pts=90_000),
        0x111: encode_pes_packet(b"program-four", stream_id=0xE0, pts=90_000),
    }
    uas = encode_uas_local_set({2: 1, 65: 19})
    source = _psi_packet(
        build_pat_section(
            transport_stream_id=9,
            programs=(ProgramAssociation(3, 0x100), ProgramAssociation(4, 0x110)),
            version_number=1,
        ),
        pid=0,
    )
    source += first_tables[-1] + second_tables[-1]
    source += b"".join(first.mux_pes(0x101, payloads[0x101]))
    source += b"".join(first.mux_async_klv(0x102, uas))
    source += b"".join(second.mux_pes(0x111, payloads[0x111]))
    source += b"".join(second.mux_async_klv(0x112, uas))
    return source, payloads


def test_live_transformer_adds_ai_vmti_and_preserves_video_and_all_audio_pes() -> None:
    source, original_pes = _source()
    vmti = _truck_vmti()

    def add_truck(event: TimedKLVPacket) -> MetadataDecision:
        if not isinstance(event.decoded, UASLocalSet):
            return MetadataDecision.pass_through()
        changed = update_uas_local_set(event.decoded, {74: vmti})
        return MetadataDecision.replace(changed)

    transformer = LiveTransportTransformer((add_truck,))
    output = bytearray()
    tapped: list[TimedKLVPacket] = []
    for index in range(0, len(source), 73):
        batch = transformer.feed(source[index : index + 73])
        output.extend(batch.transport)
        tapped.extend(batch.metadata)
    final = transformer.finish()
    output.extend(final.transport)
    tapped.extend(final.metadata)

    events = TransportDemuxer().feed(output)
    output_pmt = next(event.table for event in events if isinstance(event, PMTEvent))
    assert output_pmt.descriptors == (Descriptor(0x40, b"program"),)
    pes_events = [event for event in events if isinstance(event, PESStreamEvent)]
    media = {event.pid: event for event in pes_events if event.kind is not StreamKind.KLV}
    assert media[0x101].pes.raw == original_pes[0x101]
    assert media[0x103].pes.raw == original_pes[0x103]
    assert media[0x104].pes.raw == original_pes[0x104]

    metadata_decoder = MetadataStreamDecoder()
    metadata = [
        item
        for event in pes_events
        if event.kind is StreamKind.KLV
        for item in metadata_decoder.feed(event)
    ]
    assert len(metadata) == 1
    assert metadata[0].pts == 90_000
    assert metadata[0].metadata_service_id == 7
    assert metadata[0].random_access
    assert isinstance(metadata[0].decoded, UASLocalSet)
    embedded = metadata[0].decoded.value(74)
    assert embedded.targets[0].target_id == 42
    assert embedded.targets[0].value(5) == 97
    assert [bytes(item.packet) for item in tapped] == [bytes(metadata[0].packet)]

    assert transformer.program is not None
    assert transformer.program.descriptors == (Descriptor(0x40, b"program"),)


def test_transform_batch_exposes_input_clock_observations() -> None:
    batch = LiveTransportTransformer().feed(b"")
    assert batch.clocks == ()
    assert isinstance(ProgramClockReference(0, 0).ticks, int)


def test_live_transformer_reset_starts_a_new_transport_session() -> None:
    source, _ = _source()
    transformer = LiveTransportTransformer()
    first = transformer.feed(source)
    assert len(first.metadata) == 1
    transformer.finish()

    report = transformer.reset()

    assert isinstance(report, TransformerResetReport)
    assert report.was_finished
    assert report.had_active_program
    assert report.demux.programs == 1
    assert report.metadata.metadata_pids == 1
    assert transformer.program is None
    assert dict(transformer.klv_streams) == {}

    restarted = transformer.feed(source)
    assert len(restarted.metadata) == 1
    output = restarted.transport + transformer.finish().transport
    events = TransportDemuxer().feed(output)
    assert any(isinstance(event, PATEvent) for event in events)
    assert any(isinstance(event, PMTEvent) for event in events)
    assert transformer.reset().was_finished
    assert not transformer.reset().had_active_program


def test_live_transformer_can_drop_metadata_without_dropping_media() -> None:
    source, _ = _source()
    transformer = LiveTransportTransformer((lambda _: MetadataDecision.drop(),))
    output = transformer.feed(source).transport + transformer.finish().transport
    events = TransportDemuxer().feed(output)
    pes_events = [event for event in events if isinstance(event, PESStreamEvent)]
    assert [event.kind for event in pes_events] == [
        StreamKind.VIDEO,
        StreamKind.AUDIO,
        StreamKind.AUDIO,
    ]


def test_live_transformer_emits_external_timed_metadata_on_active_klv_pid() -> None:
    source, _ = _source()
    transformer = LiveTransportTransformer()
    initial = transformer.feed(source[: 188 * 2])
    assert initial.transport
    event = TimedKLVPacket.from_bytes(
        encode_uas_local_set({2: 1_700_000_000_000_001, 65: 19}),
        program_number=3,
        pid=0x102,
        carriage=transformer.klv_streams[0x102],
        pts=180_000,
        metadata_service_id=7,
        random_access=True,
    )
    injected = transformer.emit_metadata(event)
    assert injected.metadata == (event,)
    demuxed = TransportDemuxer().feed(initial.transport + injected.transport)
    decoded = [
        item
        for stream in demuxed
        if isinstance(stream, PESStreamEvent) and stream.kind is StreamKind.KLV
        for item in MetadataStreamDecoder().feed(stream)
    ]
    assert decoded[0].pts == 180_000


def test_timed_external_metadata_emission_prepends_due_program_tables() -> None:
    source, _ = _source()
    transformer = LiveTransportTransformer()
    transformer.feed(source[: 188 * 2], at=0)
    event = TimedKLVPacket.from_bytes(
        encode_uas_local_set({2: 1_700_000_000_000_001, 65: 19}),
        program_number=3,
        pid=0x102,
        carriage=KLVCarriage.SYNCHRONOUS,
        pts=180_000,
        metadata_service_id=7,
    )
    batch = transformer.emit_metadata(event, at=Fraction(1, 8))
    assert batch.table_emission is not None
    events = TransportDemuxer().feed(batch.transport)
    assert [type(item) for item in events[:2]] == [PATEvent, PMTEvent]
    assert isinstance(events[2], PESStreamEvent)
    assert events[2].pid == 0x102


def test_live_transformer_adds_a_signalled_klva_pid_to_media_only_input() -> None:
    source_muxer = TransportMuxer(
        transport_stream_id=9,
        program_number=3,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=(
            ElementaryStreamInfo(0x1B, 0x101, ()),
            ElementaryStreamInfo(0x03, 0x103, ()),
        ),
        version_number=31,
    )
    source = b"".join(source_muxer.program_tables())
    transformer = LiveTransportTransformer(
        additional_metadata_stream=synchronous_klv_stream(
            0x120,
            metadata_input_leak_rate=1_000,
            metadata_buffer_size=200_000,
            metadata_service_id=7,
        )
    )
    tables = transformer.feed(source).transport
    assert transformer.program is not None
    assert transformer.program.version_number == 0
    assert transformer.klv_streams[0x120] is KLVCarriage.SYNCHRONOUS

    event = TimedKLVPacket.from_bytes(
        encode_uas_local_set({2: 1, 65: 19}),
        program_number=3,
        pid=0x120,
        carriage=KLVCarriage.SYNCHRONOUS,
        pts=90_000,
        metadata_service_id=7,
    )
    injected = transformer.emit_metadata(event).transport
    events = TransportDemuxer().feed(tables + injected)
    output_pmt = next(item.table for item in events if isinstance(item, PMTEvent))
    assert output_pmt.streams[-1].elementary_pid == 0x120
    assert any(
        isinstance(item, PESStreamEvent) and item.pid == 0x120 and item.kind is StreamKind.KLV
        for item in events
    )


def test_live_transformer_cannot_add_second_synchronous_metadata_stream() -> None:
    source, _ = _source()
    transformer = LiveTransportTransformer(
        additional_metadata_stream=synchronous_klv_stream(
            0x120,
            metadata_input_leak_rate=1_000,
            metadata_buffer_size=200_000,
        )
    )

    with pytest.raises(DecodeError, match="ST 1402-13"):
        transformer.feed(source[: 188 * 2])


def test_live_transformer_regenerates_augmented_tables_at_source_cadence() -> None:
    source_muxer = TransportMuxer(
        transport_stream_id=9,
        program_number=3,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=(ElementaryStreamInfo(0x02, 0x101, ()),),
    )
    tables = b"".join(source_muxer.program_tables())
    transformer = LiveTransportTransformer(
        additional_metadata_stream=synchronous_klv_stream(
            0x120,
            metadata_input_leak_rate=1_000,
            metadata_buffer_size=200_000,
        )
    )
    output = transformer.feed(tables + tables).transport
    events = TransportDemuxer().feed(output)
    assert len([event for event in events if isinstance(event, PMTEvent)]) == 2


def test_timed_transformer_schedules_tables_during_source_silence() -> None:
    source_muxer = TransportMuxer(
        transport_stream_id=9,
        program_number=3,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=(ElementaryStreamInfo(0x02, 0x101, ()),),
    )
    source_tables = b"".join(source_muxer.program_tables())
    transformer = LiveTransportTransformer()

    initial = transformer.feed(source_tables, at=10)
    assert initial.table_emission is not None
    assert initial.table_emission.scheduled_at == 10
    assert [
        type(event) for event in TransportDemuxer().feed(initial.transport)
    ] == [PATEvent, PMTEvent]

    early = transformer.feed(b"", at=10.124)
    assert early.transport == b""
    assert early.table_emission is None

    due = transformer.feed(b"", at=10.125)
    assert due.table_emission is not None
    assert due.table_emission.late_by == 0
    assert due.table_emission.interval_compliant
    assert len(TransportDemuxer().feed(due.transport)) == 2

    late = transformer.poll_program_tables(at=10.5)
    assert late.table_emission is not None
    assert late.table_emission.missed_repetitions == 2
    assert not late.table_emission.interval_compliant


def test_timed_transformer_suppresses_early_source_repetitions() -> None:
    source_muxer = TransportMuxer(
        transport_stream_id=9,
        program_number=3,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=(ElementaryStreamInfo(0x02, 0x101, ()),),
    )
    tables = b"".join(source_muxer.program_tables())
    transformer = LiveTransportTransformer()
    first = transformer.feed(tables, at=Fraction(0))
    repeated = transformer.feed(tables, at=Fraction(1, 16))
    due = transformer.feed(tables, at=Fraction(1, 8))
    assert first.table_emission is not None
    assert repeated.transport == b""
    assert repeated.table_emission is None
    assert due.table_emission is not None


def test_explicit_table_poll_requires_active_program_and_respects_lifecycle() -> None:
    transformer = LiveTransportTransformer()
    with pytest.raises(DecodeError, match="active program map"):
        transformer.poll_program_tables(at=0)

    source, _ = _source()
    transformer.feed(source[: 188 * 2])
    polled = transformer.poll_program_tables(at=1)
    assert polled.table_emission is not None
    assert polled.transport == b"".join(polled.table_emission.packets)
    assert transformer.poll_program_tables(at=1.01).transport == b""

    transformer.finish()
    with pytest.raises(RuntimeError, match="finished"):
        transformer.poll_program_tables(at=2)


def test_live_transformer_validates_added_klva_declaration_and_pid() -> None:
    with pytest.raises(ValueError, match="explicitly signal KLVA"):
        LiveTransportTransformer(
            additional_metadata_stream=ElementaryStreamInfo(0x06, 0x120, ())
        )
    with pytest.raises(TypeError, match="ElementaryStreamInfo"):
        LiveTransportTransformer(additional_metadata_stream=object())  # type: ignore[arg-type]

    source, _ = _source()
    transformer = LiveTransportTransformer(
        additional_metadata_stream=asynchronous_klv_stream(0x101)
    )
    with pytest.raises(DecodeError, match="collides"):
        transformer.feed(source[: 188 * 2])


def test_live_transformer_rejects_multiple_programs_explicitly() -> None:
    source, _ = _multi_program_source()
    transformer = LiveTransportTransformer()
    assert transformer.selected_program_number is None

    with pytest.raises(DecodeError, match="program_number"):
        transformer.feed(source)


@pytest.mark.parametrize("program_number", [True, 0, 65_536, "3"])
def test_live_transformer_validates_program_selection(program_number: object) -> None:
    with pytest.raises(ValueError, match="program_number"):
        LiveTransportTransformer(program_number=program_number)  # type: ignore[arg-type]


def test_live_transformer_rejects_missing_selected_program() -> None:
    source, _ = _multi_program_source()
    transformer = LiveTransportTransformer(program_number=5)

    with pytest.raises(DecodeError, match="not present"):
        transformer.feed(source)


def test_live_transformer_keeps_multi_program_input_bounded() -> None:
    source, _ = _multi_program_source()
    transformer = LiveTransportTransformer(program_number=4, max_programs=1)

    with pytest.raises(DecodeError, match="exceeds limit"):
        transformer.feed(source)


def test_live_transformer_selects_one_program_from_multi_program_input() -> None:
    source, payloads = _multi_program_source()
    transformer = LiveTransportTransformer(program_number=4)
    assert transformer.selected_program_number == 4

    batch = transformer.feed(source)
    output = batch.transport + transformer.finish().transport
    assert transformer.selected_program_number == 4

    events = TransportDemuxer().feed(output)
    pat = next(event.table for event in events if isinstance(event, PATEvent))
    assert [(item.program_number, item.program_map_pid) for item in pat.programs] == [
        (4, 0x110)
    ]
    pmts = [event for event in events if isinstance(event, PMTEvent)]
    assert [(event.table.program_number, event.pid) for event in pmts] == [(4, 0x110)]
    pes = [event for event in events if isinstance(event, PESStreamEvent)]
    assert {event.program_number for event in pes} == {4}
    assert next(event.pes.raw for event in pes if event.pid == 0x111) == payloads[0x111]
    assert {event.program_number for event in batch.streams} == {4}
    assert {event.program_number for event in batch.metadata} == {4}


def test_live_transformer_selects_program_from_multisection_pat() -> None:
    source, payloads = _multi_program_source()
    packets = [source[index : index + 188] for index in range(0, len(source), 188)]
    section_one = _psi_packet(
        build_pat_section(
            transport_stream_id=9,
            programs=(ProgramAssociation(4, 0x110),),
            version_number=1,
            section_number=1,
            last_section_number=1,
        ),
        pid=0,
    )
    section_zero = _psi_packet(
        build_pat_section(
            transport_stream_id=9,
            programs=(ProgramAssociation(3, 0x100),),
            version_number=1,
            section_number=0,
            last_section_number=1,
        ),
        pid=0,
        continuity_counter=1,
    )
    transformer = LiveTransportTransformer(program_number=4)

    batch = transformer.feed(section_one + section_zero + b"".join(packets[1:]))
    output = batch.transport + transformer.finish().transport

    events = TransportDemuxer().feed(output)
    pat = next(event for event in events if isinstance(event, PATEvent))
    assert [(item.program_number, item.program_map_pid) for item in pat.programs] == [
        (4, 0x110)
    ]
    video = next(
        event
        for event in events
        if isinstance(event, PESStreamEvent) and event.kind is StreamKind.VIDEO
    )
    assert video.pes.raw == payloads[0x111]


def test_live_transformer_switches_selected_program_to_new_pmt_pid() -> None:
    streams = (
        ElementaryStreamInfo(0x1B, 0x101, ()),
        asynchronous_klv_stream(0x102),
    )
    first = TransportMuxer(
        transport_stream_id=9,
        program_number=3,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=streams,
        pat_version_number=1,
        pmt_version_number=1,
    )
    second = TransportMuxer(
        transport_stream_id=9,
        program_number=3,
        program_map_pid=0x110,
        pcr_pid=0x101,
        streams=streams,
        pat_version_number=2,
        pmt_version_number=2,
    )
    before = encode_pes_packet(b"before", stream_id=0xE0, pts=90_000)
    after = encode_pes_packet(b"after", stream_id=0xE0, pts=180_000)
    transformer = LiveTransportTransformer(program_number=3)

    output = transformer.feed(
        b"".join(first.program_tables()) + b"".join(first.mux_pes(0x101, before))
    ).transport
    output += transformer.feed(
        b"".join(second.program_tables()) + b"".join(second.mux_pes(0x101, after))
    ).transport
    output += transformer.finish().transport

    events = TransportDemuxer().feed(output)
    pats = [event.table for event in events if isinstance(event, PATEvent)]
    assert [table.programs[0].program_map_pid for table in pats] == [0x100, 0x110]
    pmts = [event for event in events if isinstance(event, PMTEvent)]
    assert [event.pid for event in pmts] == [0x100, 0x110]
    assert [
        event.pes.payload
        for event in events
        if isinstance(event, PESStreamEvent) and event.kind is StreamKind.VIDEO
    ] == [b"before", b"after"]


def test_live_transformer_rejects_unfinished_selected_pmt_reassociation() -> None:
    source, _ = _source()
    replacement = TransportMuxer(
        transport_stream_id=9,
        program_number=3,
        program_map_pid=0x110,
        pcr_pid=0x101,
        streams=_streams(),
        pat_version_number=3,
        pmt_version_number=3,
    )
    transformer = LiveTransportTransformer(program_number=3)
    transformer.feed(source)
    transformer.feed(replacement.program_tables()[0])

    with pytest.raises(DecodeError, match=r"before.*PMT arrived"):
        transformer.finish()


def test_live_transformer_applies_continuity_safe_dynamic_pmt_update() -> None:
    source_muxer = TransportMuxer(
        transport_stream_id=9,
        program_number=3,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=_streams(),
        descriptors=(Descriptor(0x40, b"initial"),),
        pat_version_number=5,
        pmt_version_number=2,
    )
    first_uas = encode_uas_local_set({2: 1_700_000_000_000_000, 65: 19})
    first = b"".join(source_muxer.program_tables())
    first += b"".join(
        source_muxer.mux_pes(
            0x101,
            encode_pes_packet(b"video-1", stream_id=0xE0, pts=90_000),
        )
    )
    first += b"".join(
        source_muxer.mux_sync_klv(
            0x102,
            first_uas,
            pts=90_000,
            metadata_service_id=7,
        )
    )

    updated_streams = (*_streams(), ElementaryStreamInfo(0x0F, 0x105, ()))
    source_muxer.reconfigure(
        pcr_pid=0x101,
        streams=updated_streams,
        descriptors=(Descriptor(0x40, b"updated"),),
        pmt_version_number=3,
    )
    second_uas = encode_uas_local_set({2: 1_700_000_000_100_000, 65: 19})
    second = b"".join(source_muxer.program_tables())
    second += b"".join(
        source_muxer.mux_pes(
            0x101,
            encode_pes_packet(b"video-2", stream_id=0xE0, pts=99_000),
        )
    )
    second += b"".join(
        source_muxer.mux_pes(
            0x105,
            encode_pes_packet(b"new-audio", stream_id=0xC2, pts=99_000),
        )
    )
    second += b"".join(
        source_muxer.mux_sync_klv(
            0x102,
            second_uas,
            pts=99_000,
            metadata_service_id=7,
        )
    )

    transformer = LiveTransportTransformer()
    output = transformer.feed(first).transport
    output += transformer.feed(second).transport
    output += transformer.finish().transport

    events = TransportDemuxer().feed(output)
    pats = [event.table for event in events if isinstance(event, PATEvent)]
    pmts = [event.table for event in events if isinstance(event, PMTEvent)]
    assert [table.version_number for table in pats] == [5, 5]
    assert [table.version_number for table in pmts] == [2, 3]
    assert pmts[-1].descriptors == (Descriptor(0x40, b"updated"),)
    assert {stream.elementary_pid for stream in pmts[-1].streams} == {
        0x101,
        0x102,
        0x103,
        0x104,
        0x105,
    }
    pes_events = [event for event in events if isinstance(event, PESStreamEvent)]
    assert [
        event.pes.payload for event in pes_events if event.kind is StreamKind.VIDEO
    ] == [b"video-1", b"video-2"]
    assert any(event.pid == 0x105 for event in pes_events)
    metadata_decoder = MetadataStreamDecoder()
    metadata = [
        item
        for event in pes_events
        if event.kind is StreamKind.KLV
        for item in metadata_decoder.feed(event)
    ]
    assert [bytes(item.packet) for item in metadata] == [first_uas, second_uas]


def test_live_transformer_retains_partial_pes_across_compatible_pmt_update() -> None:
    source_muxer = TransportMuxer(
        transport_stream_id=9,
        program_number=3,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=_streams(),
        descriptors=(Descriptor(0x40, b"initial"),),
        pmt_version_number=2,
    )
    payload = bytes(range(256)) * 2
    pes = encode_pes_packet(payload, stream_id=0xE0, pts=90_000)
    pes_packets = source_muxer.mux_pes(0x101, pes)
    assert len(pes_packets) > 1
    transformer = LiveTransportTransformer()

    output = transformer.feed(
        b"".join(source_muxer.program_tables()) + pes_packets[0]
    ).transport
    source_muxer.reconfigure(
        pcr_pid=0x101,
        streams=_streams(),
        descriptors=(Descriptor(0x40, b"updated"),),
        pmt_version_number=3,
    )
    output += transformer.feed(
        b"".join(source_muxer.program_tables()) + b"".join(pes_packets[1:])
    ).transport
    output += transformer.finish().transport

    events = TransportDemuxer().feed(output)
    pmts = [event.table for event in events if isinstance(event, PMTEvent)]
    assert [table.version_number for table in pmts] == [2, 3]
    assert pmts[-1].descriptors == (Descriptor(0x40, b"updated"),)
    video = next(
        event
        for event in events
        if isinstance(event, PESStreamEvent) and event.kind is StreamKind.VIDEO
    )
    assert video.pes.raw == pes


def test_live_transformer_rejects_unversioned_dynamic_pmt_change() -> None:
    source_muxer = TransportMuxer(
        transport_stream_id=9,
        program_number=3,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=_streams(),
        pmt_version_number=2,
    )
    transformer = LiveTransportTransformer()
    transformer.feed(b"".join(source_muxer.program_tables()))

    source_muxer.reconfigure(
        pcr_pid=0x101,
        streams=(*_streams(), ElementaryStreamInfo(0x0F, 0x105, ())),
        pmt_version_number=2,
    )
    with pytest.raises(DecodeError, match="without a PMT version change"):
        transformer.feed(b"".join(source_muxer.program_tables()))


def test_live_transformer_applies_clean_dynamic_klva_topology_changes() -> None:
    source_muxer = TransportMuxer(
        transport_stream_id=9,
        program_number=3,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=_streams(),
        pmt_version_number=2,
    )
    transformer = LiveTransportTransformer()
    first_klv = encode_uas_local_set({2: 1, 65: 19})
    first = b"".join(source_muxer.program_tables()) + b"".join(
        source_muxer.mux_sync_klv(
            0x102,
            first_klv,
            pts=90_000,
            metadata_service_id=7,
        )
    )
    output = transformer.feed(first).transport
    changed_klv = tuple(
        asynchronous_klv_stream(0x102)
        if stream.elementary_pid == 0x102
        else stream
        for stream in _streams()
    )
    source_muxer.reconfigure(
        pcr_pid=0x101,
        streams=changed_klv,
        pmt_version_number=3,
    )
    second_klv = encode_uas_local_set({2: 2, 65: 19})
    changed = b"".join(source_muxer.program_tables()) + b"".join(
        source_muxer.mux_async_klv(0x102, second_klv)
    )
    output += transformer.feed(changed).transport

    without_klv = tuple(
        stream for stream in changed_klv if stream.elementary_pid != 0x102
    )
    source_muxer.reconfigure(
        pcr_pid=0x101,
        streams=without_klv,
        pmt_version_number=4,
    )
    output += transformer.feed(b"".join(source_muxer.program_tables())).transport

    added = (*without_klv, asynchronous_klv_stream(0x106))
    source_muxer.reconfigure(
        pcr_pid=0x101,
        streams=added,
        pmt_version_number=5,
    )
    third_klv = encode_uas_local_set({2: 3, 65: 19})
    added_data = b"".join(source_muxer.program_tables()) + b"".join(
        source_muxer.mux_async_klv(0x106, third_klv)
    )
    output += transformer.feed(added_data).transport
    output += transformer.finish().transport

    events = TransportDemuxer().feed(output)
    pmts = [event.table for event in events if isinstance(event, PMTEvent)]
    assert [table.version_number for table in pmts] == [2, 3, 4, 5]
    assert dict(transformer.klv_streams) == {0x106: KLVCarriage.ASYNCHRONOUS}
    decoder = MetadataStreamDecoder()
    decoded = []
    for event in events:
        if isinstance(event, PMTEvent):
            decoder.reconfigure(dict(find_klv_streams(event.table)))
        elif isinstance(event, PESStreamEvent) and event.kind is StreamKind.KLV:
            decoded.extend(decoder.feed(event))
    assert [bytes(item.packet) for item in decoded] == [first_klv, second_klv, third_klv]


def test_live_transformer_rejects_klva_change_during_partial_item() -> None:
    streams = (
        ElementaryStreamInfo(0x1B, 0x101, ()),
        asynchronous_klv_stream(0x102),
    )
    source_muxer = TransportMuxer(
        transport_stream_id=9,
        program_number=3,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=streams,
        pmt_version_number=2,
    )
    klv = encode_uas_local_set({2: 1, 65: 19})
    partial = b"".join(source_muxer.program_tables()) + b"".join(
        source_muxer.mux_pes(
            0x102,
            encode_pes_packet(
                klv[:20],
                stream_id=0xBD,
                data_alignment_indicator=True,
            ),
        )
    )
    transformer = LiveTransportTransformer()
    transformer.feed(partial)

    source_muxer.reconfigure(
        pcr_pid=0x101,
        streams=(streams[0],),
        pmt_version_number=3,
    )
    with pytest.raises(DecodeError, match="partial asynchronous KLV item"):
        transformer.feed(b"".join(source_muxer.program_tables()))


def test_live_transformer_round_trips_asynchronous_klv() -> None:
    streams = (
        ElementaryStreamInfo(0x1B, 0x101, ()),
        asynchronous_klv_stream(0x102),
    )
    muxer = TransportMuxer(
        transport_stream_id=1,
        program_number=1,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=streams,
    )
    klv = encode_uas_local_set({2: 1, 65: 19})
    source = b"".join(muxer.program_tables()) + b"".join(muxer.mux_async_klv(0x102, klv))
    transformer = LiveTransportTransformer()
    batch = transformer.feed(source)
    events = TransportDemuxer().feed(batch.transport)
    metadata_event = next(
        event
        for event in events
        if isinstance(event, PESStreamEvent) and event.kind is StreamKind.KLV
    )
    decoded = MetadataStreamDecoder().feed(metadata_event)
    assert bytes(decoded[0].packet) == klv
    assert decoded[0].pts is None


def test_live_transformer_validates_external_metadata_context_and_lifecycle() -> None:
    data = encode_uas_local_set({2: 1, 65: 19})
    event = TimedKLVPacket.from_bytes(
        data,
        program_number=3,
        pid=0x102,
        carriage=KLVCarriage.SYNCHRONOUS,
        pts=90_000,
        metadata_service_id=7,
    )
    transformer = LiveTransportTransformer()
    with pytest.raises(DecodeError, match="active program map"):
        transformer.emit_metadata(event)

    source, _ = _source()
    transformer.feed(source[: 188 * 2])
    wrong_program = TimedKLVPacket.from_bytes(
        data,
        program_number=4,
        pid=0x102,
        carriage=KLVCarriage.SYNCHRONOUS,
        pts=90_000,
        metadata_service_id=7,
    )
    with pytest.raises(DecodeError, match="does not match"):
        transformer.emit_metadata(wrong_program)
    wrong_pid = TimedKLVPacket.from_bytes(
        data,
        program_number=3,
        pid=0x105,
        carriage=KLVCarriage.SYNCHRONOUS,
        pts=90_000,
        metadata_service_id=7,
    )
    with pytest.raises(DecodeError, match="not an active KLVA"):
        transformer.emit_metadata(wrong_pid)
    wrong_carriage = TimedKLVPacket.from_bytes(
        data,
        program_number=3,
        pid=0x102,
        carriage=KLVCarriage.ASYNCHRONOUS,
        pts=None,
    )
    with pytest.raises(DecodeError, match="does not match PID"):
        transformer.emit_metadata(wrong_carriage)

    transformer.finish()
    assert transformer.finish().transport == b""
    with pytest.raises(RuntimeError, match="finished"):
        transformer.feed(b"")
    with pytest.raises(RuntimeError, match="finished"):
        transformer.emit_metadata(event)
