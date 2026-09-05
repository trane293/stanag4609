"""Create STANAG-style MPEG-TS files from video and timestamped metadata CSV."""

from __future__ import annotations

import argparse
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TextIO

from stanag4609.csvio import CsvMetadataRecord, iter_esri_metadata_csv
from stanag4609.errors import DecodeError, LimitExceeded
from stanag4609.transport.demux import PESStreamEvent, StreamKind, TransportDemuxer
from stanag4609.transport.metadata import synchronous_klv_stream
from stanag4609.transport.processor import TimedKLVPacket
from stanag4609.transport.psi import KLVCarriage
from stanag4609.transport.timing import PTS_CLOCK_RATE, PTS_MODULUS, PTSTimeline
from stanag4609.transport.transformer import LiveTransportTransformer


class _BinaryWriter(Protocol):
    def write(self, data: bytes, /) -> int: ...


@dataclass(frozen=True, slots=True)
class CsvMuxResult:
    """Summary of one completed timestamped-CSV multiplex operation."""

    destination: Path
    records_written: int
    video_start_pts: int
    first_metadata_pts: int
    last_metadata_pts: int


def ffmpeg_transport_command(
    source: str | Path,
    destination: str | Path,
    *,
    ffmpeg: str = "ffmpeg",
) -> tuple[str, ...]:
    """Build the lossless media-remux command used before KLV injection."""

    return (
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(Path(source)),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c",
        "copy",
        "-mpegts_flags",
        "+resend_headers",
        "-pcr_period",
        "20",
        "-f",
        "mpegts",
        str(Path(destination)),
    )


def _validated_records(
    source: str | Path | TextIO,
    *,
    max_records: int,
) -> Iterable[CsvMetadataRecord]:
    if isinstance(max_records, bool) or not isinstance(max_records, int) or max_records < 1:
        raise ValueError("max_records must be a positive integer")
    previous_timestamp: int | None = None
    for count, record in enumerate(iter_esri_metadata_csv(source), start=1):
        if count > max_records:
            raise LimitExceeded(f"metadata CSV exceeds {max_records} records")
        timestamp = record.timestamp_microseconds
        if previous_timestamp is not None and timestamp < previous_timestamp:
            raise ValueError(
                f"CSV row {record.row_number} timestamp {timestamp} precedes "
                f"the previous timestamp {previous_timestamp}"
            )
        previous_timestamp = timestamp
        yield record


def _metadata_pts(base_pts: int, timestamp: int, first_timestamp: int) -> int:
    delta_microseconds = timestamp - first_timestamp
    delta_ticks = (delta_microseconds * PTS_CLOCK_RATE + 500_000) // 1_000_000
    return (base_pts + delta_ticks) % PTS_MODULUS


