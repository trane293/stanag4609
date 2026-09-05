from __future__ import annotations

import pytest

from stanag4609.errors import DecodeError
from stanag4609.transport.demux import (
    DemuxResetReport,
    PATEvent,
    PESStreamEvent,
    PMTEvent,
    ProgramClockEvent,
    StreamKind,
    TransportDemuxer,
)
from stanag4609.transport.mpegts import (
    ProgramClockReference,
    encode_program_clock_reference,
)
from stanag4609.transport.mux import build_pmt_section
from stanag4609.transport.psi import (
    Descriptor,
    ElementaryStreamInfo,
    KLVCarriage,
    mpeg2_crc32,
)

PAT = bytes.fromhex("00 B0 0D 00 01 C1 00 00 00 01 E1 00 E8 F9 5E 7D")


def _pat(
    programs: list[tuple[int, int]],
    *,
    version: int = 0,
    section: int = 0,
    last_section: int = 0,
) -> bytes:
    entries = b"".join(
        program.to_bytes(2, "big") + bytes((0xE0 | (pid >> 8), pid & 0xFF))
        for program, pid in programs
    )
    body = b"\x00\x01" + bytes(
        (0xC1 | (version << 1), section, last_section)
    ) + entries
    section_length = len(body) + 4
    head = bytes((0x00, 0xB0 | (section_length >> 8), section_length & 0xFF))
    without_crc = head + body
    return without_crc + mpeg2_crc32(without_crc).to_bytes(4, "big")


def _pmt() -> bytes:
    body = bytes.fromhex(
        "0001 C10000 E101 F000 "
        "1B E101 F000 "
        "03 E104 F000 "
        "06 E102 F006 05044B4C5641"
    )
    section_length = len(body) + 4
    head = bytes((0x02, 0xB0 | (section_length >> 8), section_length & 0xFF))
    without_crc = head + body
    return without_crc + mpeg2_crc32(without_crc).to_bytes(4, "big")


def _ts(payload: bytes, *, pid: int, start: bool = True, cc: int = 0) -> bytes:
    if not 0 < len(payload) <= 184:
        raise ValueError
    byte1 = (pid >> 8) | (0x40 if start else 0)
    byte2 = pid & 0xFF
    if len(payload) == 184:
        return bytes((0x47, byte1, byte2, 0x10 | cc)) + payload
    adaptation_length = 183 - len(payload)
    adaptation = b"" if adaptation_length == 0 else b"\x00" + b"\xff" * (
        adaptation_length - 1
    )
    return (
        bytes((0x47, byte1, byte2, 0x30 | cc, adaptation_length))
        + adaptation
        + payload
    )


def _ts_with_pcr(
    payload: bytes,
    *,
    pid: int,
    pcr: ProgramClockReference,
    start: bool = True,
    cc: int = 0,
) -> bytes:
    if not 0 < len(payload) <= 176:
        raise ValueError
    adaptation_length = 183 - len(payload)
    adaptation = (
        b"\x10"
        + encode_program_clock_reference(pcr)
        + b"\xff" * (adaptation_length - 7)
    )
    return (
        bytes(
            (
                0x47,
                (pid >> 8) | (0x40 if start else 0),
                pid & 0xFF,
                0x30 | cc,
                adaptation_length,
            )
        )
        + adaptation
        + payload
    )


def _pes(stream_id: int, payload: bytes, *, aligned: bool = False) -> bytes:
    optional = bytes((0x84 if aligned else 0x80, 0x00, 0x00))
    packet_length = len(optional) + len(payload)
    return (
        b"\x00\x00\x01"
        + bytes((stream_id,))
        + packet_length.to_bytes(2, "big")
        + optional
        + payload
    )


def _discovery_packets() -> bytes:
    return _ts(b"\x00" + PAT, pid=0) + _ts(b"\x00" + _pmt(), pid=0x100)


