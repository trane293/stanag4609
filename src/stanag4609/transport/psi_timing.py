"""Conservative PCR-bracketed PAT and PMT cadence auditing."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from fractions import Fraction

from stanag4609.transport.demux import PATEvent, PMTEvent, ProgramClockEvent
from stanag4609.transport.pcr import PCR_CLOCK_RATE, unwrap_pcr_ticks
from stanag4609.transport.psi import ST1402_MAX_PSI_INTERVAL


def _positive_interval(value: Fraction | int | float) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (Fraction, int, float)):
        raise TypeError("maximum_interval must be a Fraction, integer, or float")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("maximum_interval must be finite")
        result = Fraction(str(value))
    else:
        result = Fraction(value)
    if result <= 0:
        raise ValueError("maximum_interval must be positive")
    return result


@dataclass(frozen=True, slots=True)
class PSITimingIssue:
    """One PAT/PMT interval proven non-compliant by PCR arrival brackets."""

    code: str
    requirement: str
    table: str
    program_number: int
    previous_source_offset: int | None
    current_source_offset: int | None
    minimum_interval: Fraction
    maximum_interval: Fraction
    allowed_interval: Fraction
    message: str


@dataclass(frozen=True, slots=True)
class _Observation:
    table: str
    source_offset: int | None


@dataclass(frozen=True, slots=True)
class _ArrivalWindow:
    lower_ticks: int
    upper_ticks: int
    source_offset: int | None


@dataclass(slots=True)
class _ProgramState:
    pcr_pid: int | None = None
    pcr_ticks: int | None = None
    pending: deque[_Observation] = field(default_factory=deque)
    previous: dict[str, _ArrivalWindow] = field(default_factory=dict)
    overdue: set[str] = field(default_factory=set)


class PCRBracketedPSICadenceValidator:
    """Prove PAT/PMT cadence violations without assuming transport bit rate.

    A table packet's exact arrival time is generally unavailable in a stored
    transport stream. This validator brackets each observation between the PCR
    immediately before and after it. It emits an issue only when the *smallest*
    possible interval between consecutive occurrences is at least 250 ms, the
    strict ST 1402-02 boundary implied by "more than four times per second".

    Feed events from one :class:`~stanag4609.transport.TransportDemuxer` in wire
    order. State and pending observations are bounded per program. PCR
    discontinuities, regressions, and PCR PID changes reanchor the proof rather
    than creating false blackout reports.
    """

    __slots__ = (
        "_dropped_observations",
        "_max_pending_per_program",
        "_maximum_interval",
        "_states",
        "_unverifiable_observations",
    )

    def __init__(
        self,
        *,
        maximum_interval: Fraction | int | float = ST1402_MAX_PSI_INTERVAL,
        max_pending_per_program: int = 1024,
    ) -> None:
        self._maximum_interval = _positive_interval(maximum_interval)
        if (
            isinstance(max_pending_per_program, bool)
            or not isinstance(max_pending_per_program, int)
            or max_pending_per_program < 1
        ):
            raise ValueError("max_pending_per_program must be a positive integer")
        self._max_pending_per_program = max_pending_per_program
        self._states: dict[int, _ProgramState] = {}
        self._unverifiable_observations = 0
        self._dropped_observations = 0

    @property
    def maximum_interval(self) -> Fraction:
        return self._maximum_interval

    @property
    def programs(self) -> tuple[int, ...]:
        return tuple(sorted(self._states))

    @property
    def unverifiable_observations(self) -> int:
        """Occurrences discarded because no complete PCR bracket existed."""

        return self._unverifiable_observations

    @property
    def dropped_observations(self) -> int:
        """Occurrences dropped at the configured per-program memory bound."""

        return self._dropped_observations

    def observe(self, event: PATEvent | PMTEvent | ProgramClockEvent) -> tuple[PSITimingIssue, ...]:
        """Observe one demux event in wire order and return proven issues."""

        if isinstance(event, PATEvent):
            self._observe_pat(event)
            return ()
        if isinstance(event, PMTEvent):
            self._observe_pmt(event)
            return ()
        if isinstance(event, ProgramClockEvent):
            return self._observe_clock(event)
        raise TypeError("event must be a PATEvent, PMTEvent, or ProgramClockEvent")

    def reset(self, *, program_number: int | None = None) -> None:
        """Forget timing proof state for one program or every program."""

        if program_number is None:
            self._states.clear()
            return
        if (
            isinstance(program_number, bool)
            or not isinstance(program_number, int)
            or not 1 <= program_number <= 0xFFFF
        ):
            raise ValueError("program_number must be an integer from 1 to 65535")
        self._states.pop(program_number, None)

    def _observe_pat(self, event: PATEvent) -> None:
        if not event.table.current_next_indicator:
            return
        program_numbers = {
            association.program_number
            for association in event.programs
            if association.program_number != 0
        }
        for removed in self._states.keys() - program_numbers:
            state = self._states.pop(removed)
            self._unverifiable_observations += len(state.pending)
        for program_number in program_numbers:
            self._queue(program_number, "PAT", event.source_offset)

    def _observe_pmt(self, event: PMTEvent) -> None:
        if event.table.current_next_indicator:
            self._queue(event.table.program_number, "PMT", event.source_offset)

    def _queue(self, program_number: int, table: str, offset: int | None) -> None:
        state = self._states.setdefault(program_number, _ProgramState())
        if len(state.pending) >= self._max_pending_per_program:
            state.pending.popleft()
            self._dropped_observations += 1
        state.pending.append(_Observation(table, offset))

    def _observe_clock(self, event: ProgramClockEvent) -> tuple[PSITimingIssue, ...]:
        state = self._states.setdefault(event.program_number, _ProgramState())
        raw_ticks = event.pcr.ticks
        if state.pcr_ticks is None or state.pcr_pid != event.pid or event.discontinuity:
            self._reanchor(state, pid=event.pid, ticks=raw_ticks)
            return ()

        current_ticks = unwrap_pcr_ticks(raw_ticks, reference=state.pcr_ticks)
        if current_ticks < state.pcr_ticks:
            self._reanchor(state, pid=event.pid, ticks=raw_ticks)
            return ()

        lower_ticks = state.pcr_ticks
        issues: list[PSITimingIssue] = []
        observed_tables: set[str] = set()
        while state.pending:
            observation = state.pending.popleft()
            observed_tables.add(observation.table)
            current = _ArrivalWindow(
                lower_ticks,
                current_ticks,
                observation.source_offset,
            )
            state.previous[observation.table] = current
            state.overdue.discard(observation.table)
        for table, previous in state.previous.items():
            if table in observed_tables or table in state.overdue:
                continue
            minimum = Fraction(current_ticks - previous.upper_ticks, PCR_CLOCK_RATE)
            if minimum >= self._maximum_interval:
                maximum = Fraction(current_ticks - previous.lower_ticks, PCR_CLOCK_RATE)
                issues.append(
                    self._issue(
                        table=table,
                        program_number=event.program_number,
                        previous=previous,
                        current_source_offset=event.source_offset,
                        minimum=minimum,
                        maximum=maximum,
                    )
                )
                state.overdue.add(table)
        state.pcr_pid = event.pid
        state.pcr_ticks = current_ticks
        return tuple(issues)

    def _reanchor(self, state: _ProgramState, *, pid: int, ticks: int) -> None:
        self._unverifiable_observations += len(state.pending)
        state.pending.clear()
        state.previous.clear()
        state.overdue.clear()
        state.pcr_pid = pid
        state.pcr_ticks = ticks

    def _issue(
        self,
        *,
        table: str,
        program_number: int,
        previous: _ArrivalWindow,
        current_source_offset: int | None,
        minimum: Fraction,
        maximum: Fraction,
    ) -> PSITimingIssue:
        return PSITimingIssue(
            "interval",
            "MISB ST 1402.2 ST 1402-02",
            table,
            program_number,
            previous.source_offset,
            current_source_offset,
            minimum,
            maximum,
            self._maximum_interval,
            (
                f"program {program_number} {table} has not recurred for at least "
                f"{float(minimum) * 1_000:.6f} milliseconds; ST 1402-02 "
                "requires the table more than four times per second"
            ),
        )
