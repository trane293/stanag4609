from __future__ import annotations

from datetime import datetime, timezone
from functools import partial
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Barrier, BrokenBarrierError, Event, Lock, Thread
from types import SimpleNamespace

import pytest

from stanag4609.errors import DecodeError, LimitExceeded, TruncatedData
from stanag4609.player.live import (
    BoundedBroadcast,
    FragmentedMP4Buffer,
    LiveMetadataDecoder,
    LivePlayerGateway,
    ffmpeg_live_player_command,
)
from stanag4609.player.server import PlayerHTTPRequestHandler, _ingest_live_source
from stanag4609.player.server import main as player_main
from stanag4609.player.timeline import MetadataSample
from stanag4609.st0601 import PlatformStatus, encode_uas_local_set
from stanag4609.transport.metadata import synchronous_klv_stream
from stanag4609.transport.mux import TransportMuxer, build_pat_section, encode_pes_packet
from stanag4609.transport.psi import ElementaryStreamInfo, ProgramAssociation


def _box(kind: bytes, payload: bytes = b"") -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + kind + payload


def _psi_packet(section: bytes) -> bytes:
    payload = b"\x00" + section
    adaptation_length = 183 - len(payload)
    adaptation = b"\x00" + b"\xff" * (adaptation_length - 1)
    return bytes((0x47, 0x40, 0x00, 0x30, adaptation_length)) + adaptation + payload


def _live_source() -> bytes:
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
    first = encode_uas_local_set(
        {
            2: datetime(2026, 9, 5, tzinfo=timezone.utc),
            13: 49.0,
            14: -123.0,
            23: 49.1,
            24: -122.9,
            65: 19,
            125: PlatformStatus.EGRESS,
        }
    )
    second = encode_uas_local_set(
        {
            2: datetime(2026, 9, 5, 0, 0, 1, tzinfo=timezone.utc),
            13: 50.0,
            65: 19,
        }
    )
    return (
        b"".join(muxer.program_tables())
        + b"".join(
            muxer.mux_pes(
                0x101,
                encode_pes_packet(b"frame-1", stream_id=0xE0, pts=90_000),
            )
        )
        + b"".join(muxer.mux_sync_klv(0x102, first, pts=90_000))
        + b"".join(
            muxer.mux_pes(
                0x101,
                encode_pes_packet(b"frame-2", stream_id=0xE0, pts=180_000),
            )
        )
        + b"".join(muxer.mux_sync_klv(0x102, second, pts=180_000))
    )


def test_bounded_broadcast_reports_history_loss_and_close() -> None:
    broadcast = BoundedBroadcast[str](max_items=2)
    assert broadcast.publish("one") == 0
    assert broadcast.publish("two") == 1
    assert broadcast.publish("three") == 2

    snapshot = broadcast.poll(after_id=-1, timeout=0)
    assert snapshot.dropped == 1
    assert snapshot.items == ((1, "two"), (2, "three"))
    assert not snapshot.closed

    assert broadcast.poll(after_id=2, timeout=0).items == ()
    broadcast.close()
    assert broadcast.poll(after_id=2, timeout=0).closed
    with pytest.raises(RuntimeError, match="closed"):
        broadcast.publish("four")


def test_bounded_broadcast_validates_contract() -> None:
    with pytest.raises(ValueError, match="max_items"):
        BoundedBroadcast[bytes](max_items=0)
    broadcast = BoundedBroadcast[bytes](max_items=1)
    with pytest.raises(TypeError, match="after_id"):
        broadcast.poll(after_id=True, timeout=0)
    with pytest.raises(ValueError, match="timeout"):
        broadcast.poll(after_id=-1, timeout=-1)
    with pytest.raises(TypeError, match="error"):
        broadcast.close(error=object())  # type: ignore[arg-type]


