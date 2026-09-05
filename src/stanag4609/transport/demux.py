"""Program-aware, incremental MPEG-2 Transport Stream demultiplexing."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import TypeAlias

from stanag4609.errors import DecodeError
from stanag4609.st1001 import AudioCodec, audio_codec_for_stream_type
from stanag4609.transport.mpegts import (
    ProgramClockReference,
    TransportPacket,
    TransportStreamParser,
)
from stanag4609.transport.pes import PESAssembler, PESPacket
from stanag4609.transport.psi import (
    ElementaryStreamInfo,
    KLVCarriage,
    ProgramAssociation,
    ProgramAssociationTable,
    ProgramMapTable,
    PSISectionAssembler,
    find_klv_streams,
    parse_pat,
    parse_pmt,
)


class StreamKind(Enum):
    """Broad elementary stream role used for pipeline routing."""

    VIDEO = "video"
    AUDIO = "audio"
    KLV = "klv"
    DATA = "data"


@dataclass(frozen=True, slots=True)
class PATEvent:
    """One complete, CRC-validated Program Association Table cycle.

    ``table`` is the cycle's section zero for compatibility with single-section
    callers. ``sections`` contains every section in number order and
    ``programs`` provides their combined program loop.
    """

    table: ProgramAssociationTable
    sections: tuple[ProgramAssociationTable, ...] = ()
    source: TransportPacket | None = None

    def __post_init__(self) -> None:
        if not self.sections:
            object.__setattr__(self, "sections", (self.table,))

    @property
    def programs(self) -> tuple[ProgramAssociation, ...]:
        """Return program associations from all sections in wire-table order."""

        return tuple(program for section in self.sections for program in section.programs)

    @property
    def source_offset(self) -> int | None:
        """Offset of the TS packet that completed this PAT cycle, when known."""

        return None if self.source is None else self.source.offset


@dataclass(frozen=True, slots=True)
class PMTEvent:
    """A complete, CRC-validated Program Map Table."""

    table: ProgramMapTable
    pid: int
    source: TransportPacket | None = None

    @property
    def source_offset(self) -> int | None:
        """Offset of the TS packet that completed this PMT, when known."""

        return None if self.source is None else self.source.offset


@dataclass(frozen=True, slots=True)
class ProgramClockEvent:
    """One PCR observation associated with an active program."""

    program_number: int
    pcr: ProgramClockReference
    opcr: ProgramClockReference | None
    discontinuity: bool
    source: TransportPacket

    @property
    def pid(self) -> int:
        return self.source.pid

    @property
    def source_offset(self) -> int:
        return self.source.offset


@dataclass(frozen=True, slots=True)
class PESStreamEvent:
    """A complete PES packet with its active program/stream description."""

    program_number: int
    stream: ElementaryStreamInfo
    kind: StreamKind
    klv_carriage: KLVCarriage | None
    pes: PESPacket

    @property
    def pid(self) -> int:
        return self.stream.elementary_pid

    @property
    def audio_codec(self) -> AudioCodec | None:
        """Return the ST 1001 codec for an audio event without decoding it."""
        if self.kind is not StreamKind.AUDIO:
            return None
        return audio_codec_for_stream_type(self.stream.stream_type)


DemuxEvent: TypeAlias = PATEvent | PMTEvent | ProgramClockEvent | PESStreamEvent

_VIDEO_STREAM_TYPES = frozenset({0x01, 0x02, 0x10, 0x1B, 0x20, 0x21, 0x24, 0x42})
_AUDIO_STREAM_TYPES = frozenset({0x03, 0x04, 0x0F, 0x11, 0x1C, 0x2D})


@dataclass(frozen=True, slots=True)
class _ActiveStream:
    program_number: int
    info: ElementaryStreamInfo
    kind: StreamKind
    carriage: KLVCarriage | None


@dataclass(frozen=True, slots=True)
class DemuxResetReport:
    """State intentionally discarded at an MPEG-TS input-session boundary."""

    buffered_transport_bytes: int = 0
    buffered_psi_bytes: int = 0
    buffered_pes_bytes: int = 0
    partial_pat_sections: int = 0
    programs: int = 0
    streams: int = 0
    transport_stream_offset: int = 0
    recovered_transport_bytes: int = 0


def _stream_kind(
    stream: ElementaryStreamInfo,
    klv_streams: dict[int, KLVCarriage],
) -> tuple[StreamKind, KLVCarriage | None]:
    carriage = klv_streams.get(stream.elementary_pid)
    if carriage is not None:
        return StreamKind.KLV, carriage
    if stream.stream_type in _VIDEO_STREAM_TYPES:
        return StreamKind.VIDEO, None
    if stream.stream_type in _AUDIO_STREAM_TYPES:
        return StreamKind.AUDIO, None
    return StreamKind.DATA, None


class TransportDemuxer:
    """Discover programs and emit complete PES packets from arbitrary chunks.

    Input may begin before PAT/PMT acquisition; unknown packets are ignored until
    their program map arrives. ``program_number`` optionally limits PMT/PES/clock
    activation to one service while PAT events still expose the full association
    table. All internal partial structures have configurable bounds suitable for
    long-running streams.
    """

    def __init__(
        self,
        *,
        recover_transport: bool = False,
        max_programs: int = 64,
        max_streams_per_program: int = 64,
        max_pes_length: int = 64 * 1024 * 1024,
        program_number: int | None = None,
    ) -> None:
        if max_programs < 1:
            raise ValueError("max_programs must be positive")
        if max_streams_per_program < 1:
            raise ValueError("max_streams_per_program must be positive")
        if max_pes_length < 6:
            raise ValueError("max_pes_length must be at least six bytes")
        if program_number is not None and (
            isinstance(program_number, bool)
            or not isinstance(program_number, int)
            or not 1 <= program_number <= 0xFFFF
        ):
            raise ValueError("program_number must be an integer from 1 to 65535 or None")
        self.max_programs = max_programs
        self.max_streams_per_program = max_streams_per_program
        self.max_pes_length = max_pes_length
        self.program_number = program_number
        self.recover_transport = recover_transport
        self._transport = TransportStreamParser(recover=recover_transport)
        self._pat = PSISectionAssembler(pid=0)
        self._pat_cycle_key: tuple[int, int, int] | None = None
        self._pat_sections: dict[int, ProgramAssociationTable] = {}
        self._pmt_assemblers: dict[int, PSISectionAssembler] = {}
        self._pmt_program_numbers: dict[int, tuple[int, ...]] = {}
        self._programs: dict[int, ProgramMapTable] = {}
        self._clock_programs: dict[int, tuple[int, ...]] = {}
        self._streams: dict[int, _ActiveStream] = {}
        self._pes: dict[int, PESAssembler] = {}

    @property
    def programs(self) -> MappingProxyType[int, ProgramMapTable]:
        return MappingProxyType(self._programs)

    @property
    def buffered_transport_bytes(self) -> int:
        return self._transport.buffered_bytes

    def feed(self, data: bytes | bytearray | memoryview) -> list[DemuxEvent]:
        """Consume transport bytes and return all complete discovery/PES events."""

        events: list[DemuxEvent] = []
        for packet in self._transport.feed(data):
            events.extend(self._feed_packet(packet))
        return events

    def finish(self) -> list[DemuxEvent]:
        """Signal end-of-stream and flush complete unbounded PES packets."""

        events: list[DemuxEvent] = []
        for packet in self._transport.finish():
            events.extend(self._feed_packet(packet))
        self._pat.finish()
        for pmt_assembler in self._pmt_assemblers.values():
            pmt_assembler.finish()
        for pid, pes_assembler in self._pes.items():
            active = self._streams[pid]
            for pes in pes_assembler.finish():
                events.append(
                    PESStreamEvent(
                        active.program_number,
                        active.info,
                        active.kind,
                        active.carriage,
                        pes,
                    )
                )
        return events

    def reset(self) -> DemuxResetReport:
        """Discard all state from the current input session and start fresh.

        A reconnect is a hard boundary: buffered TS, PSI, and PES bytes cannot
        safely be joined to the next source, and its program topology must be
        rediscovered. Constructor limits and the requested program selection
        are retained. The returned report makes the intentional data loss
        observable for metrics and logs.
        """

        report = DemuxResetReport(
            buffered_transport_bytes=self._transport.buffered_bytes,
            buffered_psi_bytes=self._pat.buffered_bytes
            + sum(item.buffered_bytes for item in self._pmt_assemblers.values()),
            buffered_pes_bytes=sum(item.buffered_bytes for item in self._pes.values()),
            partial_pat_sections=len(self._pat_sections),
            programs=len(self._programs),
            streams=len(self._streams),
            transport_stream_offset=self._transport.stream_offset,
            recovered_transport_bytes=self._transport.discarded_bytes,
        )
        self._transport = TransportStreamParser(recover=self.recover_transport)
        self._pat = PSISectionAssembler(pid=0)
        self._pat_cycle_key = None
        self._pat_sections.clear()
        self._pmt_assemblers.clear()
        self._pmt_program_numbers.clear()
        self._programs.clear()
        self._clock_programs.clear()
        self._streams.clear()
        self._pes.clear()
        return report

    def _feed_packet(self, packet: TransportPacket) -> list[DemuxEvent]:
        if packet.transport_error_indicator:
            raise DecodeError(f"MPEG-2 TS packet on PID {packet.pid} has transport error set")
        events: list[DemuxEvent] = self._clock_events(packet)
        if packet.pid == 0:
            for section in self._pat.feed(packet):
                pat = parse_pat(section)
                event = self._accept_pat_section(pat, source=packet)
                if event is not None:
                    events.append(event)
            return events

        pmt_assembler = self._pmt_assemblers.get(packet.pid)
        if pmt_assembler is not None:
            for section in pmt_assembler.feed(packet):
                pmt = parse_pmt(section)
                expected_programs = self._pmt_program_numbers[packet.pid]
                if pmt.program_number not in expected_programs:
                    raise DecodeError(
                        f"PMT PID {packet.pid} describes program {pmt.program_number}, "
                        f"expected one of {expected_programs}"
                    )
                if (
                    self.program_number is not None
                    and pmt.program_number != self.program_number
                ):
                    continue
                events.append(PMTEvent(pmt, packet.pid, packet))
                if pmt.current_next_indicator:
                    self._activate_pmt(pmt)
            return events

        pes_assembler = self._pes.get(packet.pid)
        if pes_assembler is None:
            return events
        active = self._streams[packet.pid]
        for pes in pes_assembler.feed(packet):
            events.append(
                PESStreamEvent(
                    active.program_number,
                    active.info,
                    active.kind,
                    active.carriage,
                    pes,
                )
            )
        return events

    def _clock_events(self, packet: TransportPacket) -> list[DemuxEvent]:
        if packet.pcr is None:
            return []
        return [
            ProgramClockEvent(
                program_number,
                packet.pcr,
                packet.opcr,
                packet.discontinuity_indicator,
                packet,
            )
            for program_number in self._clock_programs.get(packet.pid, ())
        ]

    def _refresh_clock_programs(self) -> None:
        assignments: dict[int, list[int]] = {}
        for program_number, program in self._programs.items():
            assignments.setdefault(program.pcr_pid, []).append(program_number)
        self._clock_programs = {
            pid: tuple(sorted(program_numbers))
            for pid, program_numbers in assignments.items()
        }

    def _accept_pat_section(
        self,
        table: ProgramAssociationTable,
        *,
        source: TransportPacket,
    ) -> PATEvent | None:
        if not 0 <= table.section_number <= table.last_section_number <= 0xFF:
            raise DecodeError("PAT section_number exceeds last_section_number")
        if not table.current_next_indicator:
            return PATEvent(table, source=source)
        cycle_key = (
            table.transport_stream_id,
            table.version_number,
            table.last_section_number,
        )
        if cycle_key != self._pat_cycle_key:
            self._pat_cycle_key = cycle_key
            self._pat_sections.clear()
        previous = self._pat_sections.get(table.section_number)
        if previous is not None and previous.raw != table.raw:
            raise DecodeError(
                "PAT section content changed without a version change"
            )
        self._pat_sections[table.section_number] = table
        program_count = sum(len(section.programs) for section in self._pat_sections.values())
        if program_count > self.max_programs:
            raise DecodeError(
                f"PAT program count {program_count} exceeds limit {self.max_programs}"
            )
        if len(self._pat_sections) != table.last_section_number + 1:
            return None
        sections = tuple(
            self._pat_sections[index]
            for index in range(table.last_section_number + 1)
        )
        self._activate_pat(sections)
        self._pat_cycle_key = None
        self._pat_sections.clear()
        return PATEvent(sections[0], sections, source)

    def _activate_pat(self, sections: tuple[ProgramAssociationTable, ...]) -> None:
        assignment_lists: dict[int, list[int]] = {}
        seen_programs: set[int] = set()
        for table in sections:
            for entry in table.programs:
                if entry.program_number in seen_programs:
                    raise DecodeError(
                        f"PAT program_number {entry.program_number} occurs in multiple sections"
                    )
                seen_programs.add(entry.program_number)
                assignment_lists.setdefault(entry.program_map_pid, []).append(
                    entry.program_number
                )

        all_assignments = {
            pid: tuple(program_numbers)
            for pid, program_numbers in assignment_lists.items()
        }
        if self.program_number is None:
            assignments = all_assignments
            active_programs = seen_programs
        else:
            assignments = {
                pid: program_numbers
                for pid, program_numbers in all_assignments.items()
                if self.program_number in program_numbers
            }
            active_programs = (
                {self.program_number} if self.program_number in seen_programs else set()
            )
        removed_programs = set(self._programs) - active_programs
        removed_stream_pids = {
            pid
            for pid, active in self._streams.items()
            if active.program_number in removed_programs
        }
        for pid in removed_stream_pids:
            if self._pes[pid].buffered_bytes:
                raise DecodeError(f"PAT removed program while PID {pid} had an incomplete PES")
        for pid, program_numbers in self._pmt_program_numbers.items():
            if (
                assignments.get(pid) != program_numbers
                and self._pmt_assemblers[pid].buffered_bytes
            ):
                raise DecodeError(f"PAT changed PMT PID {pid} while a PSI section was incomplete")

        for pid in removed_stream_pids:
            del self._streams[pid]
            del self._pes[pid]
        for program_number in removed_programs:
            del self._programs[program_number]
        self._refresh_clock_programs()
        for pid in tuple(self._pmt_assemblers):
            if assignments.get(pid) != self._pmt_program_numbers[pid]:
                del self._pmt_assemblers[pid]
                del self._pmt_program_numbers[pid]
        for pid, program_numbers in assignments.items():
            self._pmt_program_numbers[pid] = program_numbers
            self._pmt_assemblers.setdefault(pid, PSISectionAssembler(pid=pid))

    def _activate_pmt(self, table: ProgramMapTable) -> None:
        if len(table.streams) > self.max_streams_per_program:
            raise DecodeError(
                f"PMT stream count {len(table.streams)} exceeds stream limit "
                f"{self.max_streams_per_program}"
            )
        klv_streams = dict(find_klv_streams(table))
        new_pids = {stream.elementary_pid for stream in table.streams}
        removed_pids = {
            pid
            for pid, active in self._streams.items()
            if active.program_number == table.program_number and pid not in new_pids
        }
        for pid in removed_pids:
            if self._pes[pid].buffered_bytes:
                raise DecodeError(f"PMT removed PID {pid} while a PES packet was incomplete")

        for stream in table.streams:
            existing = self._streams.get(stream.elementary_pid)
            if existing is not None and existing.program_number != table.program_number:
                raise DecodeError(
                    f"elementary PID {stream.elementary_pid} belongs to multiple programs"
                )
            if (
                existing is not None
                and existing.info != stream
                and self._pes[stream.elementary_pid].buffered_bytes
            ):
                raise DecodeError(
                    f"PMT changed PID {stream.elementary_pid} while a PES packet was incomplete"
                )

        for pid in removed_pids:
            del self._streams[pid]
            del self._pes[pid]
        for stream in table.streams:
            kind, carriage = _stream_kind(stream, klv_streams)
            self._streams[stream.elementary_pid] = _ActiveStream(
                table.program_number, stream, kind, carriage
            )
            self._pes.setdefault(
                stream.elementary_pid,
                PESAssembler(pid=stream.elementary_pid, max_pes_length=self.max_pes_length),
            )
        self._programs[table.program_number] = table
        self._refresh_clock_programs()
