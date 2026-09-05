from __future__ import annotations

import pytest

from stanag4609.errors import DecodeError
from stanag4609.transport.demux import (
    PATEvent,
    PESStreamEvent,
    PMTEvent,
    ProgramClockEvent,
    StreamKind,
    TransportDemuxer,
)
from stanag4609.transport.metadata import (
    asynchronous_klv_stream,
    parse_metadata_au_cells,
    synchronous_klv_stream,
)
from stanag4609.transport.mpegts import (
    ProgramClockReference,
    TransportStreamParser,
    encode_program_clock_reference,
    parse_transport_packet,
)
from stanag4609.transport.mux import (
    TransportMuxer,
    build_pat_section,
    build_pmt_section,
    encode_pcr_packet,
    encode_pes_packet,
    encode_pts,
    packetize_pes,
    packetize_pes_with_layout,
)
from stanag4609.transport.pes import PESAssembler, parse_pes_packet
from stanag4609.transport.psi import (
    Descriptor,
    ElementaryStreamInfo,
    KLVCarriage,
    ProgramAssociation,
    find_klv_streams,
    parse_pat,
    parse_pmt,
)


def test_encode_pts_and_pes_round_trip() -> None:
    assert encode_pts(90_000, prefix=0x2) == bytes.fromhex("210005BF21")
    raw = encode_pes_packet(b"metadata", stream_id=0xFC, pts=90_000)
    packet = parse_pes_packet(raw)
    assert packet.stream_id == 0xFC
    assert packet.pts == 90_000
    assert packet.dts is None
    assert packet.data_alignment_indicator
    assert packet.payload == b"metadata"

    with_dts = parse_pes_packet(
        encode_pes_packet(b"video", stream_id=0xE0, pts=90_000, dts=45_000)
    )
    assert (with_dts.pts, with_dts.dts) == (90_000, 45_000)


def test_encode_adaptation_only_pcr_packet_round_trip() -> None:
    clock = ProgramClockReference(90_000, 17)
    raw = encode_pcr_packet(
        pid=0x101,
        pcr=clock,
        continuity_counter=14,
        discontinuity=True,
    )
    packet = parse_transport_packet(raw)
    assert len(raw) == 188
    assert packet.pid == 0x101
    assert packet.continuity_counter == 14
    assert packet.adaptation_field_control == 2
    assert not packet.has_payload
    assert packet.pcr == clock
    assert packet.discontinuity_indicator
    assert packet.adaptation_field[7:] == b"\xff" * 176


def test_encode_pcr_packet_validates_clock_and_fields() -> None:
    clock = ProgramClockReference(0, 0)
    with pytest.raises(TypeError, match="ProgramClockReference"):
        encode_pcr_packet(pid=0x101, pcr=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="PID"):
        encode_pcr_packet(pid=0x2000, pcr=clock)
    with pytest.raises(ValueError, match="continuity_counter"):
        encode_pcr_packet(pid=0x101, pcr=clock, continuity_counter=16)
    with pytest.raises(TypeError, match="discontinuity"):
        encode_pcr_packet(pid=0x101, pcr=clock, discontinuity=1)  # type: ignore[arg-type]


def test_encode_asynchronous_metadata_pes_has_no_timestamp() -> None:
    packet = parse_pes_packet(
        encode_pes_packet(b"klv", stream_id=0xBD, data_alignment_indicator=True)
    )
    assert packet.pts is None
    assert packet.dts is None
    assert packet.pts_dts_flags == 0
    assert not packet.escr_flag
    assert packet.packet_length == len(packet.raw) - 6
    assert packet.packet_length > 0
    assert packet.data_alignment_indicator
    assert packet.payload == b"klv"


def test_pes_encoding_validates_parameters_and_bounded_length() -> None:
    with pytest.raises(ValueError, match="stream_id"):
        encode_pes_packet(b"x", stream_id=0x100)
    with pytest.raises(ValueError, match="PTS"):
        encode_pes_packet(b"x", stream_id=0xE0, pts=2**33)
    with pytest.raises(ValueError, match="requires PTS"):
        encode_pes_packet(b"x", stream_id=0xE0, dts=0)
    with pytest.raises(ValueError, match="65535"):
        encode_pes_packet(bytes(65_533), stream_id=0xBD)


