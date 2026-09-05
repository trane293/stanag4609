"""Bounded primitives for low-latency browser delivery of live FMV."""

from __future__ import annotations

import math
import shutil
import subprocess
import time
from collections import deque
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from threading import Condition, Lock, Thread
from typing import BinaryIO, Generic, TypeVar

from stanag4609.errors import DecodeError, LimitExceeded, TruncatedData
from stanag4609.geojson import snapshot_geojson_features
from stanag4609.player.timeline import (
    MetadataSample,
    _fields,
    _frame_corners,
    extract_overlay_detections,
)
from stanag4609.st0601 import FieldDecodingMode, UASLocalSet
from stanag4609.st0601_state import ReportOnChangeState
from stanag4609.st0903 import VMTILocalSet
from stanag4609.transport.demux import PATEvent, PESStreamEvent, StreamKind, TransportDemuxer
from stanag4609.transport.metadata_stream import KLVMetadataEvent, MetadataStreamDecoder
from stanag4609.transport.timing import PTS_CLOCK_RATE, unwrap_pts

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class BroadcastPoll(Generic[_T]):
    """One atomic view of a bounded broadcast stream."""

    items: tuple[tuple[int, _T], ...]
    dropped: int
    closed: bool
    error: str | None


class BoundedBroadcast(Generic[_T]):
    """Thread-safe numbered fan-out history with explicit slow-client loss."""

    def __init__(self, *, max_items: int) -> None:
        if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 1:
            raise ValueError("max_items must be a positive integer")
        self.max_items = max_items
        self._items: deque[tuple[int, _T]] = deque(maxlen=max_items)
        self._next_id = 0
        self._closed = False
        self._error: str | None = None
        self._condition = Condition()

    @property
    def next_id(self) -> int:
        with self._condition:
            return self._next_id

    def publish(self, item: _T) -> int:
        with self._condition:
            if self._closed:
                raise RuntimeError("broadcast is closed")
            item_id = self._next_id
            self._next_id += 1
            self._items.append((item_id, item))
            self._condition.notify_all()
            return item_id

    def close(self, *, error: str | None = None) -> None:
        if error is not None and not isinstance(error, str):
            raise TypeError("error must be a string or None")
        with self._condition:
            self._closed = True
            if error is not None:
                self._error = error
            self._condition.notify_all()

    def poll(
        self,
        *,
        after_id: int,
        timeout: float | None,
    ) -> BroadcastPoll[_T]:
        if isinstance(after_id, bool) or not isinstance(after_id, int) or after_id < -1:
            raise TypeError("after_id must be an integer of at least -1")
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                raise TypeError("timeout must be a finite non-negative number or None")
            if not math.isfinite(timeout) or timeout < 0:
                raise ValueError("timeout must be a finite non-negative number or None")

        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while not self._closed and not any(
                item_id > after_id for item_id, _item in self._items
            ):
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                if remaining == 0:
                    break
                self._condition.wait(remaining)
            oldest = self._items[0][0] if self._items else self._next_id
            dropped = max(0, oldest - (after_id + 1))
            items = tuple(item for item in self._items if item[0] > after_id)
            return BroadcastPoll(items, dropped, self._closed, self._error)


@dataclass(frozen=True, slots=True)
class MP4Initialization:
    """Initialization bytes and join cursor for a fragmented MP4 stream."""

    data: bytes
    mime_type: str
    first_fragment_id: int


