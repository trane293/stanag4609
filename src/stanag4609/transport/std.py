"""Exact and conservative System Target Decoder metadata auditing."""

from __future__ import annotations

import math
from bisect import bisect_right
from collections import defaultdict, deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from heapq import heappop, heappush
from itertools import pairwise
from typing import cast

from stanag4609.errors import LimitExceeded
from stanag4609.transport.demux import PESStreamEvent, ProgramClockEvent, StreamKind
from stanag4609.transport.metadata import MetadataSTDDescriptor
from stanag4609.transport.pcr import PCR_CLOCK_RATE, unwrap_pcr_ticks
from stanag4609.transport.psi import KLVCarriage
from stanag4609.transport.timing import PTS_CLOCK_RATE, unwrap_pts

ST1402_MAX_METADATA_DELAY = Fraction(1)
H222_TRANSPORT_BUFFER_SIZE = 512
PCR_BASE_LAST_BYTE_INDEX = 10


def _delay(value: Fraction | int | float) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (Fraction, int, float)):
        raise TypeError("maximum_delay must be a Fraction, integer, or float")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("maximum_delay must be finite")
        return Fraction(str(value))
    return Fraction(value)


def _positive_integer(value: int, *, name: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")
    return value


def _time(value: Fraction | int | float, *, name: str) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (Fraction, int, float)):
        raise TypeError(f"{name} must be a Fraction, integer, or float")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
        return Fraction(str(value))
    return Fraction(value)


@dataclass(frozen=True, slots=True)
class MetadataSTDByte:
    """One exactly timed byte at the input of the metadata T-STD.

    Transport headers and adaptation bytes set ``enters_main_buffer`` false.
    PES header and content bytes set it true and identify the PTS-controlled
    removal time. Only bytes that are actually part of a metadata access unit
    set ``access_unit_byte``; this distinction makes the normative one-second
    decoder-delay calculation exclude PES overhead while retaining that
    overhead in buffer occupancy.
    """

    arrival_time: Fraction | int | float
    enters_main_buffer: bool = False
    removal_time: Fraction | int | float | None = None
    access_unit_byte: bool = False
    source_offset: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "arrival_time",
            _time(self.arrival_time, name="arrival_time"),
        )
        if not isinstance(self.enters_main_buffer, bool):
            raise TypeError("enters_main_buffer must be a boolean")
        if not isinstance(self.access_unit_byte, bool):
            raise TypeError("access_unit_byte must be a boolean")
        if self.enters_main_buffer and self.removal_time is None:
            raise ValueError("removal_time is required for a main-buffer byte")
        if not self.enters_main_buffer and self.removal_time is not None:
            raise ValueError("removal_time requires enters_main_buffer=True")
        if self.access_unit_byte and not self.enters_main_buffer:
            raise ValueError("access_unit_byte requires enters_main_buffer=True")
        if self.removal_time is not None:
            object.__setattr__(
                self,
                "removal_time",
                _time(self.removal_time, name="removal_time"),
            )
        if self.source_offset is not None and (
            isinstance(self.source_offset, bool)
            or not isinstance(self.source_offset, int)
            or self.source_offset < 0
        ):
            raise ValueError("source_offset must be a non-negative integer or None")


@dataclass(frozen=True, slots=True)
class AsynchronousMetadataSTDByte:
    """One exactly timed byte in an asynchronous metadata transport stream.

    Every complete TS byte enters ``TBn``. Only PES header and content bytes
    enter ``Bn`` after leaking from ``TBn``; transport headers and adaptation
    bytes leave the model at that boundary.
    """

    arrival_time: Fraction | int | float
    enters_main_buffer: bool = False
    source_offset: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "arrival_time",
            _time(self.arrival_time, name="arrival_time"),
        )
        if not isinstance(self.enters_main_buffer, bool):
            raise TypeError("enters_main_buffer must be a boolean")
        if self.source_offset is not None and (
            isinstance(self.source_offset, bool)
            or not isinstance(self.source_offset, int)
            or self.source_offset < 0
        ):
            raise ValueError("source_offset must be a non-negative integer or None")


@dataclass(frozen=True, slots=True)
class MetadataSTDModelIssue:
    """One exact metadata T-STD conformance failure."""

    code: str
    requirement: str
    time: Fraction | None
    fullness: int | None
    capacity: int | None
    source_offset: int | None
    delay: Fraction | None
    permitted_delay: Fraction | None
    message: str


@dataclass(frozen=True, slots=True)
class MetadataSTDModelResult:
    """Exact occupancy and delay result for one synchronous metadata stream."""

    issues: tuple[MetadataSTDModelIssue, ...]
    transport_bytes: int
    main_buffer_bytes: int
    access_unit_bytes: int
    access_unit_removal_times: int
    maximum_transport_buffer_fullness: int
    maximum_main_buffer_fullness: int
    final_transport_buffer_fullness: int
    final_main_buffer_fullness: int
    maximum_transport_busy_interval: Fraction
    maximum_decoder_delay: Fraction | None
    minimum_decoder_delay: Fraction | None

    @property
    def conformant(self) -> bool:
        return not self.issues


@dataclass(frozen=True, slots=True)
class AsynchronousMetadataSTDModelResult:
    """Exact finite-stream occupancy result for asynchronous metadata."""

    issues: tuple[MetadataSTDModelIssue, ...]
    transport_bytes: int
    main_buffer_bytes: int
    maximum_transport_buffer_fullness: int
    maximum_main_buffer_fullness: int
    final_transport_buffer_fullness: int
    final_main_buffer_fullness: int
    maximum_transport_busy_interval: Fraction
    maximum_decoder_delay: Fraction | None
    minimum_decoder_delay: Fraction | None

    @property
    def conformant(self) -> bool:
        return not self.issues


@dataclass(slots=True)
class _RemovalGroup:
    expected: int = 0
    access_unit_bytes: int = 0
    earliest_access_arrival: Fraction | None = None
    latest_access_arrival: Fraction | None = None
    source_offset: int | None = None


@dataclass(slots=True)
class _IncrementalRemovalGroup(_RemovalGroup):
    arrived: int = 0
    remaining_entries: int = 0
    removed: bool = False