def test_packetize_pes_round_trips_and_advances_continuity() -> None:
    pes = encode_pes_packet(bytes(range(256)) * 2, stream_id=0xE0, pts=123)
    packets, next_counter = packetize_pes(pes, pid=0x101, continuity_counter=14)
    assert len(packets) == 3
    assert next_counter == 1
    parsed = TransportStreamParser().feed(b"".join(packets))
    assert [packet.continuity_counter for packet in parsed] == [14, 15, 0]
    assert parsed[0].payload_unit_start
    assert not parsed[1].payload_unit_start
    assembler = PESAssembler(pid=0x101)
    completed = []
    for packet in parsed:
        completed.extend(assembler.feed(packet))
    assert completed[0].raw == pes


def test_packetize_pes_with_layout_preserves_pcr_and_adaptation_bytes() -> None:
    pes = encode_pes_packet(bytes(range(200)) + bytes(range(100)), stream_id=0xE0, pts=123)
    clock = ProgramClockReference(90_000, 17)
    first_adaptation = b"\x50" + encode_program_clock_reference(clock)
    first = parse_transport_packet(
        bytes((0x47, 0x41, 0x01, 0x35, len(first_adaptation)))
        + first_adaptation
        + pes[:176]
    )
    remainder = pes[176:]
    second_adaptation_length = 183 - len(remainder)
    second_adaptation = b"\x00" + b"\xff" * (second_adaptation_length - 1)
    second = parse_transport_packet(
        bytes((0x47, 0x01, 0x01, 0x36, second_adaptation_length))
        + second_adaptation
        + remainder
    )

    output, next_counter = packetize_pes_with_layout(
        pes,
        pid=0x101,
        layout=(first, second),
        continuity_counter=14,
    )
    parsed = TransportStreamParser().feed(b"".join(output))
    assert [packet.continuity_counter for packet in parsed] == [14, 15]
    assert next_counter == 0
    assert parsed[0].pcr == clock
    assert parsed[0].random_access_indicator
    assert [packet.adaptation_field for packet in parsed] == [
        first.adaptation_field,
        second.adaptation_field,
    ]
    assembler = PESAssembler(pid=0x101)
    rebuilt = next(item for packet in parsed for item in assembler.feed(packet))
    assert rebuilt.raw == pes


def test_packetize_pes_with_layout_rejects_incompatible_source_packets() -> None:
    pes = encode_pes_packet(b"video", stream_id=0xE0)
    source, _ = packetize_pes(pes, pid=0x101)
    layout = tuple(parse_transport_packet(packet) for packet in source)
    with pytest.raises(ValueError, match="payload lengths"):
        packetize_pes_with_layout(pes + b"x", pid=0x101, layout=layout)
    with pytest.raises(ValueError, match="PID"):
        packetize_pes_with_layout(pes, pid=0x102, layout=layout)


def test_pat_and_pmt_builders_round_trip_losslessly() -> None:
    pat_raw = build_pat_section(
        transport_stream_id=9,
        programs=(ProgramAssociation(1, 0x100),),
        version_number=3,
    )
    assert parse_pat(pat_raw).raw == pat_raw

    streams = (
        ElementaryStreamInfo(0x1B, 0x101, ()),
        ElementaryStreamInfo(0x03, 0x104, ()),
        ElementaryStreamInfo(0x06, 0x102, (Descriptor(0x05, b"KLVA"),)),
    )
    pmt_raw = build_pmt_section(
        program_number=1,
        pcr_pid=0x101,
        streams=streams,
        version_number=2,
    )
    pmt = parse_pmt(pmt_raw)
    assert pmt.raw == pmt_raw
    assert pmt.version_number == 2
    assert find_klv_streams(pmt) == ((0x102, KLVCarriage.ASYNCHRONOUS),)


