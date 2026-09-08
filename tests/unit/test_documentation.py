from __future__ import annotations

import ast
import importlib
import importlib.metadata
import json
import re
import subprocess
import sys
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
    "transformations.md",
    "transport.md",
    "verifier.md",
}
SCREENSHOT_ASSETS = {
    "ai-sidecar-player.jpg",
    "fmv-operations-dashboard.jpg",
    "fmv-verifier-report.jpg",
    "reference-player.jpg",
}
BENCHMARK_ASSETS = {
    "live-player-day-flight.json",
    "live-player-esri-truck.json",
    "live-player-night-flight-ir.json",
}
SOAK_ASSETS = {"live-player-soak-esri-truck.json"}
MARKDOWN_PATHS = (ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md")))
NEWCOMER_PAGES = (
    ROOT / "README.md",
    ROOT / "docs" / "index.md",
    ROOT / "docs" / "INSTALLATION.md",
    ROOT / "docs" / "QUICKSTART.md",
    ROOT / "docs" / "WORKFLOWS.md",
)
TUTORIAL_PAGES = tuple(sorted((ROOT / "docs" / "tutorials").glob("*.md")))


def _fenced_blocks(language_pattern: str) -> list[tuple[Path, int, str]]:
    pattern = re.compile(
        rf"^```(?:{language_pattern})\s*\n(.*?)^```\s*$",
        re.MULTILINE | re.DOTALL,
    )
    blocks: list[tuple[Path, int, str]] = []
    for path in MARKDOWN_PATHS:
        contents = path.read_text(encoding="utf-8")
        for match in pattern.finditer(contents):
            blocks.append((path, contents.count("\n", 0, match.start()) + 1, match.group(1)))
    return blocks


def _prose_word_count(path: Path) -> int:
    contents = path.read_text(encoding="utf-8")
    without_fences = re.sub(r"```.*?```", "", contents, flags=re.DOTALL)
    return len(re.findall(r"\b[\w'-]+\b", without_fences))


def test_documentation_separates_learning_reference_and_project_material() -> None:
    navigation = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    expected_pages = {
        "Installation": "INSTALLATION.md",
        "Choose a workflow": "WORKFLOWS.md",
        "Examples": "EXAMPLES.md",
        "Common pitfalls": "COMMON_PITFALLS.md",
        "Glossary": "GLOSSARY.md",
        "User guide overview": "USER_GUIDE.md",
        "Writing style": "STYLE_GUIDE.md",
        "User-story audit": "USER_STORY_AUDIT.md",
    }

    for title, page in expected_pages.items():
        assert (ROOT / "docs" / page).is_file(), page
        assert f"{title}: {page}" in navigation


def test_newcomer_pages_are_concise_and_avoid_audit_voice() -> None:
    assert _prose_word_count(ROOT / "README.md") <= 1_600
    assert _prose_word_count(ROOT / "docs" / "index.md") <= 900
    assert _prose_word_count(ROOT / "docs" / "QUICKSTART.md") <= 900

    avoided_phrases = (
        "actual output",
        "not a claim",
        "does not pretend",
        "proof of",
        "proven release",
    )
    for path in NEWCOMER_PAGES:
        contents = path.read_text(encoding="utf-8").lower()
        for phrase in avoided_phrases:
            assert phrase not in contents, f"{path.name}: {phrase}"


def test_quickstart_has_a_complete_testable_transport_workflow() -> None:
    quickstart = (ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")

    assert "stanag4609-demo-samples demo --duration 4" in quickstart
    assert 'scan_transport_file("demo/stanag4609-day-demo.ts")' in quickstart
    assert "stanag4609-player demo/stanag4609-day-demo.ts" in quickstart
    for placeholder in (
        "for network_chunk in source",
        "frame_misp_microseconds",
        "deployment_facts",
        "demultiplexed_metadata",
    ):
        assert placeholder not in quickstart


def test_tutorials_read_as_tasks_instead_of_release_evidence() -> None:
    avoided_phrases = (
        "actual output",
        "not a broken tutorial",
        "prove the generated",
        "regression guarantee",
        "regression observation",
    )

    for path in TUTORIAL_PAGES:
        contents = path.read_text(encoding="utf-8").lower()
        for phrase in avoided_phrases:
            assert phrase not in contents, f"{path.name}: {phrase}"


def test_each_tutorial_points_to_a_runnable_entry_path() -> None:
    expected_entries = {
        "inspect_fmv.md": "stanag4609-verify",
        "create_fmv.md": "stanag4609-mux-esri",
        "ai_to_vmti.md": "python examples/tutorials/ai_to_vmti.py",
        "web_dashboard.md": "python examples/web_dashboard/server.py",
        "live_fanout.md": "LiveTransportTransformer",
    }

    assert {path.name for path in TUTORIAL_PAGES} == expected_entries.keys()
    for path in TUTORIAL_PAGES:
        assert expected_entries[path.name] in path.read_text(encoding="utf-8")


def test_task_guides_keep_assurance_detail_in_reference_pages() -> None:
    navigation = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    expected_reference_pages = {
        "Verifier checks": "VERIFIER_CHECKS.md",
        "Player runtime": "PLAYER_RUNTIME.md",
    }
    for title, page in expected_reference_pages.items():
        assert (ROOT / "docs" / page).is_file(), page
        assert f"{title}: {page}" in navigation

    limits = {
        "VERIFIER.md": 1_800,
        "PLAYER.md": 1_600,
        "AI_SIDECARS.md": 2_000,
        "LIVE_PIPELINE.md": 1_800,
    }
    for page, maximum in limits.items():
        assert _prose_word_count(ROOT / "docs" / page) <= maximum, page


def test_public_getting_started_pages_install_the_pypi_release() -> None:
    pages = (
        ROOT / "README.md",
        ROOT / "docs" / "index.md",
        ROOT / "docs" / "QUICKSTART.md",
        ROOT / "docs" / "tutorials" / "inspect_fmv.md",
    )

    for path in pages:
        contents = path.read_text(encoding="utf-8")
        assert "python -m pip install stanag4609" in contents, path

    for path in pages[1:]:
        contents = path.read_text(encoding="utf-8")
        assert "pip install -e" not in contents, path
        assert "git clone git@github.com:trane293/stanag4609.git" not in contents, path


def test_every_python_documentation_snippet_compiles_and_imports_public_symbols() -> None:
    blocks = _fenced_blocks("python|py")

    # Guard the fence-discovery regex while allowing the guides to replace
    # duplicated fragments with links to one canonical example.
    assert len(blocks) >= 60
    for path, line, snippet in blocks:
        filename = f"{path.relative_to(ROOT)}:{line}"
        tree = compile(
            snippet,
            filename,
            "exec",
            flags=ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
        )
        assert isinstance(tree, ast.Module)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "stanag4609" or alias.name.startswith("stanag4609."):
                        importlib.import_module(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module and (
                node.module == "stanag4609" or node.module.startswith("stanag4609.")
            ):
                module = importlib.import_module(node.module)
                for alias in node.names:
                    if alias.name != "*":
                        qualified_name = f"{node.module}.{alias.name}"
                        assert hasattr(module, alias.name), f"{filename}: {qualified_name}"


def test_every_console_documentation_snippet_has_valid_shell_syntax() -> None:
    blocks = _fenced_blocks("console|bash|sh|shell")

    assert len(blocks) >= 70
    for path, line, snippet in blocks:
        result = subprocess.run(
            ("bash", "-n"),
            input=snippet,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, f"{path.relative_to(ROOT)}:{line}: {result.stderr}"


def test_every_documented_cli_is_an_installed_entry_point() -> None:
    documented = {
        match.group(1)
        for _path, _line, snippet in _fenced_blocks("console|bash|sh|shell")
        for match in re.finditer(r"(?m)^\s*(stanag4609-[a-z0-9-]+)\b", snippet)
    }
    installed = {
        entry_point.name: entry_point
        for entry_point in importlib.metadata.entry_points(group="console_scripts")
        if entry_point.name.startswith("stanag4609-")
    }

    assert documented
    assert documented <= installed.keys()
    for name in documented:
        assert callable(installed[name].load()), name
        command = Path(sys.executable).with_name(name)
        assert command.is_file(), name
        result = subprocess.run(
            (str(command), "--help"),
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{name}: {result.stderr}"
        assert "usage:" in result.stdout.lower(), name


def test_every_documented_package_extra_exists() -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    optional_dependencies = project.split("[project.optional-dependencies]", 1)[1].split(
        "\n[", 1
    )[0]
    available = set(re.findall(r"(?m)^([a-z0-9-]+)\s*=\s*\[", optional_dependencies))
    documented = {
        extra.strip()
        for path in MARKDOWN_PATHS
        for group in re.findall(r"stanag4609\[([^]]+)\]", path.read_text(encoding="utf-8"))
        for extra in group.split(",")
    }

    assert documented
    assert documented <= available


def test_every_documented_repository_python_script_exists() -> None:
    references = {
        match.group(1)
        for _path, _line, snippet in _fenced_blocks("console|bash|sh|shell")
        for match in re.finditer(r"(?m)^\s*python\s+((?:examples|scripts)/[^\s\\]+\.py)\b", snippet)
    }

    assert references
    for reference in references:
        assert (ROOT / reference).is_file(), reference


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
    assert {path.name for path in screenshot_directory.glob("*.jpg")} == SCREENSHOT_ASSETS

    for name in SCREENSHOT_ASSETS:
        screenshot = screenshot_directory / name
        contents = screenshot.read_bytes()
        assert contents.startswith(b"\xff\xd8\xff")
        assert contents.endswith(b"\xff\xd9")
        assert len(contents) > 10_000


def test_real_fixture_tutorial_excerpts_match_current_audit() -> None:
    inspect = (ROOT / "docs" / "tutorials" / "inspect_fmv.md").read_text()
    create = (ROOT / "docs" / "tutorials" / "create_fmv.md").read_text()

    assert "Summary: 33 error(s), 4 warning(s), 14 passed, 1 not applicable" in inspect
    assert "Summary: 1 error(s), 0 warning(s), 26 passed, 2 not applicable" in create
    assert "st0604.timestamp.missing" in create
    assert "Result: PASS" not in create


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
