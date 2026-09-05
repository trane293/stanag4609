from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import partial
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from stanag4609.player import (
    MetadataSample,
    MetadataTimeline,
    OverlayDetection,
    scan_transport_timeline,
    summarize_detection_timeline,
)
from stanag4609.player.server import (
    PlayerHTTPRequestHandler,
    _iter_timeline_sse,
    ffmpeg_player_command,
    prepare_player_assets,
)
from stanag4609.player.timeline import extract_overlay_detections
from stanag4609.st0601 import (
    GenericFlagData,
    IMAPFieldValue,
    LaserPRFCode,
    PlatformStatus,
    PositioningMethodSource,
    SensorControlMode,
    SensorFieldOfViewName,
    WeaponFired,
    WeaponLoad,
    encode_uas_local_set,
)
from stanag4609.st0903 import (
    AlgorithmLocalSet,
    DetectionStatus,
    OntologyLocalSet,
    PixelRun,
    VMaskLocalSet,
    VObjectLocalSet,
    VTargetData,
    decode_vmti_local_set,
    encode_vmti_local_set,
)
from stanag4609.transport.metadata import synchronous_klv_stream
from stanag4609.transport.mux import TransportMuxer, encode_pes_packet
from stanag4609.transport.psi import ElementaryStreamInfo


def _source() -> bytes:
    muxer = TransportMuxer(
        transport_stream_id=1,
        program_number=1,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=(
            ElementaryStreamInfo(0x1B, 0x101, ()),
            synchronous_klv_stream(
                0x102,
                metadata_input_leak_rate=1_000,
                metadata_buffer_size=20_000,
            ),
        ),
    )
    video = encode_pes_packet(b"frame", stream_id=0xE0, pts=90_000)
    vmti = encode_vmti_local_set(
        {4: 6, 8: 100, 9: 50},
        targets=(
            VTargetData(
                7,
                {
                    1: 920,
                    2: 511,
                    3: 1430,
                    5: 93,
                    10: 0.1,
                    11: -0.2,
                    12: 150,
                    19: 10,
                    20: 20,
                    23: DetectionStatus.ACTIVE_MOVING,
                },
            ),
        ),
    )
    metadata = encode_uas_local_set(
        {
            2: 1_700_000_000_000_000,
            5: 180,
            13: 49,
            14: -123,
            15: 1_000,
            75: 1_500,
            104: IMAPFieldValue(2_000, 3),
            23: 48,
            24: -122,
            25: 100,
            34: 2,
            40: 47,
            41: -121,
            42: 50,
            47: GenericFlagData.LASER_RANGE | GenericFlagData.SLANT_RANGE_MEASURED,
            60: WeaponLoad.from_components(1, 2, 3, 4),
            61: WeaponFired.from_components(1, 2),
            62: LaserPRFCode(1743),
            63: SensorFieldOfViewName.NARROW,
            65: 19,
            74: vmti,
            77: 1,
            124: PositioningMethodSource.INS | PositioningMethodSource.GPS,
            125: PlatformStatus.EGRESS,
            126: SensorControlMode.AUTO_TRACKING,
            136: 29,
            137: 125_000,
        }
    )
    return (
        b"".join(muxer.program_tables())
        + b"".join(muxer.mux_pes(0x101, video))
        + b"".join(muxer.mux_sync_klv(0x102, metadata, pts=135_000))
    )


def _sparse_source() -> bytes:
    muxer = TransportMuxer(
        transport_stream_id=1,
        program_number=1,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=(
            ElementaryStreamInfo(0x1B, 0x101, ()),
            synchronous_klv_stream(
                0x102,
                metadata_input_leak_rate=1_000,
                metadata_buffer_size=20_000,
            ),
        ),
    )
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    first = encode_uas_local_set({2: start, 13: 49.0, 14: -123.0, 65: 19})
    second = encode_uas_local_set({2: start + timedelta(seconds=1), 65: 19})
    return (
        b"".join(muxer.program_tables())
        + b"".join(muxer.mux_pes(0x101, encode_pes_packet(b"frame", stream_id=0xE0, pts=0)))
        + b"".join(muxer.mux_sync_klv(0x102, first, pts=0))
        + b"".join(muxer.mux_sync_klv(0x102, second, pts=90_000))
    )


