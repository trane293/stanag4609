from __future__ import annotations

from io import StringIO

import pytest

from stanag4609.csvio import ESRI_COLUMN_TAGS, iter_esri_metadata_csv, parse_esri_csv_row
from stanag4609.st0601 import decode_uas_local_set

HEADER = ",".join(ESRI_COLUMN_TAGS)
FIRST_ROW = (
    "1433429777800780,5,276.689403,0.043947,6.382946,67,27.405409,-82.126628,"
    "174.209201,52.301213,31.001144,351.138175,-52.055159,0"
)


def test_csv_record_encodes_as_valid_st0601() -> None:
    record = next(iter_esri_metadata_csv(StringIO(HEADER + "\n" + FIRST_ROW + "\n")))
    decoded = decode_uas_local_set(record.encode())
    assert record.row_number == 2
    assert record.timestamp_microseconds == 1433429777800780
    assert decoded.value(13) == pytest.approx(27.405409, abs=5e-8)
    assert decoded.value(14) == pytest.approx(-82.126628, abs=9e-8)
    assert decoded.value(15) == pytest.approx(174.209201, abs=0.16)
    assert decoded.value(65) == 5


def test_csv_row_reports_missing_and_invalid_values() -> None:
    with pytest.raises(ValueError, match="missing required column"):
        parse_esri_csv_row({}, row_number=7)
    row = dict(zip(ESRI_COLUMN_TAGS, FIRST_ROW.split(","), strict=True))
    row["SensorLatitude"] = "north"
    with pytest.raises(ValueError, match="SensorLatitude"):
        parse_esri_csv_row(row, row_number=7)


def test_csv_reader_validates_header_and_timestamp() -> None:
    with pytest.raises(ValueError, match="no header"):
        list(iter_esri_metadata_csv(StringIO("")))
    with pytest.raises(ValueError, match="missing required"):
        list(iter_esri_metadata_csv(StringIO("TimeStamp\n1\n")))
    row = dict(zip(ESRI_COLUMN_TAGS, FIRST_ROW.split(","), strict=True))
    row["TimeStamp"] = ""
    with pytest.raises(ValueError, match="no TimeStamp"):
        parse_esri_csv_row(row, row_number=2)
