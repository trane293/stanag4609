from __future__ import annotations

import asyncio
import runpy
from pathlib import Path
from typing import Any, cast

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


def test_web_dashboard_uses_player_asset_contract() -> None:
    page = (ROOT / "examples/web_dashboard/index.html").read_text()

    assert 'src="media.mp4"' in page
    assert "fetch('timeline.json')" in page
    assert 'id="map"' in page
    assert 'id="telemetry"' in page
    assert 'id="feed"' in page
