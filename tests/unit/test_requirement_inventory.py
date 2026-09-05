from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from stanag4609 import MISMMSValidator

ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "references" / "requirements.json"
STANDARDS_MANIFEST = ROOT / "references" / "standards" / "manifest.json"

ST0601_ACTIVE = {
    "ST 0601.8-03",
    "ST 0601.8-08",
    "ST 0601.8-09",
    "ST 0601.8-10",
    "ST 0601.8-11",
    "ST 0601.8-12",
    "ST 0601.8-14",
    "ST 0601.8-16",
    "ST 0601.8-17",
    "ST 0601.8-19",
    "ST 0601.9-20",
    "ST 0601.9-21",
    "ST 0601.10-22",
    "ST 0601.13-23",
    "ST 0601.13-24",
    "ST 0601.13-27",
    "ST 0601.13-28",
    "ST 0601.13-29",
    "ST 0601.13-30",
    "ST 0601.14-31",
    "ST 0601.14-35",
    "ST 0601.15-36",
    "ST 0601.17-37",
    "ST 0601.17-38",
    "ST 0601.17-39",
    "ST 0601.17-40",
    "ST 0601.19-41",
    "ST 0601.19-42",
    "ST 0601.19-43",
    "ST 0601.19-44",
    "ST 0601.19-45",
    "ST 0601.19-46",
    "ST 0601.19-47",
}
ST0601_RETIRED = {
    "ST 0601.8-01",
    "ST 0601.8-02",
    "ST 0601.8-04",
    "ST 0601.8-05",
    "ST 0601.8-06",
    "ST 0601.8-07",
    "ST 0601.8-13",
    "ST 0601.8-15",
    "ST 0601.8-18",
    "ST 0601.13-25",
    "ST 0601.13-26",
    "ST 0601.14-32",
    "ST 0601.14-33",
    "ST 0601.14-34",
}
ST0902_ACTIVE = {
    "ST 0902.3-01",
    "ST 0902.3-03",
    "ST 0902.3-04",
    "ST 0902.8-05",
}


def _requirement_ids(value: str) -> set[str]:
    return set(value.split())


MISP_2019_1_ACTIVE = _requirement_ids(
    """
    MISP-2015.1-01 MISP-2015.1-02 MISP-2018.3-116 MISP-2018.1-97
    MISP-2018.1-98 MISP-2015.1-05 MISP-2015.1-06 MISP-2015.1-07
    MISP-2015.1-08 MISP-2015.1-09 MISP-2015.1-10 MISP-2015.1-11
    MISP-2015.1-12 MISP-2015.1-13 MISP-2015.1-14 MISP-2015.1-15
    MISP-2015.1-16 MISP-2015.1-17 MISP-2015.1-18 MISP-2015.1-19
    MISP-2015.1-20 MISP-2015.1-21 MISP-2015.1-22 MISP-2018.1-99
    MISP-2018.2-113 MISP-2015.1-32 MISP-2018.2-114 MISP-2015.1-34
    MISP-2018.2-115 MISP-2015.1-37 MISP-2015.1-38 MISP-2015.1-39
    MISP-2017.1-94 MISP-2015.1-41 MISP-2018.1-102 MISP-2018.1-103
    MISP-2015.1-42 MISP-2018.1-104 MISP-2015.1-45 MISP-2015.1-46
    MISP-2015.1-47 MISP-2015.1-48 MISP-2015.1-49 MISP-2015.1-50
    MISP-2015.1-51 MISP-2018.3-117 MISP-2015.1-53 MISP-2018.3-118
    MISP-2015.1-54 MISP-2015.1-55 MISP-2015.1-56 MISP-2018.3-119
    MISP-2018.1-107 MISP-2016.1-85 MISP-2018.1-108 MISP-2016.1-88
    MISP-2016.1-89 MISP-2016.1-90 MISP-2016.1-91 MISP-2015.1-57
    MISP-2018.1-109 MISP-2018.1-110 MISP-2018.1-111 MISP-2018.1-112
    MISP-2015.1-62 MISP-2015.1-63 MISP-2015.1-64 MISP-2015.1-65
    MISP-2015.1-66 MISP-2015.1-67 MISP-2016.1-92 MISP-2015.1-68
    MISP-2015.1-69 MISP-2015.1-70 MISP-2015.1-71 MISP-2015.1-72
    MISP-2015.1-73 MISP-2015.1-74 MISP-2015.1-75 MISP-2017.1-95
    MISP-2015.1-76 MISP-2015.1-77 MISP-2015.1-78 MISP-2015.1-79
    MISP-2015.1-80 MISP-2015.1-81
    """
)
MISP_2019_1_DEPRECATED = _requirement_ids(
    """
    MISP-2015.1-82 MISP-2015.1-83 MISP-2015.1-84 MISP-2015.1-40
    MISP-2015.1-33 MISP-2015.3-85 MISP-2015.1-03 MISP-2015.1-04
    MISP-2015.1-43 MISP-2015.1-44 MISP-2016.1-86 MISP-2016.1-87
    MISP-2015.1-58 MISP-2015.1-59 MISP-2015.1-60 MISP-2015.1-61
    MISP-2018.1-100 MISP-2017.1-93 MISP-2018.1-101 MISP-2015.1-35
    MISP-2015.1-36 MISP-2015.1-23 MISP-2015.1-24 MISP-2015.1-25
    MISP-2015.1-26 MISP-2015.1-27 MISP-2015.1-28 MISP-2015.1-29
    MISP-2015.1-30 MISP-2015.1-31 MISP-2015.1-52 MISP-2018.1-96
    MISP-2018.1-105 MISP-2018.1-106
    """
)


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _documents() -> dict[str, dict[str, Any]]:
    return {entry["id"]: entry for entry in _load(INVENTORY)["documents"]}


