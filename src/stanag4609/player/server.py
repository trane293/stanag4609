"""FFmpeg-backed local reference player for STANAG 4609 files."""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import secrets
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

from stanag4609.player.benchmark import _source_duration
from stanag4609.player.demo import DEMO_VARIANTS, generate_demo_fmv
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
from stanag4609.player.udp_output import (
    UDPOutputController,
    parse_udp_destination,
)


@dataclass(frozen=True, slots=True)
class PlayerAssets:
    root: Path
    media: Path
    timeline: Path
    metadata: MetadataTimeline


_PLAYER_CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "base-uri 'none'",
        "connect-src 'self'",
        "font-src 'self'",
        "frame-ancestors 'none'",
        "img-src 'self' data: https://tile.openstreetmap.org",
        "media-src 'self' blob:",
        "object-src 'none'",
        "script-src 'self' 'unsafe-inline'",
        "style-src 'self' 'unsafe-inline'",
    )
)


def _normalize_allowed_host(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("allowed host must be a non-empty value without whitespace")
    host = value[1:-1] if value.startswith("[") and value.endswith("]") else value
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        if any(character in host for character in "/@:"):
            raise ValueError(f"invalid allowed host {value!r}") from None
        try:
            normalized = host.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError as error:
            raise ValueError(f"invalid allowed host {value!r}") from error
        if not normalized:
            raise ValueError(f"invalid allowed host {value!r}") from None
        return normalized


def _request_hostname(value: str | None) -> str | None:
    if (
        value is None
        or any(character.isspace() for character in value)
        or any(character in value for character in "/@?#\\")
    ):
        return None
    try:
        parsed = urlsplit(f"//{value}")
        if parsed.hostname is None or parsed.username is not None or parsed.path:
            return None
        if parsed.port is not None and not 1 <= parsed.port <= 65_535:
            return None
        return _normalize_allowed_host(parsed.hostname)
    except ValueError:
        return None


def _is_loopback_host(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return value.rstrip(".").lower() == "localhost"


def _cli_allowed_hosts(args: argparse.Namespace) -> tuple[str, ...]:
    if _is_loopback_host(args.host):
        return tuple(
            dict.fromkeys((_normalize_allowed_host(args.host), "127.0.0.1", "::1", "localhost"))
        )
    if not args.allow_remote:
        raise SystemExit("non-loopback --host requires --allow-remote")
    if not args.allowed_host:
        raise SystemExit("non-loopback --host requires at least one --allowed-host")
    try:
        return tuple(_normalize_allowed_host(value) for value in args.allowed_host)
    except ValueError as error:
        raise SystemExit(str(error)) from error


def _player_url_host(bind_host: str, allowed_hosts: tuple[str, ...]) -> str:
    host = bind_host if _is_loopback_host(bind_host) else allowed_hosts[0]
    normalized = _normalize_allowed_host(host)
    return f"[{normalized}]" if ":" in normalized else normalized


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
        udp_output: UDPOutputController | None = None,
        control_token: str | None = None,
        allowed_hosts: Sequence[str] | None = None,
        **kwargs: Any,
    ) -> None:
        self.timeline = timeline
        self.live_media = live_media
        self.live_metadata = live_metadata
        self.udp_output = udp_output
        self.control_token = control_token
        if (udp_output is None) != (control_token is None):
            raise ValueError("udp_output and control_token must be configured together")
        if isinstance(allowed_hosts, (str, bytes)):
            raise TypeError("allowed_hosts must be a sequence of host names, not a string")
        self.allowed_hosts = (
            None
            if allowed_hosts is None
            else frozenset(_normalize_allowed_host(value) for value in allowed_hosts)
        )
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        """Serve static assets or a media-timed metadata event stream."""
        if self._reject_untrusted_host():
            return
        request = urlsplit(self.path)
        if request.path == "/output/udp":
            status = (
                {"configured": False}
                if self.udp_output is None
                else self.udp_output.status().to_dict() | {"control_token": self.control_token}
            )
            self._send_json(HTTPStatus.OK, status)
            return
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

    def do_POST(self) -> None:
        """Control only the one UDP destination explicitly allowed by the CLI."""
        if self._reject_untrusted_host():
            return
        request = urlsplit(self.path)
        if request.path not in {"/output/udp/start", "/output/udp/stop"}:
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown control endpoint")
            return
        if self.udp_output is None or self.control_token is None:
            self.send_error(HTTPStatus.NOT_FOUND, "UDP output is not configured")
            return
        fetch_site = self.headers.get("Sec-Fetch-Site")
        supplied = self.headers.get("X-STANAG4609-Control", "")
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = -1
        if (
            fetch_site not in {None, "same-origin", "none"}
            or content_length != 0
            or not secrets.compare_digest(supplied, self.control_token)
        ):
            self.send_error(HTTPStatus.FORBIDDEN, "Control request rejected")
            return
        status = (
            self.udp_output.start()
            if request.path.endswith("/start")
            else self.udp_output.stop()
        )
        self._send_json(HTTPStatus.OK, status.to_dict())

    def _send_json(self, status: HTTPStatus, value: object) -> None:
        body = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_HEAD(self) -> None:
        """Apply the same trusted-Host boundary to metadata-bearing assets."""
        if self._reject_untrusted_host():
            return
        super().do_HEAD()

    def _reject_untrusted_host(self) -> bool:
        if self.allowed_hosts is None:
            return False
        hostname = _request_hostname(self.headers.get("Host"))
        if hostname in self.allowed_hosts:
            return False
        self.send_error(HTTPStatus.MISDIRECTED_REQUEST, "Untrusted Host header")
        return True

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
            status = HTTPStatus.GONE if self.live_media.closed else HTTPStatus.SERVICE_UNAVAILABLE
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
            next_fragment_id = self.live_media.next_fragment_id
            if after >= 0 and after >= next_fragment_id:
                body = json.dumps(
                    {
                        "error": "fragment cursor is ahead of the current stream",
                        "next_id": next_fragment_id,
                    },
                    separators=(",", ":"),
                ).encode("ascii")
                self.send_response(HTTPStatus.CONFLICT)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            result = self.live_media.poll(after_id=after, timeout=wait)
        except (TypeError, ValueError) as error:
            self.send_error(HTTPStatus.BAD_REQUEST, str(error))
            return
        if result.dropped:
            self.send_response(HTTPStatus.CONFLICT)
            self.send_header("Content-Type", "application/json")
            body = (
                f'{{"error":"fragment history unavailable","dropped":{result.dropped}}}'
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
        next_event_id = self.live_metadata.next_id
        cursor_ahead = after >= 0 and after >= next_event_id
        if cursor_ahead:
            after = max(-1, next_event_id - self.live_metadata.max_items - 1)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            if cursor_ahead:
                oldest_id = max(0, next_event_id - self.live_metadata.max_items)
                reset = json.dumps(
                    {
                        "dropped": 0,
                        "oldest_id": oldest_id,
                        "reason": "cursor_ahead",
                    },
                    separators=(",", ":"),
                )
                self.wfile.write(b"event: reset\ndata: " + reset.encode("ascii") + b"\n\n")
            while True:
                result = self.live_metadata.poll(after_id=after, timeout=5.0)
                if result.dropped:
                    reset = (
                        '{"dropped":'
                        f'{result.dropped},"oldest_id":'
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
        self.send_header("Content-Security-Policy", _PLAYER_CONTENT_SECURITY_POLICY)
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), geolocation=(), microphone=()")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
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
        nargs="?",
        type=Path,
        help="MPEG-2 transport-stream input; use - for stdin with --live",
    )
    parser.add_argument(
        "--demo",
        choices=DEMO_VARIANTS,
        help="generate and play an openly redistributable synthetic FMV demo",
    )
    parser.add_argument(
        "--demo-duration",
        type=float,
        default=12,
        help="generated demo duration from 2 to 600 seconds",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Permit binding outside loopback; also requires --allowed-host",
    )
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        help="Trusted HTTP Host name/IP for remote mode; may be repeated",
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--ffmpeg", default="ffmpeg", help="FFmpeg executable")
    live_group = parser.add_mutually_exclusive_group()
    live_group.add_argument(
        "--live",
        action="store_true",
        help="Incrementally ingest a live/growing TS input and stream fragmented MP4",
    )
    live_group.add_argument(
        "--simulate-live",
        action="store_true",
        help="Pace a complete disk TS through the same live gateway",
    )
    parser.add_argument(
        "--playback-rate",
        type=float,
        default=1.0,
        help="simulated-live wall-clock multiplier (default: 1, real time)",
    )
    parser.add_argument("--ffprobe", default="ffprobe", help="FFprobe executable")
    parser.add_argument(
        "--udp-output",
        type=parse_udp_destination,
        metavar="IP:PORT",
        help="Allow the player UI to relay the source TS to this fixed UDP destination",
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
    source_duration_seconds: float | None = None,
    playback_rate: float = 1.0,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    udp_output: UDPOutputController | None = None,
) -> None:
    stream: BinaryIO
    owned = str(source) != "-"
    try:
        expanded = source.expanduser()
        stream = expanded.open("rb") if owned else sys.stdin.buffer
        try:
            if udp_output is not None:
                udp_output.begin_live()
            source_size = expanded.stat().st_size if source_duration_seconds is not None else 0
            input_bytes = 0
            started = monotonic()
            while chunk := stream.read(chunk_size):
                gateway.feed(chunk)
                if udp_output is not None:
                    udp_output.feed_live(chunk)
                if source_duration_seconds is not None:
                    input_bytes += len(chunk)
                    target_elapsed = (
                        input_bytes / source_size * source_duration_seconds / playback_rate
                    )
                    remaining = target_elapsed - (monotonic() - started)
                    if remaining > 0:
                        sleep(remaining)
            gateway.finish()
            if udp_output is not None:
                udp_output.finish_live()
        finally:
            if owned:
                stream.close()
    except Exception as error:
        if udp_output is not None:
            udp_output.finish_live()
        gateway.close()
        print(f"Live player input stopped: {error}", file=sys.stderr, flush=True)


def _run_live_player(
    args: argparse.Namespace,
    root: Path,
    *,
    allowed_hosts: tuple[str, ...],
) -> int:
    if args.live_chunk_size < 188:
        raise SystemExit("--live-chunk-size must be at least 188 bytes")
    if args.live_media_fragments < 2:
        raise SystemExit("--live-media-fragments must be at least 2")
    if args.live_metadata_samples < 1:
        raise SystemExit("--live-metadata-samples must be positive")
    source = args.source.expanduser()
    if str(source) != "-" and not source.is_file():
        raise SystemExit(f"live input does not exist: {source}")
    source_duration_seconds = None
    if args.simulate_live:
        if str(source) == "-":
            raise SystemExit("--simulate-live requires a complete disk input")
        if source.stat().st_size == 0:
            raise SystemExit("--simulate-live input must not be empty")
        if not math.isfinite(args.playback_rate) or not 0.1 <= args.playback_rate <= 16:
            raise SystemExit("--playback-rate must be between 0.1 and 16")
        source_duration_seconds = _source_duration(source, ffprobe=args.ffprobe)
        if source_duration_seconds is None or source_duration_seconds <= 0:
            raise SystemExit("--simulate-live requires FFprobe-readable positive media duration")
    _copy_player_ui(root)
    gateway = LivePlayerGateway(
        ffmpeg=args.ffmpeg,
        max_metadata_samples=args.live_metadata_samples,
        max_media_fragments=args.live_media_fragments,
        max_input_chunk_bytes=args.live_chunk_size,
        program_number=args.program_number,
    )
    gateway.start()
    udp_output = (
        None
        if args.udp_output is None
        else UDPOutputController(args.udp_output, live=True)
    )
    control_token = None if udp_output is None else secrets.token_urlsafe(32)
    handler = partial(
        PlayerHTTPRequestHandler,
        directory=str(root),
        live_media=gateway.media,
        live_metadata=gateway.metadata,
        allowed_hosts=allowed_hosts,
        udp_output=udp_output,
        control_token=control_token,
    )
    with ThreadingHTTPServer((args.host, args.port), handler) as server:
        url_host = _player_url_host(args.host, allowed_hosts)
        demo_query = "&demo=simulated" if args.simulate_live else ""
        url = f"http://{url_host}:{server.server_port}/?live=1{demo_query}"
        mode = "simulated-live disk" if args.simulate_live else "live"
        print(f"STANAG 4609 {mode} player: {url}")
        print("Reading bounded MPEG-TS input; stop with Ctrl-C.", flush=True)
        if not args.no_open:
            webbrowser.open(url)
        ingestion = Thread(
            target=_ingest_live_source,
            args=(source, gateway),
            kwargs={
                "chunk_size": args.live_chunk_size,
                "source_duration_seconds": source_duration_seconds,
                "playback_rate": args.playback_rate,
                "udp_output": udp_output,
            },
            name="stanag4609-live-input",
            daemon=True,
        )
        ingestion.start()
        with suppress(KeyboardInterrupt):
            server.serve_forever()
        gateway.close()
        if udp_output is not None:
            udp_output.close()
        ingestion.join(timeout=2)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if (args.source is None) == (args.demo is None):
        raise SystemExit("provide exactly one source or --demo")
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")
    if args.program_number is not None:
        if not 1 <= args.program_number <= 0xFFFF:
            raise SystemExit("--program-number must be between 1 and 65535")
        if not (args.live or args.simulate_live):
            raise SystemExit("--program-number currently requires --live or --simulate-live")
    if args.simulate_live:
        assert args.source is not None
        if str(args.source) == "-":
            raise SystemExit("--simulate-live requires a complete disk input")
        if not math.isfinite(args.playback_rate) or not 0.1 <= args.playback_rate <= 16:
            raise SystemExit("--playback-rate must be between 0.1 and 16")
    allowed_hosts = _cli_allowed_hosts(args)
    if shutil.which(args.ffmpeg) is None:
        raise SystemExit(f"FFmpeg executable not found: {args.ffmpeg}")
    with tempfile.TemporaryDirectory(prefix="stanag4609-player-") as temporary:
        root = Path(temporary)
        if args.demo is not None:
            args.source = root / f"stanag4609-{args.demo}-demo.ts"
            print(f"Generating {args.demo} FMV demo...", flush=True)
            try:
                generate_demo_fmv(
                    args.source,
                    variant=args.demo,
                    duration_seconds=args.demo_duration,
                    ffmpeg=args.ffmpeg,
                )
            except (ValueError, RuntimeError) as error:
                raise SystemExit(str(error)) from error
        if args.live or args.simulate_live:
            return _run_live_player(args, root, allowed_hosts=allowed_hosts)
        print("Preparing synchronized metadata and browser media...", flush=True)
        assets = prepare_player_assets(args.source, root, ffmpeg=args.ffmpeg)
        udp_output = None
        control_token = None
        if args.udp_output is not None:
            duration = _source_duration(args.source, ffprobe=args.ffprobe)
            if duration is None or duration <= 0:
                raise SystemExit("--udp-output requires FFprobe-readable positive media duration")
            udp_output = UDPOutputController(
                args.udp_output,
                live=False,
                source=args.source,
                source_duration_seconds=duration,
            )
            control_token = secrets.token_urlsafe(32)
        handler = partial(
            PlayerHTTPRequestHandler,
            directory=str(assets.root),
            timeline=assets.metadata,
            allowed_hosts=allowed_hosts,
            udp_output=udp_output,
            control_token=control_token,
        )
        with ThreadingHTTPServer((args.host, args.port), handler) as server:
            suffix = "?metadata=sse" if args.stream_metadata else ""
            url_host = _player_url_host(args.host, allowed_hosts)
            url = f"http://{url_host}:{server.server_port}/{suffix}"
            print(f"STANAG 4609 player: {url}")
            if not args.no_open:
                webbrowser.open(url)
            with suppress(KeyboardInterrupt):
                server.serve_forever()
        if udp_output is not None:
            udp_output.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
