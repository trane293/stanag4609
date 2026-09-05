"""Adapters for timestamped metadata CSV files used by FMV tooling."""

from __future__ import annotations

import csv
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from stanag4609.st0601 import encode_uas_local_set

ESRI_COLUMN_TAGS: dict[str, int] = {
    "TimeStamp": 2,
    "LDSVer": 65,
    "PlatformHeading": 5,
    "PlatformPitch": 6,
    "PlatformRoll": 7,
    "PlatformTrueAirSpeed": 8,
    "SensorLatitude": 13,
    "SensorLongitude": 14,
    "SensorAltitude": 15,
    "HorizontalFOV": 16,
    "VerticalFOV": 17,
    "SensorRelativeAzimuth": 18,
    "SensorRelativeElevation": 19,
    "SensorRelativeRoll": 20,
}

_INTEGER_COLUMNS = {"TimeStamp", "LDSVer", "PlatformTrueAirSpeed"}


@dataclass(frozen=True, slots=True)
class CsvMetadataRecord:
    """One normalized CSV row ready for ST 0601 encoding."""

    row_number: int
    values: Mapping[int, int | float]

    @property
    def timestamp_microseconds(self) -> int:
        return int(self.values[2])

    def encode(self) -> bytes:
        return encode_uas_local_set(self.values)


def parse_esri_csv_row(row: Mapping[str, str], *, row_number: int) -> CsvMetadataRecord:
    """Normalize one ArcGIS FMV Multiplexer-style metadata row."""
    values: dict[int, int | float] = {}
    for column, tag in ESRI_COLUMN_TAGS.items():
        text = row.get(column)
        if text is None:
            raise ValueError(f"CSV row {row_number} is missing required column {column!r}")
        text = text.strip()
        if not text:
            continue
        try:
            values[tag] = int(text) if column in _INTEGER_COLUMNS else float(text)
        except ValueError as error:
            raise ValueError(
                f"CSV row {row_number} column {column!r} is not numeric: {text!r}"
            ) from error
    if 2 not in values:
        raise ValueError(f"CSV row {row_number} has no TimeStamp")
    return CsvMetadataRecord(row_number, values)


def iter_esri_metadata_csv(source: str | Path | TextIO) -> Iterator[CsvMetadataRecord]:
    """Yield normalized records from an ArcGIS FMV Multiplexer-style CSV."""
    if hasattr(source, "read"):
        yield from _iter_csv_stream(source)  # type: ignore[arg-type]
        return
    with Path(source).open("r", encoding="utf-8-sig", newline="") as stream:
        yield from _iter_csv_stream(stream)


def _iter_csv_stream(stream: TextIO) -> Iterator[CsvMetadataRecord]:
    reader = csv.DictReader(stream)
    if reader.fieldnames is None:
        raise ValueError("metadata CSV has no header")
    missing = set(ESRI_COLUMN_TAGS) - set(reader.fieldnames)
    if missing:
        names = ", ".join(sorted(missing))
        raise ValueError(f"metadata CSV is missing required column(s): {names}")
    for row_number, row in enumerate(reader, start=2):
        yield parse_esri_csv_row(row, row_number=row_number)
