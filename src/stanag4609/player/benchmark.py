"""Reproducible sustained-load measurements for the live player gateway."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import shutil
import subprocess
import sys
import time
import tracemalloc
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from stanag4609.player.live import LivePlayerGateway

try:
    import resource
except ImportError:  # pragma: no cover - unavailable on Windows
    resource = None  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class LivePlayerBenchmark:
    """Machine-readable result for one complete live-gateway input epoch."""

    schema_version: int
    source: str
    source_bytes: int
    source_sha256: str
    source_duration_seconds: float | None
    elapsed_seconds: float
    input_megabits_per_second: float
    media_seconds_per_wall_second: float | None
    input_chunks: int
    input_chunk_bytes: int
    metadata_samples: int
    media_fragments: int
    retained_metadata_samples: int
    dropped_metadata_samples: int
    retained_media_fragments: int
    dropped_media_fragments: int
    retained_media_bytes: int
    python_traced_peak_bytes: int
    python_process_peak_rss_bytes: int | None
    child_process_peak_rss_bytes: int | None
    python: str
    platform: str
    ffmpeg: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""

        return asdict(self)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_duration(path: Path, *, ffprobe: str) -> float | None:
    if shutil.which(ffprobe) is None:
        return None
    completed = subprocess.run(
        (
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ),
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    if completed.returncode:
        return None
    try:
        duration = float(completed.stdout.strip())
    except ValueError:
        return None
    return duration if math.isfinite(duration) and duration >= 0 else None


def _ffmpeg_version(ffmpeg: str) -> str:
    completed = subprocess.run(
        (ffmpeg, "-version"),
        capture_output=True,
        check=True,
        text=True,
        timeout=30,
    )
    return completed.stdout.splitlines()[0]


def _peak_rss_bytes(who: int) -> int | None:
    if resource is None:
        return None
    maximum = resource.getrusage(who).ru_maxrss
    # Darwin reports bytes; Linux and the other supported CI POSIX runners use KiB.
    return int(maximum if sys.platform == "darwin" else maximum * 1024)


def benchmark_live_player(
    source: Path,
    *,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    chunk_bytes: int = 1316,
    media_fragments: int = 12,
    metadata_samples: int = 512,
    program_number: int | None = None,
) -> LivePlayerBenchmark:
    """Process one complete TS file and return bounded live-gateway measurements."""

    source_display = str(source.expanduser())
    source = source.expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"input does not exist: {source}")
    if isinstance(chunk_bytes, bool) or not isinstance(chunk_bytes, int) or chunk_bytes < 188:
        raise ValueError("chunk_bytes must be an integer of at least 188")
    if isinstance(media_fragments, bool) or not isinstance(media_fragments, int):
        raise TypeError("media_fragments must be an integer")
    if media_fragments < 2:
        raise ValueError("media_fragments must be at least two")
    if isinstance(metadata_samples, bool) or not isinstance(metadata_samples, int):
        raise TypeError("metadata_samples must be an integer")
    if metadata_samples < 1:
        raise ValueError("metadata_samples must be positive")
    if program_number is not None and (
        isinstance(program_number, bool)
        or not isinstance(program_number, int)
        or not 1 <= program_number <= 0xFFFF
    ):
        raise ValueError("program_number must be an integer from 1 to 65535 or None")

    source_bytes = source.stat().st_size
    source_hash = _sha256(source)
    duration = _source_duration(source, ffprobe=ffprobe)
    ffmpeg_identity = _ffmpeg_version(ffmpeg)
    gateway = LivePlayerGateway(
        ffmpeg=ffmpeg,
        max_metadata_samples=metadata_samples,
        max_media_fragments=media_fragments,
        max_input_chunk_bytes=chunk_bytes,
        program_number=program_number,
    )
    input_chunks = 0
    completed_cleanly = False
    owns_tracing = not tracemalloc.is_tracing()
    if owns_tracing:
        tracemalloc.start()
    else:
        tracemalloc.reset_peak()
    started = time.perf_counter()
    try:
        gateway.start()
        with source.open("rb") as stream:
            while chunk := stream.read(chunk_bytes):
                gateway.feed(chunk)
                input_chunks += 1
        gateway.finish(timeout=60)
        completed_cleanly = True
    finally:
        if not completed_cleanly:
            gateway.close()
        elapsed = time.perf_counter() - started
        _current, traced_peak = tracemalloc.get_traced_memory()
        if owns_tracing:
            tracemalloc.stop()

    stats = gateway.stats
    retained_media = gateway.media.poll(after_id=-1, timeout=0)
    retained_metadata = gateway.metadata.poll(after_id=-1, timeout=0)
    return LivePlayerBenchmark(
        schema_version=1,
        source=source_display,
        source_bytes=source_bytes,
        source_sha256=source_hash,
        source_duration_seconds=duration,
        elapsed_seconds=elapsed,
        input_megabits_per_second=source_bytes * 8 / elapsed / 1_000_000,
        media_seconds_per_wall_second=(None if duration is None else duration / elapsed),
        input_chunks=input_chunks,
        input_chunk_bytes=chunk_bytes,
        metadata_samples=stats.metadata_samples,
        media_fragments=stats.media_fragments,
        retained_metadata_samples=len(retained_metadata.items),
        dropped_metadata_samples=retained_metadata.dropped,
        retained_media_fragments=len(retained_media.items),
        dropped_media_fragments=retained_media.dropped,
        retained_media_bytes=sum(len(fragment) for _item_id, fragment in retained_media.items),
        python_traced_peak_bytes=traced_peak,
        python_process_peak_rss_bytes=_peak_rss_bytes(resource.RUSAGE_SELF) if resource else None,
        child_process_peak_rss_bytes=(
            _peak_rss_bytes(resource.RUSAGE_CHILDREN) if resource else None
        ),
        python=platform.python_version(),
        platform=platform.platform(),
        ffmpeg=ffmpeg_identity,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the live-player benchmark and emit its versioned JSON result."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="complete MPEG-2 TS FMV input")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--chunk-bytes", type=int, default=1316)
    parser.add_argument("--media-fragments", type=int, default=12)
    parser.add_argument("--metadata-samples", type=int, default=512)
    parser.add_argument("--program-number", type=int)
    parser.add_argument("--output", type=Path, help="also write the JSON result to this path")
    parser.add_argument("--force", action="store_true", help="replace an existing output file")
    arguments = parser.parse_args(argv)
    output = arguments.output.expanduser() if arguments.output is not None else None
    if output is not None:
        if output.exists() and not output.is_file():
            parser.error(f"output is not a regular file: {output}")
        if output.exists() and not arguments.force:
            parser.error(f"output already exists: {output}; pass --force to replace it")
        if not output.parent.is_dir():
            parser.error(f"output directory does not exist: {output.parent}")
    try:
        result = benchmark_live_player(
            arguments.source,
            ffmpeg=arguments.ffmpeg,
            ffprobe=arguments.ffprobe,
            chunk_bytes=arguments.chunk_bytes,
            media_fragments=arguments.media_fragments,
            metadata_samples=arguments.metadata_samples,
            program_number=arguments.program_number,
        )
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        parser.error(str(error))
    document = json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n"
    if output is not None:
        mode = "w" if arguments.force else "x"
        try:
            with output.open(mode, encoding="utf-8") as stream:
                stream.write(document)
        except OSError as error:
            parser.error(str(error))
    print(document, end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
