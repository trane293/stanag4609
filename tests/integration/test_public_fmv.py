from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path
from typing import Any

import pytest

from stanag4609.audio import AudioPESFrameParser, PyAVAudioDecoder
from stanag4609.player.server import prepare_player_assets
from stanag4609.st0601 import FieldDecodingMode, UASLocalSet
from stanag4609.transport.demux import (
    PESStreamEvent,
    ProgramClockEvent,
    StreamKind,
    TransportDemuxer,
)
from stanag4609.transport.metadata_stream import MetadataStreamDecoder
from stanag4609.transport.pcr import PCRCadenceIssue, PCRCadenceValidator
from stanag4609.transport.rate import TransportRateShaper
from stanag4609.transport.transformer import LiveTransportTransformer
from stanag4609.verifier import VerificationStatus, verify_fmv_file

REPOSITORY = Path(__file__).resolve().parents[2]
FIXTURE_DIRECTORY = REPOSITORY / "samples" / "private"
MANIFEST = REPOSITORY / "references" / "fixtures.json"


def _fixture_cases() -> list[dict[str, Any]]:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return [
        fixture
        for fixture in document["fixtures"]
        if fixture.get("kind", "fmv") == "fmv"
    ]


def _conformance_bundle() -> dict[str, Any]:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return next(
        fixture
        for fixture in document["fixtures"]
        if fixture.get("kind") == "conformance-bundle"
    )


def _fixture_path(fixture: dict[str, Any]) -> Path:
    return FIXTURE_DIRECTORY / fixture.get("path", fixture["filename"])


def _ffmpeg_fixture_cases() -> list[dict[str, Any]]:
    return [fixture for fixture in _fixture_cases() if fixture["id"].startswith("ffmpeg-")]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@pytest.mark.integration
def test_independent_negative_conformance_corpus(tmp_path: Path) -> None:
    fixture = _conformance_bundle()
    source = _fixture_path(fixture)
    if not source.is_file():
        pytest.skip(
            "run scripts/fetch_public_fixtures.py "
            "impleotv-stinspector-negative-corpus to install the fixture"
        )

    assert source.stat().st_size == fixture["size"]
    assert _sha256(source) == fixture["sha256"]
    with zipfile.ZipFile(source) as bundle:
        archive_names = set(bundle.namelist())
        for member in fixture["members"]:
            assert member["filename"] in archive_names
            contents = bundle.read(member["filename"])
            assert len(contents) == member["size"]
            assert hashlib.sha256(contents).hexdigest() == member["sha256"]
            extracted = tmp_path / member["filename"]
            extracted.write_bytes(contents)
            report = verify_fmv_file(
                extracted,
                require_security=False,
                require_miis=False,
                validate_mismms=False,
            )
            assert any(
                finding.status.value == member["expected_status"]
                and finding.code == member["expected_code"]
                and member["expected_message"] in finding.message
                for finding in report.findings
            ), member["filename"]

        asserted_names = {member["filename"] for member in fixture["members"]}
        assert asserted_names | set(fixture["unasserted_members"]) == archive_names


def _essence_fingerprint(path: Path) -> tuple[int, str, tuple[bytes, ...], int]:
    demuxer = TransportDemuxer()
    decoder = MetadataStreamDecoder(field_decoding=FieldDecodingMode.PRESERVE)
    video_count = 0
    video_digest = hashlib.sha256()
    metadata: list[bytes] = []
    pcr_count = 0

    def consume(event: object) -> None:
        nonlocal pcr_count, video_count
        if isinstance(event, ProgramClockEvent):
            pcr_count += 1
            return
        if not isinstance(event, PESStreamEvent):
            return
        if event.kind is StreamKind.VIDEO:
            video_count += 1
            video_digest.update(event.pes.raw)
        elif event.kind is StreamKind.KLV:
            metadata.extend(bytes(item.packet) for item in decoder.feed(event))

    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            for event in demuxer.feed(chunk):
                consume(event)
    for event in demuxer.finish():
        consume(event)
    decoder.finish()
    return video_count, video_digest.hexdigest(), tuple(metadata), pcr_count


