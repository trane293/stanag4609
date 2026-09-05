"""Bounded, dependency-free inference-stage runtime metrics."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Literal

from stanag4609.errors import LimitExceeded


@dataclass(frozen=True, slots=True)
class InferenceStageMetrics:
    """Immutable counters and latency totals for one named inference stage."""

    stage: str
    started: int
    succeeded: int
    failed: int
    timed_out: int
    cancelled: int
    in_flight: int
    total_duration_seconds: float
    max_duration_seconds: float
    last_duration_seconds: float | None
    last_error_type: str | None


@dataclass(slots=True)
class _MutableStageMetrics:
    started: int = 0
    succeeded: int = 0
    failed: int = 0
    timed_out: int = 0
    cancelled: int = 0
    in_flight: int = 0
    total_duration_seconds: float = 0.0
    max_duration_seconds: float = 0.0
    last_duration_seconds: float | None = None
    last_error_type: str | None = None


class InferenceMetrics:
    """Thread-safe bounded collector shared by inference stages and graphs.

    Snapshots retain only counters, durations, and exception type names. Frame
    pixels, metadata, inference payloads, messages, and tracebacks are never
    retained.
    """

    __slots__ = ("_lock", "_stages", "max_stages")

    def __init__(self, *, max_stages: int = 128) -> None:
        if isinstance(max_stages, bool) or not isinstance(max_stages, int):
            raise TypeError("max_stages must be an integer")
        if max_stages < 1:
            raise ValueError("max_stages must be positive")
        self.max_stages = max_stages
        self._lock = threading.Lock()
        self._stages: dict[str, _MutableStageMetrics] = {}

    def _start(self, stage: str) -> float:
        """Record one invocation and return its monotonic start time."""

        with self._lock:
            current = self._stages.get(stage)
            if current is None:
                if len(self._stages) >= self.max_stages:
                    raise LimitExceeded(
                        f"inference metrics exceed configured stage limit {self.max_stages}"
                    )
                current = _MutableStageMetrics()
                self._stages[stage] = current
            current.started += 1
            current.in_flight += 1
        return time.perf_counter()

    def _finish(
        self,
        stage: str,
        started_at: float,
        *,
        outcome: Literal["succeeded", "failed", "timed_out", "cancelled"],
        error: BaseException | None = None,
    ) -> None:
        """Record one terminal outcome for a previously started invocation."""

        if outcome not in {"succeeded", "failed", "timed_out", "cancelled"}:
            raise ValueError(f"unknown inference outcome {outcome!r}")
        duration = max(0.0, time.perf_counter() - started_at)
        with self._lock:
            current = self._stages[stage]
            if current.in_flight < 1:
                raise RuntimeError(f"inference stage {stage!r} has no in-flight invocation")
            current.in_flight -= 1
            setattr(current, outcome, getattr(current, outcome) + 1)
            current.total_duration_seconds += duration
            current.max_duration_seconds = max(current.max_duration_seconds, duration)
            current.last_duration_seconds = duration
            current.last_error_type = None if error is None else type(error).__name__

    def snapshot(self) -> tuple[InferenceStageMetrics, ...]:
        """Return a stable stage-name-sorted copy of all metrics."""

        with self._lock:
            return tuple(
                _snapshot_stage(stage, current)
                for stage, current in sorted(self._stages.items())
            )

    def reset(self) -> None:
        """Clear completed metrics when no invocation is in flight."""

        with self._lock:
            if any(current.in_flight for current in self._stages.values()):
                raise RuntimeError("cannot reset inference metrics while work is in flight")
            self._stages.clear()


def _snapshot_stage(
    stage: str, metrics: _MutableStageMetrics
) -> InferenceStageMetrics:
    return InferenceStageMetrics(
        stage=stage,
        started=metrics.started,
        succeeded=metrics.succeeded,
        failed=metrics.failed,
        timed_out=metrics.timed_out,
        cancelled=metrics.cancelled,
        in_flight=metrics.in_flight,
        total_duration_seconds=metrics.total_duration_seconds,
        max_duration_seconds=metrics.max_duration_seconds,
        last_duration_seconds=metrics.last_duration_seconds,
        last_error_type=metrics.last_error_type,
    )
