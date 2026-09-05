from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest

from stanag4609.csvmux import (
    ffmpeg_transport_command,
    inject_esri_csv_metadata,
    multiplex_esri_fmv,
)
from stanag4609.st0601 import UASLocalSet
from stanag4609.transport.demux import PESStreamEvent, PMTEvent, StreamKind, TransportDemuxer
from stanag4609.transport.metadata_stream import MetadataStreamDecoder
from stanag4609.transport.mux import TransportMuxer, encode_pes_packet
from stanag4609.transport.psi import ElementaryStreamInfo, KLVCarriage, find_klv_streams

_HEADER = (
    "TimeStamp,LDSVer,PlatformHeading,PlatformPitch,PlatformRoll,"
    "PlatformTrueAirSpeed,SensorLatitude,SensorLongitude,SensorAltitude,"
    "HorizontalFOV,VerticalFOV,SensorRelativeAzimuth,SensorRelativeElevation,"
    "SensorRelativeRoll\n"
)


def _csv(*timestamps: int) -> io.StringIO:
    rows = [
        f"{timestamp},19,90,1,-1,120,{40 + index},{-75 - index},1000,"
        "20,10,5,-2,0"
        for index, timestamp in enumerate(timestamps)
    ]
    return io.StringIO(_HEADER + "\n".join(rows) + "\n")


def _media_transport() -> bytes:
    muxer = TransportMuxer(
        transport_stream_id=9,
        program_number=3,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=(
            ElementaryStreamInfo(0x02, 0x101, ()),
            ElementaryStreamInfo(0x03, 0x102, ()),
        ),
    )
    return (
        b"".join(muxer.program_tables())
        + b"".join(muxer.mux_pes(0x101, encode_pes_packet(b"v1", stream_id=0xE0, pts=90_000)))
        + b"".join(muxer.mux_pes(0x102, encode_pes_packet(b"a1", stream_id=0xC0, pts=90_000)))
        + b"".join(muxer.program_tables())
        + b"".join(
            muxer.mux_pes(0x101, encode_pes_packet(b"v2", stream_id=0xE0, pts=180_000))
        )
        + b"".join(
            muxer.mux_pes(0x101, encode_pes_packet(b"v3", stream_id=0xE0, pts=270_000))
        )
    )


def _events(data: bytes) -> list[object]:
    demuxer = TransportDemuxer()
    return [*demuxer.feed(data), *demuxer.finish()]


def test_inject_esri_csv_adds_timed_klv_and_preserves_media(tmp_path: Path) -> None:
    source = tmp_path / "media.ts"
    destination = tmp_path / "fmv.ts"
    source.write_bytes(_media_transport())

    result = inject_esri_csv_metadata(
        source,
        _csv(1_700_000_000_000_000, 1_700_000_000_500_000, 1_700_000_001_500_000),
        destination,
        chunk_size=188,
    )

    assert result.destination == destination
    assert result.records_written == 3
    assert result.video_start_pts == 90_000
    assert result.first_metadata_pts == 90_000
    assert result.last_metadata_pts == 225_000

    events = _events(destination.read_bytes())
    pmt_events = [event for event in events if isinstance(event, PMTEvent)]
    assert len(pmt_events) == 2
    assert dict(find_klv_streams(pmt_events[0].table)) == {
        0x120: KLVCarriage.SYNCHRONOUS
    }
    streams = [event for event in events if isinstance(event, PESStreamEvent)]
    assert [event.pes.payload for event in streams if event.kind is StreamKind.VIDEO] == [
        b"v1",
        b"v2",
        b"v3",
    ]
    assert [event.pes.payload for event in streams if event.kind is StreamKind.AUDIO] == [b"a1"]

    decoder = MetadataStreamDecoder()
    metadata = [
        item
        for event in streams
        if event.kind is StreamKind.KLV
        for item in decoder.feed(event)
    ]
    decoder.finish()
    assert [item.pts for item in metadata] == [90_000, 135_000, 225_000]
    assert all(isinstance(item.decoded, UASLocalSet) for item in metadata)
    assert [item.decoded.value(13) for item in metadata] == pytest.approx([40, 41, 42])


def test_inject_esri_csv_is_atomic_on_invalid_timeline(tmp_path: Path) -> None:
    source = tmp_path / "media.ts"
    destination = tmp_path / "fmv.ts"
    source.write_bytes(_media_transport())

    with pytest.raises(ValueError, match="precedes"):
        inject_esri_csv_metadata(source, _csv(2_000_000, 1_000_000), destination)
    assert not destination.exists()

    destination.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        inject_esri_csv_metadata(source, _csv(1), destination)
    assert destination.read_bytes() == b"keep"


def test_ffmpeg_transport_command_and_multiplex_wrapper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "input.mpeg"
    destination = tmp_path / "fmv.ts"
    source.write_bytes(b"program stream")
    command = ffmpeg_transport_command(source, destination, ffmpeg="ffmpeg-test")
    assert command[0] == "ffmpeg-test"
    assert command[-1] == str(destination)
    assert command[command.index("-map") : command.index("-map") + 2] == (
        "-map",
        "0:v:0",
    )
    assert "0:a?" in command
    assert command[command.index("-c") + 1] == "copy"
    assert command[command.index("-pcr_period") + 1] == "20"

    def fake_run(
        invoked: tuple[str, ...],
        *,
        check: bool,
        stdout: int,
        stderr: int,
    ) -> subprocess.CompletedProcess[bytes]:
        assert check
        assert stdout == subprocess.DEVNULL
        assert stderr == subprocess.PIPE
        Path(invoked[-1]).write_bytes(_media_transport())
        return subprocess.CompletedProcess(invoked, 0, stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = multiplex_esri_fmv(
        source,
        _csv(1_000_000, 1_500_000),
        destination,
        ffmpeg="ffmpeg-test",
    )
    assert result.records_written == 2
    assert destination.is_file()


def test_multiplex_wrapper_reports_missing_ffmpeg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "input.mpeg"
    source.write_bytes(b"program stream")

    def missing(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("ffmpeg-missing")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(RuntimeError, match="FFmpeg executable not found"):
        multiplex_esri_fmv(source, _csv(1), tmp_path / "fmv.ts", ffmpeg="missing")
