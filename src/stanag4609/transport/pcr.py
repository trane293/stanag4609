"""Program-aware ST 1402 Program Clock Reference cadence auditing."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from types import MappingProxyType

from stanag4609.errors import DecodeError
from stanag4609.transport.demux import ProgramClockEvent
from stanag4609.transport.mpegts import ProgramClockReference
from stanag4609.transport.mux import TransportMuxer

PCR_CLOCK_RATE = 27_000_000
PCR_MODULUS = (1 << 33) * 300
ST1402_MAX_PCR_INTERVAL = Fraction(1, 10)
DEFAULT_PCR_SCHEDULER_INTERVAL = Fraction(1, 20)


def _validate_pcr_ticks(ticks: int) -> None:
    if isinstance(ticks, bool) or not isinstance(ticks, int) or not 0 <= ticks < PCR_MODULUS:
        raise ValueError("PCR ticks must be an unsigned 42-bit composite clock value")


def unwrap_pcr_ticks(ticks: int, *, reference: int | None = None) -> int:
    """Map wrapped 27 MHz PCR ticks into the epoch nearest ``reference``."""

    _validate_pcr_ticks(ticks)
    if reference is None:
        return ticks
    if isinstance(reference, bool) or not isinstance(reference, int):
        raise TypeError("PCR reference must be an integer")
    epoch = reference // PCR_MODULUS
    candidates = tuple((epoch + offset) * PCR_MODULUS + ticks for offset in (-1, 0, 1))
    distances = tuple(abs(candidate - reference) for candidate in candidates)
    minimum = min(distances)
    nearest = tuple(
        candidate
        for candidate, distance in zip(candidates, distances, strict=True)
        if distance == minimum
    )
    if len(nearest) != 1:
        raise DecodeError("PCR is exactly half-epoch from its reference")
    return nearest[0]


def _interval(value: Fraction | int | float) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (Fraction, int, float)):
        raise TypeError("maximum_interval must be a Fraction, integer, or float")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("maximum_interval must be finite")
        return Fraction(str(value))
    return Fraction(value)


def _timeline_time(value: Fraction | int | float, *, name: str) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (Fraction, int, float)):
        raise TypeError(f"{name} must be a Fraction, integer, or float")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
        return Fraction(str(value))
    return Fraction(value)


def pcr_from_ticks(ticks: int) -> ProgramClockReference:
    """Convert unbounded 27 MHz ticks to one wrapped MPEG-2 PCR value."""

    if isinstance(ticks, bool) or not isinstance(ticks, int):
        raise TypeError("PCR timeline ticks must be an integer")
    wrapped = ticks % PCR_MODULUS
    return ProgramClockReference(wrapped // 300, wrapped % 300)


@dataclass(frozen=True, slots=True)
class PCRCadenceIssue:
    """One PCR interval or unannounced clock-regression diagnostic."""

    code: str
    requirement: str
    program_number: int
    pid: int
    previous_ticks: int
    observed_ticks: int
    previous_source_offset: int
    current_source_offset: int
    elapsed: Fraction
    maximum_interval: Fraction
    message: str


@dataclass(frozen=True, slots=True)
class _PCRState:
    pid: int
    ticks: int
    source_offset: int


class PCRCadenceValidator:
    """Audit ST 1402's per-program PCR interval on the encoded 27 MHz clock.

    Feed the :class:`ProgramClockEvent` objects produced by one
    :class:`~stanag4609.transport.demux.TransportDemuxer`. A 100 ms interval is
    accepted; any larger interval is reported. PCR rollover is unwrapped per
    program. A declared time-base discontinuity or a PMT-driven PCR PID change
    establishes a new baseline instead of producing a false cadence issue.
    """

    __slots__ = ("_maximum_interval", "_states")

    def __init__(
        self,
        *,
        maximum_interval: Fraction | int | float = ST1402_MAX_PCR_INTERVAL,
    ) -> None:
        interval = _interval(maximum_interval)
        if interval <= 0:
            raise ValueError("maximum_interval must be positive")
        self._maximum_interval = interval
        self._states: dict[int, _PCRState] = {}

    @property
    def maximum_interval(self) -> Fraction:
        return self._maximum_interval

    @property
    def programs(self) -> tuple[int, ...]:
        """Return program numbers with an established PCR baseline."""

        return tuple(sorted(self._states))

    @property
    def last_pcr_ticks(self) -> Mapping[int, int]:
        """Return immutable unwrapped PCR watermarks by program."""

        return MappingProxyType(
            {program_number: state.ticks for program_number, state in self._states.items()}
        )

    def observe(self, event: ProgramClockEvent) -> tuple[PCRCadenceIssue, ...]:
        """Observe one demuxed PCR and return zero or one diagnostic."""

        if not isinstance(event, ProgramClockEvent):
            raise TypeError("event must be a ProgramClockEvent")
        if not 1 <= event.program_number <= 0xFFFF:
            raise DecodeError("PCR event program_number must be between 1 and 65535")
        raw_ticks = event.pcr.ticks
        state = self._states.get(event.program_number)
        if state is None or event.discontinuity or event.pid != state.pid:
            self._states[event.program_number] = _PCRState(
                event.pid,
                raw_ticks,
                event.source_offset,
            )
            return ()

        observed = unwrap_pcr_ticks(raw_ticks, reference=state.ticks)
        elapsed = Fraction(observed - state.ticks, PCR_CLOCK_RATE)
        self._states[event.program_number] = _PCRState(
            event.pid,
            observed,
            event.source_offset,
        )
        if observed < state.ticks:
            return (
                self._issue(
                    "regression",
                    event,
                    state,
                    observed,
                    elapsed,
                    "PCR time regressed without a discontinuity indicator",
                ),
            )
        if elapsed > self._maximum_interval:
            return (
                self._issue(
                    "interval",
                    event,
                    state,
                    observed,
                    elapsed,
                    (
                        f"program {event.program_number} PCR interval is "
                        f"{float(elapsed) * 1_000:.6f} milliseconds; ST 1402.2 "
                        "requires a PCR at least once every 100 milliseconds"
                    ),
                ),
            )
        return ()

    def reset(self, *, program_number: int | None = None) -> None:
        """Forget one program baseline, or all baselines when omitted."""

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

    def _issue(
        self,
        code: str,
        event: ProgramClockEvent,
        previous: _PCRState,
        observed: int,
        elapsed: Fraction,
        message: str,
    ) -> PCRCadenceIssue:
        return PCRCadenceIssue(
            code,
            "ST 1402.2 §7.2",
            event.program_number,
            event.pid,
            previous.ticks,
            observed,
            previous.source_offset,
            event.source_offset,
            elapsed,
            self._maximum_interval,
            message,
        )


@dataclass(frozen=True, slots=True)
class ProgramClockEmission:
    """One scheduled PCR packet and its output-timeline quality."""

    packet: bytes
    pcr: ProgramClockReference
    pid: int
    scheduled_at: Fraction
    emitted_at: Fraction
    previous_emitted_at: Fraction | None
    missed_repetitions: int
    discontinuity: bool

    @property
    def late_by(self) -> Fraction:
        return self.emitted_at - self.scheduled_at

    @property
    def elapsed_since_emission(self) -> Fraction | None:
        if self.previous_emitted_at is None:
            return None
        return self.emitted_at - self.previous_emitted_at

    @property
    def interval_compliant(self) -> bool:
        """Whether this packet stayed within ST 1402's inclusive 100 ms gap."""

        elapsed = self.elapsed_since_emission
        return elapsed is None or elapsed <= ST1402_MAX_PCR_INTERVAL


