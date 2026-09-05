#!/usr/bin/env python3
"""Fetch opt-in public FMV integration fixtures and verify their identities."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[1]
MANIFEST = REPOSITORY / "references" / "fixtures.json"
DEFAULT_OUTPUT = REPOSITORY / "samples" / "private"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_fixtures() -> list[dict[str, Any]]:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    fixtures = document.get("fixtures")
    if not isinstance(fixtures, list):
        raise ValueError("fixture manifest must contain a fixtures list")
    return fixtures


def _download(fixture: dict[str, Any], output: Path) -> None:
    destination = output / str(fixture.get("path", fixture["filename"]))
    expected_hash = str(fixture["sha256"])
    expected_size = int(fixture["size"])
    if destination.is_file():
        if destination.stat().st_size == expected_size and _sha256(destination) == expected_hash:
            print(f"verified {fixture['id']}: {destination}")
            return
        raise ValueError(f"existing fixture does not match manifest: {destination}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".download", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        request = urllib.request.Request(
            str(fixture["url"]),
            headers={"User-Agent": "stanag4609 fixture fetcher"},
        )
        archive = fixture.get("archive")
        download_size = int(archive["size"]) if archive else expected_size
        download_hash = str(archive["sha256"]) if archive else expected_hash
        print(f"downloading {fixture['id']} ({download_size} bytes)")
        with (
            urllib.request.urlopen(request, timeout=60) as response,
            temporary.open("wb") as target,
        ):
            shutil.copyfileobj(response, target, length=1024 * 1024)
        if temporary.stat().st_size != download_size:
            raise ValueError(
                f"size mismatch for {fixture['id']}: "
                f"expected {download_size}, observed {temporary.stat().st_size}"
            )
        observed_hash = _sha256(temporary)
        if observed_hash != download_hash:
            raise ValueError(
                f"SHA-256 mismatch for {fixture['id']}: "
                f"expected {download_hash}, observed {observed_hash}"
            )
        if archive:
            if archive.get("format") != "zip":
                raise ValueError(f"unsupported archive format for {fixture['id']}")
            member = str(archive["member"])
            descriptor, extracted_name = tempfile.mkstemp(
                prefix=f".{destination.name}.", suffix=".part", dir=destination.parent
            )
            os.close(descriptor)
            extracted = Path(extracted_name)
            try:
                with zipfile.ZipFile(temporary) as bundle:
                    member_info = bundle.getinfo(member)
                    if member_info.is_dir() or member_info.file_size != expected_size:
                        raise ValueError(f"unexpected archive member for {fixture['id']}")
                    with bundle.open(member_info) as source, extracted.open("wb") as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)
                if _sha256(extracted) != expected_hash:
                    raise ValueError(f"extracted SHA-256 mismatch for {fixture['id']}")
                extracted.replace(destination)
            finally:
                extracted.unlink(missing_ok=True)
        else:
            temporary.replace(destination)
        print(f"saved {fixture['id']}: {destination}")
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "fixture_ids",
        nargs="*",
        help="fixture IDs to fetch (default: all)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"destination directory (default: {DEFAULT_OUTPUT})",
    )
    arguments = parser.parse_args()

    fixtures = _load_fixtures()
    selected = set(arguments.fixture_ids)
    known = {str(fixture["id"]) for fixture in fixtures}
    unknown = selected - known
    if unknown:
        parser.error(f"unknown fixture ID(s): {', '.join(sorted(unknown))}")
    for fixture in fixtures:
        if not selected or fixture["id"] in selected:
            _download(fixture, arguments.output.resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
