from __future__ import annotations

import asyncio

import pytest

from stanag4609.sidecar import (
    AsyncFrameQueue,
    FrameEnvelope,
    FrameOverflowPolicy,
)


def _frame(sequence: int) -> FrameEnvelope:
    return FrameEnvelope(sequence, sequence * 3_000, 1, 1, bytes((sequence,)))


def test_block_policy_applies_backpressure_until_capacity_is_available() -> None:
    async def scenario() -> None:
        queue = AsyncFrameQueue(capacity=1)
        first = await queue.put(_frame(1))
        assert first.accepted and first.dropped is None

        pending = asyncio.create_task(queue.put(_frame(2)))
        await asyncio.sleep(0)
        assert not pending.done()
        assert await queue.get() == _frame(1)
        queue.task_done()

        second = await pending
        assert second.accepted and second.dropped is None
        assert await queue.get() == _frame(2)
        queue.task_done()
        await queue.join()
        assert queue.accepted_frames == 2
        assert queue.dropped_frames == 0

    asyncio.run(scenario())


def test_drop_oldest_policy_returns_evicted_frame_and_keeps_latest() -> None:
    async def scenario() -> None:
        queue = AsyncFrameQueue(2, policy=FrameOverflowPolicy.DROP_OLDEST)
        assert queue.empty()
        await queue.put(_frame(1))
        await queue.put(_frame(2))
        result = await queue.put(_frame(3))
        assert result.accepted
        assert result.dropped == _frame(1)
        assert queue.qsize() == 2
        assert not queue.empty()
        assert queue.full()
        assert queue.get_nowait() == _frame(2)
        queue.task_done()
        assert queue.get_nowait() == _frame(3)
        queue.task_done()
        await queue.join()
        assert queue.accepted_frames == 3
        assert queue.dropped_frames == 1

    asyncio.run(scenario())


def test_drop_newest_policy_rejects_input_without_disturbing_queue() -> None:
    async def scenario() -> None:
        queue = AsyncFrameQueue(1, policy=FrameOverflowPolicy.DROP_NEWEST)
        await queue.put(_frame(1))
        result = await queue.put(_frame(2))
        assert not result.accepted
        assert result.dropped == _frame(2)
        assert await queue.get() == _frame(1)
        queue.task_done()
        assert queue.accepted_frames == 1
        assert queue.dropped_frames == 1

    asyncio.run(scenario())


def test_raise_policy_surfaces_asyncio_queue_full() -> None:
    async def scenario() -> None:
        queue = AsyncFrameQueue(1, policy=FrameOverflowPolicy.RAISE)
        await queue.put(_frame(1))
        with pytest.raises(asyncio.QueueFull):
            await queue.put(_frame(2))
        assert queue.accepted_frames == 1
        assert queue.dropped_frames == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("capacity", [0, -1, True])
def test_frame_queue_requires_positive_integer_capacity(capacity: int) -> None:
    with pytest.raises(ValueError, match="capacity"):
        AsyncFrameQueue(capacity)


def test_frame_queue_validates_policy_and_values() -> None:
    with pytest.raises(TypeError, match="policy"):
        AsyncFrameQueue(1, policy="block")  # type: ignore[arg-type]

    async def scenario() -> None:
        queue = AsyncFrameQueue(1)
        with pytest.raises(TypeError, match="FrameEnvelope"):
            await queue.put(object())  # type: ignore[arg-type]
        with pytest.raises(asyncio.QueueEmpty):
            queue.get_nowait()
        with pytest.raises(ValueError, match="task_done"):
            queue.task_done()

    asyncio.run(scenario())