class IncrementalSynchronousMetadataSTDModel:
    """Bounded-window exact T-STD state for a continuous metadata stream.

    Each ``feed`` call is an atomic knowledge batch: all bytes in that batch
    sharing a removal time are registered before any of their timeline events
    are processed. ``advance`` then retires entries and removals through a
    nondecreasing exact watermark. This lets a PCR-window adapter preserve Bn
    occupancy across windows without retaining already processed byte events.
    """

    __slots__ = (
        "_access_count",
        "_access_removal_times",
        "_busy_issue_reported",
        "_busy_start",
        "_departures",
        "_descriptor",
        "_final_result",
        "_finished",
        "_groups",
        "_issues",
        "_last_departure",
        "_main_count",
        "_main_fullness",
        "_max_pending_removal_groups",
        "_max_pending_timeline_events",
        "_maximum_busy",
        "_maximum_decoder_delay",
        "_maximum_main",
        "_maximum_transport",
        "_minimum_delay",
        "_next_group_id",
        "_next_serial",
        "_overflow_reported",
        "_permitted_delay",
        "_previous_arrival",
        "_timeline",
        "_transport_buffer_size",
        "_transport_count",
        "_transport_overflow_reported",
        "_watermark",
        "_zero_rate_reported",
    )

    def __init__(
        self,
        descriptor: MetadataSTDDescriptor,
        *,
        transport_buffer_size: int = H222_TRANSPORT_BUFFER_SIZE,
        maximum_delay: Fraction | int | float = ST1402_MAX_METADATA_DELAY,
        max_pending_timeline_events: int = 1_000_000,
        max_pending_removal_groups: int = 65_536,
    ) -> None:
        if not isinstance(descriptor, MetadataSTDDescriptor):
            raise TypeError("descriptor must be a MetadataSTDDescriptor")
        self._descriptor = descriptor
        self._transport_buffer_size = _positive_integer(
            transport_buffer_size, name="transport_buffer_size"
        )
        self._permitted_delay = _delay(maximum_delay)
        if self._permitted_delay <= 0:
            raise ValueError("maximum_delay must be positive")
        self._max_pending_timeline_events = _positive_integer(
            max_pending_timeline_events, name="max_pending_timeline_events"
        )
        self._max_pending_removal_groups = _positive_integer(
            max_pending_removal_groups, name="max_pending_removal_groups"
        )
        self._departures: deque[Fraction] = deque()
        self._timeline: list[tuple[Fraction, int, int, int]] = []
        self._groups: dict[int, _IncrementalRemovalGroup] = {}
        self._issues: list[MetadataSTDModelIssue] = []
        self._finished = False
        self._final_result: MetadataSTDModelResult | None = None
        self._previous_arrival: Fraction | None = None
        self._watermark: Fraction | None = None
        self._last_departure: Fraction | None = None
        self._busy_start: Fraction | None = None
        self._maximum_busy = Fraction(0)
        self._main_fullness = 0
        self._maximum_main = 0
        self._maximum_transport = 0
        self._maximum_decoder_delay: Fraction | None = None
        self._minimum_delay: Fraction | None = None
        self._transport_count = 0
        self._main_count = 0
        self._access_count = 0
        self._access_removal_times: set[Fraction] = set()
        self._next_group_id = 0
        self._next_serial = 0
        self._transport_overflow_reported = False
        self._overflow_reported = False
        self._busy_issue_reported = False
        self._zero_rate_reported = False

    @property
    def pending_timeline_events(self) -> int:
        return len(self._timeline)

    @property
    def pending_removal_groups(self) -> int:
        return len(self._groups)

    def feed(
        self, values: Iterable[MetadataSTDByte]
    ) -> tuple[MetadataSTDModelIssue, ...]:
        """Register one complete knowledge batch in transport-arrival order."""

        self._require_open()
        try:
            batch = tuple(values)
        except TypeError as error:
            raise TypeError("values must be an iterable of MetadataSTDByte values") from error
        for value in batch:
            if not isinstance(value, MetadataSTDByte):
                raise TypeError("every value must be a MetadataSTDByte")
        arrivals = [cast(Fraction, value.arrival_time) for value in batch]
        if any(current < previous for previous, current in pairwise(arrivals)):
            raise ValueError("byte arrival times must be nondecreasing")
        if arrivals and self._previous_arrival is not None and arrivals[0] < self._previous_arrival:
            raise ValueError("byte arrival times must be nondecreasing across feed calls")
        if arrivals and self._watermark is not None and arrivals[0] < self._watermark:
            raise ValueError("byte arrival precedes the processed watermark")

        new_main_bytes = sum(value.enters_main_buffer for value in batch)
        new_removals = len(
            {
                cast(Fraction, value.removal_time)
                for value in batch
                if value.enters_main_buffer
            }
        )
        if (
            len(self._timeline) + new_main_bytes + new_removals
            > self._max_pending_timeline_events
        ):
            raise LimitExceeded(
                "metadata STD timeline would exceed "
                f"{self._max_pending_timeline_events} pending events"
            )
        if len(self._groups) + new_removals > self._max_pending_removal_groups:
            raise LimitExceeded(
                "metadata STD state would exceed "
                f"{self._max_pending_removal_groups} pending removal groups"
            )

        issue_start = len(self._issues)
        group_ids: dict[Fraction, int] = {}
        for value, arrival in zip(batch, arrivals, strict=True):
            if not value.enters_main_buffer:
                continue
            assert value.removal_time is not None
            removal = cast(Fraction, value.removal_time)
            group_id = group_ids.get(removal)
            if group_id is None:
                group_id = self._next_group_id
                self._next_group_id += 1
                group_ids[removal] = group_id
                self._groups[group_id] = _IncrementalRemovalGroup()
                self._push_timeline(removal, 1, group_id)
            group = self._groups[group_id]
            group.expected += 1
            group.remaining_entries += 1
            if group.source_offset is None:
                group.source_offset = value.source_offset
            if value.access_unit_byte:
                group.access_unit_bytes += 1
                group.earliest_access_arrival = (
                    arrival
                    if group.earliest_access_arrival is None
                    else min(group.earliest_access_arrival, arrival)
                )
                group.latest_access_arrival = (
                    arrival
                    if group.latest_access_arrival is None
                    else max(group.latest_access_arrival, arrival)
                )

        for value, arrival in zip(batch, arrivals, strict=True):
            self._feed_transport_byte(value, arrival, group_ids)
        if arrivals:
            self._previous_arrival = arrivals[-1]
        self._report_transport_interval_if_proven()
        return tuple(self._issues[issue_start:])

    def advance(
        self, watermark: Fraction | int | float
    ) -> tuple[MetadataSTDModelIssue, ...]:
        """Process all known buffer events at or before ``watermark``."""

        self._require_open()
        parsed = _time(watermark, name="watermark")
        if self._watermark is not None and parsed < self._watermark:
            raise ValueError("watermarks must be nondecreasing")
        if self._previous_arrival is not None and parsed < self._previous_arrival:
            raise ValueError("watermark cannot precede the latest byte arrival")
        issue_start = len(self._issues)
        self._process_timeline(parsed)
        while self._departures and self._departures[0] <= parsed:
            self._departures.popleft()
        if (
            self._busy_start is not None
            and self._last_departure is not None
            and self._last_departure <= parsed
        ):
            self._close_busy_interval()
        self._watermark = parsed
        return tuple(self._issues[issue_start:])

    def finish(self) -> MetadataSTDModelResult:
        """Drain every scheduled event and return the immutable final result."""

        self._require_open()
        if self._timeline:
            self._process_timeline(max(item[0] for item in self._timeline))
        if self._busy_start is not None and self._last_departure is not None:
            self._close_busy_interval()
        input_rate = self._descriptor.input_bits_per_second
        if input_rate == 0 and self._transport_count and not self._zero_rate_reported:
            self._zero_rate_reported = True
            self._issues.append(
                MetadataSTDModelIssue(
                    "zero_input_leak_rate",
                    "ITU-T H.222.0 §§2.6.63, 2.12.10",
                    self._previous_arrival,
                    self._transport_count,
                    self._transport_buffer_size,
                    None,
                    None,
                    None,
                    "metadata_input_leak_rate is zero, so no transport byte can enter Bn",
                )
            )
        result = MetadataSTDModelResult(
            tuple(self._issues),
            self._transport_count,
            self._main_count,
            self._access_count,
            len(self._access_removal_times),
            self._maximum_transport,
            self._maximum_main,
            self._transport_count if input_rate == 0 else 0,
            self._main_fullness,
            self._maximum_busy,
            self._maximum_decoder_delay,
            self._minimum_delay,
        )
        self._finished = True
        self._final_result = result
        return result

    def _feed_transport_byte(
        self,
        value: MetadataSTDByte,
        arrival: Fraction,
        group_ids: dict[Fraction, int],
    ) -> None:
        while self._departures and self._departures[0] <= arrival:
            self._departures.popleft()
        if self._last_departure is None or arrival >= self._last_departure:
            if self._busy_start is not None and self._last_departure is not None:
                self._close_busy_interval()
            self._busy_start = arrival
            self._busy_issue_reported = False
        input_rate = self._descriptor.input_bits_per_second
        departure: Fraction | None
        if input_rate:
            start = arrival if self._last_departure is None else max(
                arrival, self._last_departure
            )
            departure = start + Fraction(8, input_rate)
            self._last_departure = departure
            self._departures.append(departure)
        else:
            departure = None
        self._transport_count += 1
        fullness = len(self._departures) if departure is not None else self._transport_count
        self._maximum_transport = max(self._maximum_transport, fullness)
        if fullness > self._transport_buffer_size and not self._transport_overflow_reported:
            self._transport_overflow_reported = True
            self._issues.append(
                MetadataSTDModelIssue(
                    "transport_buffer_overflow",
                    "ITU-T H.222.0 §2.4.2.6",
                    arrival,
                    fullness,
                    self._transport_buffer_size,
                    value.source_offset,
                    None,
                    None,
                    f"metadata transport buffer reached {fullness} bytes; capacity is "
                    f"{self._transport_buffer_size} bytes",
                )
            )
        if not value.enters_main_buffer:
            return
        self._main_count += 1
        if value.access_unit_byte:
            self._access_count += 1
        if departure is None:
            assert value.removal_time is not None
            removal = cast(Fraction, value.removal_time)
            self._groups[group_ids[removal]].remaining_entries -= 1
            return
        assert value.removal_time is not None
        removal = cast(Fraction, value.removal_time)
        self._push_timeline(departure, 0, group_ids[removal])

    def _push_timeline(self, time: Fraction, priority: int, group_id: int) -> None:
        heappush(self._timeline, (time, priority, self._next_serial, group_id))
        self._next_serial += 1

    def _process_timeline(self, watermark: Fraction) -> None:
        while self._timeline and self._timeline[0][0] <= watermark:
            time, priority, _serial, group_id = heappop(self._timeline)
            group = self._groups[group_id]
            if priority == 0:
                self._main_fullness += 1
                group.arrived += 1
                group.remaining_entries -= 1
                self._maximum_main = max(self._maximum_main, self._main_fullness)
                if (
                    self._main_fullness > self._descriptor.buffer_bytes
                    and not self._overflow_reported
                ):
                    self._overflow_reported = True
                    self._issues.append(
                        MetadataSTDModelIssue(
                            "main_buffer_overflow",
                            "ITU-T H.222.0 §2.12.10",
                            time,
                            self._main_fullness,
                            self._descriptor.buffer_bytes,
                            group.source_offset,
                            None,
                            None,
                            f"metadata main buffer reached {self._main_fullness} bytes; "
                            f"capacity is {self._descriptor.buffer_bytes} bytes",
                        )
                    )
            else:
                self._main_fullness -= group.expected
                group.removed = True
                if group.arrived < group.expected:
                    deficit = group.arrived - group.expected
                    self._issues.append(
                        MetadataSTDModelIssue(
                            "main_buffer_underflow",
                            "ITU-T H.222.0 §§2.4.2.6, 2.12.10",
                            time,
                            deficit,
                            self._descriptor.buffer_bytes,
                            group.source_offset,
                            None,
                            None,
                            f"PTS removal requires {group.expected} bytes but only "
                            f"{group.arrived} had entered the metadata main buffer",
                        )
                    )
                self._record_group_delay(time, group)
            if group.removed and group.remaining_entries == 0:
                del self._groups[group_id]

    def _record_group_delay(
        self, pts: Fraction, group: _IncrementalRemovalGroup
    ) -> None:
        if group.earliest_access_arrival is None:
            return
        assert group.latest_access_arrival is not None
        self._access_removal_times.add(pts)
        largest = pts - group.earliest_access_arrival
        smallest = pts - group.latest_access_arrival
        self._maximum_decoder_delay = (
            largest
            if self._maximum_decoder_delay is None
            else max(self._maximum_decoder_delay, largest)
        )
        self._minimum_delay = (
            smallest if self._minimum_delay is None else min(self._minimum_delay, smallest)
        )
        if largest > self._permitted_delay:
            self._issues.append(
                MetadataSTDModelIssue(
                    "excessive_delay",
                    "ST 1402.2 ST 1402-12",
                    pts,
                    None,
                    None,
                    group.source_offset,
                    largest,
                    self._permitted_delay,
                    f"metadata access-unit decoder delay is {float(largest):.6f} seconds; "
                    f"permitted delay is {float(self._permitted_delay):.6f}",
                )
            )
        if smallest < 0:
            self._issues.append(
                MetadataSTDModelIssue(
                    "late_access_unit",
                    "ITU-T H.222.0 §§2.4.2.6, 2.12.10",
                    pts,
                    None,
                    None,
                    group.source_offset,
                    smallest,
                    Fraction(0),
                    "one or more metadata access-unit bytes arrive after their PTS",
                )
            )

    def _report_transport_interval_if_proven(self) -> None:
        if self._busy_start is None or self._last_departure is None:
            return
        duration = self._last_departure - self._busy_start
        self._maximum_busy = max(self._maximum_busy, duration)
        if duration > ST1402_MAX_METADATA_DELAY and not self._busy_issue_reported:
            self._busy_issue_reported = True
            self._issues.append(
                MetadataSTDModelIssue(
                    "transport_buffer_not_emptied",
                    "ITU-T H.222.0 §2.4.2.6",
                    None,
                    None,
                    self._transport_buffer_size,
                    None,
                    duration,
                    ST1402_MAX_METADATA_DELAY,
                    f"metadata transport buffer remained continuously non-empty for "
                    f"{float(duration):.6f} seconds",
                )
            )

    def _close_busy_interval(self) -> None:
        assert self._busy_start is not None
        assert self._last_departure is not None
        self._maximum_busy = max(
            self._maximum_busy, self._last_departure - self._busy_start
        )
        self._report_transport_interval_if_proven()
        self._busy_start = None

    def _require_open(self) -> None:
        if self._finished:
            raise RuntimeError("metadata STD model is already finished")