def _source_with_earlier_audio() -> bytes:
    muxer = TransportMuxer(
        transport_stream_id=1,
        program_number=1,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=(
            ElementaryStreamInfo(0x1B, 0x101, ()),
            ElementaryStreamInfo(0x03, 0x102, ()),
            synchronous_klv_stream(
                0x103,
                metadata_input_leak_rate=1_000,
                metadata_buffer_size=20_000,
            ),
        ),
    )
    metadata = encode_uas_local_set(
        {2: 1_700_000_000_000_000, 13: 49.0, 14: -123.0, 65: 19}
    )
    return (
        b"".join(muxer.program_tables())
        + b"".join(
            muxer.mux_pes(
                0x102,
                encode_pes_packet(b"audio", stream_id=0xC0, pts=87_000),
            )
        )
        + b"".join(
            muxer.mux_pes(
                0x101,
                encode_pes_packet(b"frame", stream_id=0xE0, pts=90_000),
            )
        )
        + b"".join(muxer.mux_sync_klv(0x103, metadata, pts=91_500))
    )


def test_timeline_is_media_relative_and_json_ready_across_arbitrary_chunks() -> None:
    source = _source()
    timeline = scan_transport_timeline(
        source[index : index + 73] for index in range(0, len(source), 73)
    )

    assert timeline.video_start_pts == 90_000
    assert len(timeline.samples) == 1
    sample = timeline.samples[0]
    assert sample.time_seconds == 0.5
    assert sample.fields["Sensor Latitude"]["value"] == pytest.approx(49)
    assert "Sensor True Altitude" not in sample.fields
    assert "Sensor Ellipsoid Height" not in sample.fields
    assert sample.fields["Sensor Ellipsoid Height Extended"]["value"] == pytest.approx(
        2_000, abs=1
    )
    assert sample.fields["Frame Center Longitude"]["value"] == pytest.approx(-122)
    assert sample.fields["Target Location Elevation"]["units"] == "metres"
    assert sample.fields["Target Location Elevation"]["vertical_datum"] == "msl"
    assert sample.fields["Target Location Elevation"]["datum_basis_tags"] == [25]
    assert sample.fields["Icing Detected"] == {
        "value": 2,
        "display": "Icing Detected (2)",
    }
    assert sample.fields["Generic Flag Data"] == {
        "value": 17,
        "display": "Laser Range + Slant Range Measured (0x11)",
        "flags": ["laser_range", "slant_range_measured"],
    }
    assert sample.fields["Weapon Load"]["value"] == 0x1234
    assert sample.fields["Weapon Load"]["components"] == {
        "station_number": 1,
        "substation_number": 2,
        "weapon_type": 3,
        "weapon_variant": 4,
    }
    assert sample.fields["Weapon Fired"]["display"] == (
        "Station 1, substation 2 (0x12)"
    )
    assert sample.fields["Laser PRF Code"]["display"] == "1743"
    assert sample.fields["Sensor Field of View Name"]["display"] == "Narrow (1)"
    assert sample.fields["Operational Mode"]["display"] == "Operational (1)"
    assert sample.fields["Positioning Method Source"]["flags"] == ["ins", "gps"]
    assert sample.fields["Platform Status"]["display"] == "Egress (9)"
    assert sample.fields["Sensor Control Mode"]["display"] == "Auto Tracking (6)"
    assert sample.fields["Precision Time Stamp"]["time_scale"] == "MISP"
    assert (
        sample.fields["Precision Time Stamp"]["microseconds_since_epoch"]
        == 1_700_000_000_000_000
    )
    assert sample.fields["UTC Timestamp"] == {
        "value": "2023-11-14T22:12:51.125000+00:00",
        "time_scale": "UTC",
        "derived_from_tags": [2, 136, 137],
    }
    assert sample.fields["VMTI Local Set"]["value"]["targets_reported"] == 1
    assert {feature["properties"]["role"] for feature in sample.geospatial} == {
        "sensor",
        "frame_center",
        "target",
        "vmti_target",
    }
    assert sample.detections[0].target_id == 7
    assert sample.detections[0].left == pytest.approx(0.1)
    assert sample.detections[0].bottom == pytest.approx(0.3)
    assert sample.detections[0].latitude == pytest.approx(48.1, abs=0.01)
    assert sample.detections[0].longitude == pytest.approx(-122.2, abs=0.01)
    assert sample.detections[0].hae == pytest.approx(150, abs=1)
    assert sample.detections[0].location_source == "parent_offset"
    vmti_panel = sample.fields["VMTI Local Set"]["value"]
    assert vmti_panel["targets"][0]["latitude"] == pytest.approx(48.1, abs=0.01)
    encoded = json.loads(timeline.to_json())
    assert encoded["samples"][0]["fields"]["Precision Time Stamp"]["value"].startswith(
        "2023-11-14T22:13:20"
    )
    assert encoded["samples"][0]["detections"][0]["confidence"] == 93
    assert encoded["samples"][0]["geospatial"][0]["type"] == "Feature"