def test_live_demux_discovers_program_and_emits_typed_pes_events() -> None:
    data = _discovery_packets() + b"".join(
        [
            _ts(_pes(0xE0, b"video"), pid=0x101),
            _ts(_pes(0xC0, b"audio"), pid=0x104),
            _ts(_pes(0xBD, b"klv", aligned=True), pid=0x102),
        ]
    )
    demuxer = TransportDemuxer()
    events = []
    for boundary in range(0, len(data), 73):
        events.extend(demuxer.feed(data[boundary : boundary + 73]))
    events.extend(demuxer.finish())

    assert isinstance(events[0], PATEvent)
    assert isinstance(events[1], PMTEvent)
    assert events[0].source_offset == 0
    assert events[1].source_offset == 188
    assert events[1].pid == 0x100
    streams = [event for event in events if isinstance(event, PESStreamEvent)]
    assert [(event.pid, event.kind, event.pes.payload) for event in streams] == [
        (0x101, StreamKind.VIDEO, b"video"),
        (0x104, StreamKind.AUDIO, b"audio"),
        (0x102, StreamKind.KLV, b"klv"),
    ]
    assert streams[2].klv_carriage is KLVCarriage.ASYNCHRONOUS
    assert streams[0].program_number == 1
    assert demuxer.programs[1].pcr_pid == 0x101


def test_live_demux_reset_discards_every_input_session_fragment() -> None:
    demuxer = TransportDemuxer(recover_transport=True)
    demuxer.feed(_discovery_packets())

    unbounded = bytearray(_pes(0xBD, b"old", aligned=True))
    unbounded[4:6] = b"\x00\x00"
    demuxer.feed(_ts(bytes(unbounded), pid=0x102))
    demuxer.feed(
        _ts(
            b"\x00" + _pat([(1, 0x100)], version=1, section=0, last_section=1),
            pid=0,
            cc=1,
        )
    )
    demuxer.feed(_ts(b"\x00" + _pmt()[:5], pid=0x100, cc=1))
    demuxer.feed(b"\x47" * 37)

    report = demuxer.reset()

    assert isinstance(report, DemuxResetReport)
    assert report.buffered_transport_bytes == 37
    assert report.buffered_psi_bytes == 5
    assert report.buffered_pes_bytes == len(unbounded)
    assert report.partial_pat_sections == 1
    assert report.programs == 1
    assert report.streams == 3
    assert report.transport_stream_offset == 5 * 188
    assert report.recovered_transport_bytes == 0
    assert demuxer.buffered_transport_bytes == 0
    assert dict(demuxer.programs) == {}

    events = demuxer.feed(
        _discovery_packets() + _ts(_pes(0xBD, b"new", aligned=True), pid=0x102)
    )
    streams = [event for event in events if isinstance(event, PESStreamEvent)]
    assert [event.pes.payload for event in streams] == [b"new"]
    assert demuxer.reset().programs == 1
    assert demuxer.reset() == DemuxResetReport()


def test_live_demux_selects_one_program_without_buffering_unrelated_streams() -> None:
    first_pmt = build_pmt_section(
        program_number=1,
        pcr_pid=0x101,
        streams=(ElementaryStreamInfo(0x1B, 0x101, ()),),
    )
    second_pmt = build_pmt_section(
        program_number=2,
        pcr_pid=0x111,
        streams=(ElementaryStreamInfo(0x1B, 0x111, ()),),
    )
    data = _ts(b"\x00" + _pat([(1, 0x100), (2, 0x110)]), pid=0)
    data += _ts(b"\x00" + first_pmt, pid=0x100)
    data += _ts(b"\x00" + second_pmt, pid=0x110)
    data += _ts(_pes(0xE0, b"ignored"), pid=0x101)
    data += _ts(_pes(0xE0, b"selected"), pid=0x111)

    events = TransportDemuxer(program_number=2).feed(data)

    assert [type(event) for event in events] == [PATEvent, PMTEvent, PESStreamEvent]
    pmt = events[1]
    stream = events[2]
    assert isinstance(pmt, PMTEvent)
    assert isinstance(stream, PESStreamEvent)
    assert pmt.pid == 0x110
    assert stream.program_number == 2
    assert stream.pes.payload == b"selected"