def test_bounded_broadcast_retention_stays_fixed_under_sustained_publication() -> None:
    broadcast = BoundedBroadcast[int](max_items=32)

    for value in range(100_000):
        broadcast.publish(value)

    snapshot = broadcast.poll(after_id=-1, timeout=0)
    assert len(snapshot.items) == 32
    assert snapshot.dropped == 100_000 - 32
    assert snapshot.items[0] == (100_000 - 32, 100_000 - 32)
    assert snapshot.items[-1] == (99_999, 99_999)


def test_fragmented_mp4_buffer_parses_arbitrary_chunks_and_bounds_history() -> None:
    initialization = _box(b"ftyp", b"isom") + _box(
        b"moov",
        b"avc1" + _box(b"avcC", bytes((1, 100, 0, 40))) + b"mp4a",
    )
    first = _box(b"moof", b"one") + _box(b"mdat", b"ONE")
    second = _box(b"moof", b"two") + _box(b"mdat", b"TWO")
    media = FragmentedMP4Buffer(max_fragments=1)
    source = initialization + first + second
    for index in range(0, len(source), 3):
        media.feed(source[index : index + 3])

    init = media.initialization(timeout=0)
    assert init.data == initialization
    assert init.mime_type == 'video/mp4; codecs="avc1.640028, mp4a.40.2"'
    assert init.first_fragment_id == 1
    fragments = media.poll(after_id=-1, timeout=0)
    assert fragments.dropped == 1
    assert fragments.items == ((1, second),)
    media.finish()
    assert media.closed
    assert media.error is None
    assert media.poll(after_id=1, timeout=0).closed


def test_fragmented_mp4_buffer_rejects_malformed_or_unbounded_boxes() -> None:
    media = FragmentedMP4Buffer(max_box_bytes=16)
    with pytest.raises(DecodeError, match="size"):
        media.feed(b"\x00\x00\x00\x04moov")

    media = FragmentedMP4Buffer(max_box_bytes=16)
    with pytest.raises(LimitExceeded, match="box"):
        media.feed((17).to_bytes(4, "big") + b"moov")

    media = FragmentedMP4Buffer()
    media.feed(_box(b"ftyp") + _box(b"moov", b"avc1") + _box(b"moof"))
    with pytest.raises(TruncatedData, match="fragment"):
        media.finish()

    incomplete = FragmentedMP4Buffer()
    incomplete.feed(b"\x00\x00")
    with pytest.raises(TruncatedData, match="box"):
        incomplete.finish()


def test_fragmented_mp4_retention_stays_fixed_under_sustained_fragments() -> None:
    media = FragmentedMP4Buffer(max_fragments=12)
    media.feed(_box(b"ftyp") + _box(b"moov", b"avc1"))
    fragment = _box(b"moof", b"sequence") + _box(b"mdat", b"payload")

    for _ in range(10_000):
        media.feed(fragment)

    snapshot = media.poll(after_id=-1, timeout=0)
    assert len(snapshot.items) == 12
    assert snapshot.dropped == 10_000 - 12
    assert all(item == fragment for _item_id, item in snapshot.items)


def test_live_metadata_decoder_emits_media_relative_sparse_state() -> None:
    decoder = LiveMetadataDecoder()
    samples = []
    source = _live_source()
    for index in range(0, len(source), 73):
        samples.extend(decoder.feed(source[index : index + 73]))
    samples.extend(decoder.finish())

    assert decoder.video_start_pts == 90_000
    assert decoder.media_start_pts == 90_000
    assert [sample.time_seconds for sample in samples] == [0.0, 1.0]
    assert samples[0].fields["Platform Status"]["display"] == "Egress (9)"
    assert samples[1].fields["Sensor Latitude"]["value"] == pytest.approx(50.0)
    assert samples[1].fields["Sensor Longitude"]["value"] == pytest.approx(-123.0)
    assert {feature["properties"]["role"] for feature in samples[1].geospatial} == {
        "sensor",
        "frame_center",
    }
    with pytest.raises(RuntimeError, match="finished"):
        decoder.feed(b"")