@pytest.mark.integration
@pytest.mark.parametrize("fixture", _ffmpeg_fixture_cases(), ids=lambda item: item["id"])
def test_public_fmv_fixture_is_demuxed_and_diagnosed_losslessly(
    fixture: dict[str, Any],
) -> None:
    path = _fixture_path(fixture)
    if not path.is_file():
        pytest.skip("run scripts/fetch_public_fixtures.py to install public FMV fixtures")

    assert path.stat().st_size == fixture["size"]
    assert _sha256(path) == fixture["sha256"]

    demuxer = TransportDemuxer()
    decoder = MetadataStreamDecoder(field_decoding=FieldDecodingMode.PRESERVE)
    metadata = []
    video_pids: set[int] = set()
    klv_pids: set[int] = set()
    pcr_count = 0
    pcr_cadence = PCRCadenceValidator()
    pcr_issues: list[PCRCadenceIssue] = []
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            for event in demuxer.feed(chunk):
                if isinstance(event, ProgramClockEvent):
                    pcr_count += 1
                    pcr_issues.extend(pcr_cadence.observe(event))
                    continue
                if not isinstance(event, PESStreamEvent):
                    continue
                if event.kind is StreamKind.VIDEO:
                    video_pids.add(event.pid)
                elif event.kind is StreamKind.KLV:
                    klv_pids.add(event.pid)
                    metadata.extend(decoder.feed(event))
    for event in demuxer.finish():
        if isinstance(event, ProgramClockEvent):
            pcr_count += 1
            pcr_issues.extend(pcr_cadence.observe(event))
        elif isinstance(event, PESStreamEvent) and event.kind is StreamKind.KLV:
            klv_pids.add(event.pid)
            metadata.extend(decoder.feed(event))
    decoder.finish()

    assert video_pids == {481}
    assert klv_pids == {497}
    assert pcr_count == fixture["expected_pcr_count"]
    assert pcr_issues == []
    assert len(metadata) == fixture["expected_uas_packets"]
    assert all(isinstance(event.decoded, UASLocalSet) for event in metadata)
    assert all(event.decoded.value(65) == 1 for event in metadata)
    assert all([issue.tag for issue in event.decoded.issues] == [22] for event in metadata)


@pytest.mark.integration
@pytest.mark.parametrize("fixture", _fixture_cases(), ids=lambda item: item["id"])
def test_public_fmv_rate_shaper_without_anchor_is_byte_exact(
    fixture: dict[str, Any],
) -> None:
    path = _fixture_path(fixture)
    if not path.is_file():
        pytest.skip("run scripts/fetch_public_fixtures.py to install public FMV fixtures")

    digest = hashlib.sha256()
    packet_count = 0
    shaper = TransportRateShaper(bit_rate=8_000_000)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            for scheduled in shaper.feed(chunk):
                assert scheduled.source
                digest.update(scheduled.packet)
                packet_count += 1
    shaper.finish()

    assert packet_count * 188 == fixture["size"]
    assert digest.hexdigest() == fixture["sha256"]