class IncrementalAsynchronousMetadataSTDModel:
    """Bounded exact T-STD state for a continuous asynchronous metadata stream.

    Transport bytes are registered in arrival order. ``advance`` processes all
    transport- and main-buffer leak events through an exact clock watermark,
    retaining only future departures. Impossible zero-rate configurations and
    buffer/delay failures are returned as soon as the available timeline proves
    them, so a live receiver need not wait for end-of-stream finalization.
    """

    __slots__ = (
        "_busy_issue_reported",
        "_busy_start",
        "_delay_reported",
        "_descriptor",
        "_finished",
        "_issues",
        "_last_output_departure",
        "_last_transport_departure",
        "_main_count",
        "_main_overflow_reported",
        "_max_pending_events",
        "_maximum_busy",
        "_maximum_decoder_delay",
        "_maximum_main",
        "_maximum_transport",
        "_minimum_decoder_delay",
        "_output_departures",
        "_output_zero_reported",
        "_permitted_delay",
        "_previous_arrival",
        "_retained_main",
        "_transport_buffer_size",
        "_transport_count",
        "_transport_departures",
        "_transport_overflow_reported",
        "_transport_zero_reported",
        "_watermark",
    )

    def __init__(
        self,
        descriptor: MetadataSTDDescriptor,
        *,
        transport_buffer_size: int = H222_TRANSPORT_BUFFER_SIZE,
        maximum_delay: Fraction | int | float = ST1402_MAX_METADATA_DELAY,
        max_pending_events: int = 1_000_000,
    ) -> None:
        if not isinstance(descriptor, MetadataSTDDescriptor):
            raise TypeError("descriptor must be a MetadataSTDDescriptor")
        self._descriptor = descriptor
        self._transport_buffer_size = _positive_integer(
            transport_buffer_size, name="transport_buffer_size"
        )
        self._permitted_delay = _delay(maximum_delay)
        if self._permitted_delay <= 0:
            raise ValueError("maximum_delay must be positive")
        self._max_pending_events = _positive_integer(
            max_pending_events, name="max_pending_events"
        )
        self._transport_departures: deque[
            tuple[Fraction, bool, Fraction, int | None]
        ] = deque()
        self._output_departures: deque[Fraction] = deque()
        self._issues: list[MetadataSTDModelIssue] = []
        self._finished = False
        self._previous_arrival: Fraction | None = None
        self._watermark: Fraction | None = None
        self._last_transport_departure: Fraction | None = None
        self._last_output_departure: Fraction | None = None
        self._busy_start: Fraction | None = None
        self._maximum_busy = Fraction(0)
        self._maximum_transport = 0
        self._maximum_main = 0
        self._maximum_decoder_delay: Fraction | None = None
        self._minimum_decoder_delay: Fraction | None = None
        self._transport_count = 0
        self._main_count = 0
        self._retained_main = 0
        self._transport_overflow_reported = False
        self._main_overflow_reported = False
        self._delay_reported = False
        self._busy_issue_reported = False
        self._transport_zero_reported = False
        self._output_zero_reported = False

    @property
    def descriptor(self) -> MetadataSTDDescriptor:
        return self._descriptor

    @property
    def transport_buffer_size(self) -> int:
        return self._transport_buffer_size

    @property
    def maximum_delay(self) -> Fraction:
        return self._permitted_delay

    @property
    def pending_events(self) -> int:
        """Return retained future transport and output departures."""

        return len(self._transport_departures) + len(self._output_departures)

    def feed(
        self, values: Iterable[AsynchronousMetadataSTDByte]
    ) -> tuple[MetadataSTDModelIssue, ...]:
        """Register one bounded batch of arrival-ordered transport bytes."""

        self._require_open()
        try:
            batch = tuple(values)
        except TypeError as error:
            raise TypeError(
                "values must be an iterable of AsynchronousMetadataSTDByte values"
            ) from error
        for value in batch:
            if not isinstance(value, AsynchronousMetadataSTDByte):
                raise TypeError("every value must be an AsynchronousMetadataSTDByte")
        arrivals = [cast(Fraction, value.arrival_time) for value in batch]
        if any(current < previous for previous, current in pairwise(arrivals)):
            raise ValueError("byte arrival times must be nondecreasing")
        if (
            arrivals
            and self._previous_arrival is not None
            and arrivals[0] < self._previous_arrival
        ):
            raise ValueError("byte arrival times must be nondecreasing across feed calls")
        if arrivals and self._watermark is not None and arrivals[0] < self._watermark:
            raise ValueError("byte arrival precedes the processed watermark")
        if self.pending_events + len(batch) > self._max_pending_events:
            raise LimitExceeded(
                "asynchronous metadata STD state would exceed "
                f"{self._max_pending_events} pending events"
            )

        issue_start = len(self._issues)
        input_rate = self._descriptor.input_bits_per_second
        input_byte_duration = None if input_rate == 0 else Fraction(8, input_rate)
        for value, arrival in zip(batch, arrivals, strict=True):
            self._process_until(arrival)
            self._transport_count += 1
            if input_byte_duration is None:
                fullness = self._transport_count
                if not self._transport_zero_reported:
                    self._transport_zero_reported = True
                    self._issues.append(
                        MetadataSTDModelIssue(
                            "zero_input_leak_rate",
                            "ITU-T H.222.0 §§2.6.63, 2.12.10",
                            arrival,
                            fullness,
                            self._transport_buffer_size,
                            value.source_offset,
                            None,
                            None,
                            "metadata_input_leak_rate is zero, so no transport "
                            "byte can enter Bn",
                        )
                    )
            else:
                if not self._transport_departures:
                    self._busy_start = arrival
                    self._busy_issue_reported = False
                start = (
                    arrival
                    if self._last_transport_departure is None
                    else max(arrival, self._last_transport_departure)
                )
                departure = start + input_byte_duration
                self._last_transport_departure = departure
                self._transport_departures.append(
                    (departure, value.enters_main_buffer, arrival, value.source_offset)
                )
                fullness = len(self._transport_departures)
                self._report_transport_interval_if_proven()
            self._maximum_transport = max(self._maximum_transport, fullness)
            if (
                fullness > self._transport_buffer_size
                and not self._transport_overflow_reported
            ):
                self._transport_overflow_reported = True
                self._issues.append(
                    MetadataSTDModelIssue(
                        "transport_buffer_overflow",
                        "ITU-T H.222.0 §2.4.2.6",
                        arrival,
                        fullness,
                        self._transport_buffer_size,
                        value.source_offset,
                        None,
                        None,
                        f"metadata transport buffer reached {fullness} bytes; capacity "
                        f"is {self._transport_buffer_size} bytes",
                    )
                )
        if arrivals:
            self._previous_arrival = arrivals[-1]
        return tuple(self._issues[issue_start:])

    def advance(
        self, watermark: Fraction | int | float
    ) -> tuple[MetadataSTDModelIssue, ...]:
        """Process all scheduled leaks at or before ``watermark``."""

        self._require_open()
        parsed = _time(watermark, name="watermark")
        if self._watermark is not None and parsed < self._watermark:
            raise ValueError("watermarks must be nondecreasing")
        if self._previous_arrival is not None and parsed < self._previous_arrival:
            raise ValueError("watermark cannot precede the latest byte arrival")
        issue_start = len(self._issues)
        self._process_until(parsed)
        self._watermark = parsed
        return tuple(self._issues[issue_start:])

    def finish(self) -> AsynchronousMetadataSTDModelResult:
        """Drain all scheduled leaks and return immutable finite-stream state."""

        self._require_open()
        if self._transport_departures:
            self._process_until(self._transport_departures[-1][0])
        if self._output_departures:
            self._process_until(self._output_departures[-1])
        result = AsynchronousMetadataSTDModelResult(
            tuple(self._issues),
            self._transport_count,
            self._main_count,
            self._maximum_transport,
            self._maximum_main,
            self._transport_count
            if self._descriptor.input_bits_per_second == 0
            else len(self._transport_departures),
            self._retained_main
            if self._descriptor.output_bits_per_second == 0
            else len(self._output_departures),
            self._maximum_busy,
            self._maximum_decoder_delay,
            self._minimum_decoder_delay,
        )
        self._finished = True
        return result

    def _process_until(self, watermark: Fraction) -> None:
        while self._transport_departures and self._transport_departures[0][0] <= watermark:
            departure, enters_main, arrival, source_offset = (
                self._transport_departures.popleft()
            )
            self._drain_output(departure)
            if enters_main:
                self._enter_main_buffer(departure, arrival, source_offset)
        self._drain_output(watermark)
        if (
            not self._transport_departures
            and self._busy_start is not None
            and self._last_transport_departure is not None
            and self._last_transport_departure <= watermark
        ):
            self._close_busy_interval()

    def _drain_output(self, watermark: Fraction) -> None:
        while self._output_departures and self._output_departures[0] <= watermark:
            self._output_departures.popleft()

    def _enter_main_buffer(
        self, entry: Fraction, arrival: Fraction, source_offset: int | None
    ) -> None:
        self._main_count += 1
        output_rate = self._descriptor.output_bits_per_second
        if output_rate == 0:
            self._retained_main += 1
            fullness = self._retained_main
            if not self._output_zero_reported:
                self._output_zero_reported = True
                self._issues.append(
                    MetadataSTDModelIssue(
                        "zero_output_leak_rate",
                        "ITU-T H.222.0 §§2.6.63, 2.12.10",
                        entry,
                        fullness,
                        self._descriptor.buffer_bytes,
                        source_offset,
                        None,
                        None,
                        "metadata_output_leak_rate is zero, so asynchronous metadata "
                        "cannot leave Bn",
                    )
                )
        else:
            output_start = (
                entry
                if self._last_output_departure is None
                else max(entry, self._last_output_departure)
            )
            output_departure = output_start + Fraction(8, output_rate)
            self._last_output_departure = output_departure
            self._output_departures.append(output_departure)
            fullness = len(self._output_departures)
            delay = output_departure - arrival
            self._maximum_decoder_delay = (
                delay
                if self._maximum_decoder_delay is None
                else max(self._maximum_decoder_delay, delay)
            )
            self._minimum_decoder_delay = (
                delay
                if self._minimum_decoder_delay is None
                else min(self._minimum_decoder_delay, delay)
            )
            if delay > self._permitted_delay and not self._delay_reported:
                self._delay_reported = True
                self._issues.append(
                    MetadataSTDModelIssue(
                        "excessive_delay",
                        "ITU-T H.222.0 §2.4.2.6",
                        output_departure,
                        None,
                        None,
                        source_offset,
                        delay,
                        self._permitted_delay,
                        f"asynchronous metadata decoder delay is {float(delay):.6f} "
                        f"seconds; permitted delay is {float(self._permitted_delay):.6f}",
                    )
                )
        self._maximum_main = max(self._maximum_main, fullness)
        if fullness > self._descriptor.buffer_bytes and not self._main_overflow_reported:
            self._main_overflow_reported = True
            self._issues.append(
                MetadataSTDModelIssue(
                    "main_buffer_overflow",
                    "ITU-T H.222.0 §2.12.10",
                    entry,
                    fullness,
                    self._descriptor.buffer_bytes,
                    source_offset,
                    None,
                    None,
                    f"metadata main buffer reached {fullness} bytes; capacity is "
                    f"{self._descriptor.buffer_bytes} bytes",
                )
            )

    def _report_transport_interval_if_proven(self) -> None:
        if self._busy_start is None or self._last_transport_departure is None:
            return
        duration = self._last_transport_departure - self._busy_start
        self._maximum_busy = max(self._maximum_busy, duration)
        if duration > ST1402_MAX_METADATA_DELAY and not self._busy_issue_reported:
            self._busy_issue_reported = True
            self._issues.append(
                MetadataSTDModelIssue(
                    "transport_buffer_not_emptied",
                    "ITU-T H.222.0 §2.4.2.6",
                    None,
                    None,
                    self._transport_buffer_size,
                    None,
                    duration,
                    ST1402_MAX_METADATA_DELAY,
                    "metadata transport buffer remained continuously non-empty for "
                    f"{float(duration):.6f} seconds",
                )
            )

    def _close_busy_interval(self) -> None:
        assert self._busy_start is not None
        assert self._last_transport_departure is not None
        self._maximum_busy = max(
            self._maximum_busy, self._last_transport_departure - self._busy_start
        )
        self._report_transport_interval_if_proven()
        self._busy_start = None

    def _require_open(self) -> None:
        if self._finished:
            raise RuntimeError("asynchronous metadata STD model is already finished")