def test_timeline_preserves_audio_lead_in_used_by_transcoded_media() -> None:
    timeline = scan_transport_timeline([_source_with_earlier_audio()])

    assert timeline.video_start_pts == 90_000
    assert timeline.media_start_pts == 87_000
    assert timeline.samples[0].time_seconds == pytest.approx(0.05)
    encoded = json.loads(timeline.to_json())
    assert encoded["media_start_pts"] == 87_000


def test_overlay_detections_include_normalized_geometry_and_vocabulary() -> None:
    vmti = decode_vmti_local_set(
        encode_vmti_local_set(
            {4: 6, 8: 100, 9: 50},
            targets=(
                VTargetData(
                    9,
                    {
                        2: 511,
                        3: 1430,
                        5: 88,
                        19: 10,
                        20: 20,
                        22: 7,
                        23: DetectionStatus.ACTIVE_STOPPED,
                        101: VMaskLocalSet(
                            pixel_contour=(1, 100, 5_000, 4_901),
                            bit_mask_series=(PixelRun(202, 3), PixelRun(399, 4)),
                        ),
                        107: (VObjectLocalSet(12, 88),),
                    },
                ),
            ),
            algorithms=(AlgorithmLocalSet(7, "truck-detector", "1.0"),),
            ontologies=(
                OntologyLocalSet(
                    12,
                    "https://example.org/objects.owl",
                    "https://example.org/objects.owl#Truck",
                    label="truck",
                ),
            ),
        ),
        standalone=False,
    )
    detection = extract_overlay_detections(
        vmti,
        frame_corners=(
            (10.0, 20.0),
            (12.0, 20.0),
            (12.0, 18.0),
            (10.0, 18.0),
        ),
    )[0]
    assert detection.status == "active_stopped"
    assert detection.label == "truck"
    assert detection.algorithm_name == "truck-detector"
    assert (detection.left, detection.top, detection.right, detection.bottom) == pytest.approx(
        (0.1, 0.1, 0.3, 0.3)
    )
    assert (detection.center_x, detection.center_y) == pytest.approx((0.195, 0.19))
    assert detection.contour == (
        (0.005, 0.01),
        (0.995, 0.01),
        (0.995, 0.99),
        (0.005, 0.99),
    )
    assert [(run.start_pixel, run.run_length) for run in detection.mask_runs] == [
        (202, 3),
        (399, 4),
    ]
    assert (detection.mask_width, detection.mask_height) == (100, 50)
    assert detection.ground_polygon_source == "frame_footprint_bilinear"
    assert tuple(value for point in detection.ground_polygon for value in point) == pytest.approx(
        (
            10.2,
            19.8,
            10.6,
            19.8,
            10.6,
            19.4,
            10.2,
            19.4,
            10.2,
            19.8,
        )
    )


