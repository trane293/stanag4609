from __future__ import annotations

import asyncio
import runpy
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from stanag4609.player import scan_transport_file
from stanag4609.sidecar import FrameEnvelope

ROOT = Path(__file__).parents[2]


def test_quickstart_generated_stream_matches_documented_timeline(tmp_path: Path) -> None:
    command = Path(sys.executable).with_name("stanag4609-demo-samples")
    if shutil.which("ffmpeg") is None:
        pytest.skip("the documented generated-stream workflow requires FFmpeg")
    if not command.is_file():
        raise RuntimeError("documentation checks require installed console entry points")

    result = subprocess.run(
        (str(command), str(tmp_path / "demo"), "--duration", "4"),
        check=True,
        capture_output=True,
        text=True,
    )
    assert "stanag4609-day-demo.ts: 8 synchronized ST 0601 packets" in result.stdout
    assert "stanag4609-thermal-demo.ts: 8 synchronized ST 0601 packets" in result.stdout

    timeline = scan_transport_file(tmp_path / "demo" / "stanag4609-day-demo.ts")
    assert len(timeline.samples) == 8
    sample = timeline.samples[0]
    assert f"{sample.time_seconds:.3f}" == "0.010"
    assert f"{sample.fields['Sensor Latitude']['value']:.5f}" == "49.28270"
    assert f"{sample.fields['Sensor Longitude']['value']:.5f}" == "-123.12070"


def test_ai_to_vmti_tutorial_runs_end_to_end(capsys: Any) -> None:
    namespace = runpy.run_path(str(ROOT / "examples/tutorials/ai_to_vmti.py"))

    asyncio.run(cast(Any, namespace["main"])())

    output = capsys.readouterr().out
    assert output == "PID=0x0120 PTS=900000 targets=[101, 202] KLV_bytes=429\n"


def test_ai_sidecar_ui_exposes_reproducible_player_command(
    capsys: Any, monkeypatch: Any
) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "examples/tutorials"))
    namespace = runpy.run_path(str(ROOT / "examples/tutorials/ai_sidecar_ui.py"))

    parser = namespace["_parser"]()
    args = parser.parse_args(["fixture.ts"])

    assert args.source == Path("fixture.ts")
    assert args.output == Path("work/ai-sidecar-ui")
    assert args.host == "127.0.0.1"
    assert args.port == 8767
    assert "Run real YOLO vehicle inference" in parser.format_help()
    assert capsys.readouterr().err == ""


def test_ai_sidecar_ui_samples_first_party_pyav_frames(monkeypatch: Any) -> None:
    namespace = runpy.run_path(str(ROOT / "examples/tutorials/ai_sidecar_ui.py"))
    frames = (
        FrameEnvelope(0, 9_000, 640, 480, "first"),
        FrameEnvelope(1, 13_500, 640, 480, "second"),
        FrameEnvelope(2, 18_000, 640, 480, "third"),
    )

    def frame_source(_media: Path) -> tuple[FrameEnvelope, ...]:
        return frames

    monkeypatch.setitem(namespace["_sampled_frames"].__globals__, "PyAVFrameSource", frame_source)
    stamp = datetime(2026, 1, 2, tzinfo=timezone.utc).isoformat()
    samples = [
        {"time_seconds": 0.0, "pts": 90_000, "fields": {"Precision Time Stamp": {"value": stamp}}},
        {"time_seconds": 0.08, "pts": 97_200, "fields": {"Precision Time Stamp": {"value": stamp}}},
    ]

    sampled = list(namespace["_sampled_frames"](Path("media.mp4"), samples))

    assert [index for index, _frame in sampled] == [0, 1]
    assert [frame.pixels for _index, frame in sampled] == ["first", "third"]
    assert [frame.pts for _index, frame in sampled] == [90_000, 97_200]
    assert sampled[0][1].timestamp_microseconds == 1_767_312_000_000_000


def test_ai_sidecar_ui_extracts_image_ordered_frame_corners() -> None:
    namespace = runpy.run_path(str(ROOT / "examples/tutorials/ai_sidecar_ui.py"))
    sample = {
        "geospatial": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [-104.1, 41.1],
                            [-104.0, 41.1],
                            [-104.0, 41.0],
                            [-104.1, 41.0],
                            [-104.1, 41.1],
                        ]
                    ],
                },
                "properties": {"role": "frame_footprint"},
            }
        ]
    }

    assert namespace["_frame_corners"](sample) == (
        (-104.1, 41.1),
        (-104.0, 41.1),
        (-104.0, 41.0),
        (-104.1, 41.0),
    )


def test_web_dashboard_uses_player_asset_contract() -> None:
    page = (ROOT / "examples/web_dashboard/index.html").read_text()

    assert 'src="media.mp4"' in page
    assert "fetch('timeline.json')" in page
    assert 'id="map"' in page
    assert 'id="telemetry"' in page
    assert 'id="feed"' in page


def test_web_dashboard_prints_the_documented_url(
    capsys: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    namespace = runpy.run_path(str(ROOT / "examples/web_dashboard/server.py"))

    class TestServer:
        server_port = 8766

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args: Any) -> None:
            pass

        def serve_forever(self) -> None:
            pass

    assets = SimpleNamespace(root=tmp_path)
    monkeypatch.setitem(
        namespace["main"].__globals__,
        "prepare_player_assets",
        lambda _source, _temporary: assets,
    )
    monkeypatch.setitem(namespace["main"].__globals__, "ThreadingHTTPServer", TestServer)

    assert namespace["main"](["fixture.ts", "--no-open"]) == 0
    assert capsys.readouterr().out == "FMV dashboard: http://127.0.0.1:8766/\n"
