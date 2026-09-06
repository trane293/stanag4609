"""Generate small, redistributable FMV demonstrations from synthetic sources."""

from __future__ import annotations

import csv
import math
import subprocess
import tempfile
from io import StringIO
from pathlib import Path

from stanag4609.csvio import ESRI_COLUMN_TAGS
from stanag4609.csvmux import CsvMuxResult, multiplex_esri_fmv

DEMO_VARIANTS = ("day", "thermal")


def ffmpeg_demo_command(
    destination: Path,
    *,
    variant: str,
    duration_seconds: float,
    ffmpeg: str = "ffmpeg",
) -> tuple[str, ...]:
    """Return the deterministic audio/video command for one synthetic demo."""

    if variant not in DEMO_VARIANTS:
        raise ValueError(f"variant must be one of {', '.join(DEMO_VARIANTS)}")
    if (
        isinstance(duration_seconds, bool)
        or not isinstance(duration_seconds, (int, float))
        or not math.isfinite(duration_seconds)
        or not 2 <= duration_seconds <= 600
    ):
        raise ValueError("duration_seconds must be a finite number from 2 to 600")
    video_filter = "testsrc2=size=640x360:rate=15"
    if variant == "thermal":
        video_filter += ",format=gray,negate"
    return (
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        video_filter,
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000",
        "-t",
        f"{float(duration_seconds):.6f}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-g",
        "15",
        "-c:a",
        "mp2",
        "-b:a",
        "128k",
        "-shortest",
        "-y",
        str(destination),
    )


def _metadata_csv(*, duration_seconds: float, variant: str) -> StringIO:
    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=tuple(ESRI_COLUMN_TAGS))
    writer.writeheader()
    start = 1_700_000_000_000_000 + (10_000_000 if variant == "thermal" else 0)
    sample_count = max(2, math.floor(duration_seconds * 2))
    for index in range(sample_count):
        progress = index / max(1, sample_count - 1)
        writer.writerow(
            {
                "TimeStamp": start + index * 500_000,
                "LDSVer": 19,
                "PlatformHeading": 35 + progress * 20,
                "PlatformPitch": -1.5 + progress,
                "PlatformRoll": math.sin(progress * math.pi * 2) * 2,
                "PlatformTrueAirSpeed": 75,
                "SensorLatitude": 49.2827 + progress * 0.015,
                "SensorLongitude": -123.1207 + progress * 0.025,
                "SensorAltitude": 1200 + progress * 50,
                "HorizontalFOV": 22,
                "VerticalFOV": 14,
                "SensorRelativeAzimuth": 8 + progress * 4,
                "SensorRelativeElevation": -35,
                "SensorRelativeRoll": 0,
            }
        )
    stream.seek(0)
    return stream


def generate_demo_fmv(
    destination: str | Path,
    *,
    variant: str = "day",
    duration_seconds: float = 12,
    ffmpeg: str = "ffmpeg",
    overwrite: bool = False,
) -> CsvMuxResult:
    """Generate H.264/AAC MPEG-TS with synchronous authored ST 0601 metadata."""

    destination = Path(destination).expanduser().resolve()
    if destination.exists() and not overwrite:
        raise FileExistsError(destination)
    command_destination: Path
    with tempfile.TemporaryDirectory(prefix="stanag4609-demo-") as temporary:
        command_destination = Path(temporary) / f"{variant}.mp4"
        try:
            subprocess.run(
                ffmpeg_demo_command(
                    command_destination,
                    variant=variant,
                    duration_seconds=duration_seconds,
                    ffmpeg=ffmpeg,
                ),
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as error:
            raise RuntimeError(f"FFmpeg executable not found: {ffmpeg}") from error
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or "").strip().splitlines()
            suffix = f": {detail[-1]}" if detail else ""
            raise RuntimeError(f"FFmpeg could not generate {variant} demo{suffix}") from error
        return multiplex_esri_fmv(
            command_destination,
            _metadata_csv(duration_seconds=duration_seconds, variant=variant),
            destination,
            ffmpeg=ffmpeg,
            chunk_size=1_316,
            overwrite=overwrite,
        )
