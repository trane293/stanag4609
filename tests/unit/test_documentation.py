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
SOAK_ASSETS = {"live-player-soak-esri-truck.json"}


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


def test_architecture_guide_maps_real_modules_and_renderable_diagrams() -> None:
    guide = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    navigation = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")

    assert "System architecture: ARCHITECTURE.md" in navigation
    assert guide.count("```mermaid") == 5
    for module in (
        "transport.demux",
        "transport.mux",
        "transport.transformer",
        "transport.udp",
        "sidecar.pipeline",
        "sidecar.vmti",
        "player.live",
        "player.udp_output",
    ):
        assert module in guide
    initializer = (ROOT / "docs" / "javascripts" / "mermaid.mjs").read_text(
        encoding="utf-8"
    )
    assert "mermaid@11.4.1/+esm" in initializer
    assert "initialize({ startOnLoad: false })" in initializer
    assert "await mermaid.render(" in initializer


def test_landing_pages_publish_the_same_three_state_standards_matrix() -> None:
    def support_rows(path: Path) -> tuple[str, ...]:
        contents = path.read_text(encoding="utf-8")
        section = contents.split("## Standards support at a glance\n", 1)[1]
        return tuple(
            line for line in section.split("\n## ", 1)[0].splitlines() if line.startswith("| **")
        )

    readme_rows = support_rows(ROOT / "README.md")
    docs_rows = support_rows(ROOT / "docs" / "index.md")

    assert readme_rows == docs_rows
    assert len(readme_rows) == 3
    assert "ST 0902.8 Minimum Metadata" in readme_rows[0]
    assert "ST 0601.19" in readme_rows[1]
    assert "ST 0801 / ST 1107" in readme_rows[2]


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
        path.read_text(encoding="utf-8") for path in (ROOT / "docs" / "tutorials").glob("*.md")
    )

    for name in SCREENSHOT_ASSETS:
        assert f"../assets/screenshots/{name}" in tutorials


def test_live_benchmark_evidence_matches_pinned_fixture_identities_and_bounds() -> None:
    fixtures = json.loads((ROOT / "references" / "fixtures.json").read_text())["fixtures"]
    fixtures_by_hash = {fixture["sha256"]: fixture for fixture in fixtures}
    benchmark_directory = ROOT / "docs" / "assets" / "benchmarks"

    assert {path.name for path in benchmark_directory.glob("*.json")} == (
        BENCHMARK_ASSETS | SOAK_ASSETS
    )
    for name in BENCHMARK_ASSETS:
        path = benchmark_directory / name
        result = json.loads(path.read_text())
        fixture = fixtures_by_hash[result["source_sha256"]]
        expected_metadata = fixture.get(
            "expected_player_timeline_samples", fixture["expected_uas_packets"]
        )
        assert result["schema_version"] == 1
        assert result["source_bytes"] == fixture["size"]
        assert result["metadata_samples"] == expected_metadata
        assert (
            result["retained_metadata_samples"] + result["dropped_metadata_samples"]
            == result["metadata_samples"]
        )
        assert (
            result["retained_media_fragments"] + result["dropped_media_fragments"]
            == result["media_fragments"]
        )
        assert result["retained_media_fragments"] == 12
        assert result["media_seconds_per_wall_second"] > 1


def test_live_soak_evidence_proves_isolated_repeatable_epochs() -> None:
    fixtures = json.loads((ROOT / "references" / "fixtures.json").read_text())["fixtures"]
    fixtures_by_hash = {fixture["sha256"]: fixture for fixture in fixtures}
    benchmark_directory = ROOT / "docs" / "assets" / "benchmarks"

    for name in SOAK_ASSETS:
        result = json.loads((benchmark_directory / name).read_text())
        fixture = fixtures_by_hash[result["source_sha256"]]
        expected_metadata = fixture["expected_player_timeline_samples"]
        epochs = result["epochs"]

        assert result["schema_version"] == 1
        assert result["passed"]
        assert result["source_bytes"] == fixture["size"]
        assert result["requested_epochs"] == result["completed_epochs"] == len(epochs)
        assert len(epochs) >= 2
        assert [epoch["epoch"] for epoch in epochs] == list(range(1, len(epochs) + 1))
        assert all(epoch["passed"] and epoch["error"] is None for epoch in epochs)
        assert all(epoch["input_bytes"] == fixture["size"] for epoch in epochs)
        assert all(epoch["metadata_samples"] == expected_metadata for epoch in epochs)
        assert len({epoch["media_fragments"] for epoch in epochs}) == 1
        assert all(
            epoch["retained_metadata_samples"] + epoch["dropped_metadata_samples"]
            == epoch["metadata_samples"]
            for epoch in epochs
        )
        assert all(
            epoch["retained_media_fragments"] + epoch["dropped_media_fragments"]
            == epoch["media_fragments"]
            for epoch in epochs
        )
        assert result["total_input_bytes"] == sum(epoch["input_bytes"] for epoch in epochs)
        assert result["total_input_chunks"] == sum(epoch["input_chunks"] for epoch in epochs)
        assert result["total_metadata_samples"] == sum(
            epoch["metadata_samples"] for epoch in epochs
        )
        assert result["total_media_fragments"] == sum(epoch["media_fragments"] for epoch in epochs)
        assert result["maximum_pacing_lag_seconds"] == max(
            epoch["maximum_pacing_lag_seconds"] for epoch in epochs
        )