def test_overlay_extraction_requires_dimensions_and_typed_input() -> None:
    vmti = decode_vmti_local_set(
        encode_vmti_local_set({4: 6}, standalone=False),
        standalone=False,
    )
    assert extract_overlay_detections(vmti) == ()
    with pytest.raises(TypeError, match="VMTILocalSet"):
        extract_overlay_detections(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="four longitude/latitude pairs"):
        extract_overlay_detections(vmti, frame_corners=((1.0, 2.0),))


def test_detection_timeline_summary_is_sparse_exact_and_label_bounded() -> None:
    def detection(identifier: int, label: str) -> OverlayDetection:
        return OverlayDetection(
            identifier,
            None,
            None,
            label,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )

    timeline = MetadataTimeline(
        90_000,
        (
            MetadataSample(0.0, 90_000, 1, 258, {}, detections=(detection(1, "truck"),)),
            MetadataSample(
                5.0,
                540_000,
                1,
                258,
                {},
                detections=tuple(
                    detection(index + 2, "truck" if index < 5 else f"class-{index}")
                    for index in range(10)
                ),
            ),
        ),
    )

    summary = summarize_detection_timeline(
        timeline,
        bin_count=10,
        duration_seconds=10,
        max_labels_per_bin=2,
    )

    assert summary.duration_seconds == 10
    assert summary.sample_count == 2
    assert summary.observation_count == 11
    assert summary.bin_count == 10
    assert [item.index for item in summary.bins] == [0, 5]
    assert summary.bins[0].count == 1
    assert [(item.label, item.count) for item in summary.bins[0].labels] == [
        ("truck", 1)
    ]
    assert summary.bins[0].other_count == 0
    assert summary.bins[1].count == 10
    assert len(summary.bins[1].labels) <= 2
    assert sum(item.count for item in summary.bins[1].labels) + summary.bins[1].other_count == 10
    assert json.loads(summary.to_json())["observation_count"] == 11


