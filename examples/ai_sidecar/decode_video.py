"""Decode video into model-neutral AI sidecar frames with optional PyAV."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from stanag4609.sidecar import PyAVFrameSource


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--video-stream", type=int, default=0)
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be positive")

    frames = iter(PyAVFrameSource(args.source, video_stream=args.video_stream))
    try:
        for _ in range(args.limit):
            frame = next(frames, None)
            if frame is None:
                break
            print(
                f"frame={frame.sequence_number} pts={frame.pts} "
                f"size={frame.width}x{frame.height} pixels={frame.pixels.shape}"
            )
    finally:
        frames.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