def test_transport_muxer_tracks_pat_and_pmt_versions_independently() -> None:
    muxer = TransportMuxer(
        transport_stream_id=1,
        program_number=1,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=(ElementaryStreamInfo(0x1B, 0x101, ()),),
        pat_version_number=3,
        pmt_version_number=7,
    )

    events = TransportDemuxer().feed(b"".join(muxer.program_tables()))
    pat = next(event.table for event in events if isinstance(event, PATEvent))
    pmt = next(event.table for event in events if isinstance(event, PMTEvent))
    assert pat.version_number == 3
    assert pmt.version_number == 7
    assert muxer.pat_version_number == 3
    assert muxer.pmt_version_number == 7
    assert muxer.version_number == 7


def test_transport_muxer_reconfiguration_preserves_or_resets_metadata_sequence() -> None:
    sync = synchronous_klv_stream(
        0x102,
        metadata_input_leak_rate=1_000,
        metadata_buffer_size=200_000,
    )
    video = ElementaryStreamInfo(0x1B, 0x101, ())
    muxer = TransportMuxer(
        transport_stream_id=1,
        program_number=1,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=(video, sync),
    )
    klv = bytes.fromhex("060E2B34020B01010E01030101000000") + b"\x00"

    first = muxer.mux_sync_klv(0x102, klv, pts=0)
    muxer.reconfigure(pcr_pid=0x101, streams=(video, sync), pmt_version_number=1)
    second = muxer.mux_sync_klv(0x102, klv, pts=90_000)

    muxer.reconfigure(
        pcr_pid=0x101,
        streams=(video, asynchronous_klv_stream(0x102)),
        pmt_version_number=2,
    )
    muxer.reconfigure(pcr_pid=0x101, streams=(video, sync), pmt_version_number=3)
    reset = muxer.mux_sync_klv(0x102, klv, pts=180_000)

    def sequence(transport: tuple[bytes, ...]) -> int:
        pes_event = next(
            event
            for event in TransportDemuxer().feed(
                b"".join(muxer.program_tables()) + b"".join(transport)
            )
            if isinstance(event, PESStreamEvent)
        )
        return parse_metadata_au_cells(pes_event.pes.payload)[0].sequence_number

    assert sequence(first) == 0
    assert sequence(second) == 1
    assert sequence(reset) == 0


def test_transport_muxer_output_is_discoverable_and_demuxable() -> None:
    klv = bytes.fromhex("060E2B34020B01010E01030101000000") + b"\x00"
    streams = (
        ElementaryStreamInfo(0x1B, 0x101, ()),
        ElementaryStreamInfo(0x03, 0x104, ()),
        ElementaryStreamInfo(0x06, 0x102, (Descriptor(0x05, b"KLVA"),)),
    )
    muxer = TransportMuxer(
        transport_stream_id=1,
        program_number=1,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=streams,
    )
    output = b"".join(muxer.program_tables())
    output += b"".join(
        muxer.mux_pes(0x101, encode_pes_packet(b"video", stream_id=0xE0, pts=90_000))
    )
    output += b"".join(
        muxer.mux_pes(0x104, encode_pes_packet(b"audio", stream_id=0xC0, pts=90_000))
    )
    output += b"".join(muxer.mux_async_klv(0x102, klv))

    events = TransportDemuxer().feed(output)
    pes_events = [event for event in events if isinstance(event, PESStreamEvent)]
    assert [(event.kind, event.pes.payload) for event in pes_events] == [
        (StreamKind.VIDEO, b"video"),
        (StreamKind.AUDIO, b"audio"),
        (StreamKind.KLV, klv),
    ]
    assert muxer.continuity_counters[0x102] == 1


def test_build_pat_section_supports_explicit_table_section_numbers() -> None:
    wire = build_pat_section(
        transport_stream_id=7,
        programs=(ProgramAssociation(2, 0x110),),
        version_number=3,
        section_number=1,
        last_section_number=2,
    )

    table = parse_pat(wire)
    assert (table.section_number, table.last_section_number) == (1, 2)

    with pytest.raises(ValueError, match="section_number"):
        build_pat_section(
            transport_stream_id=7,
            programs=(),
            section_number=2,
            last_section_number=1,
        )


