"""Compose synchronous and asynchronous inference stages."""

import asyncio

from stanag4609.sidecar import (
    FrameEnvelope,
    InferenceContext,
    InferenceOutput,
    InferenceStage,
    Parallel,
    Sequential,
)


def local_detector(context: InferenceContext) -> InferenceOutput:
    return InferenceOutput(data={"frame": context.frame.sequence_number, "source": "local"})


async def remote_detector(context: InferenceContext) -> InferenceOutput:
    await asyncio.sleep(0)
    return InferenceOutput(data={"frame": context.frame.sequence_number, "source": "remote"})


def fuse(context: InferenceContext) -> InferenceOutput:
    return InferenceOutput(data=(context.result("local"), context.result("remote")))


async def main() -> None:
    graph = Sequential(
        Parallel(
            InferenceStage("local", local_detector, threaded=True),
            InferenceStage("remote", remote_detector, timeout_seconds=0.25),
            max_concurrency=2,
        ),
        InferenceStage("fusion", fuse),
    )
    frame = FrameEnvelope(1, 90_000, 640, 480, pixels=None)
    completed = await graph.run(InferenceContext(frame))
    print(tuple(result.stage for result in completed.results))


if __name__ == "__main__":
    asyncio.run(main())
