"""Incremental demux-process-remux orchestration for live MPEG-2 TS."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from fractions import Fraction
from types import MappingProxyType

from stanag4609.errors import DecodeError
from stanag4609.st0601 import FieldDecodingMode
from stanag4609.transport.demux import (
    DemuxEvent,
    DemuxResetReport,
    PATEvent,
    PESStreamEvent,
    PMTEvent,
    ProgramClockEvent,
    StreamKind,
    TransportDemuxer,
)
from stanag4609.transport.metadata_stream import (
    MetadataDecoderResetReport,
    MetadataStreamDecoder,
)
from stanag4609.transport.mux import (
    ProgramTableEmission,
    ProgramTableScheduler,
    TransportMuxer,
)
from stanag4609.transport.processor import (
    MetadataProcessor,
    MetadataProcessorChain,
    TimedKLVPacket,
)
from stanag4609.transport.psi import (
    ElementaryStreamInfo,
    KLVCarriage,
    ProgramAssociation,
    ProgramAssociationTable,
    ProgramMapTable,
    find_klv_streams,
    klv_carriage,
)


@dataclass(frozen=True, slots=True)
class TransformBatch:
    """Output produced synchronously from one input chunk or explicit emission."""

    transport: bytes
    streams: tuple[PESStreamEvent, ...] = ()
    metadata: tuple[TimedKLVPacket, ...] = ()
    clocks: tuple[ProgramClockEvent, ...] = ()
    table_emission: ProgramTableEmission | None = None


@dataclass(frozen=True, slots=True)
class TransformerResetReport:
    """State discarded when a transformer starts a new source session."""

    demux: DemuxResetReport
    metadata: MetadataDecoderResetReport
    had_active_program: bool
    had_pending_association: bool
    was_finished: bool


class LiveTransportTransformer:
    """Transform timed KLV while repacketizing unchanged media PES byte-for-byte.

    The orchestrator emits one selected program from either a single- or
    multi-program input. It applies continuity-safe PAT/PMT updates at complete
    PES and metadata-item boundaries. KLVA PIDs may be added, removed, or
    change carriage when no affected item/access unit is partial.
    """

    def __init__(
        self,
        metadata_processors: Iterable[MetadataProcessor] = (),
        *,
        max_pes_length: int = 64 * 1024 * 1024,
        max_klv_value_length: int = 64 * 1024 * 1024,
        max_access_unit_length: int = 64 * 1024 * 1024,
        max_packets_per_input: int = 256,
        max_programs: int = 64,
        program_number: int | None = None,
        field_decoding: FieldDecodingMode = FieldDecodingMode.STRICT,
        additional_metadata_stream: ElementaryStreamInfo | None = None,
    ) -> None:
        if program_number is not None and (
            isinstance(program_number, bool)
            or not isinstance(program_number, int)
            or not 1 <= program_number <= 0xFFFF
        ):
            raise ValueError("program_number must be an integer from 1 to 65535 or None")
        if additional_metadata_stream is not None and not isinstance(
            additional_metadata_stream, ElementaryStreamInfo
        ):
            raise TypeError("additional_metadata_stream must be ElementaryStreamInfo or None")
        if additional_metadata_stream is not None and klv_carriage(
            additional_metadata_stream
        ) is None:
            raise ValueError("additional_metadata_stream must explicitly signal KLVA carriage")
        self._demuxer = TransportDemuxer(
            max_programs=max_programs,
            max_pes_length=max_pes_length,
            program_number=program_number,
        )
        self._metadata_decoder = MetadataStreamDecoder(
            max_klv_value_length=max_klv_value_length,
            max_access_unit_length=max_access_unit_length,
            field_decoding=field_decoding,
        )
        self._processors = MetadataProcessorChain(
            metadata_processors,
            max_packets_per_input=max_packets_per_input,
            max_packet_length=max_klv_value_length,
        )
        self._pat: ProgramAssociationTable | None = None
        self._association: ProgramAssociation | None = None
        self._pending_association: ProgramAssociation | None = None
        self._requested_program_number = program_number
        self._program: ProgramMapTable | None = None
        self._source_program: ProgramMapTable | None = None
        self._muxer: TransportMuxer | None = None
        self._table_scheduler: ProgramTableScheduler | None = None
        self._klv_streams: dict[int, KLVCarriage] = {}
        self._additional_metadata_stream = additional_metadata_stream
        self._finished = False

    @property
    def program(self) -> ProgramMapTable | None:
        return self._program

    @property
    def klv_streams(self) -> MappingProxyType[int, KLVCarriage]:
        return MappingProxyType(self._klv_streams)

    @property
    def selected_program_number(self) -> int | None:
        """Return the requested or auto-selected output program number."""

        if self._association is not None:
            return self._association.program_number
        return self._requested_program_number

    def feed(
        self,
        data: bytes | bytearray | memoryview,
        *,
        at: Fraction | int | float | None = None,
    ) -> TransformBatch:
        """Consume live TS, optionally scheduling PAT/PMT on monotonic seconds."""

        if self._finished:
            raise RuntimeError("cannot feed a finished live transport transformer")
        return self._consume(self._demuxer.feed(data), at=at)

    def finish(self) -> TransformBatch:
        """Flush complete PES packets and reject incomplete KLV structures."""

        if self._finished:
            return TransformBatch(b"")
        batch = self._consume(self._demuxer.finish())
        if self._pending_association is not None:
            raise DecodeError(
                "input ended before the selected program's reassociated PMT arrived"
            )
        self._metadata_decoder.finish()
        self._finished = True
        return batch

    def reset(self) -> TransformerResetReport:
        """Discard the source and output session, ready for a reconnect.

        The next input must supply fresh PAT/PMT discovery and begins a fresh
        output transport continuity epoch. Configured metadata processors and
        their caller-owned state are retained; buffered transport/KLV state is
        discarded and reported. This method may also reopen a transformer
        after :meth:`finish`.
        """

        report = TransformerResetReport(
            demux=self._demuxer.reset(),
            metadata=self._metadata_decoder.reset(),
            had_active_program=self._program is not None,
            had_pending_association=self._pending_association is not None,
            was_finished=self._finished,
        )
        self._pat = None
        self._association = None
        self._pending_association = None
        self._program = None
        self._source_program = None
        self._muxer = None
        self._table_scheduler = None
        self._klv_streams.clear()
        self._finished = False
        return report

    def emit_metadata(
        self,
        event: TimedKLVPacket,
        *,
        at: Fraction | int | float | None = None,
    ) -> TransformBatch:
        """Inject an externally produced timed KLV packet into the active stream."""

        if self._finished:
            raise RuntimeError("cannot emit metadata after the transformer is finished")
        emitted = self._processors.process(event)
        table_emission = self._poll_tables(at) if at is not None else None
        tables = b"" if table_emission is None else b"".join(table_emission.packets)
        return TransformBatch(
            tables + self._mux_metadata(emitted),
            metadata=emitted,
            table_emission=table_emission,
        )

    def poll_program_tables(self, *, at: Fraction | int | float) -> TransformBatch:
        """Emit scheduled PAT/PMT during idle input periods when they are due."""

        if self._finished:
            raise RuntimeError("cannot poll program tables after the transformer is finished")
        emission = self._poll_tables(at)
        transport = b"" if emission is None else b"".join(emission.packets)
        return TransformBatch(transport, table_emission=emission)

    def _consume(
        self,
        events: Iterable[DemuxEvent],
        *,
        at: Fraction | int | float | None = None,
    ) -> TransformBatch:
        output: list[bytes] = []
        streams: list[PESStreamEvent] = []
        metadata: list[TimedKLVPacket] = []
        clocks: list[ProgramClockEvent] = []
        table_emission = (
            self._poll_tables(at)
            if at is not None and self._table_scheduler is not None
            else None
        )
        if table_emission is not None:
            output.extend(table_emission.packets)
        for event in events:
            if isinstance(event, PATEvent):
                self._accept_pat(event)
            elif isinstance(event, PMTEvent):
                output.extend(self._accept_pmt(event, emit_tables=at is None))
                if (
                    at is not None
                    and event.table.current_next_indicator
                    and self._table_scheduler is not None
                ):
                    emission = self._poll_tables(at)
                    if emission is not None:
                        table_emission = emission
                        output.extend(emission.packets)
            elif isinstance(event, ProgramClockEvent):
                if event.program_number == self.selected_program_number:
                    clocks.append(event)
            else:
                if event.program_number != self.selected_program_number:
                    continue
                streams.append(event)
                if self._muxer is None:
                    raise DecodeError("PES arrived before an active program map")
                if event.kind is not StreamKind.KLV:
                    output.extend(
                        self._muxer.mux_pes(
                            event.pid,
                            event.pes.raw,
                            layout=event.pes.transport_packets,
                        )
                    )
                    continue
                decoded = self._metadata_decoder.feed(event)
                transformed = tuple(
                    result
                    for item in decoded
                    for result in self._processors.process(TimedKLVPacket.from_event(item))
                )
                metadata.extend(transformed)
                output.append(self._mux_metadata(transformed))
        return TransformBatch(
            b"".join(output),
            tuple(streams),
            tuple(metadata),
            tuple(clocks),
            table_emission,
        )

    def _accept_pat(self, event: PATEvent) -> None:
        table = event.table
        if not table.current_next_indicator:
            return
        previous = self._pat
        if previous is not None and table.transport_stream_id != previous.transport_stream_id:
            raise DecodeError("live transport_stream_id changed")
        selected: ProgramAssociation | None
        if self._requested_program_number is None:
            if len(event.programs) != 1:
                raise DecodeError(
                    "multi-program input requires an explicit program_number selection"
                )
            selected = event.programs[0]
        else:
            selected = next(
                (
                    association
                    for association in event.programs
                    if association.program_number == self._requested_program_number
                ),
                None,
            )
            if selected is None:
                raise DecodeError(
                    f"selected program_number {self._requested_program_number} "
                    "is not present in the active PAT"
                )
        assert selected is not None
        if (
            self._association is not None
            and selected.program_number != self._association.program_number
        ):
            raise DecodeError("the auto-selected program number changed")
        self._pat = table
        if (
            self._association is not None
            and selected.program_map_pid != self._association.program_map_pid
        ):
            self._pending_association = selected
            return
        self._association = selected
        self._pending_association = None
        if (
            previous is not None
            and table.version_number != previous.version_number
            and self._muxer is not None
        ):
            self._muxer.reconfigure(
                pcr_pid=self._muxer.pcr_pid,
                streams=self._muxer.streams,
                descriptors=self._muxer.descriptors,
                pat_version_number=table.version_number,
            )

    def _accept_pmt(
        self,
        event: PMTEvent,
        *,
        emit_tables: bool = True,
    ) -> tuple[bytes, ...]:
        table = event.table
        if not table.current_next_indicator:
            return ()
        if self._pat is None or self._association is None:
            raise DecodeError("PMT arrived before an active PAT")
        association = self._pending_association or self._association
        if (
            table.program_number != association.program_number
            or event.pid != association.program_map_pid
        ):
            return ()
        association_changed = association != self._association
        if self._source_program is not None:
            if table.raw == self._source_program.raw and not association_changed:
                assert self._muxer is not None
                return self._muxer.program_tables() if emit_tables else ()
            if (
                table.raw != self._source_program.raw
                and table.version_number == self._source_program.version_number
            ):
                raise DecodeError(
                    "live program map content changed without a PMT version change"
                )
            updated_klv_streams = dict(find_klv_streams(table))
            assert self._muxer is not None
            streams, version_number = self._output_program_configuration(
                table,
                association.program_map_pid,
            )
            self._metadata_decoder.validate_reconfiguration(updated_klv_streams)
            self._muxer.reconfigure(
                pcr_pid=table.pcr_pid,
                streams=streams,
                descriptors=table.descriptors,
                program_map_pid=association.program_map_pid,
                pat_version_number=self._pat.version_number,
                pmt_version_number=version_number,
            )
            self._metadata_decoder.reconfigure(updated_klv_streams)
            self._source_program = table
            self._association = association
            self._pending_association = None
            self._program = self._muxer.program_map
            self._klv_streams = dict(find_klv_streams(self._program))
            return self._muxer.program_tables() if emit_tables else ()
        self._source_program = table
        source_klv_streams = dict(find_klv_streams(table))
        streams, version_number = self._output_program_configuration(
            table,
            association.program_map_pid,
        )
        self._muxer = TransportMuxer(
            transport_stream_id=self._pat.transport_stream_id,
            program_number=table.program_number,
            program_map_pid=association.program_map_pid,
            pcr_pid=table.pcr_pid,
            streams=streams,
            descriptors=table.descriptors,
            pat_version_number=self._pat.version_number,
            pmt_version_number=version_number,
        )
        self._metadata_decoder.reconfigure(source_klv_streams)
        self._association = association
        self._pending_association = None
        self._program = self._muxer.program_map
        self._klv_streams = dict(find_klv_streams(self._program))
        self._table_scheduler = ProgramTableScheduler(self._muxer)
        return self._muxer.program_tables() if emit_tables else ()

    def _output_program_configuration(
        self,
        table: ProgramMapTable,
        program_map_pid: int,
    ) -> tuple[tuple[ElementaryStreamInfo, ...], int]:
        streams = table.streams
        version_number = table.version_number
        if self._additional_metadata_stream is not None:
            additional_carriage = klv_carriage(self._additional_metadata_stream)
            if additional_carriage is KLVCarriage.SYNCHRONOUS and any(
                carriage is KLVCarriage.SYNCHRONOUS
                for _, carriage in find_klv_streams(table)
            ):
                raise DecodeError(
                    "ST 1402-13 requires new synchronous metadata to use the "
                    "existing synchronous metadata elementary stream"
                )
            pid = self._additional_metadata_stream.elementary_pid
            if pid in {stream.elementary_pid for stream in streams} or pid in {
                0,
                program_map_pid,
            }:
                raise DecodeError(f"additional KLVA PID {pid} collides with the input program")
            streams += (self._additional_metadata_stream,)
            version_number = (version_number + 1) & 0x1F
        return streams, version_number

    def _poll_tables(self, at: Fraction | int | float) -> ProgramTableEmission | None:
        if self._table_scheduler is None:
            raise DecodeError("cannot schedule program tables before an active program map")
        return self._table_scheduler.poll(at=at)

    def _mux_metadata(self, events: Iterable[TimedKLVPacket]) -> bytes:
        if self._muxer is None or self._program is None:
            raise DecodeError("cannot emit metadata before an active program map")
        output: list[bytes] = []
        for event in events:
            if event.program_number != self._program.program_number:
                raise DecodeError(
                    f"metadata program {event.program_number} does not match active program "
                    f"{self._program.program_number}"
                )
            expected_carriage = self._klv_streams.get(event.pid)
            if expected_carriage is None:
                raise DecodeError(f"PID {event.pid} is not an active KLVA metadata stream")
            if event.carriage is not expected_carriage:
                raise DecodeError(
                    f"metadata carriage {event.carriage.value} does not match PID "
                    f"{event.pid} carriage {expected_carriage.value}"
                )
            if event.carriage is KLVCarriage.ASYNCHRONOUS:
                output.extend(self._muxer.mux_async_klv(event.pid, bytes(event.packet)))
            else:
                assert event.pts is not None
                assert event.metadata_service_id is not None
                output.extend(
                    self._muxer.mux_sync_klv(
                        event.pid,
                        bytes(event.packet),
                        pts=event.pts,
                        metadata_service_id=event.metadata_service_id,
                        random_access=event.random_access,
                    )
                )
        return b"".join(output)
