from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from functools import partial
from http.server import ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlsplit

import pytest

from stanag4609.player.live import LivePlayerGateway
from stanag4609.player.server import PlayerHTTPRequestHandler
from stanag4609.player.timeline import MetadataSample, MetadataTimeline, OverlayDetection

playwright = pytest.importorskip("playwright.sync_api")


def _write_fixture_media(destination: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.fail("FFmpeg is required for the browser-player acceptance test")
    subprocess.run(
        (
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x90:r=10:d=2",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-y",
            str(destination),
        ),
        check=True,
        capture_output=True,
        text=True,
    )


def _write_live_transport(destination: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.fail("FFmpeg is required for the live browser-player acceptance test")
    subprocess.run(
        (
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=15:duration=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=3",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-g",
            "15",
            "-c:a",
            "aac",
            "-shortest",
            "-f",
            "mpegts",
            "-y",
            str(destination),
        ),
        check=True,
        capture_output=True,
        text=True,
    )


class _QuietPlayerHandler(PlayerHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


def _sample(
    time_seconds: float,
    *,
    latitude: float,
    target_count: int,
) -> MetadataSample:
    detections = tuple(
        OverlayDetection(
            target_id=index + 1,
            status="active_moving",
            confidence=93,
            label="truck",
            algorithm_id=1,
            algorithm_name="fixture-detector",
            left=0.1 + 0.2 * index,
            top=0.15,
            right=0.25 + 0.2 * index,
            bottom=0.45,
            center_x=0.175 + 0.2 * index,
            center_y=0.3,
        )
        for index in range(target_count)
    )
    geospatial = (
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-123.0, latitude]},
            "properties": {"role": "sensor"},
        },
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-122.9, latitude + 0.1]},
            "properties": {"role": "frame_center"},
        },
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-122.8, latitude + 0.2]},
            "properties": {"role": "target"},
        },
    )
    return MetadataSample(
        time_seconds=time_seconds,
        pts=90_000 + round(time_seconds * 90_000),
        program_number=1,
        pid=258,
        fields={
            "Precision Time Stamp": {
                "value": f"2026-09-05T13:00:0{round(time_seconds)}+00:00",
                "units": "MISP microseconds since epoch",
            },
            "Platform Heading Angle": {"value": 180.0, "units": "degrees"},
            "Platform Pitch Angle": {"value": 1.25, "units": "degrees"},
            "Platform Roll Angle": {"value": -2.5, "units": "degrees"},
            "Platform Status": {"value": 9, "display": "Egress (9)"},
            "Sensor Latitude": {"value": latitude, "units": "degrees"},
            "Sensor Longitude": {"value": -123.0, "units": "degrees"},
            "Sensor True Altitude": {"value": 1500.0, "units": "metres"},
            "Sensor Control Mode": {"value": 6, "display": "Auto Tracking (6)"},
            "Sensor Relative Azimuth Angle": {"value": 45.0, "units": "degrees"},
            "Sensor Relative Elevation Angle": {"value": -20.0, "units": "degrees"},
            "Sensor Relative Roll Angle": {"value": 0.0, "units": "degrees"},
            "Frame Center Latitude": {"value": latitude + 0.1, "units": "degrees"},
            "Frame Center Longitude": {"value": -122.9, "units": "degrees"},
            "Frame Center Elevation": {"value": 100.0, "units": "metres"},
            "Target Location Latitude": {"value": latitude + 0.2, "units": "degrees"},
            "Target Location Longitude": {"value": -122.8, "units": "degrees"},
            "Target Location Elevation": {"value": 90.0, "units": "metres"},
            "AI Sidecar": {"value": "deterministic browser fixture"},
        },
        geospatial=geospatial,
        detections=detections,
        issues=("fixture diagnostic",) if time_seconds == 0 else (),
    )


@pytest.fixture
def player_url(tmp_path: Path) -> Iterator[str]:
    timeline = MetadataTimeline(
        video_start_pts=90_000,
        media_start_pts=90_000,
        samples=(
            _sample(0.0, latitude=49.0, target_count=1),
            _sample(0.5, latitude=50.0, target_count=2),
        ),
    )
    static = files("stanag4609.player").joinpath("static/index.html")
    (tmp_path / "index.html").write_text(static.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "timeline.json").write_text(timeline.to_json(), encoding="utf-8")
    _write_fixture_media(tmp_path / "media.mp4")
    handler = partial(_QuietPlayerHandler, directory=str(tmp_path), timeline=timeline)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _canvas_has_ink(page: object, selector: str) -> bool:
    return bool(
        page.locator(selector).evaluate(  # type: ignore[attr-defined]
            """canvas => {
                const pixels = canvas.getContext('2d').getImageData(
                    0, 0, canvas.width, canvas.height
                ).data;
                for (let index = 3; index < pixels.length; index += 4) {
                    if (pixels[index] !== 0) return true;
                }
                return false;
            }"""
        )
    )