def test_detection_timeline_summary_bounds_adversarial_label_cardinality() -> None:
    detections = tuple(
        OverlayDetection(
            index,
            None,
            None,
            f"unique-{index}",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        for index in range(10_000)
    )
    timeline = MetadataTimeline(
        0,
        (MetadataSample(0.0, 0, 1, 258, {}, detections=detections),),
    )

    summary = summarize_detection_timeline(
        timeline,
        bin_count=1,
        max_labels_per_bin=4,
    )

    assert summary.observation_count == 10_000
    assert len(summary.bins) == 1
    assert len(summary.bins[0].labels) <= 4
    represented = sum(item.count for item in summary.bins[0].labels)
    assert represented + summary.bins[0].other_count == 10_000


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"bin_count": 0}, "bin_count"),
        ({"bin_count": 2049}, "bin_count"),
        ({"bin_count": 10, "duration_seconds": float("nan")}, "duration_seconds"),
        ({"bin_count": 10, "duration_seconds": -1}, "duration_seconds"),
        ({"bin_count": 10, "max_labels_per_bin": 0}, "max_labels_per_bin"),
    ],
)
def test_detection_timeline_summary_validates_bounds(
    kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        summarize_detection_timeline(MetadataTimeline(None, ()), **kwargs)  # type: ignore[arg-type]


def test_contour_only_detection_derives_overlay_center() -> None:
    vmti = decode_vmti_local_set(
        encode_vmti_local_set(
            {4: 6, 8: 4, 9: 3},
            targets=(
                VTargetData(
                    1,
                    {101: VMaskLocalSet(pixel_contour=(1, 4, 12, 9))},
                ),
            ),
            standalone=False,
        ),
        standalone=False,
    )
    detection = extract_overlay_detections(vmti)[0]
    assert detection.left is None
    assert detection.center_x == pytest.approx(0.5)
    assert detection.center_y == pytest.approx(0.5)
    assert detection.mask_runs == ()


def test_timeline_reconstructs_sparse_report_on_change_values() -> None:
    timeline = scan_transport_timeline((_sparse_source(),))
    assert len(timeline.samples) == 2
    assert timeline.samples[1].fields["Sensor Latitude"]["value"] == pytest.approx(49.0)
    assert timeline.samples[1].fields["Sensor Longitude"]["value"] == pytest.approx(-123.0)


def test_file_scan_chunk_size_is_bounded() -> None:
    from stanag4609.player import scan_transport_file

    with pytest.raises(ValueError, match="at least 188"):
        scan_transport_file("unused.ts", chunk_size=1)


def test_reference_player_transcode_is_browser_compatible_and_drops_data_streams() -> None:
    command = ffmpeg_player_command(Path("input.ts"), Path("media.mp4"), ffmpeg="ffmpeg7")
    assert command[0] == "ffmpeg7"
    assert command[-1] == "media.mp4"
    assert command[command.index("-map") : command.index("-map") + 2] == ("-map", "0:v:0")
    assert "0:a:0?" in command
    assert "-dn" in command
    assert "libx264" in command
    assert "+faststart" in command


def test_reference_player_inline_javascript_parses() -> None:
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("Node.js is required for the browser syntax gate in CI")
        pytest.skip("Node.js is unavailable for the optional browser syntax gate")
    html = (
        Path(__file__).parents[2]
        / "src"
        / "stanag4609"
        / "player"
        / "static"
        / "index.html"
    ).read_text(encoding="utf-8")
    _before, separator, script_and_after = html.partition("<script>")
    script, end_separator, _after = script_and_after.partition("</script>")
    assert separator and end_separator

    result = subprocess.run(
        (node, "--check"),
        input=script,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_reference_player_serves_seekable_byte_ranges(tmp_path: Path) -> None:
    media = b"0123456789"
    (tmp_path / "media.mp4").write_bytes(media)
    handler = partial(PlayerHTTPRequestHandler, directory=str(tmp_path))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request("GET", "/media.mp4", headers={"Range": "bytes=2-5"})
        response = connection.getresponse()
        assert response.status == 206
        assert response.getheader("Accept-Ranges") == "bytes"
        assert response.getheader("Content-Range") == "bytes 2-5/10"
        assert response.getheader("Content-Length") == "4"
        assert response.read() == b"2345"

        connection.request("GET", "/media.mp4", headers={"Range": "bytes=-3"})
        suffix = connection.getresponse()
        assert suffix.status == 206
        assert suffix.getheader("Content-Range") == "bytes 7-9/10"
        assert suffix.read() == b"789"

        connection.request("GET", "/media.mp4", headers={"Range": "bytes=10-20"})
        invalid = connection.getresponse()
        assert invalid.status == 416
        assert invalid.getheader("Content-Range") == "bytes */10"
        invalid.read()
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_timeline_sse_replays_at_media_time_with_bounded_heartbeats() -> None:
    timeline = scan_transport_timeline((_source(),))
    now = [100.0]
    sleeps: list[float] = []

    def monotonic() -> float:
        return now[0]

    def sleep(duration: float) -> None:
        sleeps.append(duration)
        now[0] += duration

    chunks = list(
        _iter_timeline_sse(
            timeline,
            start_seconds=0.0,
            heartbeat_seconds=0.2,
            monotonic=monotonic,
            sleep=sleep,
        )
    )

    assert sleeps == pytest.approx([0.2, 0.2, 0.1])
    assert chunks[0] == b": keep-alive\n\n"
    assert chunks[1] == b": keep-alive\n\n"
    assert chunks[2].startswith(b"event: sample\nid: 0\ndata: {")
    payload = json.loads(chunks[2].split(b"data: ", 1)[1])
    assert payload["time_seconds"] == 0.5
    assert payload["fields"]["Sensor Latitude"]["value"] == pytest.approx(49.0)
    assert chunks[3] == b'event: end\ndata: {"samples":1}\n\n'

    sleeps.clear()
    current = list(
        _iter_timeline_sse(
            timeline,
            start_seconds=0.5,
            monotonic=monotonic,
            sleep=sleep,
        )
    )
    assert sleeps == []
    assert current[0].startswith(b"event: sample\nid: 0\n")


def test_timeline_sse_validates_timing_contract_and_order() -> None:
    timeline = scan_transport_timeline((_source(),))
    empty = MetadataTimeline(None, ())
    assert list(_iter_timeline_sse(empty, start_seconds=0)) == [
        b'event: end\ndata: {"samples":0}\n\n'
    ]

    with pytest.raises(TypeError, match="MetadataTimeline"):
        list(_iter_timeline_sse(object(), start_seconds=0))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="start_seconds"):
        list(_iter_timeline_sse(timeline, start_seconds=True))
    for value in (float("nan"), float("inf")):
        with pytest.raises(ValueError, match="finite"):
            list(_iter_timeline_sse(timeline, start_seconds=value))
    with pytest.raises(ValueError, match="non-negative"):
        list(_iter_timeline_sse(timeline, start_seconds=-0.1))
    with pytest.raises(ValueError, match="playback_rate"):
        list(_iter_timeline_sse(timeline, start_seconds=0, playback_rate=0))
    with pytest.raises(ValueError, match="heartbeat_seconds"):
        list(_iter_timeline_sse(timeline, start_seconds=0, heartbeat_seconds=0))

    sample = timeline.samples[0]
    unordered = MetadataTimeline(
        timeline.video_start_pts,
        (replace(sample, time_seconds=2), replace(sample, time_seconds=1)),
    )
    with pytest.raises(ValueError, match="ordered"):
        list(_iter_timeline_sse(unordered, start_seconds=0))


def test_reference_player_serves_timed_metadata_events(tmp_path: Path) -> None:
    timeline = scan_transport_timeline((_source(),))
    handler = partial(
        PlayerHTTPRequestHandler,
        directory=str(tmp_path),
        timeline=timeline,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request("GET", "/metadata/events?start=0.5")
        response = connection.getresponse()
        body = response.read()
        connection.close()
        assert response.status == 200
        assert response.getheader("Content-Type") == "text/event-stream; charset=utf-8"
        assert response.getheader("Cache-Control") == "no-store"
        assert b"event: sample\nid: 0\n" in body
        assert body.endswith(b'event: end\ndata: {"samples":1}\n\n')

        summary_connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        summary_connection.request("GET", "/metadata/summary?bins=16&duration=10")
        summary_response = summary_connection.getresponse()
        summary = json.loads(summary_response.read())
        summary_connection.close()
        assert summary_response.status == 200
        assert summary_response.getheader("Content-Type") == "application/json; charset=utf-8"
        assert summary_response.getheader("Cache-Control") == "private, max-age=60"
        assert summary["duration_seconds"] == 10
        assert summary["sample_count"] == 1
        assert summary["observation_count"] == 1
        assert summary["bin_count"] == 16
        assert summary["bins"] == [
            {
                "index": 0,
                "count": 1,
                "labels": [{"label": "target 7", "count": 1}],
                "other_count": 0,
            }
        ]

        for query in (
            "start=-1",
            "start=nan",
            "start=",
            "start=1&start=2",
            "rate=0",
            "rate=17",
            "rate=wat",
        ):
            invalid = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            invalid.request("GET", f"/metadata/events?{query}")
            invalid_response = invalid.getresponse()
            assert invalid_response.status == 400
            invalid_response.read()
            invalid.close()

        for query in ("bins=0", "bins=2049", "bins=wat", "duration=-1", "duration=nan"):
            invalid = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            invalid.request("GET", f"/metadata/summary?{query}")
            invalid_response = invalid.getresponse()
            assert invalid_response.status == 400
            invalid_response.read()
            invalid.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("byte_range", [None, (0, 2)])
def test_reference_player_ignores_normal_client_disconnects(
    byte_range: tuple[int, int] | None,
) -> None:
    class DisconnectedClient(io.BytesIO):
        def write(self, data: bytes) -> int:
            raise BrokenPipeError

    handler = object.__new__(PlayerHTTPRequestHandler)
    handler._byte_range = byte_range

    handler.copyfile(io.BytesIO(b"media"), DisconnectedClient())


def test_prepare_player_assets_writes_timeline_ui_and_transcode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "input.ts"
    source.write_bytes(_source())

    def fake_run(
        command: tuple[str, ...],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert check
        assert capture_output
        assert text
        Path(command[-1]).write_bytes(b"mock mp4")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assets = prepare_player_assets(source, tmp_path / "site", ffmpeg="ffmpeg-test")
    assert assets.media.read_bytes() == b"mock mp4"
    html = (assets.root / "index.html").read_text()
    assert "STANAG 4609 / MISB" in html
    assert 'canvas id="overlay"' in html
    assert 'canvas id="map"' in html
    assert 'canvas id="detection-timeline"' in html
    assert 'input id="detection-scrubber"' in html
    assert 'ol id="activity-list"' in html
    assert 'id="map-attribution"' in html
    assert "https://tile.openstreetmap.org/{z}/{x}/{y}.png" in html
    assert "MAX_TILE_CACHE = 96" in html
    assert "MAX_ACTIVITY_ITEMS = 40" in html
    assert "Math.min(Math.ceil(width), 2048)" in html
    assert "map.dataset.detectionPolygons" in html
    assert "detection.contour" in html
    assert "detection.mask_runs" in html
    assert "dd.textContent = displayValue(entry)" in html
    assert "maximumFractionDigits: 6" in html
    assert "if (entry.display != null) return entry.display" in html
    assert "clamp(420px, 36vw, 560px)" in html
    assert "white-space: nowrap" in html
    assert "@media (max-width: 520px)" in html
    assert "measureText(point.label)" in html
    assert 'section id="diagnostics"' in html
    assert "issueList.replaceChildren(...issues.map" in html
    assert "diagnostics.hidden = issues.length === 0" in html
    assert "timeline request returned HTTP" in html
    assert "timeline does not contain a samples array" in html
    assert "No ST 0601 metadata samples found" in html
    assert "Waiting for first metadata sample" in html
    assert "render(findSample(video.currentTime))" in html
    assert "new EventSource" in html
    assert "metadata/events?start=" in html
    assert "metadata/summary?bins=2048&duration=" in html
    assert "loading full-mission overview" in html
    assert "timelineOverview.observation_count" in html
    assert "if (!liveMode && video.paused) stopMetadataStream()" in html
    assert "video.playbackRate" in html
    assert "video.addEventListener('waiting', () => { if (!liveMode)" in html
    assert "SSE live · " in html
    assert "samples.length > MAX_STREAM_SAMPLES" in html
    assert "new MediaSource()" in html
    assert "media/init.mp4?wait=10" in html
    assert "media/fragment?after=" in html
    assert "metadata/live?after=" in html
    assert "live media history was exceeded" in html
    assert "render(0);" not in html
    prepared = json.loads(assets.timeline.read_text())
    assert prepared["samples"][0]["time_seconds"] == 0.5
    assert prepared["samples"][0]["fields"]["Platform Status"] == {
        "value": 9,
        "display": "Egress (9)",
    }


def test_prepare_player_assets_reports_missing_input_and_ffmpeg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(FileNotFoundError):
        prepare_player_assets(tmp_path / "missing.ts", tmp_path / "site")

    source = tmp_path / "input.ts"
    source.write_bytes(_source())

    def missing(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("ffmpeg-missing")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(RuntimeError, match="FFmpeg executable not found"):
        prepare_player_assets(source, tmp_path / "site", ffmpeg="ffmpeg-missing")

    def failed(*args: object, **kwargs: object) -> None:
        raise subprocess.CalledProcessError(1, ["ffmpeg"], stderr="first\nuseful detail")

    monkeypatch.setattr(subprocess, "run", failed)
    with pytest.raises(RuntimeError, match=r"could not transcode.*useful detail"):
        prepare_player_assets(source, tmp_path / "site", ffmpeg="ffmpeg-broken")
