from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from stanag4609.csvio import iter_esri_metadata_csv
from stanag4609.csvmux import CsvMuxResult
from stanag4609.player import demo as demo_module
from stanag4609.player.demo import (
    _metadata_csv,
    ffmpeg_demo_command,
    generate_demo_fmv,
)
from stanag4609.player.server import main as player_main


def test_demo_command_has_deterministic_open_synthetic_sources(tmp_path: Path) -> None:
    day = ffmpeg_demo_command(
        tmp_path / "day.mp4", variant="day", duration_seconds=12, ffmpeg="ffmpeg7"
    )
    thermal = ffmpeg_demo_command(tmp_path / "thermal.mp4", variant="thermal", duration_seconds=12)

    assert day[0] == "ffmpeg7"
    assert "testsrc2=size=640x360:rate=15" in day
    assert "testsrc2=size=640x360:rate=15,format=gray,negate" in thermal
    assert "libx264" in day
    assert "mp2" in day


def test_demo_metadata_is_ordered_authored_st0601_input() -> None:
    records = tuple(iter_esri_metadata_csv(_metadata_csv(duration_seconds=3, variant="day")))

    assert len(records) == 6
    assert [record.timestamp_microseconds for record in records] == sorted(
        record.timestamp_microseconds for record in records
    )
    assert records[0].values[65] == 19
    assert records[0].values[13] == pytest.approx(49.2827)
    assert records[-1].values[14] > records[0].values[14]


def test_demo_generation_connects_ffmpeg_to_csv_multiplexer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "demo.ts"
    calls: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **_kwargs: object) -> None:
        calls.append(command)

    def multiplex(source: Path, metadata: object, output: Path, **kwargs: object) -> CsvMuxResult:
        assert source.name == "day.mp4"
        assert len(tuple(iter_esri_metadata_csv(metadata))) == 8  # type: ignore[arg-type]
        assert output == destination.resolve()
        assert kwargs["chunk_size"] == 1_316
        assert kwargs["overwrite"] is False
        return CsvMuxResult(output, 8, 90_000, 90_000, 405_000)

    monkeypatch.setattr(demo_module.subprocess, "run", run)
    monkeypatch.setattr(demo_module, "multiplex_esri_fmv", multiplex)

    result = generate_demo_fmv(destination, duration_seconds=4)
    assert result.records_written == 8
    assert calls and calls[0][0] == "ffmpeg"


def test_demo_generation_reports_ffmpeg_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise subprocess.CalledProcessError(1, "ffmpeg", stderr="encoder failed")

    monkeypatch.setattr(demo_module.subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="encoder failed"):
        generate_demo_fmv(tmp_path / "demo.ts")


@pytest.mark.parametrize(
    "variant,duration",
    [("unknown", 12), ("day", 1), ("day", 601), ("day", float("nan"))],
)
def test_demo_command_rejects_unsupported_or_unbounded_input(
    tmp_path: Path, variant: str, duration: float
) -> None:
    with pytest.raises(ValueError):
        ffmpeg_demo_command(tmp_path / "demo.mp4", variant=variant, duration_seconds=duration)


def test_player_requires_exactly_one_source_or_demo() -> None:
    with pytest.raises(SystemExit, match="exactly one"):
        player_main(["--no-open"])
    with pytest.raises(SystemExit, match="exactly one"):
        player_main(["mission.ts", "--demo", "day", "--no-open"])
