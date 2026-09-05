from __future__ import annotations

import asyncio

import pytest

from stanag4609.errors import LimitExceeded
from stanag4609.sidecar import (
    FrameEnvelope,
    InferenceContext,
    InferenceMetrics,
    InferenceOutput,
    InferenceStage,
)


def _context(sequence_number: int = 1) -> InferenceContext:
    return InferenceContext(FrameEnvelope(sequence_number, 90_000, 640, 480, None))


def test_inference_metrics_record_success_and_failure_without_payload_retention() -> None:
    metrics = InferenceMetrics()
    successful = InferenceStage(
        "detector", lambda _: InferenceOutput(data={"large": object()}), metrics=metrics
    )

    def fail(_: InferenceContext) -> InferenceOutput:
        raise ValueError("model rejected frame")

    failing = InferenceStage("classifier", fail, metrics=metrics)
    asyncio.run(successful.run(_context()))
    with pytest.raises(ValueError, match="rejected"):
        asyncio.run(failing.run(_context(2)))

    detector, classifier = metrics.snapshot()
    assert detector.stage == "classifier"
    assert detector.started == detector.failed == 1
    assert detector.succeeded == detector.timed_out == detector.cancelled == 0
    assert detector.in_flight == 0
    assert detector.last_error_type == "ValueError"
    assert classifier.stage == "detector"
    assert classifier.started == classifier.succeeded == 1
    assert classifier.failed == classifier.timed_out == classifier.cancelled == 0
    assert classifier.total_duration_seconds >= 0
    assert classifier.max_duration_seconds == classifier.last_duration_seconds
    assert "large" not in repr(classifier)


def test_inference_metrics_distinguish_timeout_and_cancellation() -> None:
    metrics = InferenceMetrics()

    async def wait(_: InferenceContext) -> InferenceOutput:
        await asyncio.sleep(10)
        return InferenceOutput()

    timed = InferenceStage("timed", wait, timeout_seconds=0.001, metrics=metrics)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(timed.run(_context()))

    async def cancel() -> None:
        stage = InferenceStage("cancelled", wait, metrics=metrics)
        task = asyncio.create_task(stage.run(_context()))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel())
    snapshots = {item.stage: item for item in metrics.snapshot()}
    assert snapshots["timed"].timed_out == 1
    assert snapshots["timed"].failed == 0
    assert snapshots["cancelled"].cancelled == 1
    assert snapshots["cancelled"].failed == 0
    assert all(item.in_flight == 0 for item in snapshots.values())


def test_inference_metrics_expose_in_flight_work_and_can_reset() -> None:
    async def exercise() -> tuple[int, int]:
        metrics = InferenceMetrics()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def wait(_: InferenceContext) -> InferenceOutput:
            entered.set()
            await release.wait()
            return InferenceOutput()

        task = asyncio.create_task(InferenceStage("live", wait, metrics=metrics).run(_context()))
        await entered.wait()
        during = metrics.snapshot()[0]
        with pytest.raises(RuntimeError, match="while work is in flight"):
            metrics.reset()
        release.set()
        await task
        after = metrics.snapshot()[0]
        metrics.reset()
        assert metrics.snapshot() == ()
        return during.in_flight, after.in_flight

    assert asyncio.run(exercise()) == (1, 0)


def test_inference_metrics_bound_stage_cardinality() -> None:
    metrics = InferenceMetrics(max_stages=1)
    asyncio.run(
        InferenceStage("first", lambda _: InferenceOutput(), metrics=metrics).run(_context())
    )
    with pytest.raises(LimitExceeded, match="stage limit 1"):
        asyncio.run(
            InferenceStage("second", lambda _: InferenceOutput(), metrics=metrics).run(
                _context()
            )
        )


@pytest.mark.parametrize("max_stages", [True, 1.5, 0])
def test_inference_metrics_validate_cardinality(max_stages: object) -> None:
    with pytest.raises((TypeError, ValueError), match="max_stages"):
        InferenceMetrics(max_stages=max_stages)  # type: ignore[arg-type]


def test_inference_metrics_count_result_merge_errors_as_failures() -> None:
    metrics = InferenceMetrics()
    first = asyncio.run(
        InferenceStage("detector", lambda _: InferenceOutput()).run(_context())
    )
    duplicate = InferenceStage(
        "detector", lambda _: InferenceOutput(), metrics=metrics
    )
    with pytest.raises(ValueError, match="already produced"):
        asyncio.run(duplicate.run(first))
    snapshot = metrics.snapshot()[0]
    assert snapshot.failed == 1
    assert snapshot.succeeded == 0