@pytest.mark.integration
@pytest.mark.parametrize("fixture", _ffmpeg_fixture_cases(), ids=lambda item: item["id"])
def test_ffmpeg_fmv_fixtures_build_geospatial_player_assets(
    fixture: dict[str, Any], tmp_path: Path
) -> None:
    source = _fixture_path(fixture)
    if not source.is_file():
        pytest.skip("run scripts/fetch_public_fixtures.py to install public FMV fixtures")

    assets = prepare_player_assets(source, tmp_path / fixture["id"])
    timeline = json.loads(assets.timeline.read_text(encoding="utf-8"))

    assert assets.media.stat().st_size > 0
    assert len(timeline["samples"]) == fixture["expected_uas_packets"]
    assert all(sample["geospatial"] for sample in timeline["samples"])
    required_fields = {
        "Precision Time Stamp",
        "Sensor Latitude",
        "Sensor Longitude",
        "Sensor True Altitude",
        "Platform Heading Angle",
        "Platform Pitch Angle",
        "Platform Roll Angle",
        "Sensor Relative Azimuth Angle",
        "Sensor Relative Elevation Angle",
        "Sensor Relative Roll Angle",
        "Frame Center Latitude",
        "Frame Center Longitude",
        "Frame Center Elevation",
        "Target Location Latitude",
        "Target Location Longitude",
        "Target Location Elevation",
    }
    for sample in timeline["samples"]:
        assert required_fields <= sample["fields"].keys()
        assert sample["fields"]["Precision Time Stamp"]["time_scale"] == "MISP"
        assert {
            feature["properties"]["role"] for feature in sample["geospatial"]
        } == {"sensor", "frame_center", "target"}
        assert -90.0 <= sample["fields"]["Sensor Latitude"]["value"] <= 90.0
        assert -180.0 <= sample["fields"]["Sensor Longitude"]["value"] <= 180.0
        assert -90.0 <= sample["fields"]["Target Location Latitude"]["value"] <= 90.0
        assert -180.0 <= sample["fields"]["Target Location Longitude"]["value"] <= 180.0
    sample_times = [sample["time_seconds"] for sample in timeline["samples"]]
    assert sample_times == sorted(sample_times)
    assert sample_times[0] >= 0.0


@pytest.mark.integration
def test_day_flight_noop_transform_preserves_video_pes_and_klv(tmp_path: Path) -> None:
    fixture = next(item for item in _fixture_cases() if item["id"] == "ffmpeg-day-flight")
    source = _fixture_path(fixture)
    if not source.is_file():
        pytest.skip("run scripts/fetch_public_fixtures.py to install public FMV fixtures")

    transformed = tmp_path / "day-flight-noop.ts"
    transformer = LiveTransportTransformer(field_decoding=FieldDecodingMode.PRESERVE)
    clock_count = 0
    with source.open("rb") as input_stream, transformed.open("wb") as output_stream:
        for chunk in iter(lambda: input_stream.read(1024 * 1024), b""):
            batch = transformer.feed(chunk)
            output_stream.write(batch.transport)
            clock_count += len(batch.clocks)
        output_stream.write(transformer.finish().transport)

    assert clock_count == fixture["expected_pcr_count"]
    source_fingerprint = _essence_fingerprint(source)
    transformed_fingerprint = _essence_fingerprint(transformed)
    assert transformed_fingerprint == source_fingerprint


@pytest.mark.integration
def test_day_flight_verifier_reports_real_strengths_and_defects() -> None:
    fixture = next(item for item in _fixture_cases() if item["id"] == "ffmpeg-day-flight")
    source = _fixture_path(fixture)
    if not source.is_file():
        pytest.skip("run scripts/fetch_public_fixtures.py to install public FMV fixtures")

    report = verify_fmv_file(source)

    assert not report.ok
    assert report.st0601_packets == fixture["expected_uas_packets"]
    assert {(stream.kind, stream.pid) for stream in report.streams} == {
        ("video", 481),
        ("klv", 497),
    }
    assert any(
        finding.code == "st0601.field"
        and finding.tags == (22,)
        and finding.count == fixture["expected_uas_packets"]
        for finding in report.errors
    )
    assert not any(finding.code.startswith("st1402.pcr.") for finding in report.errors)
    assert {
        finding.code
        for finding in report.findings
        if finding.status is VerificationStatus.PASS
    } >= {"transport.structure", "transport.continuity", "fmv.video", "fmv.klv"}