def test_live_metadata_decoder_requires_program_selection_for_mpts() -> None:
    pat = build_pat_section(
        transport_stream_id=1,
        programs=(ProgramAssociation(1, 0x100), ProgramAssociation(2, 0x110)),
    )
    with pytest.raises(DecodeError, match="multiple programs"):
        LiveMetadataDecoder().feed(_psi_packet(pat))

    selected = LiveMetadataDecoder(program_number=2)
    assert selected.feed(_psi_packet(pat)) == ()
    with pytest.raises(ValueError, match="program_number"):
        LiveMetadataDecoder(program_number=0)


def test_live_ffmpeg_command_is_fragmented_low_latency_and_pipe_based() -> None:
    command = ffmpeg_live_player_command(ffmpeg="ffmpeg7")
    assert command[0] == "ffmpeg7"
    assert command[command.index("-i") + 1] == "pipe:0"
    assert command[-1] == "pipe:1"
    assert "empty_moov" in command[command.index("-movflags") + 1]
    assert "frag_keyframe" in command[command.index("-movflags") + 1]
    assert "zerolatency" in command
    assert "ultrafast" in command
    assert "0:a:0?" in command
    assert "nobuffer" not in command
    assert command[command.index("-probesize") + 1] == "32768"
    assert command[command.index("-analyzeduration") + 1] == "500000"
    assert "expr:gte(t,n_forced*1)" in command
    assert "-level:v" not in command
    selected = ffmpeg_live_player_command(program_number=7)
    assert "0:p:7:v:0" in selected
    assert "0:p:7:a:0?" in selected
    with pytest.raises(ValueError, match="program_number"):
        ffmpeg_live_player_command(program_number=True)  # type: ignore[arg-type]


def test_live_gateway_rejects_missing_ffmpeg_and_empty_session() -> None:
    missing = LivePlayerGateway(ffmpeg="definitely-not-a-real-ffmpeg")
    with pytest.raises(RuntimeError, match="executable not found"):
        missing.start()
    missing.close()

    empty = LivePlayerGateway()
    with pytest.raises(RuntimeError, match="before FFmpeg started"):
        empty.finish()
    assert empty.media.closed
    assert empty.media.error == "live input ended before FFmpeg started"
    assert empty.metadata.poll(after_id=-1, timeout=0).closed
    assert empty.finish() == ()


def test_live_gateway_enforces_an_explicit_input_call_bound() -> None:
    gateway = LivePlayerGateway(max_input_chunk_bytes=188)

    with pytest.raises(LimitExceeded, match="input chunk"):
        gateway.feed(bytes(189))
    assert gateway.stats.input_bytes == 0
    gateway.close()

    with pytest.raises(ValueError, match="max_input_chunk_bytes"):
        LivePlayerGateway(max_input_chunk_bytes=0)


def test_live_gateway_propagates_writer_backpressure_to_feed_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = Event()
    release = Event()

    class BlockingInput:
        def write(self, data: bytes) -> int:
            entered.set()
            assert release.wait(timeout=2)
            return len(data)

    gateway = LivePlayerGateway()
    monkeypatch.setattr(gateway, "start", lambda: None)
    gateway._process = SimpleNamespace(stdin=BlockingInput())  # type: ignore[assignment]
    completed: list[tuple[MetadataSample, ...]] = []
    producer = Thread(target=lambda: completed.append(gateway.feed(b"x")))

    producer.start()
    assert entered.wait(timeout=1)
    assert producer.is_alive()
    assert gateway.stats.input_bytes == 0
    release.set()
    producer.join(timeout=1)

    assert not producer.is_alive()
    assert completed == [()]
    assert gateway.stats.input_bytes == 1
    gateway._process = None
    gateway.close()