class AsynchronousMetadataSTDModel:
    """Simulate the exact H.222.0 STD for asynchronous metadata bytes.

    ``TBn`` leaks at ``metadata_input_leak_rate``. PES bytes then enter ``Bn``
    and leak continuously to the metadata decoder at
    ``metadata_output_leak_rate``; unlike synchronous metadata, no PTS removes
    an access unit instantaneously.
    """

    __slots__ = ("_descriptor", "_maximum_delay", "_transport_buffer_size")

    def __init__(
        self,
        descriptor: MetadataSTDDescriptor,
        *,
        transport_buffer_size: int = H222_TRANSPORT_BUFFER_SIZE,
        maximum_delay: Fraction | int | float = ST1402_MAX_METADATA_DELAY,
    ) -> None:
        if not isinstance(descriptor, MetadataSTDDescriptor):
            raise TypeError("descriptor must be a MetadataSTDDescriptor")
        self._descriptor = descriptor
        self._transport_buffer_size = _positive_integer(
            transport_buffer_size, name="transport_buffer_size"
        )
        self._maximum_delay = _delay(maximum_delay)
        if self._maximum_delay <= 0:
            raise ValueError("maximum_delay must be positive")

    @property
    def descriptor(self) -> MetadataSTDDescriptor:
        return self._descriptor

    @property
    def transport_buffer_size(self) -> int:
        return self._transport_buffer_size

    @property
    def maximum_delay(self) -> Fraction:
        return self._maximum_delay

    def simulate(
        self, values: Iterable[AsynchronousMetadataSTDByte]
    ) -> AsynchronousMetadataSTDModelResult:
        """Consume arrival-ordered bytes and return exact finite-stream state."""

        try:
            iterator = iter(values)
        except TypeError as error:
            raise TypeError(
                "values must be an iterable of AsynchronousMetadataSTDByte values"
            ) from error

        issues: list[MetadataSTDModelIssue] = []
        transport_departures: deque[Fraction] = deque()
        output_departures: deque[Fraction] = deque()
        previous_arrival: Fraction | None = None
        last_transport_departure: Fraction | None = None
        last_output_departure: Fraction | None = None
        busy_start: Fraction | None = None
        maximum_busy = Fraction(0)
        maximum_transport = 0
        maximum_main = 0
        transport_count = 0
        main_count = 0
        retained_main = 0
        maximum_decoder_delay: Fraction | None = None
        minimum_decoder_delay: Fraction | None = None
        transport_overflow_reported = False
        main_overflow_reported = False
        delay_reported = False
        input_rate = self._descriptor.input_bits_per_second
        output_rate = self._descriptor.output_bits_per_second
        input_byte_duration = None if input_rate == 0 else Fraction(8, input_rate)
        output_byte_duration = None if output_rate == 0 else Fraction(8, output_rate)

        for value in iterator:
            if not isinstance(value, AsynchronousMetadataSTDByte):
                raise TypeError("every value must be an AsynchronousMetadataSTDByte")
            arrival = cast(Fraction, value.arrival_time)
            if previous_arrival is not None and arrival < previous_arrival:
                raise ValueError("byte arrival times must be nondecreasing")
            previous_arrival = arrival
            transport_count += 1

            while transport_departures and transport_departures[0] <= arrival:
                transport_departures.popleft()
            if last_transport_departure is None or arrival >= last_transport_departure:
                if busy_start is not None and last_transport_departure is not None:
                    maximum_busy = max(
                        maximum_busy, last_transport_departure - busy_start
                    )
                busy_start = arrival

            if input_byte_duration is None:
                transport_departure = None
            else:
                transport_start = (
                    arrival
                    if last_transport_departure is None
                    else max(arrival, last_transport_departure)
                )
                transport_departure = transport_start + input_byte_duration
                last_transport_departure = transport_departure
                transport_departures.append(transport_departure)

            transport_fullness = (
                len(transport_departures)
                if transport_departure is not None
                else transport_count
            )
            maximum_transport = max(maximum_transport, transport_fullness)
            if (
                transport_fullness > self._transport_buffer_size
                and not transport_overflow_reported
            ):
                transport_overflow_reported = True
                issues.append(
                    MetadataSTDModelIssue(
                        "transport_buffer_overflow",
                        "ITU-T H.222.0 §2.4.2.6",
                        arrival,
                        transport_fullness,
                        self._transport_buffer_size,
                        value.source_offset,
                        None,
                        None,
                        f"metadata transport buffer reached {transport_fullness} bytes; "
                        f"capacity is {self._transport_buffer_size} bytes",
                    )
                )

            if not value.enters_main_buffer or transport_departure is None:
                continue
            main_count += 1
            while output_departures and output_departures[0] <= transport_departure:
                output_departures.popleft()
            if output_byte_duration is None:
                retained_main += 1
                main_fullness = retained_main
            else:
                output_start = (
                    transport_departure
                    if last_output_departure is None
                    else max(transport_departure, last_output_departure)
                )
                output_departure = output_start + output_byte_duration
                last_output_departure = output_departure
                output_departures.append(output_departure)
                main_fullness = len(output_departures)
                delay = output_departure - arrival
                maximum_decoder_delay = (
                    delay
                    if maximum_decoder_delay is None
                    else max(maximum_decoder_delay, delay)
                )
                minimum_decoder_delay = (
                    delay
                    if minimum_decoder_delay is None
                    else min(minimum_decoder_delay, delay)
                )
                if delay > self._maximum_delay and not delay_reported:
                    delay_reported = True
                    issues.append(
                        MetadataSTDModelIssue(
                            "excessive_delay",
                            "ITU-T H.222.0 §2.4.2.6",
                            output_departure,
                            None,
                            None,
                            value.source_offset,
                            delay,
                            self._maximum_delay,
                            f"asynchronous metadata decoder delay is "
                            f"{float(delay):.6f} seconds; permitted delay is "
                            f"{float(self._maximum_delay):.6f}",
                        )
                    )
            maximum_main = max(maximum_main, main_fullness)
            if (
                main_fullness > self._descriptor.buffer_bytes
                and not main_overflow_reported
            ):
                main_overflow_reported = True
                issues.append(
                    MetadataSTDModelIssue(
                        "main_buffer_overflow",
                        "ITU-T H.222.0 §2.12.10",
                        transport_departure,
                        main_fullness,
                        self._descriptor.buffer_bytes,
                        value.source_offset,
                        None,
                        None,
                        f"metadata main buffer reached {main_fullness} bytes; capacity is "
                        f"{self._descriptor.buffer_bytes} bytes",
                    )
                )

        if busy_start is not None and last_transport_departure is not None:
            maximum_busy = max(
                maximum_busy, last_transport_departure - busy_start
            )
        if input_rate == 0 and transport_count:
            issues.append(
                MetadataSTDModelIssue(
                    "zero_input_leak_rate",
                    "ITU-T H.222.0 §§2.6.63, 2.12.10",
                    previous_arrival,
                    transport_count,
                    self._transport_buffer_size,
                    None,
                    None,
                    None,
                    "metadata_input_leak_rate is zero, so no transport byte can enter Bn",
                )
            )
        elif maximum_busy > ST1402_MAX_METADATA_DELAY:
            issues.append(
                MetadataSTDModelIssue(
                    "transport_buffer_not_emptied",
                    "ITU-T H.222.0 §2.4.2.6",
                    None,
                    None,
                    self._transport_buffer_size,
                    None,
                    maximum_busy,
                    ST1402_MAX_METADATA_DELAY,
                    f"metadata transport buffer remained continuously non-empty for "
                    f"{float(maximum_busy):.6f} seconds",
                )
            )
        if output_rate == 0 and main_count:
            issues.append(
                MetadataSTDModelIssue(
                    "zero_output_leak_rate",
                    "ITU-T H.222.0 §§2.6.63, 2.12.10",
                    previous_arrival,
                    retained_main,
                    self._descriptor.buffer_bytes,
                    None,
                    None,
                    None,
                    "metadata_output_leak_rate is zero, so asynchronous metadata "
                    "cannot leave Bn",
                )
            )

        return AsynchronousMetadataSTDModelResult(
            tuple(issues),
            transport_count,
            main_count,
            maximum_transport,
            maximum_main,
            transport_count if input_rate == 0 else 0,
            retained_main if output_rate == 0 else 0,
            maximum_busy,
            maximum_decoder_delay,
            minimum_decoder_delay,
        )


