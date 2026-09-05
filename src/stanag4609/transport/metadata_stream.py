"""Reconstruct typed KLV events from asynchronous or synchronous PES metadata."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from stanag4609.errors import DecodeError, LimitExceeded, TruncatedData
from stanag4609.klv.model import KLVPacket
from stanag4609.klv.stream import KLVStreamParser
from stanag4609.st0601 import (
    ST0601_KEY,
    FieldDecodingMode,
    ST0601ValidationContext,
    decode_uas_local_set,
)
from stanag4609.st0903 import (
    VMTI_KEY,
    OntologyResolver,
    VMTIValidationContext,
    decode_vmti_local_set,
)
from stanag4609.transport.demux import PESStreamEvent, StreamKind
from stanag4609.transport.metadata import (
    CellFragmentation,
    MetadataAUCell,
    klva_metadata_service_ids,
    parse_metadata_au_cells,
)
from stanag4609.transport.psi import KLVCarriage


@dataclass(frozen=True, slots=True)
class KLVMetadataEvent:
    """One complete KLV packet with transport timing and optional typed metadata."""

    program_number: int
    pid: int
    carriage: KLVCarriage
    pts: int | None
    metadata_service_id: int | None
    random_access: bool
    packet: KLVPacket
    decoded: Any
    source: PESStreamEvent
    validation_context: ST0601ValidationContext | None = None

    @property
    def pts_seconds(self) -> float | None:
        return None if self.pts is None else self.pts / 90_000


ST0601ContextProvider = Callable[
    [PESStreamEvent, KLVPacket], ST0601ValidationContext | None
]


@dataclass(slots=True)
class _FragmentState:
    data: bytearray
    pts: int
    random_access: bool


@dataclass(frozen=True, slots=True)
class MetadataDecoderResetReport:
    """Metadata state intentionally discarded at an input-session boundary."""

    asynchronous_klv_bytes: int = 0
    asynchronous_partial_items: int = 0
    synchronous_fragment_bytes: int = 0
    synchronous_partial_access_units: int = 0
    metadata_pids: int = 0
    metadata_services: int = 0


def decode_known_klv(
    packet: KLVPacket,
    *,
    field_decoding: FieldDecodingMode = FieldDecodingMode.STRICT,
    ontology_resolver: OntologyResolver | None = None,
    st0601_context: ST0601ValidationContext | None = None,
) -> Any:
    """Decode a recognized MISB Universal Set, or return ``None`` if unknown."""

    if st0601_context is not None and not isinstance(
        st0601_context, ST0601ValidationContext
    ):
        raise TypeError("st0601_context must be an ST0601ValidationContext or None")
    vmti_context = (
        VMTIValidationContext(ontology_resolver=ontology_resolver)
        if ontology_resolver is not None
        else None
    )
    if packet.key == ST0601_KEY:
        context = st0601_context
        if vmti_context is not None:
            configured_vmti = None if context is None else context.vmti_context
            if configured_vmti is not None:
                if (
                    configured_vmti.ontology_resolver is not None
                    and configured_vmti.ontology_resolver is not ontology_resolver
                ):
                    raise ValueError(
                        "st0601_context and ontology_resolver specify different "
                        "ontology resolvers"
                    )
                vmti_context = replace(
                    configured_vmti,
                    ontology_resolver=ontology_resolver,
                )
            context = replace(
                context or ST0601ValidationContext(),
                vmti_context=vmti_context,
            )
        return decode_uas_local_set(
            packet,
            field_decoding=field_decoding,
            context=context,
        )
    if packet.key == VMTI_KEY:
        return decode_vmti_local_set(packet, context=vmti_context)
    return None


class MetadataStreamDecoder:
    """Convert KLVA PES events into complete, typed KLV packet events.

    A decoder may follow a changing PMT with :meth:`reconfigure`. State for an
    unchanged PID/carriage pair is retained. Removing a PID or changing its
    carriage is accepted only at a complete KLV/access-unit boundary, then all
    carriage-specific state for that PID is discarded.
    """

    def __init__(
        self,
        *,
        max_klv_value_length: int = 64 * 1024 * 1024,
        max_access_unit_length: int = 64 * 1024 * 1024,
        max_services_per_pid: int = 256,
        field_decoding: FieldDecodingMode = FieldDecodingMode.STRICT,
        validate_sequence: bool = True,
        ontology_resolver: OntologyResolver | None = None,
        st0601_context_provider: ST0601ContextProvider | None = None,
    ) -> None:
        if max_klv_value_length < 0:
            raise ValueError("max_klv_value_length cannot be negative")
        if max_access_unit_length < 1:
            raise ValueError("max_access_unit_length must be positive")
        if not 1 <= max_services_per_pid <= 256:
            raise ValueError("max_services_per_pid must be between 1 and 256")
        if not isinstance(field_decoding, FieldDecodingMode):
            raise TypeError("field_decoding must be a FieldDecodingMode")
        if not isinstance(validate_sequence, bool):
            raise TypeError("validate_sequence must be boolean")
        if ontology_resolver is not None and not isinstance(
            ontology_resolver, OntologyResolver
        ):
            raise TypeError("ontology_resolver must implement resolve_entity or be None")
        if st0601_context_provider is not None and not callable(
            st0601_context_provider
        ):
            raise TypeError("st0601_context_provider must be callable or None")
        self.max_klv_value_length = max_klv_value_length
        self.max_access_unit_length = max_access_unit_length
        self.max_services_per_pid = max_services_per_pid
        self.field_decoding = field_decoding
        self.validate_sequence = validate_sequence
        self.ontology_resolver = ontology_resolver
        self.st0601_context_provider = st0601_context_provider
        self._async_parsers: dict[int, KLVStreamParser] = {}
        self._async_synchronized: set[int] = set()
        self._next_sequence: dict[int, int] = {}
        self._services: dict[int, set[int]] = {}
        self._fragments: dict[tuple[int, int], _FragmentState] = {}
        self._carriage_by_pid: dict[int, KLVCarriage] = {}
        self._topology_configured = False

    def feed(self, event: PESStreamEvent) -> list[KLVMetadataEvent]:
        """Consume one demultiplexed KLVA PES event."""

        if event.kind is not StreamKind.KLV or event.klv_carriage is None:
            raise ValueError("MetadataStreamDecoder requires a KLV PES event")
        expected = self._carriage_by_pid.get(event.pid)
        if self._topology_configured and expected is None:
            raise DecodeError(
                f"KLV PID {event.pid} is not in the active metadata topology"
            )
        if expected is not None and event.klv_carriage is not expected:
            raise DecodeError(
                f"KLV PID {event.pid} changed from {expected.value} to "
                f"{event.klv_carriage.value} carriage without safe reconfiguration"
            )
        self._carriage_by_pid[event.pid] = event.klv_carriage
        if event.klv_carriage is KLVCarriage.ASYNCHRONOUS:
            return self._feed_async(event)
        return self._feed_sync(event)

    def validate_reconfiguration(
        self,
        active_streams: Mapping[int, KLVCarriage],
    ) -> None:
        """Validate that ``active_streams`` can replace the current topology.

        This method does not mutate decoder state. It is useful when a caller
        must validate several cooperating components before committing an
        atomic program-map transition.
        """

        topology = self._validate_topology(active_streams)
        self._validate_clean_transition(topology)

    def reconfigure(self, active_streams: Mapping[int, KLVCarriage]) -> None:
        """Atomically adopt an authoritative KLVA PID/carriage topology."""

        topology = self._validate_topology(active_streams)
        affected = self._validate_clean_transition(topology)
        for pid in affected:
            self._async_parsers.pop(pid, None)
            self._async_synchronized.discard(pid)
            self._next_sequence.pop(pid, None)
            self._services.pop(pid, None)
            for key in tuple(self._fragments):
                if key[0] == pid:
                    del self._fragments[key]
        self._carriage_by_pid = topology
        self._topology_configured = True

    @staticmethod
    def _validate_topology(
        active_streams: Mapping[int, KLVCarriage],
    ) -> dict[int, KLVCarriage]:
        if not isinstance(active_streams, Mapping):
            raise TypeError("active_streams must be a mapping")
        topology: dict[int, KLVCarriage] = {}
        for pid, carriage in active_streams.items():
            if (
                isinstance(pid, bool)
                or not isinstance(pid, int)
                or not 0 <= pid <= 0x1FFF
            ):
                raise ValueError("metadata PID must be an integer from 0 to 8191")
            if not isinstance(carriage, KLVCarriage):
                raise TypeError("metadata carriage must be a KLVCarriage")
            topology[pid] = carriage
        return topology

    def _validate_clean_transition(
        self,
        topology: Mapping[int, KLVCarriage],
    ) -> set[int]:
        affected = {
            pid
            for pid, carriage in self._carriage_by_pid.items()
            if topology.get(pid) is not carriage
        }
        for pid in affected:
            parser = self._async_parsers.get(pid)
            if parser is not None and parser.buffered_bytes:
                raise DecodeError(
                    f"cannot reconfigure KLV PID {pid} with a partial asynchronous KLV item"
                )
            if any(fragment_pid == pid for fragment_pid, _ in self._fragments):
                raise DecodeError(
                    f"cannot reconfigure KLV PID {pid} with a partial synchronous "
                    "metadata access unit"
                )
        return affected

    def finish(self) -> None:
        """Reject truncated KLV packets or fragmented metadata access units."""

        for parser in self._async_parsers.values():
            parser.finish()
        if self._fragments:
            pid, service_id = next(iter(self._fragments))
            raise TruncatedData(
                f"synchronous metadata ended with incomplete access unit on PID {pid}, "
                f"service {service_id}"
            )

    def reset(self) -> MetadataDecoderResetReport:
        """Discard partial metadata and topology for a new input session.

        Unlike :meth:`finish`, reset deliberately accepts truncation because a
        reconnect means the previous source can no longer complete it. Decode
        limits, field policy, validation policy, and callback configuration are
        retained. Sequence validation begins a new epoch on the next cell.
        """

        report = MetadataDecoderResetReport(
            asynchronous_klv_bytes=sum(
                parser.buffered_bytes for parser in self._async_parsers.values()
            ),
            asynchronous_partial_items=sum(
                parser.buffered_bytes > 0 for parser in self._async_parsers.values()
            ),
            synchronous_fragment_bytes=sum(
                len(fragment.data) for fragment in self._fragments.values()
            ),
            synchronous_partial_access_units=len(self._fragments),
            metadata_pids=len(self._carriage_by_pid),
            metadata_services=sum(len(services) for services in self._services.values()),
        )
        self._async_parsers.clear()
        self._async_synchronized.clear()
        self._next_sequence.clear()
        self._services.clear()
        self._fragments.clear()
        self._carriage_by_pid.clear()
        self._topology_configured = False
        return report

    def _feed_async(self, event: PESStreamEvent) -> list[KLVMetadataEvent]:
        pes = event.pes
        if pes.stream_id != 0xBD:
            raise DecodeError("asynchronous KLVA PES stream_id must be 0xBD")
        if pes.packet_length == 0:
            raise DecodeError(
                "asynchronous KLVA PES requires a non-zero PES_packet_length"
            )
        if pes.pts is not None or pes.dts is not None:
            raise DecodeError("asynchronous KLVA PES must not contain PTS or DTS")
        if pes.escr_flag:
            raise DecodeError("asynchronous KLVA PES must not assert the ESCR flag")
        if event.pid not in self._async_synchronized:
            if not pes.data_alignment_indicator:
                return []
            self._async_synchronized.add(event.pid)
        parser = self._async_parsers.setdefault(
            event.pid,
            KLVStreamParser(max_value_length=self.max_klv_value_length),
        )
        if not pes.payload:
            # Empty private-stream PES packets occur in deployed STANAG 4609
            # files. They contain no first data byte and cannot change KLV
            # parser alignment, regardless of the otherwise meaningless flag.
            return []
        begins_item = parser.buffered_bytes == 0
        if begins_item and not pes.data_alignment_indicator:
            raise DecodeError(
                "asynchronous KLVA data_alignment_indicator must be set when the PES "
                "payload begins a KLV item"
            )
        if not begins_item and pes.data_alignment_indicator:
            raise DecodeError(
                "asynchronous KLVA data_alignment_indicator must be clear when the PES "
                "payload continues a KLV item"
            )
        return [
            self._event(packet, event, service_id=None, random_access=False)
            for packet in parser.feed(pes.payload)
        ]

    def _feed_sync(self, event: PESStreamEvent) -> list[KLVMetadataEvent]:
        pes = event.pes
        if pes.stream_id != 0xFC:
            raise DecodeError("synchronous KLVA PES stream_id must be 0xFC")
        if pes.pts is None:
            raise DecodeError("synchronous KLVA PES requires a PTS")
        if pes.dts is not None:
            raise DecodeError("synchronous KLVA PES must not contain a DTS")
        if not pes.data_alignment_indicator:
            raise DecodeError("synchronous KLVA PES must begin on a Metadata AU cell")

        result: list[KLVMetadataEvent] = []
        declared_services = klva_metadata_service_ids(event.stream)
        for cell in parse_metadata_au_cells(pes.payload):
            if cell.metadata_service_id not in declared_services:
                raise DecodeError(
                    f"ST 1402-15 requires metadata service "
                    f"{cell.metadata_service_id} on PID {event.pid} to have a matching "
                    "PMT metadata_descriptor"
                )
            if self.validate_sequence:
                self._validate_cell_sequence(event.pid, cell)
            services = self._services.setdefault(event.pid, set())
            services.add(cell.metadata_service_id)
            if len(services) > self.max_services_per_pid:
                raise LimitExceeded(
                    f"synchronous metadata PID {event.pid} exceeds "
                    f"{self.max_services_per_pid} services"
                )
            access_unit = self._consume_cell(event, cell)
            if access_unit is not None:
                data, random_access = access_unit
                result.extend(
                    self._decode_access_unit(
                        data,
                        event,
                        service_id=cell.metadata_service_id,
                        random_access=random_access,
                    )
                )
        return result

    def _validate_cell_sequence(self, pid: int, cell: MetadataAUCell) -> None:
        expected = self._next_sequence.get(pid)
        if expected is not None and cell.sequence_number != expected:
            raise DecodeError(
                f"metadata AU cell sequence is {cell.sequence_number}, expected {expected} "
                f"on PID {pid}"
            )
        self._next_sequence[pid] = (cell.sequence_number + 1) & 0xFF

    def _consume_cell(
        self,
        event: PESStreamEvent,
        cell: MetadataAUCell,
    ) -> tuple[bytes, bool] | None:
        assert event.pes.pts is not None
        key = (event.pid, cell.metadata_service_id)
        state = self._fragments.get(key)
        if cell.fragmentation is CellFragmentation.COMPLETE:
            if state is not None:
                raise DecodeError("complete metadata AU cell interrupts a fragmented access unit")
            self._check_access_unit_size(len(cell.data))
            return cell.data, cell.random_access
        if cell.fragmentation is CellFragmentation.FIRST:
            if state is not None:
                raise DecodeError("first metadata AU cell interrupts a fragmented access unit")
            self._check_access_unit_size(len(cell.data))
            self._fragments[key] = _FragmentState(
                bytearray(cell.data), event.pes.pts, cell.random_access
            )
            return None
        if state is None:
            raise DecodeError("metadata AU continuation cell has no first fragment")
        if state.pts != event.pes.pts:
            raise DecodeError("metadata AU fragments do not carry the same PTS")
        self._check_access_unit_size(len(state.data) + len(cell.data))
        state.data.extend(cell.data)
        if cell.fragmentation is CellFragmentation.LAST:
            del self._fragments[key]
            return bytes(state.data), state.random_access
        return None

    def _check_access_unit_size(self, size: int) -> None:
        if size > self.max_access_unit_length:
            raise LimitExceeded(
                f"metadata access unit exceeds configured limit {self.max_access_unit_length}"
            )

    def _decode_access_unit(
        self,
        data: bytes,
        event: PESStreamEvent,
        *,
        service_id: int,
        random_access: bool,
    ) -> list[KLVMetadataEvent]:
        parser = KLVStreamParser(max_value_length=self.max_klv_value_length)
        packets = parser.feed(data)
        packets.extend(parser.finish())
        return [
            self._event(
                packet,
                event,
                service_id=service_id,
                random_access=random_access,
            )
            for packet in packets
        ]

    def _event(
        self,
        packet: KLVPacket,
        source: PESStreamEvent,
        *,
        service_id: int | None,
        random_access: bool,
    ) -> KLVMetadataEvent:
        assert source.klv_carriage is not None
        st0601_context = None
        if packet.key == ST0601_KEY and self.st0601_context_provider is not None:
            st0601_context = self.st0601_context_provider(source, packet)
            if st0601_context is not None and not isinstance(
                st0601_context, ST0601ValidationContext
            ):
                raise TypeError(
                    "st0601_context_provider must return an "
                    "ST0601ValidationContext or None"
                )
        return KLVMetadataEvent(
            program_number=source.program_number,
            pid=source.pid,
            carriage=source.klv_carriage,
            pts=source.pes.pts,
            metadata_service_id=service_id,
            random_access=random_access,
            packet=packet,
            decoded=decode_known_klv(
                packet,
                field_decoding=self.field_decoding,
                ontology_resolver=self.ontology_resolver,
                st0601_context=st0601_context,
            ),
            source=source,
            validation_context=st0601_context,
        )
