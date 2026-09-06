"""Command-line generator for first-party synthetic FMV demonstration files."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from stanag4609.player.demo import DEMO_VARIANTS, generate_demo_fmv


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="output directory")
    parser.add_argument("--duration", type=float, default=12)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--force", action="store_true")
    arguments = parser.parse_args(argv)
    arguments.output.mkdir(parents=True, exist_ok=True)
    for variant in DEMO_VARIANTS:
        destination = arguments.output / f"stanag4609-{variant}-demo.ts"
        result = generate_demo_fmv(
            destination,
            variant=variant,
            duration_seconds=arguments.duration,
            ffmpeg=arguments.ffmpeg,
            overwrite=arguments.force,
        )
        print(f"{result.destination}: {result.records_written} synchronized ST 0601 packets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