class SynchronousMetadataSTDModel:
    """Simulate the exact H.222.0 T-STD for synchronous metadata bytes.

    The caller supplies the exact ``t(i)`` arrival time for every transport
    byte. This deliberately keeps PCR interpolation outside the mathematical
    model: stored-stream adapters can derive exact values from two PCR samples,
    while live receivers can retain honest timing bounds until the following
    sample exists.
    """

    __slots__ = ("_descriptor", "_maximum_delay", "_transport_buffer_size")

    def __init__(
        self,
        descriptor: MetadataSTDDescriptor,
        *,
        transport_buffer_size: int = H222_TRANSPORT_BUFFER_SIZE,
        maximum_delay: Fraction | int | float = ST1402_MAX_METADATA_DELAY,
    ) -> None:
        if not isinstance(descriptor, MetadataSTDDescriptor):
            raise TypeError("descriptor must be a MetadataSTDDescriptor")
        self._descriptor = descriptor
        self._transport_buffer_size = _positive_integer(
            transport_buffer_size, name="transport_buffer_size"
        )
        self._maximum_delay = _delay(maximum_delay)
        if self._maximum_delay <= 0:
            raise ValueError("maximum_delay must be positive")

    @property
    def descriptor(self) -> MetadataSTDDescriptor:
        return self._descriptor

    @property
    def transport_buffer_size(self) -> int:
        return self._transport_buffer_size

    @property
    def maximum_delay(self) -> Fraction:
        return self._maximum_delay

    def simulate(self, values: Iterable[MetadataSTDByte]) -> MetadataSTDModelResult:
        """Consume arrival-ordered bytes and return exact finite-stream results."""

        try:
            iterator = iter(values)
        except TypeError as error:
            raise TypeError("values must be an iterable of MetadataSTDByte values") from error

        issues: list[MetadataSTDModelIssue] = []
        departures: deque[Fraction] = deque()
        main_entries: list[tuple[Fraction, Fraction]] = []
        removals: dict[Fraction, _RemovalGroup] = defaultdict(_RemovalGroup)
        previous_arrival: Fraction | None = None
        last_departure: Fraction | None = None
        busy_start: Fraction | None = None
        maximum_busy = Fraction(0)
        maximum_transport = 0
        transport_count = 0
        main_count = 0
        access_count = 0
        overflow_reported = False
        input_rate = self._descriptor.input_bits_per_second
        byte_duration = None if input_rate == 0 else Fraction(8, input_rate)

        for value in iterator:
            if not isinstance(value, MetadataSTDByte):
                raise TypeError("every value must be a MetadataSTDByte")
            arrival = cast(Fraction, value.arrival_time)
            if previous_arrival is not None and arrival < previous_arrival:
                raise ValueError("byte arrival times must be nondecreasing")
            previous_arrival = arrival
            transport_count += 1

            while departures and departures[0] <= arrival:
                departures.popleft()
            if last_departure is None or arrival >= last_departure:
                if busy_start is not None and last_departure is not None:
                    maximum_busy = max(maximum_busy, last_departure - busy_start)
                busy_start = arrival

            if byte_duration is not None:
                service_start = arrival if last_departure is None else max(
                    arrival, last_departure
                )
                departure = service_start + byte_duration
                last_departure = departure
                departures.append(departure)
            else:
                departure = None

            fullness = len(departures) if departure is not None else transport_count
            maximum_transport = max(maximum_transport, fullness)
            if fullness > self._transport_buffer_size and not overflow_reported:
                overflow_reported = True
                issues.append(
                    MetadataSTDModelIssue(
                        "transport_buffer_overflow",
                        "ITU-T H.222.0 §2.4.2.6",
                        arrival,
                        fullness,
                        self._transport_buffer_size,
                        value.source_offset,
                        None,
                        None,
                        f"metadata transport buffer reached {fullness} bytes; "
                        f"capacity is {self._transport_buffer_size} bytes",
                    )
                )

            if not value.enters_main_buffer:
                continue
            assert value.removal_time is not None
            removal_time = cast(Fraction, value.removal_time)
            group = removals[removal_time]
            group.expected += 1
            if group.source_offset is None:
                group.source_offset = value.source_offset
            main_count += 1
            if departure is not None:
                main_entries.append((departure, removal_time))
            if value.access_unit_byte:
                access_count += 1
                group.access_unit_bytes += 1
                group.earliest_access_arrival = (
                    arrival
                    if group.earliest_access_arrival is None
                    else min(group.earliest_access_arrival, arrival)
                )
                group.latest_access_arrival = (
                    arrival
                    if group.latest_access_arrival is None
                    else max(group.latest_access_arrival, arrival)
                )

        if busy_start is not None and last_departure is not None:
            maximum_busy = max(maximum_busy, last_departure - busy_start)
        if input_rate == 0 and transport_count:
            issues.append(
                MetadataSTDModelIssue(
                    "zero_input_leak_rate",
                    "ITU-T H.222.0 §§2.6.63, 2.12.10",
                    previous_arrival,
                    transport_count,
                    self._transport_buffer_size,
                    None,
                    None,
                    None,
                    "metadata_input_leak_rate is zero, so no transport byte can enter Bn",
                )
            )
        elif maximum_busy > ST1402_MAX_METADATA_DELAY:
            issues.append(
                MetadataSTDModelIssue(
                    "transport_buffer_not_emptied",
                    "ITU-T H.222.0 §2.4.2.6",
                    None,
                    None,
                    self._transport_buffer_size,
                    None,
                    maximum_busy,
                    ST1402_MAX_METADATA_DELAY,
                    f"metadata transport buffer remained continuously non-empty for "
                    f"{float(maximum_busy):.6f} seconds",
                )
            )

        maximum_main, final_main = self._simulate_main_buffer(
            main_entries, removals, issues
        )
        maximum_delay, minimum_delay = self._check_delays(removals, issues)
        final_transport = transport_count if input_rate == 0 else 0
        return MetadataSTDModelResult(
            tuple(issues),
            transport_count,
            main_count,
            access_count,
            sum(group.access_unit_bytes > 0 for group in removals.values()),
            maximum_transport,
            maximum_main,
            final_transport,
            final_main,
            maximum_busy,
            maximum_delay,
            minimum_delay,
        )

    def _simulate_main_buffer(
        self,
        entries: list[tuple[Fraction, Fraction]],
        removals: dict[Fraction, _RemovalGroup],
        issues: list[MetadataSTDModelIssue],
    ) -> tuple[int, int]:
        entries_by_time: dict[Fraction, list[Fraction]] = defaultdict(list)
        for time, removal in entries:
            entries_by_time[time].append(removal)
        fullness = 0
        maximum = 0
        arrived: dict[Fraction, int] = defaultdict(int)
        overflow_reported = False
        for time in sorted(set(entries_by_time) | set(removals)):
            for removal in entries_by_time.get(time, ()):
                fullness += 1
                arrived[removal] += 1
            maximum = max(maximum, fullness)
            if fullness > self._descriptor.buffer_bytes and not overflow_reported:
                overflow_reported = True
                issues.append(
                    MetadataSTDModelIssue(
                        "main_buffer_overflow",
                        "ITU-T H.222.0 §2.12.10",
                        time,
                        fullness,
                        self._descriptor.buffer_bytes,
                        None,
                        None,
                        None,
                        f"metadata main buffer reached {fullness} bytes; capacity is "
                        f"{self._descriptor.buffer_bytes} bytes",
                    )
                )
            group = removals.get(time)
            if group is not None:
                available = arrived[time]
                fullness -= group.expected
                if available < group.expected:
                    deficit = available - group.expected
                    issues.append(
                        MetadataSTDModelIssue(
                            "main_buffer_underflow",
                            "ITU-T H.222.0 §§2.4.2.6, 2.12.10",
                            time,
                            deficit,
                            self._descriptor.buffer_bytes,
                            group.source_offset,
                            None,
                            None,
                            f"PTS removal requires {group.expected} bytes but only "
                            f"{available} had entered the metadata main buffer",
                        )
                    )
        return maximum, fullness

    def _check_delays(
        self,
        removals: dict[Fraction, _RemovalGroup],
        issues: list[MetadataSTDModelIssue],
    ) -> tuple[Fraction | None, Fraction | None]:
        maximum: Fraction | None = None
        minimum: Fraction | None = None
        for pts, group in removals.items():
            if group.earliest_access_arrival is None:
                continue
            assert group.latest_access_arrival is not None
            largest = pts - group.earliest_access_arrival
            smallest = pts - group.latest_access_arrival
            maximum = largest if maximum is None else max(maximum, largest)
            minimum = smallest if minimum is None else min(minimum, smallest)
            if largest > self._maximum_delay:
                issues.append(
                    MetadataSTDModelIssue(
                        "excessive_delay",
                        "ST 1402.2 ST 1402-12",
                        pts,
                        None,
                        None,
                        group.source_offset,
                        largest,
                        self._maximum_delay,
                        f"metadata access-unit decoder delay is {float(largest):.6f} "
                        f"seconds; permitted delay is {float(self._maximum_delay):.6f}",
                    )
                )
            if smallest < 0:
                issues.append(
                    MetadataSTDModelIssue(
                        "late_access_unit",
                        "ITU-T H.222.0 §§2.4.2.6, 2.12.10",
                        pts,
                        None,
                        None,
                        group.source_offset,
                        smallest,
                        Fraction(0),
                        "one or more metadata access-unit bytes arrive after their PTS",
                    )
                )
        return maximum, minimum


