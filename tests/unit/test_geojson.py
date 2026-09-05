from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from stanag4609.geojson import (
    export_geojson_sequence,
    iter_geojson_feature_collections,
    main,
)
from stanag4609.st0601 import IMAPFieldValue, SpecialValue, encode_uas_local_set
from stanag4609.st0903 import (
    Location,
    VTargetData,
    decode_vmti_local_set,
    encode_vmti_local_set,
)
from stanag4609.transport.metadata import synchronous_klv_stream
from stanag4609.transport.mux import TransportMuxer, encode_pes_packet
from stanag4609.transport.psi import ElementaryStreamInfo

_START = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)


def _transport(*packets: dict[int, object]) -> bytes:
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
    transport = bytearray(b"".join(muxer.program_tables()))
    transport.extend(
        b"".join(muxer.mux_pes(0x101, encode_pes_packet(b"v", stream_id=0xE0, pts=0)))
    )
    for index, values in enumerate(packets):
        required = {2: _START + timedelta(seconds=index), 65: 19}
        required.update(values)
        transport.extend(
            b"".join(muxer.mux_sync_klv(0x120, encode_uas_local_set(required), pts=90_000 * index))
        )
    return bytes(transport)


def _features_by_role(collection: dict[str, object]) -> dict[str, dict[str, object]]:
    features = collection["features"]
    assert isinstance(features, list)
    return {
        feature["properties"]["role"]: feature
        for feature in features
        if isinstance(feature, dict) and isinstance(feature.get("properties"), dict)
    }


def test_geojson_exports_points_and_prefers_full_frame_corners() -> None:
    data = _transport(
        {
            13: 40,
            14: -75,
            15: 1_000,
            23: 39.9,
            24: -74.9,
            25: 50,
            26: 0.01,
            27: 0.01,
            28: 0.01,
            29: -0.01,
            30: -0.01,
            31: -0.01,
            32: -0.01,
            33: 0.01,
            40: 39.95,
            41: -74.95,
            42: 75,
            82: 40.0,
            83: -75.0,
            84: 40.0,
            85: -74.8,
            86: 39.8,
            87: -74.8,
            88: 39.8,
            89: -75.0,
        }
    )
    chunks = (data[index : index + 193] for index in range(0, len(data), 193))
    collections = list(iter_geojson_feature_collections(chunks))

    assert len(collections) == 1
    collection = collections[0]
    assert collection["type"] == "FeatureCollection"
    assert collection["properties"] == {
        "timestamp": "2025-01-01T12:00:00+00:00",
        "timestamp_time_scale": "MISP",
        "program_number": 3,
        "metadata_pid": 0x120,
        "pts": 0,
        "pts_seconds": 0.0,
        "issues": [],
    }
    features = _features_by_role(collection)
    assert features["sensor"]["geometry"] == {
        "type": "Point",
        "coordinates": pytest.approx([-75, 40, 1_000], abs=0.1),
    }
    assert features["frame_center"]["geometry"] == {
        "type": "Point",
        "coordinates": pytest.approx([-74.9, 39.9, 50], abs=0.2),
    }
    assert features["target"]["geometry"] == {
        "type": "Point",
        "coordinates": pytest.approx([-74.95, 39.95, 75], abs=0.2),
    }
    polygon = features["frame_footprint"]["geometry"]
    assert polygon == {
        "type": "Polygon",
        "coordinates": [[
            pytest.approx([-75.0, 39.8]),
            pytest.approx([-74.8, 39.8]),
            pytest.approx([-74.8, 40.0]),
            pytest.approx([-75.0, 40.0]),
            pytest.approx([-75.0, 39.8]),
        ]],
    }
    assert features["frame_footprint"]["properties"]["corner_source"] == "full"


def test_geojson_labels_misp_time_and_exposes_packet_derived_utc() -> None:
    collection = next(
        iter_geojson_feature_collections((_transport({136: 29, 137: 125_000}),))
    )

    assert collection["properties"]["timestamp"] == "2025-01-01T12:00:00+00:00"
    assert collection["properties"]["timestamp_time_scale"] == "MISP"
    assert collection["properties"]["utc_timestamp"] == (
        "2025-01-01T11:59:31.125000+00:00"
    )


def test_geojson_prefers_extended_hae_and_labels_vertical_datum() -> None:
    data = _transport(
        {
            13: 40,
            14: -75,
            15: 100,
            75: 200,
            104: IMAPFieldValue(300, 3),
            23: 39.9,
            24: -74.9,
            25: 400,
            78: 500,
            40: 39.8,
            41: -74.8,
            42: 600,
        }
    )

    features = _features_by_role(next(iter_geojson_feature_collections((data,))))

    assert features["sensor"]["geometry"]["coordinates"] == pytest.approx(
        [-75, 40, 300], abs=1
    )
    assert features["sensor"]["properties"] == {
        "role": "sensor",
        "altitude_tag": 104,
        "vertical_datum": "hae",
    }
    assert features["frame_center"]["geometry"]["coordinates"] == pytest.approx(
        [-74.9, 39.9, 500], abs=0.3
    )
    assert features["frame_center"]["properties"]["altitude_tag"] == 78
    assert features["frame_center"]["properties"]["vertical_datum"] == "hae"
    assert features["target"]["properties"]["vertical_datum"] == "hae"


