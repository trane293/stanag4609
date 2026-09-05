"""Bounded exact PCR-window validation for synchronous metadata T-STD state."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from fractions import Fraction

from stanag4609.errors import DecodeError, LimitExceeded
from stanag4609.transport.demux import PESStreamEvent, ProgramClockEvent, StreamKind
from stanag4609.transport.metadata import (
    MetadataSTDDescriptor,
    decode_metadata_std_descriptor,
)
from stanag4609.transport.pcr import PCR_CLOCK_RATE, unwrap_pcr_ticks
from stanag4609.transport.psi import KLVCarriage, ProgramMapTable, find_klv_streams
from stanag4609.transport.std import (
    PCR_BASE_LAST_BYTE_INDEX,
    AsynchronousMetadataSTDByte,
    IncrementalAsynchronousMetadataSTDModel,
    IncrementalSynchronousMetadataSTDModel,
    MetadataSTDByte,
    MetadataSTDModelIssue,
    asynchronous_metadata_std_bytes_from_pes,
    metadata_std_bytes_from_pes,
)


def _positive_integer(value: int, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class MetadataSTDStreamIssue:
    """One exact model failure with its transport stream identity."""

    code: str
    requirement: str
    program_number: int
    pid: int
    source_offset: int | None
    time: Fraction | None
    fullness: int | None
    capacity: int | None
    delay: Fraction | None
    permitted_delay: Fraction | None
    message: str


@dataclass(frozen=True, slots=True)
class _ClockSample:
    event: ProgramClockEvent
    offset: int
    ticks: int


@dataclass(frozen=True, slots=True)
class _PendingPES:
    event: PESStreamEvent
    descriptor: MetadataSTDDescriptor
    transport_bytes: int

    @property
    def first_offset(self) -> int:
        return self.event.pes.transport_packets[0].offset

    @property
    def last_offset(self) -> int:
        packet = self.event.pes.transport_packets[-1]
        return packet.offset + len(packet.raw) - 1


@dataclass(slots=True)
class _ModelState:
    model: (
        IncrementalSynchronousMetadataSTDModel
        | IncrementalAsynchronousMetadataSTDModel
    )
    reported_issues: int = 0
    exact_pes: int = 0


class MetadataSTDStreamValidator:
    """Resolve exact metadata occupancy one bounded PCR window at a time.

    PES packets wait only for their following PCR. Resolved byte events are fed
    to an incremental model and retired at the new exact clock watermark.
    Descriptor changes and missing brackets remain explicit coverage gaps; they
    are never converted into conformance passes.
    """

    __slots__ = (
        "_asynchronous_descriptors",
        "_carriages",
        "_clock_samples",
        "_configs",
        "_disabled",
        "_exact_pes",
        "_finished",
        "_max_clock_points",
        "_max_pending_pes",
        "_max_pending_removal_groups",
        "_max_pending_timeline_events",
        "_max_pending_transport_bytes",
        "_models",
        "_observed_pes",
        "_pending",
        "_pending_transport_bytes",
        "_unverifiable_pes",
        "_violations",
    )

    def __init__(
        self,
        *,
        max_clock_points: int = 64,
        max_pending_pes: int = 1_024,
        max_pending_transport_bytes: int = 8 * 1024 * 1024,
        max_pending_timeline_events: int = 1_000_000,
        max_pending_removal_groups: int = 65_536,
        asynchronous_descriptors: Mapping[
            tuple[int, int], MetadataSTDDescriptor
        ] | None = None,
    ) -> None:
        self._max_clock_points = _positive_integer(
            max_clock_points, name="max_clock_points"
        )
        if self._max_clock_points < 2:
            raise ValueError("max_clock_points must be at least two")
        self._max_pending_pes = _positive_integer(
            max_pending_pes, name="max_pending_pes"
        )
        self._max_pending_transport_bytes = _positive_integer(
            max_pending_transport_bytes, name="max_pending_transport_bytes"
        )
        self._max_pending_timeline_events = _positive_integer(
            max_pending_timeline_events, name="max_pending_timeline_events"
        )
        self._max_pending_removal_groups = _positive_integer(
            max_pending_removal_groups, name="max_pending_removal_groups"
        )
        if asynchronous_descriptors is None:
            asynchronous_descriptors = {}
        if not isinstance(asynchronous_descriptors, Mapping):
            raise TypeError("asynchronous_descriptors must be a mapping")
        validated_descriptors: dict[tuple[int, int], MetadataSTDDescriptor] = {}
        for key, descriptor in asynchronous_descriptors.items():
            if not isinstance(key, tuple) or len(key) != 2:
                raise ValueError(
                    "asynchronous descriptor key must be a (program, PID) pair"
                )
            program_number, pid = key
            if (
                isinstance(program_number, bool)
                or not isinstance(program_number, int)
                or not 1 <= program_number <= 0xFFFF
            ):
                raise ValueError("asynchronous descriptor program number must be 1..65535")
            if (
                isinstance(pid, bool)
                or not isinstance(pid, int)
                or not 0 <= pid <= 0x1FFF
            ):
                raise ValueError("asynchronous descriptor PID must be 0..8191")
            if not isinstance(descriptor, MetadataSTDDescriptor):
                raise TypeError(
                    "asynchronous descriptor values must be MetadataSTDDescriptor"
                )
            validated_descriptors[key] = descriptor
        self._clock_samples: dict[int, list[_ClockSample]] = {}
        self._configs: dict[tuple[int, int], MetadataSTDDescriptor] = {}
        self._asynchronous_descriptors = validated_descriptors
        self._carriages: dict[tuple[int, int], KLVCarriage] = {}
        self._disabled: set[tuple[int, int]] = set()
        self._models: dict[tuple[int, int], _ModelState] = {}
        self._pending: list[_PendingPES] = []
        self._pending_transport_bytes = 0
        self._observed_pes = 0
        self._exact_pes = 0
        self._unverifiable_pes = 0
        self._violations = 0
        self._finished = False

    @property
    def observed_pes(self) -> int:
        return self._observed_pes

    @property
    def exact_pes(self) -> int:
        return self._exact_pes

    @property
    def unverifiable_pes(self) -> int:
        return self._unverifiable_pes

    @property
    def pending_pes(self) -> int:
        return len(self._pending)

    @property
    def pending_transport_bytes(self) -> int:
        return self._pending_transport_bytes

    @property
    def violations(self) -> int:
        return self._violations

    @property
    def compliant(self) -> bool:
        return (
            self._observed_pes > 0
            and self._exact_pes == self._observed_pes
            and self._violations == 0
            and not self._disabled
        )

    def observe_pmt(
        self, table: ProgramMapTable
    ) -> tuple[MetadataSTDStreamIssue, ...]:
        """Install stable metadata carriage and available STD declarations."""

        if not isinstance(table, ProgramMapTable):
            raise TypeError("table must be a ProgramMapTable")
        self._require_open()
        carriage = dict(find_klv_streams(table))
        active: set[tuple[int, int]] = set()
        for stream in table.streams:
            selected_carriage = carriage.get(stream.elementary_pid)
            if selected_carriage is None:
                continue
            key = (table.program_number, stream.elementary_pid)
            active.add(key)
            previous_carriage = self._carriages.get(key)
            if previous_carriage is not None and previous_carriage is not selected_carriage:
                self._disabled.add(key)
                self._configs.pop(key, None)
                continue
            self._carriages[key] = selected_carriage
            selected_descriptor: MetadataSTDDescriptor | None
            if selected_carriage is KLVCarriage.SYNCHRONOUS:
                descriptors = [item for item in stream.descriptors if item.tag == 0x27]
                if len(descriptors) != 1:
                    self._configs.pop(key, None)
                    continue
                try:
                    selected_descriptor = decode_metadata_std_descriptor(descriptors[0])
                except DecodeError:
                    self._configs.pop(key, None)
                    continue
            else:
                selected_descriptor = self._asynchronous_descriptors.get(key)
                if selected_descriptor is None:
                    self._configs.pop(key, None)
                    continue
            previous = self._configs.get(key)
            if previous is not None and previous != selected_descriptor:
                self._disabled.add(key)
                self._configs.pop(key, None)
                continue
            if key not in self._disabled:
                self._configs[key] = selected_descriptor
        stale = {
            key
            for key in self._carriages
            if key[0] == table.program_number and key not in active
        }
        for key in stale:
            self._configs.pop(key, None)
            self._carriages.pop(key, None)
            self._disabled.add(key)
        return ()

    def observe_pes(
        self, event: PESStreamEvent
    ) -> tuple[MetadataSTDStreamIssue, ...]:
        """Queue one supported metadata PES until its following PCR arrives."""

        if not isinstance(event, PESStreamEvent):
            raise TypeError("event must be a PESStreamEvent")
        self._require_open()
        if event.kind is not StreamKind.KLV or event.klv_carriage is None:
            return ()
        if (
            event.klv_carriage is KLVCarriage.SYNCHRONOUS
            and event.pes.pts is None
        ):
            return ()
        self._observed_pes += 1
        key = (event.program_number, event.pid)
        descriptor = self._configs.get(key)
        if descriptor is None or not event.pes.transport_packets:
            self._unverifiable_pes += 1
            return ()
        samples = self._clock_samples.get(event.program_number, ())
        first_offset = event.pes.transport_packets[0].offset
        if not any(sample.offset <= first_offset for sample in samples):
            self._unverifiable_pes += 1
            return ()
        transport_bytes = sum(len(packet.raw) for packet in event.pes.transport_packets)
        if transport_bytes > self._max_pending_transport_bytes:
            self._unverifiable_pes += 1
            self._disabled.add(key)
            return ()
        while self._pending and (
            len(self._pending) >= self._max_pending_pes
            or self._pending_transport_bytes + transport_bytes
            > self._max_pending_transport_bytes
        ):
            dropped = self._pending.pop(0)
            self._pending_transport_bytes -= dropped.transport_bytes
            self._unverifiable_pes += 1
            self._disabled.add((dropped.event.program_number, dropped.event.pid))
        self._pending.append(_PendingPES(event, descriptor, transport_bytes))
        self._pending_transport_bytes += transport_bytes
        return ()

    def observe_clock(
        self, event: ProgramClockEvent
    ) -> tuple[MetadataSTDStreamIssue, ...]:
        """Resolve every PES now bracketed by this program clock sample."""

        if not isinstance(event, ProgramClockEvent):
            raise TypeError("event must be a ProgramClockEvent")
        self._require_open()
        if event.discontinuity:
            issues = self._reset_program(event.program_number)
            self._clock_samples[event.program_number] = [
                _ClockSample(
                    event,
                    event.source_offset + PCR_BASE_LAST_BYTE_INDEX,
                    event.pcr.ticks,
                )
            ]
            return issues
        samples = self._clock_samples.get(event.program_number)
        reference = None if not samples else samples[-1].ticks
        ticks = unwrap_pcr_ticks(event.pcr.ticks, reference=reference)
        sample = _ClockSample(
            event,
            event.source_offset + PCR_BASE_LAST_BYTE_INDEX,
            ticks,
        )
        if samples and (sample.offset <= samples[-1].offset or ticks <= samples[-1].ticks):
            issues = self._reset_program(event.program_number)
            samples = None
        else:
            issues = ()
        if samples is None:
            samples = []
            self._clock_samples[event.program_number] = samples
        samples.append(sample)

        resolvable: list[_PendingPES] = []
        retained: list[_PendingPES] = []
        for pending in self._pending:
            if (
                pending.event.program_number == event.program_number
                and pending.last_offset <= sample.offset
            ):
                resolvable.append(pending)
                self._pending_transport_bytes -= pending.transport_bytes
            else:
                retained.append(pending)
        self._pending = retained
        generated: dict[
            tuple[int, int],
            list[MetadataSTDByte | AsynchronousMetadataSTDByte],
        ] = defaultdict(list)
        generated_counts: dict[tuple[int, int], int] = defaultdict(int)
        for pending in resolvable:
            key = (pending.event.program_number, pending.event.pid)
            if key in self._disabled:
                self._unverifiable_pes += 1
                continue
            preceding = max(
                (
                    index
                    for index, point in enumerate(samples)
                    if point.offset <= pending.first_offset
                ),
                default=-1,
            )
            if preceding < 0:
                self._unverifiable_pes += 1
                continue
            relevant = samples[preceding:]
            try:
                if pending.event.klv_carriage is KLVCarriage.ASYNCHRONOUS:
                    asynchronous = asynchronous_metadata_std_bytes_from_pes(
                        pending.event, tuple(point.event for point in relevant)
                    )
                    local: tuple[
                        MetadataSTDByte | AsynchronousMetadataSTDByte, ...
                    ] = asynchronous
                else:
                    local = metadata_std_bytes_from_pes(
                        pending.event, tuple(point.event for point in relevant)
                    )
            except (DecodeError, ValueError):
                self._unverifiable_pes += 1
                self._disabled.add(key)
                continue
            shift = Fraction(
                relevant[0].ticks - relevant[0].event.pcr.ticks,
                PCR_CLOCK_RATE,
            )
            if pending.event.klv_carriage is KLVCarriage.ASYNCHRONOUS:
                generated[key].extend(
                    replace(value, arrival_time=value.arrival_time + shift)
                    for value in local
                )
            else:
                generated[key].extend(
                    replace(
                        value,
                        arrival_time=value.arrival_time + shift,
                        removal_time=(
                            None
                            if value.removal_time is None
                            else value.removal_time + shift
                        ),
                    )
                    for value in local
                    if isinstance(value, MetadataSTDByte)
                )
            generated_counts[key] += 1

        output = list(issues)
        for key, values in generated.items():
            state = self._models.get(key)
            if state is None:
                if self._carriages[key] is KLVCarriage.ASYNCHRONOUS:
                    model: (
                        IncrementalSynchronousMetadataSTDModel
                        | IncrementalAsynchronousMetadataSTDModel
                    ) = IncrementalAsynchronousMetadataSTDModel(
                        self._configs[key],
                        max_pending_events=self._max_pending_timeline_events,
                    )
                else:
                    model = IncrementalSynchronousMetadataSTDModel(
                        self._configs[key],
                        max_pending_timeline_events=self._max_pending_timeline_events,
                        max_pending_removal_groups=self._max_pending_removal_groups,
                    )
                state = _ModelState(model)
                self._models[key] = state
            values.sort(
                key=lambda value: (
                    value.arrival_time,
                    -1 if value.source_offset is None else value.source_offset,
                )
            )
            try:
                if isinstance(state.model, IncrementalAsynchronousMetadataSTDModel):
                    async_values = tuple(
                        value
                        for value in values
                        if isinstance(value, AsynchronousMetadataSTDByte)
                    )
                    if len(async_values) != len(values):
                        raise ValueError("metadata STD carriage changed within one epoch")
                    model_issues = state.model.feed(async_values)
                else:
                    synchronous_values = tuple(
                        value for value in values if isinstance(value, MetadataSTDByte)
                    )
                    if len(synchronous_values) != len(values):
                        raise ValueError("metadata STD carriage changed within one epoch")
                    model_issues = state.model.feed(synchronous_values)
                output.extend(self._wrap(key, state, model_issues))
                self._exact_pes += generated_counts[key]
                state.exact_pes += generated_counts[key]
            except (LimitExceeded, ValueError):
                self._exact_pes -= state.exact_pes
                self._unverifiable_pes += state.exact_pes + generated_counts[key]
                self._disabled.add(key)
                self._models.pop(key, None)

        watermark = Fraction(ticks, PCR_CLOCK_RATE)
        for key, state in tuple(self._models.items()):
            if key[0] != event.program_number:
                continue
            output.extend(self._wrap(key, state, state.model.advance(watermark)))
        self._trim_clocks(event.program_number)
        return tuple(output)

    def finish(self) -> tuple[MetadataSTDStreamIssue, ...]:
        """Finalize exact state and classify all trailing unbracketed PES."""

        self._require_open()
        self._unverifiable_pes += len(self._pending)
        self._pending.clear()
        self._pending_transport_bytes = 0
        output: list[MetadataSTDStreamIssue] = []
        for key, state in self._models.items():
            result = state.model.finish()
            output.extend(self._wrap(key, state, result.issues[state.reported_issues :]))
        self._models.clear()
        self._finished = True
        return tuple(output)

    def _wrap(
        self,
        key: tuple[int, int],
        state: _ModelState,
        issues: Sequence[MetadataSTDModelIssue],
    ) -> tuple[MetadataSTDStreamIssue, ...]:
        state.reported_issues += len(issues)
        self._violations += len(issues)
        return tuple(
            MetadataSTDStreamIssue(
                issue.code,
                issue.requirement,
                key[0],
                key[1],
                issue.source_offset,
                issue.time,
                issue.fullness,
                issue.capacity,
                issue.delay,
                issue.permitted_delay,
                issue.message,
            )
            for issue in issues
        )

    def _reset_program(self, program_number: int) -> tuple[MetadataSTDStreamIssue, ...]:
        removed = [
            pending
            for pending in self._pending
            if pending.event.program_number == program_number
        ]
        self._unverifiable_pes += len(removed)
        self._pending_transport_bytes -= sum(item.transport_bytes for item in removed)
        self._pending = [
            pending
            for pending in self._pending
            if pending.event.program_number != program_number
        ]
        output: list[MetadataSTDStreamIssue] = []
        for key, state in tuple(self._models.items()):
            if key[0] != program_number:
                continue
            result = state.model.finish()
            output.extend(self._wrap(key, state, result.issues[state.reported_issues :]))
            del self._models[key]
        self._clock_samples.pop(program_number, None)
        self._disabled = {key for key in self._disabled if key[0] != program_number}
        return tuple(output)

    def _trim_clocks(self, program_number: int) -> None:
        samples = self._clock_samples[program_number]
        if len(samples) <= self._max_clock_points:
            return
        del samples[: len(samples) - self._max_clock_points]
        oldest = samples[0].offset
        retained: list[_PendingPES] = []
        for pending in self._pending:
            if (
                pending.event.program_number == program_number
                and pending.first_offset < oldest
            ):
                self._unverifiable_pes += 1
                self._pending_transport_bytes -= pending.transport_bytes
                self._disabled.add((program_number, pending.event.pid))
            else:
                retained.append(pending)
        self._pending = retained

    def _require_open(self) -> None:
        if self._finished:
            raise RuntimeError("metadata STD stream validator is already finished")