@dataclass(frozen=True, slots=True)
class _TimedTransportByte:
    arrival_time: Fraction
    enters_main_buffer: bool
    source_offset: int


def _timed_metadata_transport_bytes(
    event: PESStreamEvent,
    clock_events: Sequence[ProgramClockEvent],
) -> tuple[_TimedTransportByte, ...]:
    """Apply H.222.0 equations 2-4/2-5 to one metadata PES packet set."""

    if not event.pes.transport_packets:
        raise ValueError("PES must retain its source transport packets")
    if len(clock_events) < 2:
        raise ValueError("at least two PCR events are required")

    points: list[tuple[int, int]] = []
    ticks_reference: int | None = None
    for clock in clock_events:
        if not isinstance(clock, ProgramClockEvent):
            raise TypeError("every clock event must be a ProgramClockEvent")
        if clock.program_number != event.program_number:
            raise ValueError("PCR and PES events must belong to the same program")
        if clock.discontinuity:
            raise ValueError("PCR interpolation cannot cross a timebase discontinuity")
        offset = clock.source_offset + PCR_BASE_LAST_BYTE_INDEX
        ticks = unwrap_pcr_ticks(clock.pcr.ticks, reference=ticks_reference)
        if points and offset <= points[-1][0]:
            raise ValueError("PCR source offsets must be strictly increasing")
        if points and ticks <= points[-1][1]:
            raise ValueError("PCR times must be strictly increasing")
        points.append((offset, ticks))
        ticks_reference = ticks

    point_offsets = [point[0] for point in points]
    first_source = event.pes.transport_packets[0].offset
    last_packet = event.pes.transport_packets[-1]
    last_source = last_packet.offset + len(last_packet.raw) - 1
    if first_source < point_offsets[0] or last_source > point_offsets[-1]:
        raise ValueError("PCR events must bracket every source transport byte")

    pes_cursor = 0
    result: list[_TimedTransportByte] = []
    for packet in event.pes.transport_packets:
        if len(packet.raw) != 188:
            raise ValueError("source transport packets must retain exactly 188 raw bytes")
        payload_offset = len(packet.raw) - len(packet.payload)
        for packet_index in range(len(packet.raw)):
            source_offset = packet.offset + packet_index
            right = bisect_right(point_offsets, source_offset)
            if right == 0 or right == len(points):
                # A byte at the final PCR sample has its exact encoded time.
                if source_offset == point_offsets[-1]:
                    arrival_ticks = Fraction(points[-1][1])
                else:
                    raise ValueError("PCR events do not bracket a source byte")
            else:
                left_offset, left_ticks = points[right - 1]
                right_offset, right_ticks = points[right]
                arrival_ticks = Fraction(left_ticks) + Fraction(
                    (source_offset - left_offset) * (right_ticks - left_ticks),
                    right_offset - left_offset,
                )
            in_payload = packet_index >= payload_offset
            if in_payload:
                if pes_cursor >= len(event.pes.raw):
                    raise ValueError("transport payload exceeds the reconstructed PES")
                pes_cursor += 1
            result.append(
                _TimedTransportByte(
                    arrival_ticks / PCR_CLOCK_RATE,
                    in_payload,
                    source_offset,
                )
            )
    if pes_cursor != len(event.pes.raw):
        raise ValueError("transport payload does not contain the complete reconstructed PES")
    return tuple(result)


