from __future__ import annotations

import asyncio
import runpy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from stanag4609.sidecar import FrameEnvelope

ROOT = Path(__file__).parents[2]


def test_ai_to_vmti_tutorial_runs_end_to_end(capsys: Any) -> None:
    namespace = runpy.run_path(str(ROOT / "examples/tutorials/ai_to_vmti.py"))

    asyncio.run(cast(Any, namespace["main"])())

    output = capsys.readouterr().out
    assert "PID=0x0120 PTS=900000" in output
    assert "targets=[101, 202]" in output
    assert "KLV_bytes=" in output


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
