"""Composable sequential and bounded-parallel inference graphs."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from stanag4609.errors import LimitExceeded
from stanag4609.sidecar.metrics import InferenceMetrics
from stanag4609.sidecar.model import InferenceContext, InferenceOutput, InferenceResult

InferenceProcessor = Callable[
    [InferenceContext], InferenceOutput | Awaitable[InferenceOutput]
]


class InferenceStep(Protocol):
    """A composable node in a sidecar inference graph."""

    @property
    def stage_names(self) -> tuple[str, ...]: ...

    async def run(self, context: InferenceContext) -> InferenceContext: ...


def _validate_steps(steps: tuple[InferenceStep, ...], *, limit: int) -> tuple[str, ...]:
    if not steps:
        raise ValueError("inference graph requires at least one step")
    if len(steps) > limit:
        raise LimitExceeded(f"inference graph exceeds configured step limit {limit}")
    names = tuple(name for step in steps for name in step.stage_names)
    if len(names) != len(set(names)):
        raise ValueError("inference stage names must be unique")
    return names


@dataclass(frozen=True, slots=True)
class InferenceStage:
    """Adapt one sync or async callable into a named inference step."""

    name: str
    processor: InferenceProcessor
    threaded: bool = False
    timeout_seconds: float | None = None
    metrics: InferenceMetrics | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("inference stage name must not be empty")
        if not callable(self.processor):
            raise TypeError("inference processor must be callable")
        if not isinstance(self.threaded, bool):
            raise TypeError("threaded must be bool")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.metrics is not None and not isinstance(self.metrics, InferenceMetrics):
            raise TypeError("metrics must be InferenceMetrics or None")

    @property
    def stage_names(self) -> tuple[str, ...]:
        return (self.name,)

    async def run(self, context: InferenceContext) -> InferenceContext:
        async def invoke() -> InferenceOutput:
            candidate = (
                await asyncio.to_thread(self.processor, context)
                if self.threaded
                else self.processor(context)
            )
            if inspect.isawaitable(candidate):
                candidate = await candidate
            if not isinstance(candidate, InferenceOutput):
                raise TypeError("inference processor must return InferenceOutput")
            return candidate

        started_at = None if self.metrics is None else self.metrics._start(self.name)
        try:
            if self.timeout_seconds is None:
                output = await invoke()
            else:
                output = await asyncio.wait_for(invoke(), timeout=self.timeout_seconds)
            updated = context.with_result(InferenceResult(self.name, output))
        except asyncio.CancelledError as error:
            if self.metrics is not None and started_at is not None:
                self.metrics._finish(self.name, started_at, outcome="cancelled", error=error)
            raise
        except asyncio.TimeoutError as error:
            if self.metrics is not None and started_at is not None:
                self.metrics._finish(self.name, started_at, outcome="timed_out", error=error)
            raise
        except Exception as error:
            if self.metrics is not None and started_at is not None:
                self.metrics._finish(self.name, started_at, outcome="failed", error=error)
            raise
        if self.metrics is not None and started_at is not None:
            self.metrics._finish(self.name, started_at, outcome="succeeded")
        return updated


class Sequential:
    """Run stages or nested graphs in declaration order."""

    def __init__(self, *steps: InferenceStep, max_steps: int = 64) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        self.steps = steps
        self._stage_names = _validate_steps(steps, limit=max_steps)

    @property
    def stage_names(self) -> tuple[str, ...]:
        return self._stage_names

    async def run(self, context: InferenceContext) -> InferenceContext:
        current = context
        for step in self.steps:
            current = await step.run(current)
        return current


class Parallel:
    """Run branches on one immutable snapshot and merge results deterministically."""

    def __init__(
        self,
        *steps: InferenceStep,
        max_concurrency: int = 4,
        max_steps: int = 64,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        self.steps = steps
        self.max_concurrency = max_concurrency
        self._stage_names = _validate_steps(steps, limit=max_steps)

    @property
    def stage_names(self) -> tuple[str, ...]:
        return self._stage_names

    async def run(self, context: InferenceContext) -> InferenceContext:
        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def execute(step: InferenceStep) -> InferenceContext:
            async with semaphore:
                return await step.run(context)

        branches = await asyncio.gather(*(execute(step) for step in self.steps))
        current = context
        prefix_length = len(context.results)
        for branch in branches:
            changed_frame = branch.frame is not context.frame
            changed_prefix = branch.results[:prefix_length] != context.results
            if changed_frame or changed_prefix:
                raise ValueError("parallel inference branch changed its input context")
            for result in branch.results[prefix_length:]:
                current = current.with_result(result)
        return current