def test_live_gateway_sustained_input_cannot_bypass_writer_backpressure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked = Event()
    release = Event()

    class GatedInput:
        def __init__(self) -> None:
            self.write_calls = 0

        def write(self, data: bytes) -> int:
            self.write_calls += 1
            if self.write_calls == 250:
                blocked.set()
                assert release.wait(timeout=2)
            return len(data)

    input_pipe = GatedInput()
    gateway = LivePlayerGateway()
    monkeypatch.setattr(gateway, "start", lambda: None)
    monkeypatch.setattr(gateway._metadata_decoder, "feed", lambda _data: ())
    gateway._process = SimpleNamespace(stdin=input_pipe)  # type: ignore[assignment]
    producer = Thread(target=lambda: [gateway.feed(b"x") for _ in range(2_000)])

    producer.start()
    assert blocked.wait(timeout=1)
    assert producer.is_alive()
    assert input_pipe.write_calls == 250
    assert gateway.stats.input_bytes == 249
    release.set()
    producer.join(timeout=2)

    assert not producer.is_alive()
    assert input_pipe.write_calls == 2_000
    assert gateway.stats.input_bytes == 2_000
    gateway._process = None
    gateway.close()


def test_live_player_cli_validates_program_selection() -> None:
    with pytest.raises(SystemExit, match="requires --live"):
        player_main(["missing.ts", "--program-number", "7", "--no-open"])
    with pytest.raises(SystemExit, match="between 1 and 65535"):
        player_main(["-", "--live", "--program-number", "0", "--no-open"])


def test_simulated_live_cli_requires_disk_and_valid_rate(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="complete disk input"):
        player_main(["-", "--simulate-live", "--no-open"])
    source = tmp_path / "source.ts"
    source.write_bytes(b"x")
    with pytest.raises(SystemExit, match="playback-rate"):
        player_main([str(source), "--simulate-live", "--playback-rate", "0", "--no-open"])


def test_simulated_live_disk_ingest_is_paced_by_media_duration(tmp_path: Path) -> None:
    source = tmp_path / "source.ts"
    source.write_bytes(b"0123456789")
    fed: list[bytes] = []
    finished: list[bool] = []
    now = [0.0]

    class Gateway:
        def feed(self, chunk: bytes) -> None:
            fed.append(chunk)

        def finish(self) -> None:
            finished.append(True)

        def close(self) -> None:
            pytest.fail("valid paced ingestion must not close as an error")

    def sleep(seconds: float) -> None:
        now[0] += seconds

    _ingest_live_source(
        source,
        Gateway(),  # type: ignore[arg-type]
        chunk_size=4,
        source_duration_seconds=5,
        playback_rate=1,
        monotonic=lambda: now[0],
        sleep=sleep,
    )

    assert fed == [b"0123", b"4567", b"89"]
    assert finished == [True]
    assert now[0] == pytest.approx(5)


def test_live_player_cli_requires_explicit_remote_exposure() -> None:
    with pytest.raises(SystemExit, match="--allow-remote"):
        player_main(["missing.ts", "--host", "0.0.0.0", "--no-open"])
    with pytest.raises(SystemExit, match="--allowed-host"):
        player_main(["missing.ts", "--host", "0.0.0.0", "--allow-remote", "--no-open"])