def test_geojson_marks_target_elevation_with_no_frame_height_datum_as_unknown() -> None:
    data = _transport({40: 39.8, 41: -74.8, 42: 600})

    target = _features_by_role(next(iter_geojson_feature_collections((data,))))[
        "target"
    ]

    assert target["geometry"]["coordinates"] == pytest.approx(
        [-74.8, 39.8, 600], abs=0.4
    )
    assert target["properties"] == {
        "role": "target",
        "altitude_tag": 42,
        "vertical_datum": "unknown",
    }


def test_geojson_reconstructs_sparse_offsets_and_wraps_antimeridian() -> None:
    data = _transport(
        {
            23: 10,
            24: 179.99,
            26: 0.01,
            27: 0.02,
            28: 0.01,
            29: -0.02,
            30: -0.01,
            31: -0.02,
            32: -0.01,
            33: 0.02,
        },
        {},
    )
    collections = list(iter_geojson_feature_collections((data,)))

    assert len(collections) == 2
    for collection in collections:
        footprint = _features_by_role(collection)["frame_footprint"]
        assert footprint["properties"]["corner_source"] == "offset"
        geometry = footprint["geometry"]
        assert geometry["type"] == "MultiPolygon"
        polygons = geometry["coordinates"]
        assert len(polygons) == 2
        assert all(polygon[0][-1] == polygon[0][0] for polygon in polygons)
        longitudes = [point[0] for polygon in polygons for point in polygon[0]]
        assert min(longitudes) == pytest.approx(-180)
        assert max(longitudes) == pytest.approx(180)
    assert collections[1]["properties"]["timestamp"] == "2025-01-01T12:00:01+00:00"


def test_geojson_omits_invalid_or_incomplete_geometry() -> None:
    data = _transport(
        {
            13: SpecialValue.RESERVED,
            14: -75,
            23: 40,
            24: -74,
            26: 0.01,
            27: 0.01,
        }
    )
    collection = next(iter_geojson_feature_collections((data,)))
    features = _features_by_role(collection)

    assert set(features) == {"frame_center"}


def test_geojson_includes_resolved_embedded_vmti_target_locations() -> None:
    vmti = decode_vmti_local_set(
        encode_vmti_local_set(
            {4: 6},
            targets=(
                VTargetData(7, {10: 0.1, 11: 0.2, 12: 500}),
                VTargetData(8, {17: Location(48.5, -122.5, 600)}),
            ),
        ),
        standalone=False,
    )
    data = _transport({23: 48, 24: -123, 65: 19, 74: vmti})

    collection = next(iter_geojson_feature_collections((data,)))
    targets = [
        feature
        for feature in collection["features"]
        if feature["properties"]["role"] == "vmti_target"
    ]

    assert [feature["properties"]["target_id"] for feature in targets] == [7, 8]
    assert targets[0]["geometry"]["coordinates"] == pytest.approx(
        [-122.8, 48.1, 500], abs=0.01
    )
    assert targets[0]["properties"]["location_source"] == "parent_offset"
    assert targets[1]["geometry"]["coordinates"] == pytest.approx(
        [-122.5, 48.5, 600], abs=0.01
    )
    assert targets[1]["properties"]["location_source"] == "absolute"


def test_geojson_sequence_export_is_atomic_and_cli_reports_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "fmv.ts"
    destination = tmp_path / "metadata.geojsonl"
    source.write_bytes(_transport({13: 40, 14: -75}, {23: 41, 24: -76}))

    result = export_geojson_sequence(source, destination, chunk_size=188)
    assert result.records_written == 2
    assert result.features_written == 3
    assert result.program_numbers == frozenset({3})
    assert result.metadata_pids == frozenset({0x120})
    lines = destination.read_text().splitlines()
    assert len(lines) == 2
    assert all(json.loads(line)["type"] == "FeatureCollection" for line in lines)

    with pytest.raises(FileExistsError):
        export_geojson_sequence(source, destination)
    with pytest.raises(ValueError, match="at least 188"):
        export_geojson_sequence(source, tmp_path / "bad.jsonl", chunk_size=1)
    with pytest.raises(ValueError, match="different"):
        export_geojson_sequence(source, source, overwrite=True)
    with pytest.raises(FileNotFoundError):
        export_geojson_sequence(tmp_path / "missing.ts", tmp_path / "missing.jsonl")

    cli_destination = tmp_path / "cli.geojsonl"
    assert main([str(source), str(cli_destination)]) == 0
    output = capsys.readouterr().out
    assert "wrote 2 metadata records and 3 features" in output
    assert "0x120" in output