def metadata_std_bytes_from_pes(
    event: PESStreamEvent,
    clock_events: Sequence[ProgramClockEvent],
) -> tuple[MetadataSTDByte, ...]:
    """Derive exact T-STD byte events for one PCR-bracketed metadata PES.

    Arrival times follow H.222.0 equations 2-4 and 2-5. At least two PCR
    observations from the same program must bracket every byte of every source
    transport packet. A discontinuity is rejected because those equations are
    expressly inapplicable across it.
    """

    if not isinstance(event, PESStreamEvent):
        raise TypeError("event must be a PESStreamEvent")
    if event.kind is not StreamKind.KLV:
        raise ValueError("event must describe a KLV stream")
    if event.klv_carriage is not KLVCarriage.SYNCHRONOUS:
        raise ValueError("event must use synchronous KLV carriage")
    if event.pes.pts is None:
        raise ValueError("synchronous metadata PES must carry a PTS")

    timed_bytes = _timed_metadata_transport_bytes(event, clock_events)
    reference_pts = timed_bytes[0].arrival_time * PTS_CLOCK_RATE
    removal_time = Fraction(
        unwrap_pts(event.pes.pts, reference=int(reference_pts)), PTS_CLOCK_RATE
    )
    payload_start = len(event.pes.raw) - len(event.pes.payload)
    access_mask = bytearray(len(event.pes.payload))
    # The five-byte cell wrapper is PES overhead for occupancy purposes; only
    # AU_cell_data_byte values belong to the access unit for decoder delay.
    from stanag4609.transport.metadata import parse_metadata_au_cells

    payload_cursor = 0
    for cell in parse_metadata_au_cells(event.pes.payload):
        data_start = payload_cursor + len(cell.raw) - len(cell.data)
        access_mask[data_start : payload_cursor + len(cell.raw)] = b"\x01" * len(
            cell.data
        )
        payload_cursor += len(cell.raw)

    pes_cursor = 0
    result: list[MetadataSTDByte] = []
    for value in timed_bytes:
        access = False
        if value.enters_main_buffer:
            access = (
                pes_cursor >= payload_start
                and bool(access_mask[pes_cursor - payload_start])
            )
            pes_cursor += 1
        result.append(
            MetadataSTDByte(
                value.arrival_time,
                enters_main_buffer=value.enters_main_buffer,
                removal_time=removal_time if value.enters_main_buffer else None,
                access_unit_byte=access,
                source_offset=value.source_offset,
            )
        )
    return tuple(result)


def asynchronous_metadata_std_bytes_from_pes(
    event: PESStreamEvent,
    clock_events: Sequence[ProgramClockEvent],
) -> tuple[AsynchronousMetadataSTDByte, ...]:
    """Derive exact T-STD bytes for one PCR-bracketed asynchronous KLV PES."""

    if not isinstance(event, PESStreamEvent):
        raise TypeError("event must be a PESStreamEvent")
    if event.kind is not StreamKind.KLV:
        raise ValueError("event must describe a KLV stream")
    if event.klv_carriage is not KLVCarriage.ASYNCHRONOUS:
        raise ValueError("event must use asynchronous KLV carriage")
    if event.pes.pts is not None or event.pes.dts is not None:
        raise ValueError("asynchronous metadata PES must not carry PTS or DTS")
    return tuple(
        AsynchronousMetadataSTDByte(
            value.arrival_time,
            enters_main_buffer=value.enters_main_buffer,
            source_offset=value.source_offset,
        )
        for value in _timed_metadata_transport_bytes(event, clock_events)
    )


def simulate_synchronous_metadata_pes(
    descriptor: MetadataSTDDescriptor,
    pes_events: Iterable[PESStreamEvent],
    clock_events: Sequence[ProgramClockEvent],
    *,
    transport_buffer_size: int = H222_TRANSPORT_BUFFER_SIZE,
    maximum_delay: Fraction | int | float = ST1402_MAX_METADATA_DELAY,
) -> MetadataSTDModelResult:
    """Run one exact, aggregate metadata T-STD audit over recorded PES events.

    Unlike evaluating each PES independently, this convenience function retains
    occupancy from earlier presentation times and can therefore prove aggregate
    buffer overflow. It is intended for a finite PCR-bracketed capture; live
    callers should use bounded receiver diagnostics until a complete bracket is
    available.
    """

    try:
        events = tuple(pes_events)
    except TypeError as error:
        raise TypeError("pes_events must be an iterable of PESStreamEvent values") from error
    values: list[MetadataSTDByte] = []
    for event in events:
        if not isinstance(event, PESStreamEvent):
            raise TypeError("every PES event must be a PESStreamEvent")
        values.extend(metadata_std_bytes_from_pes(event, clock_events))
    values.sort(key=lambda value: (cast(Fraction, value.arrival_time), value.source_offset))
    return SynchronousMetadataSTDModel(
        descriptor,
        transport_buffer_size=transport_buffer_size,
        maximum_delay=maximum_delay,
    ).simulate(values)


def simulate_asynchronous_metadata_pes(
    descriptor: MetadataSTDDescriptor,
    pes_events: Iterable[PESStreamEvent],
    clock_events: Sequence[ProgramClockEvent],
    *,
    transport_buffer_size: int = H222_TRANSPORT_BUFFER_SIZE,
    maximum_delay: Fraction | int | float = ST1402_MAX_METADATA_DELAY,
) -> AsynchronousMetadataSTDModelResult:
    """Run an aggregate asynchronous metadata STD audit over recorded PES."""

    try:
        events = tuple(pes_events)
    except TypeError as error:
        raise TypeError("pes_events must be an iterable of PESStreamEvent values") from error
    values: list[AsynchronousMetadataSTDByte] = []
    for event in events:
        if not isinstance(event, PESStreamEvent):
            raise TypeError("every PES event must be a PESStreamEvent")
        values.extend(asynchronous_metadata_std_bytes_from_pes(event, clock_events))
    values.sort(key=lambda value: (cast(Fraction, value.arrival_time), value.source_offset))
    return AsynchronousMetadataSTDModel(
        descriptor,
        transport_buffer_size=transport_buffer_size,
        maximum_delay=maximum_delay,
    ).simulate(values)