def test_live_demux_activates_complete_multisection_pat_atomically() -> None:
    first_pmt = build_pmt_section(
        program_number=1,
        pcr_pid=0x101,
        streams=(ElementaryStreamInfo(0x1B, 0x101, ()),),
    )
    second_pmt = build_pmt_section(
        program_number=2,
        pcr_pid=0x111,
        streams=(ElementaryStreamInfo(0x1B, 0x111, ()),),
    )
    demuxer = TransportDemuxer()

    assert demuxer.feed(
        _ts(b"\x00" + _pat([(2, 0x110)], section=1, last_section=1), pid=0)
    ) == []
    assert dict(demuxer.programs) == {}
    events = demuxer.feed(
        _ts(b"\x00" + _pat([(1, 0x100)], section=0, last_section=1), pid=0, cc=1)
    )

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, PATEvent)
    assert [section.section_number for section in event.sections] == [0, 1]
    assert [(item.program_number, item.program_map_pid) for item in event.programs] == [
        (1, 0x100),
        (2, 0x110),
    ]
    events = demuxer.feed(_ts(b"\x00" + first_pmt, pid=0x100))
    events += demuxer.feed(_ts(b"\x00" + second_pmt, pid=0x110))
    events += demuxer.feed(_ts(_pes(0xE0, b"one"), pid=0x101))
    events += demuxer.feed(_ts(_pes(0xE0, b"two"), pid=0x111))
    assert [event.program_number for event in events if isinstance(event, PESStreamEvent)] == [
        1,
        2,
    ]


def test_live_demux_retains_active_routes_during_next_pat_cycle() -> None:
    demuxer = TransportDemuxer()
    demuxer.feed(_discovery_packets())

    assert demuxer.feed(
        _ts(
            b"\x00" + _pat([(2, 0x110)], version=1, section=0, last_section=1),
            pid=0,
            cc=1,
        )
    ) == []
    events = demuxer.feed(_ts(_pes(0xE0, b"still-active"), pid=0x101))
    stream = next(event for event in events if isinstance(event, PESStreamEvent))
    assert stream.program_number == 1
    assert stream.pes.payload == b"still-active"

    events = demuxer.feed(
        _ts(
            b"\x00" + _pat([(3, 0x120)], version=1, section=1, last_section=1),
            pid=0,
            cc=2,
        )
    )
    assert len(events) == 1
    assert isinstance(events[0], PATEvent)
    assert dict(demuxer.programs) == {}
    assert demuxer.feed(_ts(_pes(0xE0, b"removed"), pid=0x101, cc=1)) == []


def test_live_demux_bounds_and_rejects_conflicting_multisection_pat() -> None:
    demuxer = TransportDemuxer(max_programs=1)
    demuxer.feed(
        _ts(b"\x00" + _pat([(1, 0x100)], section=0, last_section=1), pid=0)
    )
    with pytest.raises(DecodeError, match="program count 2 exceeds limit 1"):
        demuxer.feed(
            _ts(b"\x00" + _pat([(2, 0x110)], section=1, last_section=1), pid=0, cc=1)
        )

    demuxer = TransportDemuxer()
    first = _pat([(1, 0x100)], section=0, last_section=1)
    demuxer.feed(_ts(b"\x00" + first, pid=0))
    assert demuxer.feed(_ts(b"\x00" + first, pid=0, cc=1)) == []
    with pytest.raises(DecodeError, match="changed without a version change"):
        demuxer.feed(
            _ts(
                b"\x00" + _pat([(1, 0x101)], section=0, last_section=1),
                pid=0,
                cc=2,
            )
        )

    demuxer = TransportDemuxer()
    demuxer.feed(
        _ts(b"\x00" + _pat([(1, 0x100)], section=0, last_section=1), pid=0)
    )
    with pytest.raises(DecodeError, match="program_number 1 occurs"):
        demuxer.feed(
            _ts(
                b"\x00" + _pat([(1, 0x110)], section=1, last_section=1),
                pid=0,
                cc=1,
            )
        )


