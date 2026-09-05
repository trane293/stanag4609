from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

import pytest

from stanag4609.csvexport import export_esri_metadata_csv, iter_esri_metadata_rows, main
from stanag4609.csvio import ESRI_COLUMN_TAGS, iter_esri_metadata_csv
from stanag4609.st0601 import SpecialValue, encode_uas_local_set
from stanag4609.transport.metadata import synchronous_klv_stream
from stanag4609.transport.mux import TransportMuxer, encode_pes_packet
from stanag4609.transport.psi import ElementaryStreamInfo

_START = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)


def _transport() -> bytes:
    muxer = TransportMuxer(
        transport_stream_id=1,
        program_number=3,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=(
            ElementaryStreamInfo(0x02, 0x101, ()),
            synchronous_klv_stream(
                0x120,
                metadata_input_leak_rate=1_000,
                metadata_buffer_size=200_000,
            ),
        ),
    )
    first = encode_uas_local_set(
        {
            2: _START,
            5: 90,
            6: -1,
            7: 2,
            8: 120,
            13: 40,
            14: -75,
            15: 1_000,
            16: 20,
            17: 10,
            18: 5,
            19: -2,
            20: 0,
            65: 19,
        }
    )
    second = encode_uas_local_set({2: _START + timedelta(seconds=1), 65: 19})
    return (
        b"".join(muxer.program_tables())
        + b"".join(muxer.mux_pes(0x101, encode_pes_packet(b"v", stream_id=0xE0, pts=0)))
        + b"".join(muxer.mux_sync_klv(0x120, first, pts=0))
        + b"".join(muxer.mux_sync_klv(0x120, second, pts=90_000))
    )


def test_iter_esri_rows_reconstructs_sparse_metadata_across_chunks() -> None:
    data = _transport()
    chunks = (data[index : index + 191] for index in range(0, len(data), 191))
    rows = list(iter_esri_metadata_rows(chunks))
    assert len(rows) == 2
    assert tuple(rows[0]) == tuple(ESRI_COLUMN_TAGS)
    assert rows[0]["TimeStamp"] == 1_735_732_800_000_000
    assert rows[1]["TimeStamp"] == 1_735_732_801_000_000
    assert rows[1]["SensorLatitude"] == pytest.approx(40)
    assert rows[1]["SensorLongitude"] == pytest.approx(-75)
    assert rows[1]["PlatformHeading"] == pytest.approx(90, abs=0.002)


def test_export_esri_csv_round_trips_through_import_adapter(tmp_path: Path) -> None:
    source = tmp_path / "fmv.ts"
    destination = tmp_path / "metadata.csv"
    source.write_bytes(_transport())
    result = export_esri_metadata_csv(source, destination, chunk_size=188)

    assert result.records_written == 2
    assert result.program_numbers == frozenset({3})
    assert result.metadata_pids == frozenset({0x120})
    records = list(iter_esri_metadata_csv(destination))
    assert len(records) == 2
    assert records[0].timestamp_microseconds == 1_735_732_800_000_000
    assert records[1].values[13] == pytest.approx(40)
    with destination.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[1]["SensorLatitude"] == rows[0]["SensorLatitude"]


def test_export_esri_csv_protects_existing_destination_and_validates_paths(
    tmp_path: Path,
) -> None:
    source = tmp_path / "fmv.ts"
    destination = tmp_path / "metadata.csv"
    source.write_bytes(_transport())
    destination.write_text("keep")

    with pytest.raises(FileExistsError):
        export_esri_metadata_csv(source, destination)
    assert destination.read_text() == "keep"
    result = export_esri_metadata_csv(source, destination, overwrite=True)
    assert result.records_written == 2

    with pytest.raises(ValueError, match="at least 188"):
        export_esri_metadata_csv(source, tmp_path / "bad.csv", chunk_size=1)
    with pytest.raises(ValueError, match="different"):
        export_esri_metadata_csv(source, source, overwrite=True)
    with pytest.raises(FileNotFoundError):
        export_esri_metadata_csv(tmp_path / "missing.ts", tmp_path / "missing.csv")


def test_csv_export_scalar_policy_handles_exact_and_special_values() -> None:
    from stanag4609.csvexport import _csv_scalar

    assert _csv_scalar(Fraction(3, 1)) == 3
    assert _csv_scalar(Fraction(1, 3)) == pytest.approx(1 / 3)
    assert _csv_scalar(SpecialValue.UNKNOWN) == ""
    assert _csv_scalar(None) == ""
    with pytest.raises(TypeError, match="cannot represent"):
        _csv_scalar(True)


def test_csv_export_cli_reports_records(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = tmp_path / "fmv.ts"
    destination = tmp_path / "metadata.csv"
    source.write_bytes(_transport())

    assert main([str(source), str(destination)]) == 0
    output = capsys.readouterr().out
    assert "wrote 2 metadata records" in output
    assert "0x120" in output