def test_requirement_inventory_names_the_exact_normative_populations() -> None:
    inventory = _load(INVENTORY)
    assert inventory["schema_version"] == 1
    documents = _documents()
    assert set(documents) == {
        "MISB-ST-0601.19",
        "MISB-ST-0902.8",
        "MISB-MISP-2019.1",
    }

    st0601 = documents["MISB-ST-0601.19"]
    assert set(st0601["active_requirements"]) == ST0601_ACTIVE
    assert len(st0601["active_requirements"]) == len(ST0601_ACTIVE) == 33
    assert {
        entry["id"] for entry in st0601["inactive_requirements"]
    } == ST0601_RETIRED
    assert {
        entry["status"] for entry in st0601["inactive_requirements"]
    } == {"retired"}

    st0902 = documents["MISB-ST-0902.8"]
    assert set(st0902["active_requirements"]) == ST0902_ACTIVE
    assert st0902["inactive_requirements"] == [
        {"id": "ST 0902.3-02", "status": "deprecated"}
    ]

    misp = documents["MISB-MISP-2019.1"]
    assert set(misp["active_requirements"]) == MISP_2019_1_ACTIVE
    assert len(misp["active_requirements"]) == len(MISP_2019_1_ACTIVE) == 86
    assert {
        entry["id"] for entry in misp["inactive_requirements"]
    } == MISP_2019_1_DEPRECATED
    assert len(misp["inactive_requirements"]) == len(MISP_2019_1_DEPRECATED) == 34
    assert {
        entry["status"] for entry in misp["inactive_requirements"]
    } == {"deprecated"}


def test_every_active_requirement_has_human_readable_trace_evidence() -> None:
    for document in _documents().values():
        trace = (ROOT / document["trace"]).read_text(encoding="utf-8")
        for requirement in document["active_requirements"]:
            assert requirement in trace, f"{requirement} has no trace evidence"


def test_requirement_inventory_is_bound_to_acquired_source_digests() -> None:
    standards = {
        entry["id"]: entry
        for entry in _load(STANDARDS_MANIFEST)["documents"]
    }
    for document in _documents().values():
        source = standards[document["id"]]
        assert document["source_sha256"] == source["sha256"]
        local_source = ROOT / "references" / "standards" / source["local_file"]
        if local_source.exists():
            assert hashlib.sha256(local_source.read_bytes()).hexdigest() == source["sha256"]


def test_all_locally_acquired_standards_match_unique_manifest_entries() -> None:
    documents = _load(STANDARDS_MANIFEST)["documents"]
    identifiers = [document["id"] for document in documents]

    assert len(identifiers) == len(set(identifiers))
    for document in documents:
        local_source = (
            ROOT / "references" / "standards" / document["local_file"]
        )
        if local_source.exists():
            assert (
                hashlib.sha256(local_source.read_bytes()).hexdigest()
                == document["sha256"]
            ), document["id"]


def test_requirement_manifests_are_included_in_source_distributions() -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert '"/references/*.json"' in project
    assert '"/references/standards/manifest.json"' in project


def test_st0902_default_validator_exactly_matches_table_one_profile() -> None:
    document = _documents()["MISB-ST-0902.8"]
    expected = {
        tuple(tuple(path) for path in alternatives)
        for alternatives in document["minimum_profile_tag_paths"]
    }
    actual = {coverage.tag_paths for coverage in MISMMSValidator().coverage()}

    assert actual == expected