class FragmentedMP4Buffer:
    """Parse and retain bounded, complete fragmented-MP4 media units."""

    def __init__(
        self,
        *,
        max_fragments: int = 12,
        max_box_bytes: int = 32 * 1024 * 1024,
        max_initialization_bytes: int = 4 * 1024 * 1024,
        max_fragment_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        for name, value in (
            ("max_box_bytes", max_box_bytes),
            ("max_initialization_bytes", max_initialization_bytes),
            ("max_fragment_bytes", max_fragment_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 8:
                raise ValueError(f"{name} must be an integer of at least eight bytes")
        self.max_box_bytes = max_box_bytes
        self.max_initialization_bytes = max_initialization_bytes
        self.max_fragment_bytes = max_fragment_bytes
        self._fragments = BoundedBroadcast[bytes](max_items=max_fragments)
        self._buffer = bytearray()
        self._initialization_parts: list[bytes] = []
        self._initialization: bytes | None = None
        self._pending_fragment: bytearray | None = None
        self._condition = Condition()
        self._closed = False
        self._error: str | None = None

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    @property
    def error(self) -> str | None:
        with self._condition:
            return self._error

    def feed(self, data: bytes | bytearray | memoryview) -> int:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("fragmented MP4 input must be bytes-like")
        with self._condition:
            if self._closed:
                raise RuntimeError("fragmented MP4 buffer is closed")
            self._buffer.extend(data)
            completed = 0
            while True:
                box = self._take_box()
                if box is None:
                    break
                if self._accept_box(box):
                    completed += 1
            return completed

    def _take_box(self) -> bytes | None:
        if len(self._buffer) < 8:
            return None
        size = int.from_bytes(self._buffer[:4], "big")
        header_size = 8
        if size == 1:
            if len(self._buffer) < 16:
                return None
            size = int.from_bytes(self._buffer[8:16], "big")
            header_size = 16
        if size == 0 or size < header_size:
            raise DecodeError("fragmented MP4 box has invalid size")
        if size > self.max_box_bytes:
            raise LimitExceeded(
                f"fragmented MP4 box size {size} exceeds limit {self.max_box_bytes}"
            )
        if len(self._buffer) < size:
            return None
        box = bytes(self._buffer[:size])
        del self._buffer[:size]
        return box

    def _accept_box(self, box: bytes) -> bool:
        kind = box[4:8]
        if self._initialization is None:
            if kind == b"moof":
                raise DecodeError("fragmented MP4 media arrived before the moov box")
            self._initialization_parts.append(box)
            size = sum(len(part) for part in self._initialization_parts)
            if size > self.max_initialization_bytes:
                raise LimitExceeded(
                    f"fragmented MP4 initialization exceeds limit {self.max_initialization_bytes}"
                )
            if kind == b"moov":
                self._initialization = b"".join(self._initialization_parts)
                self._initialization_parts.clear()
                self._condition.notify_all()
            return False

        if kind == b"moof":
            if self._pending_fragment is not None:
                raise DecodeError("fragmented MP4 moof is missing its mdat box")
            self._pending_fragment = bytearray(box)
            return False
        if self._pending_fragment is None:
            return False
        self._pending_fragment.extend(box)
        if len(self._pending_fragment) > self.max_fragment_bytes:
            raise LimitExceeded(
                f"fragmented MP4 media fragment exceeds limit {self.max_fragment_bytes}"
            )
        if kind != b"mdat":
            return False
        self._fragments.publish(bytes(self._pending_fragment))
        self._pending_fragment = None
        return True

    def initialization(self, *, timeout: float | None) -> MP4Initialization:
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                raise TypeError("timeout must be a finite non-negative number or None")
            if not math.isfinite(timeout) or timeout < 0:
                raise ValueError("timeout must be a finite non-negative number or None")
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while self._initialization is None and not self._closed:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                if remaining == 0:
                    break
                self._condition.wait(remaining)
            if self._initialization is None:
                detail = f": {self._error}" if self._error else ""
                raise TimeoutError(f"fragmented MP4 initialization is unavailable{detail}")
            first_id = max(0, self._fragments.next_id - self._fragments.max_items)
            codecs = self._avc_codec(self._initialization)
            if b"mp4a" in self._initialization:
                codecs += ", mp4a.40.2"
            return MP4Initialization(
                self._initialization,
                f'video/mp4; codecs="{codecs}"',
                first_id,
            )

    @staticmethod
    def _avc_codec(initialization: bytes) -> str:
        cursor = 0
        while True:
            kind_offset = initialization.find(b"avcC", cursor)
            if kind_offset < 4:
                return "avc1.42E01F"
            box_offset = kind_offset - 4
            size = int.from_bytes(initialization[box_offset:kind_offset], "big")
            payload_offset = kind_offset + 4
            if (
                size >= 12
                and box_offset + size <= len(initialization)
                and initialization[payload_offset] == 1
            ):
                profile = initialization[payload_offset + 1 : payload_offset + 4]
                return f"avc1.{profile.hex().upper()}"
            cursor = kind_offset + 4

    def poll(self, *, after_id: int, timeout: float | None) -> BroadcastPoll[bytes]:
        return self._fragments.poll(after_id=after_id, timeout=timeout)

    def finish(self) -> None:
        with self._condition:
            if self._buffer:
                raise TruncatedData("fragmented MP4 ended with a partial box")
            if self._pending_fragment is not None:
                raise TruncatedData("fragmented MP4 ended with a partial media fragment")
            if self._initialization is None:
                raise TruncatedData("fragmented MP4 ended before its initialization")
            self._closed = True
            self._fragments.close()
            self._condition.notify_all()

    def close(self, *, error: str) -> None:
        if not isinstance(error, str):
            raise TypeError("error must be a string")
        with self._condition:
            self._closed = True
            self._error = error
            self._fragments.close(error=error)
            self._condition.notify_all()


class LiveMetadataDecoder:
    """Incrementally project live MPEG-TS KLV into browser-ready samples."""

    def __init__(
        self,
        *,
        max_pending_metadata: int = 512,
        program_number: int | None = None,
    ) -> None:
        if (
            isinstance(max_pending_metadata, bool)
            or not isinstance(max_pending_metadata, int)
            or max_pending_metadata < 1
        ):
            raise ValueError("max_pending_metadata must be a positive integer")
        if program_number is not None and (
            isinstance(program_number, bool)
            or not isinstance(program_number, int)
            or not 1 <= program_number <= 0xFFFF
        ):
            raise ValueError("program_number must be an integer from 1 to 65535 or None")
        self.max_pending_metadata = max_pending_metadata
        self.program_number = program_number
        self._demuxer = TransportDemuxer(program_number=program_number)
        self._decoder = MetadataStreamDecoder(
            field_decoding=FieldDecodingMode.PRESERVE,
            validate_sequence=False,
        )
        self._receiver_states: dict[tuple[int, int], ReportOnChangeState] = {}
        self._pending: deque[KLVMetadataEvent] = deque()
        self._first_timestamp: datetime | None = None
        self._finished = False
        self.video_start_pts: int | None = None
        self.media_start_pts: int | None = None

    def feed(self, data: bytes | bytearray | memoryview) -> tuple[MetadataSample, ...]:
        if self._finished:
            raise RuntimeError("live metadata decoder is finished")
        return self._consume(self._demuxer.feed(data), final=False)

    def finish(self) -> tuple[MetadataSample, ...]:
        if self._finished:
            return ()
        samples = self._consume(self._demuxer.finish(), final=True)
        self._decoder.finish()
        self._finished = True
        return samples

    def _consume(
        self,
        events: Iterable[object],
        *,
        final: bool,
    ) -> tuple[MetadataSample, ...]:
        for event in events:
            if (
                isinstance(event, PATEvent)
                and event.table.current_next_indicator
                and self.program_number is None
                and len(event.programs) > 1
            ):
                raise DecodeError("live player input has multiple programs; select program_number")
            if not isinstance(event, PESStreamEvent):
                continue
            if event.kind in {StreamKind.VIDEO, StreamKind.AUDIO} and event.pes.pts is not None:
                if self.media_start_pts is None:
                    self.media_start_pts = event.pes.pts
                if event.kind is StreamKind.VIDEO and self.video_start_pts is None:
                    self.video_start_pts = event.pes.pts
            elif event.kind is StreamKind.KLV:
                for metadata in self._decoder.feed(event):
                    if isinstance(metadata.decoded, UASLocalSet):
                        self._pending.append(metadata)
                        if len(self._pending) > self.max_pending_metadata:
                            raise LimitExceeded(
                                "live metadata waiting for a media clock exceeds limit "
                                f"{self.max_pending_metadata}"
                            )
        if self.media_start_pts is None and not final:
            return ()
        samples = tuple(self._project(event) for event in self._pending)
        self._pending.clear()
        return samples

    def _project(self, event: KLVMetadataEvent) -> MetadataSample:
        assert isinstance(event.decoded, UASLocalSet)
        timestamp = event.decoded.value(2)
        if self._first_timestamp is None and isinstance(timestamp, datetime):
            self._first_timestamp = timestamp
        if event.pts is not None and self.media_start_pts is not None:
            unwrapped = unwrap_pts(event.pts, reference=self.media_start_pts)
            seconds = (unwrapped - self.media_start_pts) / PTS_CLOCK_RATE
        else:
            seconds = (
                (timestamp - self._first_timestamp).total_seconds()
                if isinstance(timestamp, datetime) and self._first_timestamp is not None
                else 0.0
            )
        state = self._receiver_states.setdefault(
            (event.program_number, event.pid), ReportOnChangeState()
        )
        snapshot = state.observe(event.decoded)
        vmti = snapshot.value(74)
        frame_corners = _frame_corners(snapshot)
        return MetadataSample(
            max(0.0, seconds),
            event.pts,
            event.program_number,
            event.pid,
            _fields(
                snapshot.fields,
                frame_center_latitude=snapshot.value(23),
                frame_center_longitude=snapshot.value(24),
            ),
            snapshot_geojson_features(snapshot),
            (
                extract_overlay_detections(
                    vmti,
                    frame_center_latitude=snapshot.value(23),
                    frame_center_longitude=snapshot.value(24),
                    frame_corners=frame_corners,
                )
                if isinstance(vmti, VMTILocalSet)
                else ()
            ),
            tuple(issue.message for issue in snapshot.issues),
        )


def ffmpeg_live_player_command(
    *,
    ffmpeg: str = "ffmpeg",
    program_number: int | None = None,
) -> tuple[str, ...]:
    """Return the pipe-to-fragmented-MP4 low-latency FFmpeg command."""

    if program_number is not None and (
        isinstance(program_number, bool)
        or not isinstance(program_number, int)
        or not 1 <= program_number <= 0xFFFF
    ):
        raise ValueError("program_number must be an integer from 1 to 65535 or None")
    video_map = "0:v:0" if program_number is None else f"0:p:{program_number}:v:0"
    audio_map = "0:a:0?" if program_number is None else f"0:p:{program_number}:a:0?"
    return (
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-flags",
        "low_delay",
        "-probesize",
        "32768",
        "-analyzeduration",
        "500000",
        "-f",
        "mpegts",
        "-i",
        "pipe:0",
        "-map",
        video_map,
        "-map",
        audio_map,
        "-sn",
        "-dn",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-g",
        "60",
        "-keyint_min",
        "1",
        "-sc_threshold",
        "0",
        "-force_key_frames",
        "expr:gte(t,n_forced*1)",
        "-profile:v",
        "baseline",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-f",
        "mp4",
        "-movflags",
        "frag_keyframe+empty_moov+default_base_moof+omit_tfhd_offset",
        "pipe:1",
    )


@dataclass(frozen=True, slots=True)
class LivePlayerStats:
    """Monotonic counters for one live player input session."""

    input_bytes: int
    metadata_samples: int
    media_fragments: int


class LivePlayerGateway:
    """Fan live MPEG-TS into synchronized KLV and fragmented browser media.

    ``feed`` deliberately applies FFmpeg pipe backpressure to the caller. This
    keeps memory bounded and makes overload policy an explicit responsibility
    of the source adapter rather than silently dropping transport bytes. One
    source owner must serialize calls to ``feed``, ``finish``, and ``close``;
    the media and metadata broadcast consumers are safe to use concurrently.
    """

    def __init__(
        self,
        *,
        ffmpeg: str = "ffmpeg",
        max_metadata_samples: int = 512,
        max_media_fragments: int = 12,
        max_input_chunk_bytes: int = 1024 * 1024,
        stderr_bytes: int = 64 * 1024,
        program_number: int | None = None,
    ) -> None:
        if not isinstance(ffmpeg, str) or not ffmpeg:
            raise ValueError("ffmpeg must be a non-empty string")
        if isinstance(stderr_bytes, bool) or not isinstance(stderr_bytes, int) or stderr_bytes < 1:
            raise ValueError("stderr_bytes must be a positive integer")
        if (
            isinstance(max_input_chunk_bytes, bool)
            or not isinstance(max_input_chunk_bytes, int)
            or max_input_chunk_bytes < 1
        ):
            raise ValueError("max_input_chunk_bytes must be a positive integer")
        self.ffmpeg = ffmpeg
        self.program_number = program_number
        self.max_input_chunk_bytes = max_input_chunk_bytes
        self.stderr_bytes = stderr_bytes
        self.metadata = BoundedBroadcast[MetadataSample](max_items=max_metadata_samples)
        self.media = FragmentedMP4Buffer(max_fragments=max_media_fragments)
        self._metadata_decoder = LiveMetadataDecoder(
            max_pending_metadata=max_metadata_samples,
            program_number=program_number,
        )
        self._process: subprocess.Popen[bytes] | None = None
        self._media_thread: Thread | None = None
        self._stderr_thread: Thread | None = None
        self._stderr = bytearray()
        self._lock = Lock()
        self._finished = False
        self._input_bytes = 0
        self._metadata_samples = 0
        self._media_fragments = 0

    @property
    def stats(self) -> LivePlayerStats:
        with self._lock:
            return LivePlayerStats(
                self._input_bytes,
                self._metadata_samples,
                self._media_fragments,
            )

    @property
    def stderr_tail(self) -> str:
        with self._lock:
            return bytes(self._stderr).decode("utf-8", errors="replace").strip()

    def start(self) -> None:
        with self._lock:
            if self._finished:
                raise RuntimeError("live player gateway is finished")
            if self._process is not None:
                return
            if shutil.which(self.ffmpeg) is None:
                raise RuntimeError(f"FFmpeg executable not found: {self.ffmpeg}")
            try:
                process = subprocess.Popen(
                    ffmpeg_live_player_command(
                        ffmpeg=self.ffmpeg,
                        program_number=self.program_number,
                    ),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                )
            except OSError as error:
                raise RuntimeError(f"could not start FFmpeg: {error}") from error
            assert process.stdin is not None
            assert process.stdout is not None
            assert process.stderr is not None
            self._process = process
            self._media_thread = Thread(
                target=self._read_media,
                args=(process.stdout,),
                name="stanag4609-live-media",
                daemon=True,
            )
            self._stderr_thread = Thread(
                target=self._read_stderr,
                args=(process.stderr,),
                name="stanag4609-live-ffmpeg-stderr",
                daemon=True,
            )
            self._media_thread.start()
            self._stderr_thread.start()

    def feed(self, data: bytes | bytearray | memoryview) -> tuple[MetadataSample, ...]:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("live player input must be bytes-like")
        input_bytes = memoryview(data).nbytes
        if input_bytes > self.max_input_chunk_bytes:
            raise LimitExceeded(
                f"live player input chunk size {input_bytes} exceeds limit "
                f"{self.max_input_chunk_bytes}"
            )
        chunk = bytes(data)
        with self._lock:
            if self._finished:
                raise RuntimeError("live player gateway is finished")
        self.start()
        try:
            samples = self._metadata_decoder.feed(chunk)
        except Exception:
            self.close()
            raise
        process = self._process
        assert process is not None and process.stdin is not None
        try:
            process.stdin.write(chunk)
        except (BrokenPipeError, OSError) as error:
            detail = self.stderr_tail
            suffix = f": {detail.splitlines()[-1]}" if detail else ""
            self.close()
            raise RuntimeError(f"FFmpeg live media pipe closed{suffix}") from error
        for sample in samples:
            self.metadata.publish(sample)
        with self._lock:
            self._input_bytes += len(chunk)
            self._metadata_samples += len(samples)
        return samples

    def finish(self, *, timeout: float = 15.0) -> tuple[MetadataSample, ...]:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise TypeError("timeout must be a finite positive number")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a finite positive number")
        with self._lock:
            if self._finished:
                return ()
            self._finished = True
            process = self._process
        try:
            samples = self._metadata_decoder.finish()
        except Exception:
            self.metadata.close(error="live metadata ended with incomplete input")
            self.media.close(error="live metadata ended with incomplete input")
            if process is not None:
                self._stop_process(process, timeout=timeout)
                self._join_threads(timeout)
            raise
        for sample in samples:
            self.metadata.publish(sample)
        with self._lock:
            self._metadata_samples += len(samples)
        self.metadata.close()
        if process is None:
            self.media.close(error="live input ended before FFmpeg started")
            raise RuntimeError("live input ended before FFmpeg started")
        assert process.stdin is not None
        process.stdin.close()
        try:
            return_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            process.kill()
            process.wait()
            self.media.close(error="FFmpeg did not stop within the configured timeout")
            raise RuntimeError("FFmpeg did not stop within the configured timeout") from error
        self._join_threads(timeout)
        if return_code:
            detail = self.stderr_tail
            suffix = f": {detail.splitlines()[-1]}" if detail else ""
            self.media.close(error=f"FFmpeg exited with status {return_code}{suffix}")
            raise RuntimeError(f"FFmpeg exited with status {return_code}{suffix}")
        if self._media_thread is not None and self._media_thread.is_alive():
            self.media.close(error="fragmented MP4 reader did not reach end-of-stream")
            raise RuntimeError("fragmented MP4 reader did not reach end-of-stream")
        if self.media.error:
            raise RuntimeError(f"invalid fragmented MP4 output: {self.media.error}")
        if self.stats.media_fragments == 0:
            self.media.close(error="FFmpeg produced no complete media fragments")
            raise RuntimeError("FFmpeg produced no complete media fragments")
        return samples

    def close(self, *, timeout: float = 2.0) -> None:
        """Stop an unfinished live session without claiming a clean EOF."""

        with self._lock:
            if self._finished:
                return
            self._finished = True
            process = self._process
        self.metadata.close(error="live player session stopped")
        self.media.close(error="live player session stopped")
        if process is None:
            return
        self._stop_process(process, timeout=timeout)
        self._join_threads(timeout)

    @staticmethod
    def _stop_process(process: subprocess.Popen[bytes], *, timeout: float) -> None:
        """Best-effort cleanup that never masks the caller's primary error."""

        if process.stdin is not None:
            with suppress(BrokenPipeError, OSError, ValueError):
                process.stdin.close()
        if process.poll() is None:
            with suppress(OSError):
                process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            with suppress(OSError):
                process.kill()
            with suppress(OSError):
                process.wait()

    def _join_threads(self, timeout: float) -> None:
        if self._media_thread is not None:
            self._media_thread.join(timeout=timeout)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=timeout)

    def _read_media(self, stream: BinaryIO) -> None:
        try:
            while chunk := stream.read(64 * 1024):
                completed = self.media.feed(chunk)
                with self._lock:
                    self._media_fragments += completed
            self.media.finish()
        except Exception as error:
            self.media.close(error=str(error))
            process = self._process
            if process is not None and process.poll() is None:
                with suppress(OSError):
                    process.terminate()

    def _read_stderr(self, stream: BinaryIO) -> None:
        while chunk := stream.read(4 * 1024):
            with self._lock:
                self._stderr.extend(chunk)
                overflow = len(self._stderr) - self.stderr_bytes
                if overflow > 0:
                    del self._stderr[:overflow]
