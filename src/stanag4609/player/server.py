"""FFmpeg-backed local reference player for STANAG 4609 files."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
import time
import webbrowser
from collections.abc import Callable, Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from threading import Thread
from typing import Any, BinaryIO
from urllib.parse import parse_qs, urlsplit

from stanag4609.player.live import (
    BoundedBroadcast,
    FragmentedMP4Buffer,
    LivePlayerGateway,
)
from stanag4609.player.timeline import (
    MetadataSample,
    MetadataTimeline,
    scan_transport_file,
    summarize_detection_timeline,
)


@dataclass(frozen=True, slots=True)
class PlayerAssets:
    root: Path
    media: Path
    timeline: Path
    metadata: MetadataTimeline


def _iter_timeline_sse(
    timeline: MetadataTimeline,
    *,
    start_seconds: float,
    playback_rate: float = 1.0,
    heartbeat_seconds: float = 5.0,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[bytes]:
    """Yield timed SSE records, including the current state at ``start_seconds``."""
    if not isinstance(timeline, MetadataTimeline):
        raise TypeError("timeline must be MetadataTimeline")
    for name, value in (
        ("start_seconds", start_seconds),
        ("playback_rate", playback_rate),
        ("heartbeat_seconds", heartbeat_seconds),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name} must be a finite number")
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
    if start_seconds < 0:
        raise ValueError("start_seconds must be non-negative")
    if playback_rate <= 0:
        raise ValueError("playback_rate must be positive")
    if heartbeat_seconds <= 0:
        raise ValueError("heartbeat_seconds must be positive")

    current_index: int | None = None
    previous_time = -math.inf
    for index, sample in enumerate(timeline.samples):
        if sample.time_seconds < previous_time:
            raise ValueError("timeline samples must be ordered by time_seconds")
        previous_time = sample.time_seconds
        if sample.time_seconds <= start_seconds:
            current_index = index

    first_index = current_index if current_index is not None else 0
    started = monotonic()
    emitted = 0
    for index in range(first_index, len(timeline.samples)):
        sample = timeline.samples[index]
        deadline = max(0.0, (sample.time_seconds - start_seconds) / playback_rate)
        while True:
            remaining = deadline - (monotonic() - started)
            if remaining <= 0:
                break
            sleep(min(remaining, heartbeat_seconds))
            if deadline - (monotonic() - started) > 1e-9:
                yield b": keep-alive\n\n"
        payload = sample.to_json().encode("utf-8")
        yield b"event: sample\nid: " + str(index).encode("ascii") + b"\ndata: " + payload + b"\n\n"
        emitted += 1
    yield b'event: end\ndata: {"samples":' + str(emitted).encode("ascii") + b"}\n\n"


def _parse_byte_range(value: str, size: int) -> tuple[int, int]:
    if not value.startswith("bytes=") or "," in value:
        raise ValueError("only one bytes range is supported")
    start_text, separator, end_text = value[6:].partition("-")
    if not separator or (not start_text and not end_text) or size <= 0:
        raise ValueError("invalid or unsatisfiable bytes range")
    try:
        if not start_text:
            suffix_length = int(end_text)
            if suffix_length <= 0:
                raise ValueError
            return max(0, size - suffix_length), size - 1
        start = int(start_text)
        end = size - 1 if not end_text else min(int(end_text), size - 1)
    except ValueError as error:
        raise ValueError("invalid or unsatisfiable bytes range") from error
    if start < 0 or start >= size or end < start:
        raise ValueError("invalid or unsatisfiable bytes range")
    return start, end


class PlayerHTTPRequestHandler(SimpleHTTPRequestHandler):
    """Static-file handler with browser-friendly single-range responses."""

    _byte_range: tuple[int, int] | None = None

    def __init__(
        self,
        *args: Any,
        timeline: MetadataTimeline | None = None,
        live_media: FragmentedMP4Buffer | None = None,
        live_metadata: BoundedBroadcast[MetadataSample] | None = None,
        **kwargs: Any,
    ) -> None:
        self.timeline = timeline
        self.live_media = live_media
        self.live_metadata = live_metadata
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        """Serve static assets or a media-timed metadata event stream."""
        request = urlsplit(self.path)
        if request.path == "/media/init.mp4":
            self._serve_live_initialization(request.query)
            return
        if request.path == "/media/fragment":
            self._serve_live_fragment(request.query)
            return
        if request.path == "/metadata/live":
            self._serve_live_metadata(request.query)
            return
        if request.path == "/metadata/summary":
            self._serve_metadata_summary(request.query)
            return
        if request.path != "/metadata/events":
            super().do_GET()
            return
        if self.timeline is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Metadata timeline unavailable")
            return
        query = parse_qs(request.query, keep_blank_values=True)
        try:
            start = self._query_float(query, "start", default=0.0)
            rate = self._query_float(query, "rate", default=1.0)
            if start < 0:
                raise ValueError("start must be non-negative")
            if not 0.1 <= rate <= 16:
                raise ValueError("rate must be between 0.1 and 16")
        except ValueError as error:
            self.send_error(HTTPStatus.BAD_REQUEST, str(error))
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            for chunk in _iter_timeline_sse(
                self.timeline,
                start_seconds=start,
                playback_rate=rate,
            ):
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def _serve_metadata_summary(self, query_string: str) -> None:
        if self.timeline is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Metadata timeline unavailable")
            return
        query = parse_qs(query_string, keep_blank_values=True)
        try:
            bin_count = self._query_int(query, "bins", default=2048)
            duration = (
                None
                if "duration" not in query
                else self._query_float(query, "duration", default=0.0)
            )
            summary = summarize_detection_timeline(
                self.timeline,
                bin_count=bin_count,
                duration_seconds=duration,
            )
        except (TypeError, ValueError) as error:
            self.send_error(HTTPStatus.BAD_REQUEST, str(error))
            return
        body = summary.to_json().encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=60")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _serve_live_initialization(self, query_string: str) -> None:
        if self.live_media is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Live media unavailable")
            return
        try:
            query = parse_qs(query_string, keep_blank_values=True)
            wait = self._query_float(query, "wait", default=10.0)
            if not 0 <= wait <= 30:
                raise ValueError("wait must be between 0 and 30")
            initialization = self.live_media.initialization(timeout=wait)
        except ValueError as error:
            self.send_error(HTTPStatus.BAD_REQUEST, str(error))
            return
        except TimeoutError as error:
            status = (
                HTTPStatus.GONE
                if self.live_media.closed
                else HTTPStatus.SERVICE_UNAVAILABLE
            )
            self.send_error(status, str(error))
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", initialization.mime_type)
        self.send_header("Content-Length", str(len(initialization.data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-First-Fragment-ID", str(initialization.first_fragment_id))
        self.end_headers()
        try:
            self.wfile.write(initialization.data)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _serve_live_fragment(self, query_string: str) -> None:
        if self.live_media is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Live media unavailable")
            return
        query = parse_qs(query_string, keep_blank_values=True)
        try:
            after = self._query_int(query, "after")
            wait = self._query_float(query, "wait", default=10.0)
            if after < -1:
                raise ValueError("after must be at least -1")
            if not 0 <= wait <= 30:
                raise ValueError("wait must be between 0 and 30")
            result = self.live_media.poll(after_id=after, timeout=wait)
        except (TypeError, ValueError) as error:
            self.send_error(HTTPStatus.BAD_REQUEST, str(error))
            return
        if result.dropped:
            self.send_response(HTTPStatus.CONFLICT)
            self.send_header("Content-Type", "application/json")
            body = (
                '{"error":"fragment history unavailable","dropped":'
                f"{result.dropped}}}"
            ).encode("ascii")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if not result.items:
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Cache-Control", "no-store")
            if result.closed:
                self.send_header("X-Stream-End", "true")
            if result.error:
                self.send_header("X-Stream-Error", "true")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        fragment_id, fragment = result.items[0]
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "video/iso.segment")
        self.send_header("Content-Length", str(len(fragment)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Fragment-ID", str(fragment_id))
        self.end_headers()
        try:
            self.wfile.write(fragment)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _serve_live_metadata(self, query_string: str) -> None:
        if self.live_metadata is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Live metadata unavailable")
            return
        query = parse_qs(query_string, keep_blank_values=True)
        try:
            after = self._query_int(query, "after", default=-1)
            last_event_id = self.headers.get("Last-Event-ID")
            if last_event_id is not None:
                try:
                    after = max(after, int(last_event_id))
                except ValueError as error:
                    raise ValueError("Last-Event-ID must be an integer") from error
            if after < -1:
                raise ValueError("after must be at least -1")
        except (TypeError, ValueError) as error:
            self.send_error(HTTPStatus.BAD_REQUEST, str(error))
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            while True:
                result = self.live_metadata.poll(after_id=after, timeout=5.0)
                if result.dropped:
                    reset = (
                        '{"dropped":'
                        f"{result.dropped},\"oldest_id\":"
                        f"{result.items[0][0] if result.items else after + result.dropped + 1}}}"
                    )
                    self.wfile.write(b"event: reset\ndata: " + reset.encode("ascii") + b"\n\n")
                for event_id, sample in result.items:
                    self.wfile.write(
                        b"event: sample\nid: "
                        + str(event_id).encode("ascii")
                        + b"\ndata: "
                        + sample.to_json().encode("utf-8")
                        + b"\n\n"
                    )
                    after = event_id
                if result.error:
                    self.wfile.write(
                        b"event: error\ndata: "
                        + json.dumps({"message": result.error}).encode("utf-8")
                        + b"\n\n"
                    )
                if result.closed:
                    self.wfile.write(b'event: end\ndata: {"live":true}\n\n')
                    self.wfile.flush()
                    return
                if not result.items:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    @staticmethod
    def _query_float(
        query: dict[str, list[str]],
        name: str,
        *,
        default: float,
    ) -> float:
        values = query.get(name)
        if values is None:
            return default
        if len(values) != 1 or not values[0]:
            raise ValueError(f"{name} must occur once with a numeric value")
        try:
            value = float(values[0])
        except ValueError as error:
            raise ValueError(f"{name} must be numeric") from error
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
        return value

    @staticmethod
    def _query_int(
        query: dict[str, list[str]],
        name: str,
        *,
        default: int | None = None,
    ) -> int:
        values = query.get(name)
        if values is None:
            if default is None:
                raise ValueError(f"{name} is required")
            return default
        if len(values) != 1 or not values[0]:
            raise ValueError(f"{name} must occur once with an integer value")
        try:
            return int(values[0])
        except ValueError as error:
            raise ValueError(f"{name} must be an integer") from error

    def end_headers(self) -> None:
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def send_head(self) -> BinaryIO | None:
        self._byte_range = None
        range_header = self.headers.get("Range")
        path = Path(self.translate_path(self.path))
        if range_header is None or not path.is_file():
            return super().send_head()
        try:
            source = path.open("rb")
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return None
        size = path.stat().st_size
        try:
            start, end = _parse_byte_range(range_header, size)
        except ValueError:
            source.close()
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        self._byte_range = (start, end)
        self.send_response(HTTPStatus.PARTIAL_CONTENT)
        self.send_header("Content-Type", self.guess_type(str(path)))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Last-Modified", self.date_time_string(path.stat().st_mtime))
        self.end_headers()
        return source

    def copyfile(self, source: Any, outputfile: Any) -> None:
        try:
            if self._byte_range is None:
                super().copyfile(source, outputfile)
                return
            start, end = self._byte_range
            source.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = source.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                outputfile.write(chunk)
                remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # Browsers routinely abandon an in-flight media response when
            # seeking or closing a tab. This is normal client behavior.
            return


def ffmpeg_player_command(
    source: Path,
    destination: Path,
    *,
    ffmpeg: str = "ffmpeg",
) -> tuple[str, ...]:
    """Return the deterministic browser-compatible reference transcode command."""
    return (
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-map_metadata",
        "-1",
        "-sn",
        "-dn",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(destination),
    )


def prepare_player_assets(
    source: str | Path,
    destination: str | Path,
    *,
    ffmpeg: str = "ffmpeg",
) -> PlayerAssets:
    """Decode metadata and transcode one source into self-contained web assets."""
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    root = Path(destination).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    media = root / "media.mp4"
    timeline_path = root / "timeline.json"
    metadata = scan_transport_file(source_path)
    timeline_path.write_text(metadata.to_json(), encoding="utf-8")
    static = files("stanag4609.player").joinpath("static/index.html")
    (root / "index.html").write_text(static.read_text(encoding="utf-8"), encoding="utf-8")
    try:
        subprocess.run(
            ffmpeg_player_command(source_path, media, ffmpeg=ffmpeg),
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise RuntimeError(f"FFmpeg executable not found: {ffmpeg}") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "").strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise RuntimeError(f"FFmpeg could not transcode {source_path}{suffix}") from error
    return PlayerAssets(root, media, timeline_path, metadata)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Play STANAG 4609 video with synchronized MISB data"
    )
    parser.add_argument(
        "source",
        type=Path,
        help="MPEG-2 transport-stream input; use - for stdin with --live",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--ffmpeg", default="ffmpeg", help="FFmpeg executable")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Incrementally ingest a live/growing TS input and stream fragmented MP4",
    )
    parser.add_argument(
        "--live-chunk-size",
        type=int,
        default=1316,
        help="Bytes read from a live file/stdin per feed (default: seven TS packets)",
    )
    parser.add_argument(
        "--live-media-fragments",
        type=int,
        default=12,
        help="Complete one-second browser fragments retained for late clients",
    )
    parser.add_argument(
        "--live-metadata-samples",
        type=int,
        default=512,
        help="Metadata samples retained for live clients",
    )
    parser.add_argument(
        "--program-number",
        type=int,
        help="MPEG-TS program to play; required for ambiguous live MPTS input",
    )
    parser.add_argument(
        "--stream-metadata",
        action="store_true",
        help="Replay metadata incrementally to the browser with Server-Sent Events",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open the browser automatically",
    )
    return parser


def _copy_player_ui(root: Path) -> None:
    static = files("stanag4609.player").joinpath("static/index.html")
    (root / "index.html").write_text(static.read_text(encoding="utf-8"), encoding="utf-8")


def _ingest_live_source(
    source: Path,
    gateway: LivePlayerGateway,
    *,
    chunk_size: int,
) -> None:
    stream: BinaryIO
    owned = str(source) != "-"
    try:
        stream = source.expanduser().open("rb") if owned else sys.stdin.buffer
        try:
            while chunk := stream.read(chunk_size):
                gateway.feed(chunk)
            gateway.finish()
        finally:
            if owned:
                stream.close()
    except Exception as error:
        gateway.close()
        print(f"Live player input stopped: {error}", file=sys.stderr, flush=True)


def _run_live_player(args: argparse.Namespace, root: Path) -> int:
    if args.live_chunk_size < 188:
        raise SystemExit("--live-chunk-size must be at least 188 bytes")
    if args.live_media_fragments < 2:
        raise SystemExit("--live-media-fragments must be at least 2")
    if args.live_metadata_samples < 1:
        raise SystemExit("--live-metadata-samples must be positive")
    source = args.source.expanduser()
    if str(source) != "-" and not source.is_file():
        raise SystemExit(f"live input does not exist: {source}")
    _copy_player_ui(root)
    gateway = LivePlayerGateway(
        ffmpeg=args.ffmpeg,
        max_metadata_samples=args.live_metadata_samples,
        max_media_fragments=args.live_media_fragments,
        max_input_chunk_bytes=args.live_chunk_size,
        program_number=args.program_number,
    )
    gateway.start()
    handler = partial(
        PlayerHTTPRequestHandler,
        directory=str(root),
        live_media=gateway.media,
        live_metadata=gateway.metadata,
    )
    with ThreadingHTTPServer((args.host, args.port), handler) as server:
        url = f"http://{args.host}:{server.server_port}/?live=1"
        print(f"STANAG 4609 live player: {url}")
        print("Reading bounded live MPEG-TS input; stop with Ctrl-C.", flush=True)
        if not args.no_open:
            webbrowser.open(url)
        ingestion = Thread(
            target=_ingest_live_source,
            args=(source, gateway),
            kwargs={"chunk_size": args.live_chunk_size},
            name="stanag4609-live-input",
            daemon=True,
        )
        ingestion.start()
        with suppress(KeyboardInterrupt):
            server.serve_forever()
        gateway.close()
        ingestion.join(timeout=2)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")
    if args.program_number is not None:
        if not 1 <= args.program_number <= 0xFFFF:
            raise SystemExit("--program-number must be between 1 and 65535")
        if not args.live:
            raise SystemExit("--program-number currently requires --live")
    if shutil.which(args.ffmpeg) is None:
        raise SystemExit(f"FFmpeg executable not found: {args.ffmpeg}")
    with tempfile.TemporaryDirectory(prefix="stanag4609-player-") as temporary:
        root = Path(temporary)
        if args.live:
            return _run_live_player(args, root)
        print("Preparing synchronized metadata and browser media...", flush=True)
        assets = prepare_player_assets(args.source, root, ffmpeg=args.ffmpeg)
        handler = partial(
            PlayerHTTPRequestHandler,
            directory=str(assets.root),
            timeline=assets.metadata,
        )
        with ThreadingHTTPServer((args.host, args.port), handler) as server:
            suffix = "?metadata=sse" if args.stream_metadata else ""
            url = f"http://{args.host}:{server.server_port}/{suffix}"
            print(f"STANAG 4609 player: {url}")
            if not args.no_open:
                webbrowser.open(url)
            with suppress(KeyboardInterrupt):
                server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
