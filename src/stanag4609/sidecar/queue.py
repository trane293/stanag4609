"""Bounded asyncio queue for decoded frames with explicit overload policy."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum

from stanag4609.sidecar.model import FrameEnvelope


class FrameOverflowPolicy(Enum):
    """Action taken when a decoded-frame queue reaches capacity."""

    BLOCK = "block"
    DROP_OLDEST = "drop_oldest"
    DROP_NEWEST = "drop_newest"
    RAISE = "raise"


@dataclass(frozen=True, slots=True)
class FrameQueuePutResult:
    """Outcome of one frame enqueue attempt."""

    accepted: bool
    dropped: FrameEnvelope | None = None


class AsyncFrameQueue:
    """A bounded decoded-frame queue with visible loss and backpressure.

    ``BLOCK`` is the default and propagates backpressure to the producer.
    Real-time displays commonly choose ``DROP_OLDEST`` to bound latency, while
    archival analytics may use ``RAISE`` to make any overload fatal.
    """

    def __init__(
        self,
        capacity: int,
        *,
        policy: FrameOverflowPolicy = FrameOverflowPolicy.BLOCK,
    ) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("capacity must be a positive integer")
        if not isinstance(policy, FrameOverflowPolicy):
            raise TypeError("policy must be FrameOverflowPolicy")
        self.capacity = capacity
        self.policy = policy
        self._queue: asyncio.Queue[FrameEnvelope] = asyncio.Queue(maxsize=capacity)
        self._accepted_frames = 0
        self._dropped_frames = 0

    @property
    def accepted_frames(self) -> int:
        return self._accepted_frames

    @property
    def dropped_frames(self) -> int:
        return self._dropped_frames

    def qsize(self) -> int:
        return self._queue.qsize()

    def empty(self) -> bool:
        return self._queue.empty()

    def full(self) -> bool:
        return self._queue.full()

    async def put(self, frame: FrameEnvelope) -> FrameQueuePutResult:
        """Apply the configured policy and report any frame that was lost."""
        if not isinstance(frame, FrameEnvelope):
            raise TypeError("frame must be FrameEnvelope")
        if self.policy is FrameOverflowPolicy.BLOCK:
            await self._queue.put(frame)
        elif self.policy is FrameOverflowPolicy.DROP_NEWEST and self.full():
            self._dropped_frames += 1
            return FrameQueuePutResult(False, frame)
        elif self.policy is FrameOverflowPolicy.DROP_OLDEST and self.full():
            dropped = self._queue.get_nowait()
            self._queue.task_done()
            self._queue.put_nowait(frame)
            self._accepted_frames += 1
            self._dropped_frames += 1
            return FrameQueuePutResult(True, dropped)
        else:
            self._queue.put_nowait(frame)
        self._accepted_frames += 1
        return FrameQueuePutResult(True)

    async def get(self) -> FrameEnvelope:
        """Wait for and remove the next queued frame."""
        return await self._queue.get()

    def get_nowait(self) -> FrameEnvelope:
        """Remove the next frame or raise :class:`asyncio.QueueEmpty`."""
        return self._queue.get_nowait()

    def task_done(self) -> None:
        """Mark a retrieved frame complete for :meth:`join` accounting."""
        self._queue.task_done()

    async def join(self) -> None:
        """Wait until every accepted, non-evicted frame is marked complete."""
        await self._queue.join()