@pytest.mark.browser
def test_reference_player_renders_and_resynchronizes_in_chromium(player_url: str) -> None:
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(player_url, wait_until="domcontentloaded")
        video = page.locator("#video")
        video.evaluate(
            """async video => {
                video.pause();
                if (video.readyState < 1) {
                    await new Promise(resolve => video.addEventListener(
                        'loadedmetadata', resolve, {once: true}
                    ));
                }
                video.currentTime = 0;
            }"""
        )

        playwright.expect(page.locator("#status")).to_contain_text(
            "t=0.000s · PID 258 · PTS 90000 · 1 targets · 1 diagnostics"
        )
        playwright.expect(page.locator("#fields")).to_contain_text("Sensor Latitude49degrees")
        playwright.expect(page.locator("#fields")).to_contain_text("Platform StatusEgress (9)")
        playwright.expect(page.locator("#fields")).to_contain_text(
            "Sensor Control ModeAuto Tracking (6)"
        )
        playwright.expect(page.locator("#fields")).to_contain_text(
            "Target Location Longitude-122.8degrees"
        )
        playwright.expect(page.locator("#diagnostics")).to_contain_text("fixture diagnostic")
        playwright.expect(page.locator("#map-caption")).not_to_contain_text("Waiting")
        assert _canvas_has_ink(page, "#overlay")
        assert _canvas_has_ink(page, "#map")

        video.evaluate("video => { video.currentTime = 0.75; }")
        playwright.expect(page.locator("#status")).to_contain_text(
            "t=0.500s · PID 258 · PTS 135000 · 2 targets · 0 diagnostics"
        )
        playwright.expect(page.locator("#fields")).to_contain_text("Sensor Latitude50degrees")
        playwright.expect(page.locator("#diagnostics")).to_be_hidden()
        browser.close()


@pytest.mark.browser
def test_reference_player_restarts_sse_from_current_media_time(player_url: str) -> None:
    event_requests: list[str] = []
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        page = browser.new_page()
        page.on(
            "request",
            lambda request: event_requests.append(request.url)
            if "/metadata/events?" in request.url
            else None,
        )
        page.goto(f"{player_url}?metadata=sse", wait_until="domcontentloaded")
        video = page.locator("#video")
        video.evaluate(
            """async video => {
                if (video.readyState < 1) {
                    await new Promise(resolve => video.addEventListener(
                        'loadedmetadata', resolve, {once: true}
                    ));
                }
                video.currentTime = 0;
                await video.play();
            }"""
        )
        assert video.evaluate("video => !video.paused") is True
        playwright.expect(page.locator("#status")).to_contain_text(
            "SSE live · t=0.000s",
            timeout=5_000,
        )

        video.evaluate("video => { video.currentTime = 0.75; }")
        playwright.expect(page.locator("#status")).to_contain_text(
            "SSE live · t=0.500s",
            timeout=5_000,
        )
        assert len(event_requests) >= 2
        starts = [
            float(parse_qs(urlsplit(url).query)["start"][0])
            for url in event_requests
        ]
        assert starts[0] == pytest.approx(0.0, abs=0.05)
        assert any(start >= 0.7 for start in starts[1:])
        browser.close()


@pytest.mark.browser
def test_live_reference_player_plays_incremental_fragmented_media(
    tmp_path: Path,
) -> None:
    source = tmp_path / "live.ts"
    _write_live_transport(source)
    gateway = LivePlayerGateway(max_media_fragments=8)
    data = source.read_bytes()

    static = files("stanag4609.player").joinpath("static/index.html")
    (tmp_path / "index.html").write_text(static.read_text(encoding="utf-8"), encoding="utf-8")
    handler = partial(
        _QuietPlayerHandler,
        directory=str(tmp_path),
        live_media=gateway.media,
        live_metadata=gateway.metadata,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(
                f"http://127.0.0.1:{server.server_port}/?live=1",
                wait_until="domcontentloaded",
            )
            video = page.locator("#video")
            video.evaluate("video => { video.muted = true; }")
            gateway.metadata.publish(_sample(0.0, latitude=49.0, target_count=1))
            gateway.metadata.publish(_sample(0.5, latitude=50.0, target_count=2))
            split = ((len(data) * 3) // 4 // 1316) * 1316
            for offset in range(0, split, 1316):
                gateway.feed(data[offset : offset + 1316])
            initialization = gateway.media.initialization(timeout=5)
            before_eof = gateway.media.poll(after_id=-1, timeout=5)
            assert initialization.first_fragment_id == 0
            assert before_eof.items
            assert not gateway.media.closed
            for offset in range(split, len(data), 1316):
                gateway.feed(data[offset : offset + 1316])
            gateway.finish(timeout=20)
            assert gateway.stats.media_fragments >= 2
            assert "mp4a.40.2" in gateway.media.initialization(timeout=0).mime_type
            page.wait_for_function(
                """() => {
                    const video = document.querySelector('#video');
                    return video.readyState >= 2 && video.currentTime > 0.2;
                }""",
                timeout=10_000,
            )
            playwright.expect(page.locator("#status")).to_contain_text("SSE live")
            playwright.expect(page.locator("#fields")).to_contain_text(
                "Sensor Latitude50degrees",
                timeout=5_000,
            )
            assert video.evaluate("video => video.error") is None
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