def test_player_http_rejects_untrusted_hosts_and_sets_browser_security_headers(
    tmp_path: Path,
) -> None:
    (tmp_path / "index.html").write_text("player", encoding="utf-8")
    handler = partial(
        PlayerHTTPRequestHandler,
        directory=str(tmp_path),
        allowed_hosts=("127.0.0.1", "localhost"),
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        rejected = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        rejected.putrequest("GET", "/", skip_host=True)
        rejected.putheader("Host", "attacker.example")
        rejected.endheaders()
        response = rejected.getresponse()
        assert response.status == 421
        response.read()
        rejected.close()

        accepted = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        accepted.request("GET", "/", headers={"Host": f"127.0.0.1:{server.server_port}"})
        response = accepted.getresponse()
        assert response.status == 200
        assert response.getheader("X-Content-Type-Options") == "nosniff"
        assert response.getheader("X-Frame-Options") == "DENY"
        assert response.getheader("Referrer-Policy") == "no-referrer"
        assert response.getheader("Cross-Origin-Resource-Policy") == "same-origin"
        assert response.getheader("Permissions-Policy") == (
            "camera=(), geolocation=(), microphone=()"
        )
        content_security_policy = response.getheader("Content-Security-Policy")
        assert content_security_policy is not None
        assert "frame-ancestors 'none'" in content_security_policy
        assert "https://tile.openstreetmap.org" in content_security_policy
        assert "media-src 'self' blob:" in content_security_policy
        assert response.read() == b"player"
        accepted.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    invalid_handler = partial(PlayerHTTPRequestHandler, allowed_hosts="localhost")
    with pytest.raises(TypeError, match="sequence of host names"):
        invalid_handler(None, None, None)


def test_live_player_http_endpoints_deliver_numbered_media_and_metadata(
    tmp_path: Path,
) -> None:
    media = FragmentedMP4Buffer()
    initialization = _box(b"ftyp", b"isom") + _box(b"moov", b"avc1")
    fragment = _box(b"moof", b"one") + _box(b"mdat", b"ONE")
    media.feed(initialization + fragment)
    media.finish()
    metadata = BoundedBroadcast[MetadataSample](max_items=2)
    metadata.publish(MetadataSample(0.25, 22_500, 1, 258, {"Sensor Latitude": {"value": 49}}))
    metadata.close()
    handler = partial(
        PlayerHTTPRequestHandler,
        directory=str(tmp_path),
        live_media=media,
        live_metadata=metadata,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request("GET", "/media/init.mp4?wait=0")
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("X-First-Fragment-ID") == "0"
        assert response.getheader("Content-Type") == 'video/mp4; codecs="avc1.42E01F"'
        assert response.read() == initialization

        connection.request("GET", "/media/fragment?after=-1&wait=0")
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("X-Fragment-ID") == "0"
        assert response.read() == fragment

        connection.request("GET", "/media/fragment?after=0&wait=0")
        response = connection.getresponse()
        assert response.status == 204
        assert response.getheader("X-Stream-End") == "true"
        response.read()

        connection.request("GET", "/metadata/live")
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200
        assert b"event: sample\nid: 0\n" in body
        assert b'"time_seconds":0.25' in body
        assert body.endswith(b'event: end\ndata: {"live":true}\n\n')

        connection.request(
            "GET",
            "/metadata/live?after=-1",
            headers={"Last-Event-ID": "0"},
        )
        response = connection.getresponse()
        resumed = response.read()
        assert response.status == 200
        assert b"event: sample" not in resumed
        assert resumed == b'event: end\ndata: {"live":true}\n\n'
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_live_player_http_reports_fragment_history_gap_and_bad_queries(
    tmp_path: Path,
) -> None:
    media = FragmentedMP4Buffer(max_fragments=1)
    media.feed(
        _box(b"ftyp")
        + _box(b"moov", b"avc1")
        + _box(b"moof", b"one")
        + _box(b"mdat", b"ONE")
        + _box(b"moof", b"two")
        + _box(b"mdat", b"TWO")
    )
    metadata = BoundedBroadcast[MetadataSample](max_items=1)
    metadata.publish(MetadataSample(0.0, 0, 1, 258, {}))
    metadata.publish(MetadataSample(1.0, 90_000, 1, 258, {}))
    metadata.close()
    handler = partial(
        PlayerHTTPRequestHandler,
        directory=str(tmp_path),
        live_media=media,
        live_metadata=metadata,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for path, expected in (
            ("/media/fragment?after=-1&wait=0", 409),
            ("/media/fragment?after=50&wait=0", 409),
            ("/media/fragment?after=wat", 400),
            ("/media/fragment?after=-2", 400),
            ("/media/init.mp4?wait=31", 400),
        ):
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            connection.request("GET", path)
            response = connection.getresponse()
            assert response.status == expected
            response.read()
            connection.close()

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request("GET", "/metadata/live?after=-1")
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200
        assert b'event: reset\ndata: {"dropped":1,"oldest_id":1}\n\n' in body
        assert b"event: sample\nid: 1\n" in body
        connection.close()

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request(
            "GET",
            "/metadata/live?after=-1",
            headers={"Last-Event-ID": "50"},
        )
        response = connection.getresponse()
        restarted = response.read()
        assert response.status == 200
        assert (
            b'event: reset\ndata: {"dropped":0,"oldest_id":1,"reason":"cursor_ahead"}\n\n'
        ) in restarted
        assert b"event: sample\nid: 1\n" in restarted
        connection.close()

        ended = FragmentedMP4Buffer()
        ended.close(error="stream failed before initialization")
        server.RequestHandlerClass.keywords["live_media"] = ended
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request("GET", "/media/init.mp4?wait=0")
        response = connection.getresponse()
        assert response.status == 410
        assert b"stream failed before initialization" in response.read()
        connection.close()
    finally:
        media.close(error="test ended")
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_live_player_http_fans_out_identical_media_and_metadata_concurrently(
    tmp_path: Path,
) -> None:
    viewer_count = 8
    item_count = 16
    initialization = _box(b"ftyp", b"isom") + _box(b"moov", b"avc1")
    media = FragmentedMP4Buffer(max_fragments=item_count)
    media.feed(initialization)
    metadata = BoundedBroadcast[MetadataSample](max_items=item_count)
    handler = partial(
        PlayerHTTPRequestHandler,
        directory=str(tmp_path),
        live_media=media,
        live_metadata=metadata,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    ready = Barrier(viewer_count + 1)
    error_lock = Lock()
    errors: list[BaseException] = []
    results: list[tuple[tuple[bytes, ...], bytes] | None] = [None] * viewer_count
    publication_finished = False

    def request(path: str) -> tuple[int, dict[str, str], bytes]:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read()
        headers = {name: value for name, value in response.getheaders()}
        status = response.status
        connection.close()
        return status, headers, body

    def consume(viewer: int) -> None:
        try:
            status, _headers, body = request("/media/init.mp4?wait=2")
            assert status == 200
            assert body == initialization
            ready.wait(timeout=5)

            fragments: list[bytes] = []
            after = -1
            while True:
                status, headers, body = request(f"/media/fragment?after={after}&wait=2")
                if status == 204:
                    assert headers.get("X-Stream-End") == "true"
                    break
                assert status == 200
                after = int(headers["X-Fragment-ID"])
                fragments.append(body)

            status, _headers, events = request("/metadata/live?after=-1")
            assert status == 200
            results[viewer] = (tuple(fragments), events)
        except BaseException as error:
            with error_lock:
                errors.append(error)
            ready.abort()

    viewers = [
        Thread(target=consume, args=(viewer,), name=f"viewer-{viewer}")
        for viewer in range(viewer_count)
    ]
    for viewer in viewers:
        viewer.start()
    try:
        ready.wait(timeout=5)
        expected_fragments = tuple(
            _box(b"moof", index.to_bytes(4, "big"))
            + _box(b"mdat", f"frame-{index}".encode("ascii"))
            for index in range(item_count)
        )
        for index, fragment in enumerate(expected_fragments):
            media.feed(fragment)
            metadata.publish(MetadataSample(float(index), index * 90_000, 1, 258, {}))
        media.finish()
        metadata.close()
        publication_finished = True
        for viewer in viewers:
            viewer.join(timeout=10)

        assert not errors
        assert all(not viewer.is_alive() for viewer in viewers)
        for result in results:
            assert result is not None
            fragments, events = result
            assert fragments == expected_fragments
            assert events.count(b"event: sample\n") == item_count
            assert events.count(b"event: reset\n") == 0
            assert b"event: sample\nid: 0\n" in events
            assert f"event: sample\nid: {item_count - 1}\n".encode() in events
            assert events.endswith(b'event: end\ndata: {"live":true}\n\n')
    except BrokenBarrierError:
        pytest.fail(f"a viewer failed before publication: {errors!r}")
    finally:
        if not media.closed:
            media.close(error="test ended")
        if not publication_finished:
            metadata.close(error="test ended")
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
        for viewer in viewers:
            viewer.join(timeout=2)