def test_transport_muxer_reassociates_pmt_without_resetting_retained_pids() -> None:
    video = ElementaryStreamInfo(0x1B, 0x101, ())
    muxer = TransportMuxer(
        transport_stream_id=1,
        program_number=1,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=(video,),
        pat_version_number=1,
        pmt_version_number=1,
    )
    first_tables = tuple(parse_transport_packet(packet) for packet in muxer.program_tables())
    first_video = tuple(
        parse_transport_packet(packet)
        for packet in muxer.mux_pes(
            0x101,
            encode_pes_packet(b"first", stream_id=0xE0, pts=0),
        )
    )

    muxer.reconfigure(
        program_map_pid=0x110,
        pcr_pid=0x101,
        streams=(video,),
        pat_version_number=2,
        pmt_version_number=2,
    )
    second_tables = tuple(parse_transport_packet(packet) for packet in muxer.program_tables())
    second_video = tuple(
        parse_transport_packet(packet)
        for packet in muxer.mux_pes(
            0x101,
            encode_pes_packet(b"second", stream_id=0xE0, pts=90_000),
        )
    )

    assert [packet.pid for packet in first_tables] == [0, 0x100]
    assert [packet.pid for packet in second_tables] == [0, 0x110]
    assert first_tables[0].continuity_counter == 0
    assert second_tables[0].continuity_counter == 1
    assert first_video[0].continuity_counter == 0
    assert second_video[0].continuity_counter == 1
    assert second_tables[1].continuity_counter == 0


def test_transport_muxer_inserts_pcr_without_advancing_payload_continuity() -> None:
    muxer = TransportMuxer(
        transport_stream_id=1,
        program_number=1,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=(ElementaryStreamInfo(0x1B, 0x101, ()),),
    )
    first_clock = parse_transport_packet(muxer.mux_pcr(ProgramClockReference(0, 0)))
    first_media = tuple(
        parse_transport_packet(packet)
        for packet in muxer.mux_pes(
            0x101,
            encode_pes_packet(b"first", stream_id=0xE0, pts=0),
        )
    )
    second_clock = parse_transport_packet(
        muxer.mux_pcr(ProgramClockReference(9_000, 0))
    )
    second_media = tuple(
        parse_transport_packet(packet)
        for packet in muxer.mux_pes(
            0x101,
            encode_pes_packet(b"second", stream_id=0xE0, pts=9_000),
        )
    )

    assert first_clock.continuity_counter == 0
    assert first_media[0].continuity_counter == 0
    assert second_clock.continuity_counter == 0
    assert second_media[0].continuity_counter == 1
    assert muxer.continuity_counters[0x101] == 2


def test_muxed_pcr_is_discovered_for_the_declared_program() -> None:
    muxer = TransportMuxer(
        transport_stream_id=1,
        program_number=7,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=(ElementaryStreamInfo(0x1B, 0x101, ()),),
    )
    clock = ProgramClockReference(123_456, 299)
    data = b"".join(muxer.program_tables()) + muxer.mux_pcr(clock)
    events = TransportDemuxer().feed(data)
    clocks = [event for event in events if isinstance(event, ProgramClockEvent)]
    assert len(clocks) == 1
    assert clocks[0].program_number == 7
    assert clocks[0].pid == 0x101
    assert clocks[0].pcr == clock


def test_transport_muxer_rejects_undeclared_or_wrong_klv_pid() -> None:
    muxer = TransportMuxer(
        transport_stream_id=1,
        program_number=1,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=(ElementaryStreamInfo(0x1B, 0x101, ()),),
    )
    with pytest.raises(DecodeError, match="not declared"):
        muxer.mux_pes(0x102, encode_pes_packet(b"x", stream_id=0xE0))
    with pytest.raises(DecodeError, match="asynchronous KLVA"):
        muxer.mux_async_klv(0x101, b"klv")