class ProgramClockScheduler:
    """Schedule PCR packets against an application's real output timeline.

    ``start`` anchors a supplied encoder clock to one monotonic output instant
    and emits that first PCR. Later ``poll`` calls derive the PCR sample from
    the actual poll time, never from the ideal slot, so late delivery cannot be
    hidden behind a stale clock value. At most one packet is emitted per poll;
    skipped slots and a gap beyond 100 ms remain observable.
    """

    __slots__ = (
        "_anchor_at",
        "_anchor_ticks",
        "_interval",
        "_last_emitted_at",
        "_muxer",
        "_next_due_at",
        "_observed_at",
        "_total_emissions",
        "_total_missed_repetitions",
    )

    def __init__(
        self,
        muxer: TransportMuxer,
        *,
        interval: Fraction | int | float = DEFAULT_PCR_SCHEDULER_INTERVAL,
    ) -> None:
        if not isinstance(muxer, TransportMuxer):
            raise TypeError("muxer must be a TransportMuxer")
        cadence = _timeline_time(interval, name="interval")
        if cadence <= 0:
            raise ValueError("interval must be positive")
        if cadence > ST1402_MAX_PCR_INTERVAL:
            raise ValueError("interval must be no greater than 0.1 seconds for ST 1402.2")
        self._muxer = muxer
        self._interval = cadence
        self._anchor_at: Fraction | None = None
        self._anchor_ticks: int | None = None
        self._next_due_at: Fraction | None = None
        self._observed_at: Fraction | None = None
        self._last_emitted_at: Fraction | None = None
        self._total_emissions = 0
        self._total_missed_repetitions = 0

    @property
    def interval(self) -> Fraction:
        return self._interval

    @property
    def next_due_at(self) -> Fraction | None:
        return self._next_due_at

    @property
    def total_emissions(self) -> int:
        return self._total_emissions

    @property
    def total_missed_repetitions(self) -> int:
        return self._total_missed_repetitions

    def start(
        self,
        pcr: ProgramClockReference,
        *,
        at: Fraction | int | float,
        discontinuity: bool = False,
    ) -> ProgramClockEmission:
        """Anchor the encoder clock and emit the first PCR packet."""

        if self._anchor_at is not None:
            raise RuntimeError("program-clock scheduler is already started")
        if not isinstance(pcr, ProgramClockReference):
            raise TypeError("pcr must be a ProgramClockReference")
        if not isinstance(discontinuity, bool):
            raise TypeError("discontinuity must be a boolean")
        timestamp = _timeline_time(at, name="timestamp")
        self._anchor_at = timestamp
        self._anchor_ticks = pcr.ticks
        self._next_due_at = timestamp + self._interval
        self._observed_at = timestamp
        emission = self._emission(
            pcr,
            scheduled_at=timestamp,
            emitted_at=timestamp,
            missed=0,
            discontinuity=discontinuity,
        )
        return emission

    def poll(
        self,
        *,
        at: Fraction | int | float,
    ) -> ProgramClockEmission | None:
        """Emit one current PCR packet when its monotonic deadline is due."""

        if self._anchor_at is None or self._anchor_ticks is None or self._next_due_at is None:
            raise RuntimeError("program-clock scheduler must be started before polling")
        timestamp = _timeline_time(at, name="timestamp")
        if self._observed_at is not None and timestamp < self._observed_at:
            raise DecodeError("program-clock scheduler timestamps must be monotonic")
        self._observed_at = timestamp
        if timestamp < self._next_due_at:
            return None

        due_count = ((timestamp - self._next_due_at) // self._interval) + 1
        missed = due_count - 1
        scheduled_at = self._next_due_at + (missed * self._interval)
        self._next_due_at += due_count * self._interval
        elapsed_ticks = ((timestamp - self._anchor_at) * PCR_CLOCK_RATE)
        clock_ticks = self._anchor_ticks + (
            elapsed_ticks.numerator // elapsed_ticks.denominator
        )
        return self._emission(
            pcr_from_ticks(clock_ticks),
            scheduled_at=scheduled_at,
            emitted_at=timestamp,
            missed=missed,
            discontinuity=False,
        )

    def reset(self) -> None:
        """Discard the clock anchor and metrics without rewinding mux state."""

        self._anchor_at = None
        self._anchor_ticks = None
        self._next_due_at = None
        self._observed_at = None
        self._last_emitted_at = None
        self._total_emissions = 0
        self._total_missed_repetitions = 0

    def _emission(
        self,
        pcr: ProgramClockReference,
        *,
        scheduled_at: Fraction,
        emitted_at: Fraction,
        missed: int,
        discontinuity: bool,
    ) -> ProgramClockEmission:
        emission = ProgramClockEmission(
            self._muxer.mux_pcr(pcr, discontinuity=discontinuity),
            pcr,
            self._muxer.pcr_pid,
            scheduled_at,
            emitted_at,
            self._last_emitted_at,
            missed,
            discontinuity,
        )
        self._last_emitted_at = emitted_at
        self._total_emissions += 1
        self._total_missed_repetitions += missed
        return emission
