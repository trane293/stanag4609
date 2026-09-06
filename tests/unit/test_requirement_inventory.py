from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from stanag4609 import MISMMSValidator

ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "references" / "requirements.json"
STANDARDS_MANIFEST = ROOT / "references" / "standards" / "manifest.json"


def _requirement_ids(value: str) -> set[str]:
    return set(value.split())


def _st_requirement_ids(value: str) -> set[str]:
    return {item.replace("_", " ") for item in value.split()}


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
ST0102_ACTIVE = {
    *(f"ST 0102.10-{index:02d}" for index in range(2, 12)),
    *(f"ST 0102.10-{index:02d}" for index in range(13, 18)),
    *(f"ST 0102.10-{index:02d}" for index in range(21, 28)),
    *(f"ST 0102.10-{index:02d}" for index in range(49, 52)),
    *(f"ST 0102.10-{index:02d}" for index in range(54, 63)),
    "ST 0102.11-63",
    "ST 0102.11-64",
    "ST 0102.12-65",
    "ST 0102.12-66",
}
ST0102_DEPRECATED = {
    "ST 0102.10-01",
    "ST 0102.10-12",
    *(f"ST 0102.10-{index:02d}" for index in range(18, 21)),
    *(f"ST 0102.10-{index:02d}" for index in range(28, 49)),
    "ST 0102.10-52",
    "ST 0102.10-53",
}
ST0107_ACTIVE = {
    "ST 0107.2-01",
    "ST 0107.2-02",
    *(f"ST 0107.3-{index:02d}" for index in range(3, 14)),
    *(f"ST 0107.4-{index:02d}" for index in range(14, 20)),
    "ST 0107.5-20",
}
ST0806_ACTIVE = {f"ST 0806.4-{index:02d}" for index in range(1, 26)}
ST1002_ACTIVE = {
    "ST 1002.1-02",
    "ST 1002.1-03",
    "ST 1002.1-05",
    "ST 1002.1-06",
    "ST 1002.1-07",
    "ST 1002.1-08",
    "ST 1002.1-09",
    "ST 1002.1-13",
    "ST 1002.1-14",
    "ST 1002.1-15",
    *(f"ST 1002.3-{index:02d}" for index in range(16, 26)),
}
ST1002_RETIRED = {
    "ST 1002.1-01",
    "ST 1002.1-04",
    "ST 1002.1-10",
    "ST 1002.1-11",
    "ST 1002.1-12",
}
ST1010_ACTIVE = {
    "ST 1010.2-09",
    "ST 1010.1-01",
    "ST 1010.2-10",
    "ST 1010.1-02",
    "ST 1010.2-11",
    "ST 1010.1-04",
    "ST 1010.2-12",
    "ST 1010.1-05",
    "ST 1010.1-08",
    "ST 1010.1-06",
    "ST 1010.2-13",
}
ST1010_DEPRECATED = {"ST 1010.1-03", "ST 1010.1-07"}
ST0902_ACTIVE = {
    "ST 0902.3-01",
    "ST 0902.3-03",
    "ST 0902.3-04",
    "ST 0902.8-05",
}
ST1206_ACTIVE = {
    "ST 1206-01",
    "ST 1206-03",
    "ST 1206-04",
    "ST 1206-05",
}
ST1204_ACTIVE = {
    *(f"ST 1204.1-{index:02d}" for index in range(1, 33)),
    "ST 1204.1-34",
    *(f"ST 1204.1-{index:02d}" for index in range(39, 45)),
    "ST 1204.2-45",
}
ST1204_DEPRECATED = {
    "ST 1204.1-33",
    "ST 1204.1-35",
    "ST 1204.1-36",
    "ST 1204.1-37",
    "ST 1204.1-38",
}
ST1601_ACTIVE = {
    "ST 1601-01",
    "ST 1601-02",
    "ST 1601.1-03",
    "ST 1601-04",
    "ST 1601-05",
    "ST 1601-06",
}
ST1602_ACTIVE = {
    "ST 1602-01",
    "ST 1602-02",
    "ST 1602-03",
    "ST 1602-04",
    "ST 1602.1-05",
    "ST 1602.1-06",
    "ST 1602.1-07",
    "ST 1602.1-08",
    "ST 1602.1-09",
    "ST 1602.1-10",
}
ST1607_ACTIVE = {
    "ST 1607-01",
    "ST 1607-02",
    "ST 1607.2-09",
    "ST 1607-04",
    "ST 1607.2-07",
    "ST 1607.2-08",
    "ST 1607-05",
    "ST 1607-06",
}
ST1607_RETIRED = {"ST 1607-03"}
ST0903_ACTIVE = _st_requirement_ids(
    """
    ST_0903.4-01 ST_0903.4-03 ST_0903.4-10 ST_0903.4-13 ST_0903.4-14
    ST_0903.4-15 ST_0903.4-17 ST_0903.4-18 ST_0903.4-19 ST_0903.4-26
    ST_0903.4-27 ST_0903.4-35 ST_0903.4-37 ST_0903.4-38 ST_0903.4-40
    ST_0903.4-56 ST_0903.4-59 ST_0903.4-62 ST_0903.4-63 ST_0903.4-66
    ST_0903.4-75 ST_0903.4-82 ST_0903.4-85 ST_0903.4-92 ST_0903.4-93
    ST_0903.4-94 ST_0903.5-98 ST_0903.5-99 ST_0903.5-100 ST_0903.5-101
    ST_0903.5-102 ST_0903.5-103 ST_0903.5-104 ST_0903.5-105 ST_0903.5-106
    ST_0903.5-107 ST_0903.5-108 ST_0903.6-116 ST_0903.6-117 ST_0903.6-118
    ST_0903.6-119 ST_0903.6-120 ST_0903.6-121 ST_0903.6-122 ST_0903.6-123
    ST_0903.6-124 ST_0903.6-125 ST_0903.6-126 ST_0903.6-127 ST_0903.6-128
    ST_0903.6-129 ST_0903.6-130 ST_0903.6-131 ST_0903.6-132 ST_0903.6-133
    ST_0903.6-134 ST_0903.6-135 ST_0903.6-136 ST_0903.6-137 ST_0903.6-138
    ST_0903.6-139 ST_0903.6-140 ST_0903.6-141 ST_0903.6-142 ST_0903.6-143
    """
)
ST0903_DEPRECATED = _st_requirement_ids(
    """
    ST_0903.4-02 ST_0903.4-04 ST_0903.4-05 ST_0903.4-06 ST_0903.4-07
    ST_0903.4-08 ST_0903.4-09 ST_0903.4-11 ST_0903.4-12 ST_0903.4-16
    ST_0903.4-20 ST_0903.4-21 ST_0903.4-22 ST_0903.4-23 ST_0903.4-24
    ST_0903.4-25 ST_0903.4-28 ST_0903.4-29 ST_0903.4-30 ST_0903.4-31
    ST_0903.4-32 ST_0903.4-33 ST_0903.4-34 ST_0903.4-36 ST_0903.4-39
    ST_0903.4-41 ST_0903.4-42 ST_0903.4-43 ST_0903.4-44 ST_0903.4-45
    ST_0903.4-46 ST_0903.4-47 ST_0903.4-48 ST_0903.4-49 ST_0903.4-50
    ST_0903.4-51 ST_0903.4-52 ST_0903.4-53 ST_0903.4-54 ST_0903.4-55
    ST_0903.4-57 ST_0903.4-58 ST_0903.4-60 ST_0903.4-61 ST_0903.4-64
    ST_0903.4-65 ST_0903.4-67 ST_0903.4-68 ST_0903.4-69 ST_0903.4-70
    ST_0903.4-71 ST_0903.4-72 ST_0903.4-73 ST_0903.4-74 ST_0903.4-76
    ST_0903.4-77 ST_0903.4-78 ST_0903.4-79 ST_0903.4-80 ST_0903.4-81
    ST_0903.4-83 ST_0903.4-84 ST_0903.4-86 ST_0903.4-87 ST_0903.4-88
    ST_0903.4-89 ST_0903.4-90 ST_0903.4-91 ST_0903.4-95 ST_0903.4-96
    ST_0903.4-97 ST_0903.5-109 ST_0903.5-110 ST_0903.5-111 ST_0903.5-112
    ST_0903.5-113 ST_0903.5-114 ST_0903.5-115
    """
)


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
        "MISB-ST-0102.12",
        "MISB-ST-0107.5",
        "MISB-ST-0806.4",
        "MISB-ST-0902.8",
        "MISB-ST-0903.6",
        "MISB-ST-1002.3",
        "MISB-ST-1010.3",
        "MISB-ST-1204.3",
        "MISB-ST-1206.1",
        "MISB-ST-1601.2",
        "MISB-ST-1602.2",
        "MISB-ST-1607.2",
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

    st0102 = documents["MISB-ST-0102.12"]
    assert set(st0102["active_requirements"]) == ST0102_ACTIVE
    assert len(st0102["active_requirements"]) == len(ST0102_ACTIVE) == 38
    assert {
        entry["id"] for entry in st0102["inactive_requirements"]
    } == ST0102_DEPRECATED
    assert len(st0102["inactive_requirements"]) == len(ST0102_DEPRECATED) == 28
    assert {
        entry["status"] for entry in st0102["inactive_requirements"]
    } == {"deprecated"}

    st0107 = documents["MISB-ST-0107.5"]
    assert set(st0107["active_requirements"]) == ST0107_ACTIVE
    assert len(st0107["active_requirements"]) == len(ST0107_ACTIVE) == 20
    assert st0107["inactive_requirements"] == []

    st0806 = documents["MISB-ST-0806.4"]
    assert set(st0806["active_requirements"]) == ST0806_ACTIVE
    assert len(st0806["active_requirements"]) == len(ST0806_ACTIVE) == 25
    assert st0806["inactive_requirements"] == []

    st1002 = documents["MISB-ST-1002.3"]
    assert set(st1002["active_requirements"]) == ST1002_ACTIVE
    assert len(st1002["active_requirements"]) == len(ST1002_ACTIVE) == 20
    assert {
        entry["id"] for entry in st1002["inactive_requirements"]
    } == ST1002_RETIRED
    assert {
        entry["status"] for entry in st1002["inactive_requirements"]
    } == {"retired"}

    st1010 = documents["MISB-ST-1010.3"]
    assert set(st1010["active_requirements"]) == ST1010_ACTIVE
    assert len(st1010["active_requirements"]) == len(ST1010_ACTIVE) == 11
    assert {
        entry["id"] for entry in st1010["inactive_requirements"]
    } == ST1010_DEPRECATED
    assert {
        entry["status"] for entry in st1010["inactive_requirements"]
    } == {"deprecated"}

    st0902 = documents["MISB-ST-0902.8"]
    assert set(st0902["active_requirements"]) == ST0902_ACTIVE
    assert st0902["inactive_requirements"] == [
        {"id": "ST 0902.3-02", "status": "deprecated"}
    ]

    st1206 = documents["MISB-ST-1206.1"]
    assert set(st1206["active_requirements"]) == ST1206_ACTIVE
    assert len(st1206["active_requirements"]) == len(ST1206_ACTIVE) == 4
    assert st1206["inactive_requirements"] == [
        {"id": "ST 1206-02", "status": "deprecated"}
    ]

    st1204 = documents["MISB-ST-1204.3"]
    assert set(st1204["active_requirements"]) == ST1204_ACTIVE
    assert len(st1204["active_requirements"]) == len(ST1204_ACTIVE) == 40
    assert {entry["id"] for entry in st1204["inactive_requirements"]} == ST1204_DEPRECATED
    assert {entry["status"] for entry in st1204["inactive_requirements"]} == {"deprecated"}

    st1601 = documents["MISB-ST-1601.2"]
    assert set(st1601["active_requirements"]) == ST1601_ACTIVE
    assert len(st1601["active_requirements"]) == len(ST1601_ACTIVE) == 6
    assert st1601["inactive_requirements"] == []

    st1602 = documents["MISB-ST-1602.2"]
    assert set(st1602["active_requirements"]) == ST1602_ACTIVE
    assert len(st1602["active_requirements"]) == len(ST1602_ACTIVE) == 10
    assert st1602["inactive_requirements"] == []

    st1607 = documents["MISB-ST-1607.2"]
    assert set(st1607["active_requirements"]) == ST1607_ACTIVE
    assert len(st1607["active_requirements"]) == len(ST1607_ACTIVE) == 8
    assert {
        entry["id"] for entry in st1607["inactive_requirements"]
    } == ST1607_RETIRED
    assert {
        entry["status"] for entry in st1607["inactive_requirements"]
    } == {"retired"}

    st0903 = documents["MISB-ST-0903.6"]
    assert set(st0903["active_requirements"]) == ST0903_ACTIVE
    assert len(st0903["active_requirements"]) == len(ST0903_ACTIVE) == 65
    assert {
        entry["id"] for entry in st0903["inactive_requirements"]
    } == ST0903_DEPRECATED
    assert len(st0903["inactive_requirements"]) == len(ST0903_DEPRECATED) == 78
    assert {
        entry["status"] for entry in st0903["inactive_requirements"]
    } == {"deprecated"}
    assert ST0903_ACTIVE.isdisjoint(ST0903_DEPRECATED)
    assert len(ST0903_ACTIVE | ST0903_DEPRECATED) == 143

    st0903_trace = (ROOT / st0903["trace"]).read_text(encoding="utf-8")
    for requirement in ST0903_DEPRECATED:
        assert requirement not in st0903_trace, (
            f"deprecated {requirement} is presented in the active ST 0903.6 trace"
        )

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