@pytest.mark.parametrize("program_number", [True, 0, 65_536, "1"])
def test_live_demux_validates_program_selection(program_number: object) -> None:
    with pytest.raises(ValueError, match="program_number"):
        TransportDemuxer(program_number=program_number)  # type: ignore[arg-type]


def test_demux_emits_program_clock_context_before_its_pes() -> None:
    pcr = ProgramClockReference(90_000, 12)
    data = _discovery_packets() + _ts_with_pcr(
        _pes(0xE0, b"video"), pid=0x101, pcr=pcr
    )
    events = TransportDemuxer().feed(data)
    assert [type(event) for event in events] == [
        PATEvent,
        PMTEvent,
        ProgramClockEvent,
        PESStreamEvent,
    ]
    clock = events[2]
    assert isinstance(clock, ProgramClockEvent)
    assert clock.program_number == 1
    assert clock.pid == 0x101
    assert clock.pcr == pcr
    assert clock.source_offset == 376
    assert not clock.discontinuity


def test_demux_live_join_ignores_elementary_data_until_pmt_arrives() -> None:
    demuxer = TransportDemuxer()
    assert demuxer.feed(_ts(_pes(0xE0, b"ignored"), pid=0x101)) == []
    events = demuxer.feed(_discovery_packets())
    assert [type(event) for event in events] == [PATEvent, PMTEvent]


def test_pmt_cannot_change_stream_definition_during_incomplete_pes() -> None:
    demuxer = TransportDemuxer()
    demuxer.feed(_discovery_packets())
    unbounded_video = bytes.fromhex("000001E00000800000") + b"partial"
    assert demuxer.feed(_ts(unbounded_video, pid=0x101)) == []
    changed = build_pmt_section(
        program_number=1,
        pcr_pid=0x101,
        streams=(
            ElementaryStreamInfo(0x03, 0x101, ()),
            ElementaryStreamInfo(0x03, 0x104, ()),
            ElementaryStreamInfo(0x06, 0x102, (Descriptor(0x05, b"KLVA"),)),
        ),
        version_number=1,
    )

    with pytest.raises(DecodeError, match=r"changed PID 257.*PES packet was incomplete"):
        demuxer.feed(_ts(b"\x00" + changed, pid=0x100, cc=1))


def test_rejected_pmt_update_leaves_previous_stream_routes_active() -> None:
    demuxer = TransportDemuxer()
    demuxer.feed(_discovery_packets())
    unbounded_video = bytes.fromhex("000001E00000800000") + b"partial"
    demuxer.feed(_ts(unbounded_video, pid=0x101))
    changed = build_pmt_section(
        program_number=1,
        pcr_pid=0x101,
        streams=(ElementaryStreamInfo(0x03, 0x101, ()),),
        version_number=1,
    )

    with pytest.raises(DecodeError, match=r"changed PID 257.*PES packet was incomplete"):
        demuxer.feed(_ts(b"\x00" + changed, pid=0x100, cc=1))

    events = demuxer.feed(_ts(_pes(0xC0, b"still-routed"), pid=0x104))
    audio = next(event for event in events if isinstance(event, PESStreamEvent))
    assert audio.kind is StreamKind.AUDIO
    assert audio.pes.payload == b"still-routed"
    assert {stream.elementary_pid for stream in demuxer.programs[1].streams} == {
        0x101,
        0x102,
        0x104,
    }