@dataclass(frozen=True, slots=True)
class MetadataDelayIssue:
    """One delay violation proven by the PCR samples bracketing a PES arrival."""

    code: str
    requirement: str
    program_number: int
    pid: int
    pts: int
    source_offset: int
    previous_pcr_offset: int
    next_pcr_offset: int
    minimum_delay: Fraction
    maximum_delay: Fraction
    permitted_delay: Fraction
    message: str


@dataclass(frozen=True, slots=True)
class _ClockPoint:
    source_offset: int
    ticks: int


@dataclass(frozen=True, slots=True)
class _PendingPES:
    program_number: int
    pid: int
    pts: int
    source_offset: int
    previous: _ClockPoint


class MetadataDelayValidator:
    """Audit ST 1402-12 without inventing arrival times between PCR samples.

    A file records PCR time only at discrete byte positions. For a PES byte
    between two samples, its actual arrival lies somewhere within that clock
    interval. The validator therefore uses a closed delay range: it reports a
    violation only when every possible arrival is too early or too late, and
    calls a PES compliant only when the complete range is within zero and one
    second. Straddling ranges are counted as indeterminate.
    """

    __slots__ = (
        "_clock_points",
        "_compliant_pes",
        "_indeterminate_pes",
        "_max_clock_points",
        "_max_pending_pes",
        "_maximum_delay",
        "_pending",
        "_unverifiable_pes",
        "_violating_pes",
    )

    def __init__(
        self,
        *,
        maximum_delay: Fraction | int | float = ST1402_MAX_METADATA_DELAY,
        max_clock_points: int = 64,
        max_pending_pes: int = 1024,
    ) -> None:
        parsed_delay = _delay(maximum_delay)
        if parsed_delay <= 0:
            raise ValueError("maximum_delay must be positive")
        self._maximum_delay = parsed_delay
        self._max_clock_points = _positive_integer(
            max_clock_points, name="max_clock_points", minimum=2
        )
        self._max_pending_pes = _positive_integer(
            max_pending_pes, name="max_pending_pes"
        )
        self._clock_points: dict[int, list[_ClockPoint]] = {}
        self._pending: list[_PendingPES] = []
        self._compliant_pes = 0
        self._violating_pes = 0
        self._indeterminate_pes = 0
        self._unverifiable_pes = 0

    @property
    def maximum_delay(self) -> Fraction:
        return self._maximum_delay

    @property
    def programs(self) -> tuple[int, ...]:
        return tuple(sorted(self._clock_points))

    @property
    def pending_pes(self) -> int:
        return len(self._pending)

    @property
    def observed_pes(self) -> int:
        return (
            self._compliant_pes
            + self._violating_pes
            + self._indeterminate_pes
            + self._unverifiable_pes
            + len(self._pending)
        )

    @property
    def compliant_pes(self) -> int:
        return self._compliant_pes

    @property
    def violating_pes(self) -> int:
        return self._violating_pes

    @property
    def indeterminate_pes(self) -> int:
        return self._indeterminate_pes

    @property
    def unverifiable_pes(self) -> int:
        return self._unverifiable_pes

    def observe_clock(self, event: ProgramClockEvent) -> tuple[MetadataDelayIssue, ...]:
        """Observe a PCR and resolve PES arrivals that it now brackets."""

        if not isinstance(event, ProgramClockEvent):
            raise TypeError("event must be a ProgramClockEvent")
        points = self._clock_points.get(event.program_number)
        if event.discontinuity:
            self._discard_pending(event.program_number)
            points = None
        reference = None if not points else points[-1].ticks
        ticks = unwrap_pcr_ticks(event.pcr.ticks, reference=reference)
        if points and ticks < points[-1].ticks:
            self._discard_pending(event.program_number)
            points = None
        point = _ClockPoint(event.source_offset, ticks)
        if points is None:
            points = []
            self._clock_points[event.program_number] = points
        points.append(point)
        if len(points) > self._max_clock_points:
            del points[: len(points) - self._max_clock_points]

        issues: list[MetadataDelayIssue] = []
        retained: list[_PendingPES] = []
        for pending in self._pending:
            if (
                pending.program_number == event.program_number
                and pending.source_offset <= event.source_offset
            ):
                issues.extend(self._evaluate(pending, point))
            else:
                retained.append(pending)
        self._pending = retained
        return tuple(issues)

    def observe_pes(self, event: PESStreamEvent) -> tuple[MetadataDelayIssue, ...]:
        """Observe one PES, ignoring streams outside synchronous KLV carriage."""

        if not isinstance(event, PESStreamEvent):
            raise TypeError("event must be a PESStreamEvent")
        if (
            event.kind is not StreamKind.KLV
            or event.klv_carriage is not KLVCarriage.SYNCHRONOUS
            or event.pes.pts is None
        ):
            return ()
        source_offset = (
            event.pes.transport_packets[0].offset
            if event.pes.transport_packets
            else event.pes.offset
        )
        points = self._clock_points.get(event.program_number, ())
        previous = next(
            (point for point in reversed(points) if point.source_offset <= source_offset),
            None,
        )
        following = next(
            (point for point in points if point.source_offset >= source_offset),
            None,
        )
        if previous is None:
            self._unverifiable_pes += 1
            return ()
        pending = _PendingPES(
            event.program_number,
            event.pid,
            event.pes.pts,
            source_offset,
            previous,
        )
        if following is not None:
            return self._evaluate(pending, following)
        if len(self._pending) == self._max_pending_pes:
            self._pending.pop(0)
            self._unverifiable_pes += 1
        self._pending.append(pending)
        return ()

    def finish(self) -> tuple[MetadataDelayIssue, ...]:
        """Finalize coverage; trailing PES values without a PCR are unverifiable."""

        self._unverifiable_pes += len(self._pending)
        self._pending.clear()
        return ()

    def reset(self, *, program_number: int | None = None) -> None:
        """Reset all PCR brackets or one program after a timeline discontinuity."""

        if program_number is None:
            self._unverifiable_pes += len(self._pending)
            self._pending.clear()
            self._clock_points.clear()
            return
        if (
            isinstance(program_number, bool)
            or not isinstance(program_number, int)
            or not 1 <= program_number <= 0xFFFF
        ):
            raise ValueError("program_number must be an integer from 1 to 65535")
        self._discard_pending(program_number)
        self._clock_points.pop(program_number, None)

    def _discard_pending(self, program_number: int) -> None:
        retained = [
            pending
            for pending in self._pending
            if pending.program_number != program_number
        ]
        self._unverifiable_pes += len(self._pending) - len(retained)
        self._pending = retained

    def _evaluate(
        self,
        pending: _PendingPES,
        following: _ClockPoint,
    ) -> tuple[MetadataDelayIssue, ...]:
        reference_pts = pending.previous.ticks // 300
        pts_ticks = unwrap_pts(pending.pts, reference=reference_pts) * 300
        minimum = Fraction(pts_ticks - following.ticks, PCR_CLOCK_RATE)
        maximum = Fraction(pts_ticks - pending.previous.ticks, PCR_CLOCK_RATE)
        if minimum > self._maximum_delay:
            self._violating_pes += 1
            return (self._issue("excessive_delay", pending, following, minimum, maximum),)
        if maximum < 0:
            self._violating_pes += 1
            return (self._issue("late", pending, following, minimum, maximum),)
        if minimum >= 0 and maximum <= self._maximum_delay:
            self._compliant_pes += 1
        else:
            self._indeterminate_pes += 1
        return ()

    def _issue(
        self,
        code: str,
        pending: _PendingPES,
        following: _ClockPoint,
        minimum: Fraction,
        maximum: Fraction,
    ) -> MetadataDelayIssue:
        if code == "late":
            message = (
                f"program {pending.program_number} PID {pending.pid} synchronous metadata "
                "arrived after its PTS under every PCR-bracketed arrival time"
            )
        else:
            message = (
                f"program {pending.program_number} PID {pending.pid} synchronous metadata "
                f"has a minimum decoder delay of {float(minimum):.6f} seconds; "
                "ST 1402-12 limits delay to one second"
            )
        return MetadataDelayIssue(
            code,
            "ST 1402.2 ST 1402-12",
            pending.program_number,
            pending.pid,
            pending.pts,
            pending.source_offset,
            pending.previous.source_offset,
            following.source_offset,
            minimum,
            maximum,
            self._maximum_delay,
            message,
        )
