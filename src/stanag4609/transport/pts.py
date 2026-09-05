"""Per-elementary-stream ST 1402 presentation-timestamp cadence auditing."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from types import MappingProxyType

from stanag4609.transport.demux import PESStreamEvent
from stanag4609.transport.timing import PTS_CLOCK_RATE, unwrap_pts

ST1402_MAX_PTS_INTERVAL = Fraction(7, 10)


def _interval(value: Fraction | int | float) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (Fraction, int, float)):
        raise TypeError("maximum_interval must be a Fraction, integer, or float")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("maximum_interval must be finite")
        return Fraction(str(value))
    return Fraction(value)


@dataclass(frozen=True, slots=True)
class PTSCadenceIssue:
    """One excessive interval between successive PTS values."""

    code: str
    requirement: str
    program_number: int
    pid: int
    stream_type: int
    previous_pts: int
    observed_pts: int
    previous_source_offset: int
    current_source_offset: int
    difference: Fraction
    maximum_interval: Fraction
    message: str


@dataclass(frozen=True, slots=True)
class _PTSState:
    stream_type: int
    pts: int
    source_offset: int
    observations: int


class PTSCadenceValidator:
    """Audit ST 1402.2 §7.3 for each elementary stream independently.

    The comparison uses the shortest signed distance across the 33-bit PTS
    rollover. Small presentation-order regressions are accepted; the standard
    constrains the magnitude of the difference, not its direction. A transport
    discontinuity or a stream-type change on a reused PID establishes a new
    baseline.
    """

    __slots__ = ("_maximum_interval", "_states")

    def __init__(
        self,
        *,
        maximum_interval: Fraction | int | float = ST1402_MAX_PTS_INTERVAL,
    ) -> None:
        interval = _interval(maximum_interval)
        if interval <= 0:
            raise ValueError("maximum_interval must be positive")
        self._maximum_interval = interval
        self._states: dict[tuple[int, int], _PTSState] = {}

    @property
    def maximum_interval(self) -> Fraction:
        return self._maximum_interval

    @property
    def streams(self) -> tuple[tuple[int, int], ...]:
        """Return ``(program_number, PID)`` keys with a PTS baseline."""

        return tuple(sorted(self._states))

    @property
    def last_pts(self) -> Mapping[tuple[int, int], int]:
        """Return immutable unwrapped PTS watermarks in transport order."""

        return MappingProxyType({key: state.pts for key, state in self._states.items()})

    @property
    def observation_counts(self) -> Mapping[tuple[int, int], int]:
        """Return the number of PTS-bearing PES packets observed per stream."""

        return MappingProxyType(
            {key: state.observations for key, state in self._states.items()}
        )

    def observe(self, event: PESStreamEvent) -> tuple[PTSCadenceIssue, ...]:
        """Observe one PES event and return zero or one cadence issue."""

        if not isinstance(event, PESStreamEvent):
            raise TypeError("event must be a PESStreamEvent")
        pts = event.pes.pts
        if pts is None:
            return ()
        key = (event.program_number, event.pid)
        discontinuity = any(
            packet.discontinuity_indicator for packet in event.pes.transport_packets
        )
        state = self._states.get(key)
        if (
            state is None
            or discontinuity
            or state.stream_type != event.stream.stream_type
        ):
            self._states[key] = _PTSState(
                event.stream.stream_type,
                pts,
                event.pes.offset,
                1,
            )
            return ()

        observed = unwrap_pts(pts, reference=state.pts)
        difference = Fraction(abs(observed - state.pts), PTS_CLOCK_RATE)
        self._states[key] = _PTSState(
            event.stream.stream_type,
            observed,
            event.pes.offset,
            state.observations + 1,
        )
        if difference <= self._maximum_interval:
            return ()
        return (
            PTSCadenceIssue(
                "interval",
                "ST 1402.2 §7.3",
                event.program_number,
                event.pid,
                event.stream.stream_type,
                state.pts,
                observed,
                state.source_offset,
                event.pes.offset,
                difference,
                self._maximum_interval,
                (
                    f"program {event.program_number} PID {event.pid} successive PTS values "
                    f"differ by {float(difference):.6f} seconds; ST 1402.2 limits the "
                    "difference to 0.7 seconds"
                ),
            ),
        )

    def reset(
        self,
        *,
        program_number: int | None = None,
        pid: int | None = None,
    ) -> None:
        """Forget all baselines, one program, or one program/PID stream."""

        if program_number is None:
            if pid is not None:
                raise ValueError("pid requires program_number")
            self._states.clear()
            return
        if (
            isinstance(program_number, bool)
            or not isinstance(program_number, int)
            or not 1 <= program_number <= 0xFFFF
        ):
            raise ValueError("program_number must be an integer from 1 to 65535")
        if pid is None:
            for key in tuple(self._states):
                if key[0] == program_number:
                    del self._states[key]
            return
        if isinstance(pid, bool) or not isinstance(pid, int) or not 0 <= pid <= 0x1FFF:
            raise ValueError("pid must be an integer from 0 to 8191")
        self._states.pop((program_number, pid), None)