def inject_esri_csv_metadata(
    source_transport: str | Path,
    metadata_csv: str | Path | TextIO,
    destination: str | Path,
    *,
    metadata_pid: int = 0x120,
    metadata_service_id: int = 0,
    metadata_input_leak_rate: int = 1_000,
    metadata_buffer_size: int = 200_000,
    chunk_size: int = 64 * 1024,
    max_records: int = 1_000_000,
    overwrite: bool = False,
) -> CsvMuxResult:
    """Inject ArcGIS FMV Multiplexer-style CSV rows into an MPEG-2 TS file.

    The first CSV timestamp is aligned with the first video PTS. Subsequent
    timestamps are converted to the MPEG 90 kHz clock using integer arithmetic.
    The destination is replaced atomically only after complete validation.
    """

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

    records = iter(_validated_records(metadata_csv, max_records=max_records))
    pending = next(records, None)
    if pending is None:
        raise ValueError("metadata CSV has no data rows")
    first_timestamp = pending.timestamp_microseconds

    stream_info = synchronous_klv_stream(
        metadata_pid,
        metadata_input_leak_rate=metadata_input_leak_rate,
        metadata_buffer_size=metadata_buffer_size,
        metadata_service_id=metadata_service_id,
    )
    transformer = LiveTransportTransformer(additional_metadata_stream=stream_info)
    observer = TransportDemuxer(max_programs=1)
    video_timeline = PTSTimeline()
    video_start_pts: int | None = None
    latest_video_pts: int | None = None
    first_metadata_pts: int | None = None
    last_metadata_pts: int | None = None
    records_written = 0

    def observe_video(events: Iterable[object]) -> None:
        nonlocal video_start_pts, latest_video_pts
        for event in events:
            if not isinstance(event, PESStreamEvent):
                continue
            if event.kind is not StreamKind.VIDEO or event.pes.pts is None:
                continue
            unwrapped = video_timeline.observe(event.pes.pts)
            if video_start_pts is None:
                video_start_pts = unwrapped
            if latest_video_pts is None or unwrapped > latest_video_pts:
                latest_video_pts = unwrapped

    def emit_due(output: _BinaryWriter) -> None:
        nonlocal pending, records_written, first_metadata_pts, last_metadata_pts
        if video_start_pts is None or latest_video_pts is None:
            return
        while pending is not None:
            pts = _metadata_pts(video_start_pts, pending.timestamp_microseconds, first_timestamp)
            unwrapped_pts = video_timeline.near(pts)
            if unwrapped_pts > latest_video_pts:
                break
            program = transformer.program
            if program is None:
                raise DecodeError("video arrived before the active program map")
            event = TimedKLVPacket.from_bytes(
                pending.encode(),
                program_number=program.program_number,
                pid=metadata_pid,
                carriage=KLVCarriage.SYNCHRONOUS,
                pts=pts,
                metadata_service_id=metadata_service_id,
                random_access=True,
            )
            output.write(transformer.emit_metadata(event).transport)
            records_written += 1
            if first_metadata_pts is None:
                first_metadata_pts = pts
            last_metadata_pts = pts
            pending = next(records, None)

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination_path.name}.",
            suffix=".tmp",
            dir=destination_path.parent,
            delete=False,
        ) as output:
            temporary_path = Path(output.name)
            with source_path.open("rb") as source:
                while chunk := source.read(chunk_size):
                    observe_video(observer.feed(chunk))
                    batch = transformer.feed(chunk)
                    output.write(batch.transport)
                    emit_due(output)

            # The observer reveals an unbounded final video PES without closing
            # the transformer, allowing final due metadata to be injected first.
            observe_video(observer.finish())
            emit_due(output)
            if video_start_pts is None:
                raise DecodeError("input transport contains no video PTS")
            if pending is not None:
                raise ValueError(
                    f"CSV row {pending.row_number} occurs after the final video PTS"
                )
            output.write(transformer.finish().transport)
        assert temporary_path is not None
        temporary_path.replace(destination_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    assert video_start_pts is not None
    assert first_metadata_pts is not None
    assert last_metadata_pts is not None
    return CsvMuxResult(
        destination_path,
        records_written,
        video_start_pts % PTS_MODULUS,
        first_metadata_pts,
        last_metadata_pts,
    )


def multiplex_esri_fmv(
    source_video: str | Path,
    metadata_csv: str | Path | TextIO,
    destination: str | Path,
    *,
    ffmpeg: str = "ffmpeg",
    metadata_pid: int = 0x120,
    metadata_service_id: int = 0,
    chunk_size: int = 64 * 1024,
    max_records: int = 1_000_000,
    overwrite: bool = False,
) -> CsvMuxResult:
    """Remux video/audio without transcoding, then add synchronous ST 0601 KLV."""

    source_path = Path(source_video)
    destination_path = Path(destination)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if destination_path.exists() and not overwrite:
        raise FileExistsError(destination_path)
    with tempfile.TemporaryDirectory(prefix="stanag4609-csvmux-") as temporary:
        intermediate = Path(temporary) / "media.ts"
        command = ffmpeg_transport_command(source_path, intermediate, ffmpeg=ffmpeg)
        try:
            subprocess.run(
                command,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as error:
            raise RuntimeError(f"FFmpeg executable not found: {ffmpeg}") from error
        except subprocess.CalledProcessError as error:
            detail = error.stderr.decode("utf-8", errors="replace").strip()
            message = "FFmpeg could not remux the input media"
            raise RuntimeError(f"{message}: {detail}" if detail else message) from error
        return inject_esri_csv_metadata(
            intermediate,
            metadata_csv,
            destination_path,
            metadata_pid=metadata_pid,
            metadata_service_id=metadata_service_id,
            chunk_size=chunk_size,
            max_records=max_records,
            overwrite=overwrite,
        )


def main(argv: list[str] | None = None) -> int:
    """Run the ArcGIS-style video/CSV multiplexer command-line interface."""

    parser = argparse.ArgumentParser(
        description="Remux video/audio and add ESRI-style CSV metadata as ST 0601 KLV"
    )
    parser.add_argument("video", type=Path, help="input video or MPEG program/transport stream")
    parser.add_argument("metadata_csv", type=Path, help="ArcGIS FMV Multiplexer metadata CSV")
    parser.add_argument("destination", type=Path, help="output MPEG-2 transport stream")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="FFmpeg executable")
    parser.add_argument("--metadata-pid", type=lambda value: int(value, 0), default=0x120)
    parser.add_argument("--metadata-service-id", type=int, default=0)
    parser.add_argument("--force", action="store_true", help="replace an existing destination")
    args = parser.parse_args(argv)
    result = multiplex_esri_fmv(
        args.video,
        args.metadata_csv,
        args.destination,
        ffmpeg=args.ffmpeg,
        metadata_pid=args.metadata_pid,
        metadata_service_id=args.metadata_service_id,
        overwrite=args.force,
    )
    print(
        f"wrote {result.records_written} metadata records to {result.destination} "
        f"(PTS {result.first_metadata_pts}..{result.last_metadata_pts})"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
