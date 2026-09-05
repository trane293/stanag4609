from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
API_PAGES = {
    "index.md",
    "klv.md",
    "media.md",
    "player.md",
    "sidecar.md",
    "st0601.md",
    "st0902.md",
    "st0903.md",
    "transport.md",
    "verifier.md",
}
SCREENSHOT_ASSETS = {
    "ai-sidecar-player.jpg",
    "fmv-operations-dashboard.jpg",
    "fmv-verifier-report.jpg",
}
BENCHMARK_ASSETS = {
    "live-player-day-flight.json",
    "live-player-esri-truck.json",
    "live-player-night-flight-ir.json",
}


def test_every_api_reference_page_is_in_navigation() -> None:
    navigation = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")

    for page in API_PAGES:
        assert f"api/{page}" in navigation


def test_every_api_reference_target_imports() -> None:
    targets: set[str] = set()
    for page in API_PAGES - {"index.md"}:
        contents = (ROOT / "docs" / "api" / page).read_text(encoding="utf-8")
        targets.update(re.findall(r"^:::\s+([\w.]+)\s*$", contents, re.MULTILINE))

    assert targets
    for target in targets:
        assert importlib.import_module(target).__name__ == target


def test_docs_extra_installs_the_api_generator() -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert '"mkdocstrings[python]>=1.0,<2"' in project


def test_tutorial_screenshot_assets_are_real_jpeg_files() -> None:
    screenshot_directory = ROOT / "docs" / "assets" / "screenshots"

    for name in SCREENSHOT_ASSETS:
        screenshot = screenshot_directory / name
        contents = screenshot.read_bytes()
        assert contents.startswith(b"\xff\xd8\xff")
        assert contents.endswith(b"\xff\xd9")
        assert len(contents) > 10_000


def test_tutorials_reference_every_screenshot_asset() -> None:
    tutorials = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "docs" / "tutorials").glob("*.md")
    )

    for name in SCREENSHOT_ASSETS:
        assert f"../assets/screenshots/{name}" in tutorials


def test_live_benchmark_evidence_matches_pinned_fixture_identities_and_bounds() -> None:
    fixtures = json.loads((ROOT / "references" / "fixtures.json").read_text())["fixtures"]
    fixtures_by_hash = {fixture["sha256"]: fixture for fixture in fixtures}
    benchmark_directory = ROOT / "docs" / "assets" / "benchmarks"

    assert {path.name for path in benchmark_directory.glob("*.json")} == BENCHMARK_ASSETS
    for path in benchmark_directory.glob("*.json"):
        result = json.loads(path.read_text())
        fixture = fixtures_by_hash[result["source_sha256"]]
        expected_metadata = fixture.get(
            "expected_player_timeline_samples", fixture["expected_uas_packets"]
        )
        assert result["schema_version"] == 1
        assert result["source_bytes"] == fixture["size"]
        assert result["metadata_samples"] == expected_metadata
        assert result["retained_metadata_samples"] + result["dropped_metadata_samples"] == result[
            "metadata_samples"
        ]
        assert result["retained_media_fragments"] + result["dropped_media_fragments"] == result[
            "media_fragments"
        ]
        assert result["retained_media_fragments"] == 12
        assert result["media_seconds_per_wall_second"] > 1
