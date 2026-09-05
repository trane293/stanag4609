"""Stream ST 0601 metadata into ArcGIS FMV-compatible CSV sidecars."""

from __future__ import annotations

import argparse
import csv
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any

from stanag4609.csvio import ESRI_COLUMN_TAGS
from stanag4609.st0601 import FieldDecodingMode, SpecialValue, UASLocalSet
from stanag4609.st0601_state import ReportOnChangeState
from stanag4609.transport.demux import PESStreamEvent, StreamKind, TransportDemuxer
from stanag4609.transport.metadata_stream import MetadataStreamDecoder

_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class CsvExportResult:
    """Summary of one completed ST 0601 sidecar export."""

    destination: Path
    records_written: int
    program_numbers: frozenset[int]
    metadata_pids: frozenset[int]


def _timestamp_microseconds(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("ST 0601 timestamp must be timezone-aware")
    delta = value.astimezone(timezone.utc) - _UNIX_EPOCH
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _csv_scalar(value: Any) -> int | float | str:
    if value is None or isinstance(value, SpecialValue):
        return ""
    if isinstance(value, datetime):
        return _timestamp_microseconds(value)
    if isinstance(value, Fraction):
        return value.numerator if value.denominator == 1 else float(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"ESRI CSV field cannot represent {type(value).__name__}")
    return value


class _EsriRowDecoder:
    __slots__ = ("_decoder", "_demuxer", "_states", "metadata_pids", "program_numbers")

    def __init__(self) -> None:
        self._demuxer = TransportDemuxer()
        self._decoder = MetadataStreamDecoder(
            field_decoding=FieldDecodingMode.PRESERVE,
            validate_sequence=False,
        )
        self._states: dict[tuple[int, int], ReportOnChangeState] = {}
        self.program_numbers: set[int] = set()
        self.metadata_pids: set[int] = set()

    def feed(self, data: bytes | bytearray | memoryview) -> list[dict[str, int | float | str]]:
        return self._consume(self._demuxer.feed(data))

    def finish(self) -> list[dict[str, int | float | str]]:
        rows = self._consume(self._demuxer.finish())
        self._decoder.finish()
        return rows

    def _consume(self, events: Iterable[object]) -> list[dict[str, int | float | str]]:
        rows: list[dict[str, int | float | str]] = []
        for event in events:
            if not isinstance(event, PESStreamEvent) or event.kind is not StreamKind.KLV:
                continue
            for metadata in self._decoder.feed(event):
                if not isinstance(metadata.decoded, UASLocalSet):
                    continue
                self.program_numbers.add(metadata.program_number)
                self.metadata_pids.add(metadata.pid)
                state = self._states.setdefault(
                    (metadata.program_number, metadata.pid),
                    ReportOnChangeState(),
                )
                snapshot = state.observe(metadata.decoded)
                rows.append(
                    {
                        column: _csv_scalar(snapshot.value(tag))
                        for column, tag in ESRI_COLUMN_TAGS.items()
                    }
                )
        return rows


def iter_esri_metadata_rows(
    chunks: Iterable[bytes],
) -> Iterator[dict[str, int | float | str]]:
    """Yield report-on-change-reconstructed ArcGIS FMV metadata rows."""

    decoder = _EsriRowDecoder()
    for chunk in chunks:
        yield from decoder.feed(chunk)
    yield from decoder.finish()


def export_esri_metadata_csv(
    source_transport: str | Path,
    destination: str | Path,
    *,
    chunk_size: int = 1024 * 1024,
    overwrite: bool = False,
) -> CsvExportResult:
    """Atomically export ST 0601 from MPEG-TS to an ArcGIS FMV CSV sidecar."""

    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size < 188:
        raise ValueError("chunk_size must be an integer of at least 188 bytes")
    source_path = Path(source_transport)
    destination_path = Path(destination)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if source_path.resolve() == destination_path.resolve():
        raise ValueError("source and destination must be different files")
    if destination_path.exists() and not overwrite:
        raise FileExistsError(destination_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    decoder = _EsriRowDecoder()
    temporary_path: Path | None = None
    records_written = 0
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            prefix=f".{destination_path.name}.",
            suffix=".tmp",
            dir=destination_path.parent,
            delete=False,
        ) as output:
            temporary_path = Path(output.name)
            writer = csv.DictWriter(output, fieldnames=tuple(ESRI_COLUMN_TAGS))
            writer.writeheader()
            with source_path.open("rb") as source:
                while chunk := source.read(chunk_size):
                    for row in decoder.feed(chunk):
                        writer.writerow(row)
                        records_written += 1
            for row in decoder.finish():
                writer.writerow(row)
                records_written += 1
        assert temporary_path is not None
        temporary_path.replace(destination_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return CsvExportResult(
        destination_path,
        records_written,
        frozenset(decoder.program_numbers),
        frozenset(decoder.metadata_pids),
    )


def main(argv: list[str] | None = None) -> int:
    """Run the ST 0601 to ArcGIS FMV CSV export command-line interface."""

    parser = argparse.ArgumentParser(
        description="Export ST 0601 metadata from MPEG-TS to an ESRI-compatible CSV sidecar"
    )
    parser.add_argument("transport", type=Path, help="input STANAG-style MPEG-2 TS")
    parser.add_argument("destination", type=Path, help="output metadata CSV")
    parser.add_argument("--force", action="store_true", help="replace an existing destination")
    args = parser.parse_args(argv)
    result = export_esri_metadata_csv(
        args.transport,
        args.destination,
        overwrite=args.force,
    )
    print(
        f"wrote {result.records_written} metadata records to {result.destination} "
        f"from PID(s) {', '.join(hex(pid) for pid in sorted(result.metadata_pids)) or 'none'}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