def test_demux_validates_configuration_and_program_limits() -> None:
    with pytest.raises(ValueError, match="max_programs"):
        TransportDemuxer(max_programs=0)
    with pytest.raises(ValueError, match="max_streams"):
        TransportDemuxer(max_streams_per_program=0)
    with pytest.raises(ValueError, match="max_pes_length"):
        TransportDemuxer(max_pes_length=5)
    demuxer = TransportDemuxer(max_streams_per_program=2)
    with pytest.raises(DecodeError, match="stream limit"):
        demuxer.feed(_discovery_packets())


def test_demux_finish_rejects_partial_transport_data() -> None:
    demuxer = TransportDemuxer()
    demuxer.feed(b"\x47")
    with pytest.raises(DecodeError, match="incomplete"):
        demuxer.finish()


def test_current_pat_update_removes_stale_program_and_stream_routes() -> None:
    demuxer = TransportDemuxer()
    demuxer.feed(_discovery_packets())
    assert 1 in demuxer.programs
    events = demuxer.feed(_ts(b"\x00" + _pat([], version=1), pid=0, cc=1))
    assert len(events) == 1
    assert isinstance(events[0], PATEvent)
    assert dict(demuxer.programs) == {}
    assert demuxer.feed(_ts(_pes(0xE0, b"ignored"), pid=0x101, cc=1)) == []


def test_pat_allows_multiple_program_maps_on_one_pid() -> None:
    demuxer = TransportDemuxer()
    first_pmt = build_pmt_section(
        program_number=1,
        pcr_pid=0x101,
        streams=(ElementaryStreamInfo(0x1B, 0x101, ()),),
    )
    second_pmt = build_pmt_section(
        program_number=2,
        pcr_pid=0x111,
        streams=(ElementaryStreamInfo(0x1B, 0x111, ()),),
    )

    events = demuxer.feed(_ts(b"\x00" + _pat([(1, 0x100), (2, 0x100)]), pid=0))
    events += demuxer.feed(_ts(b"\x00" + first_pmt, pid=0x100))
    events += demuxer.feed(_ts(b"\x00" + second_pmt, pid=0x100, cc=1))
    events += demuxer.feed(_ts(_pes(0xE0, b"one"), pid=0x101))
    events += demuxer.feed(_ts(_pes(0xE0, b"two"), pid=0x111))

    assert [event.table.program_number for event in events if isinstance(event, PMTEvent)] == [
        1,
        2,
    ]
    assert [event.program_number for event in events if isinstance(event, PESStreamEvent)] == [
        1,
        2,
    ]


def test_selected_demux_ignores_unselected_map_on_shared_pmt_pid() -> None:
    first_pmt = build_pmt_section(
        program_number=1,
        pcr_pid=0x101,
        streams=(ElementaryStreamInfo(0x1B, 0x101, ()),),
    )
    second_pmt = build_pmt_section(
        program_number=2,
        pcr_pid=0x111,
        streams=(ElementaryStreamInfo(0x1B, 0x111, ()),),
    )
    demuxer = TransportDemuxer(program_number=2)

    events = demuxer.feed(_ts(b"\x00" + _pat([(1, 0x100), (2, 0x100)]), pid=0))
    events += demuxer.feed(_ts(b"\x00" + first_pmt, pid=0x100))
    events += demuxer.feed(_ts(b"\x00" + second_pmt, pid=0x100, cc=1))
    events += demuxer.feed(_ts(_pes(0xE0, b"ignored"), pid=0x101))
    events += demuxer.feed(_ts(_pes(0xE0, b"selected"), pid=0x111))

    assert [type(event) for event in events] == [PATEvent, PMTEvent, PESStreamEvent]
    assert isinstance(events[1], PMTEvent)
    assert events[1].table.program_number == 2
    assert isinstance(events[2], PESStreamEvent)
    assert events[2].pes.payload == b"selected"
