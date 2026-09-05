"""Keep live inference latency bounded by dropping the stalest decoded frame."""

import asyncio

from stanag4609.sidecar import AsyncFrameQueue, FrameEnvelope, FrameOverflowPolicy


async def main() -> None:
    queue = AsyncFrameQueue(2, policy=FrameOverflowPolicy.DROP_OLDEST)
    for sequence in range(3):
        frame = FrameEnvelope(sequence, sequence * 3_000, 640, 480, b"frame handle")
        result = await queue.put(frame)
        if result.dropped is not None:
            print("dropped", result.dropped.sequence_number)

    while not queue.empty():
        frame = await queue.get()
        print("process", frame.sequence_number)
        queue.task_done()
    await queue.join()


asyncio.run(main())