@pytest.mark.integration
def test_esri_truck_exercises_h264_aac_and_synchronous_klva() -> None:
    fixture = next(item for item in _fixture_cases() if item["id"] == "esri-truck")
    source = _fixture_path(fixture)
    if not source.is_file():
        pytest.skip("run scripts/fetch_public_fixtures.py esri-truck to install the fixture")

    assert source.stat().st_size == fixture["size"]
    assert _sha256(source) == fixture["sha256"]

    report = verify_fmv_file(source)

    assert report.st0601_packets == fixture["expected_uas_packets"]
    assert {(stream.kind, stream.pid) for stream in report.streams} == {
        ("video", 256),
        ("audio", 257),
        ("klv", 258),
    }
    assert any(
        finding.code == "st1402.metadata.metadata_std_descriptor_count"
        for finding in report.errors
    )
    assert any(finding.code == "metadata.decode" for finding in report.errors)


@pytest.mark.integration
def test_esri_truck_aac_demuxes_timestamps_and_decodes_to_pcm() -> None:
    pytest.importorskip("av")
    fixture = next(item for item in _fixture_cases() if item["id"] == "esri-truck")
    source = _fixture_path(fixture)
    if not source.is_file():
        pytest.skip("run scripts/fetch_public_fixtures.py esri-truck to install the fixture")

    demuxer = TransportDemuxer()
    parser = AudioPESFrameParser()
    decoder = None
    compressed_frames = 0
    decoded_frames = 0
    decoded_samples = 0

    def consume(event: object) -> None:
        nonlocal compressed_frames, decoded_frames, decoded_samples, decoder
        if not isinstance(event, PESStreamEvent) or event.kind is not StreamKind.AUDIO:
            return
        if decoder is None:
            assert event.audio_codec is not None
            decoder = PyAVAudioDecoder(event.audio_codec)
        for timed in parser.feed(event):
            assert timed.presentation_seconds is not None
            compressed_frames += 1
            for frame in decoder.decode(timed.frame):
                assert frame.sample_rate == fixture["expected_audio_sample_rate"]
                assert len(frame.layout.channels) == fixture["expected_audio_channels"]
                decoded_frames += 1
                decoded_samples += frame.samples

    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            for event in demuxer.feed(chunk):
                consume(event)
    for event in demuxer.finish():
        consume(event)

    assert parser.finish() == []
    assert decoder is not None
    delayed = decoder.flush()
    assert delayed == ()
    assert compressed_frames == fixture["expected_audio_frames"]
    assert decoded_frames == compressed_frames
    assert decoded_samples == fixture["expected_audio_samples"]


@pytest.mark.integration
def test_esri_truck_builds_synchronized_reference_player_assets(tmp_path: Path) -> None:
    fixture = next(item for item in _fixture_cases() if item["id"] == "esri-truck")
    source = _fixture_path(fixture)
    if not source.is_file():
        pytest.skip("run scripts/fetch_public_fixtures.py esri-truck to install the fixture")
    if shutil.which("ffmpeg") is None:
        pytest.skip("FFmpeg is required for the reference-player acceptance test")

    assets = prepare_player_assets(source, tmp_path / "player")
    timeline = json.loads(assets.timeline.read_text(encoding="utf-8"))

    assert assets.media.stat().st_size > 1_000_000
    with assets.media.open("rb") as media_stream:
        assert media_stream.read(12).endswith(b"ftypisom")
    assert (assets.root / "index.html").is_file()
    assert timeline["video_start_pts"] == fixture["expected_video_start_pts"]
    assert timeline["media_start_pts"] == fixture["expected_media_start_pts"]
    assert len(timeline["samples"]) == fixture["expected_player_timeline_samples"]
    assert timeline["samples"][0]["time_seconds"] == pytest.approx(
        fixture["expected_first_player_sample_seconds"]
    )
    assert {
        "Precision Time Stamp",
        "Sensor Latitude",
        "Sensor Longitude",
        "Sensor True Altitude",
        "Frame Center Latitude",
        "Frame Center Longitude",
        "Frame Center Elevation",
    } <= timeline["samples"][0]["fields"].keys()
