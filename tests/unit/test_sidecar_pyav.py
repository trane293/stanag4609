from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest

from stanag4609.sidecar.pyav import PyAVFrameSource
from stanag4609.transport.timing import PTS_MODULUS


class _Frame:
    def __init__(
        self,
        pts: int | None,
        *,
        time_base: Fraction | None = Fraction(1, 30),
        width: int = 640,
        height: int = 480,
    ) -> None:
        self.pts = pts
        self.time_base = time_base
        self.width = width
        self.height = height
        self.formats: list[str] = []

    def to_ndarray(self, *, format: str) -> tuple[str, int | None]:
        self.formats.append(format)
        return (format, self.pts)


class _Container:
    def __init__(self, frames: list[_Frame], streams: list[object]) -> None:
        self.frames = frames
        self.streams = SimpleNamespace(video=streams)
        self.decode_calls: list[object] = []
        self.closed = False

    def decode(self, stream: object):
        self.decode_calls.append(stream)
        yield from self.frames

    def close(self) -> None:
        self.closed = True


class _AV:
    def __init__(self, container: _Container) -> None:
        self.container = container
        self.calls: list[tuple[object, dict[str, str] | None]] = []

    def open(self, source: object, *, options: dict[str, str] | None = None) -> _Container:
        self.calls.append((source, options))
        return self.container


def test_pyav_frame_source_yields_bgr_envelopes_with_90khz_pts() -> None:
    stream = SimpleNamespace(time_base=Fraction(1, 90_000))
    frames = [_Frame(0), _Frame(1, time_base=Fraction(1, 60), width=1280, height=720)]
    container = _Container(frames, [stream])
    backend = _AV(container)
    source = PyAVFrameSource(
        Path("flight.ts"),
        av_module=backend,
        program_number=7,
        video_pid=0x101,
        options={"rtsp_transport": "tcp"},
    )

    decoded = tuple(source)

    assert [frame.sequence_number for frame in decoded] == [0, 1]
    assert [frame.pts for frame in decoded] == [0, 1_500]
    assert decoded[1].width == 1280
    assert decoded[1].height == 720
    assert decoded[1].pixels == ("bgr24", 1)
    assert decoded[1].program_number == 7
    assert decoded[1].video_pid == 0x101
    assert decoded[1].timestamp_microseconds is None
    assert frames[1].formats == ["bgr24"]
    assert container.decode_calls == [stream]
    assert container.closed
    assert backend.calls == [(Path("flight.ts"), {"rtsp_transport": "tcp"})]
    assert source.decoded_frames == 2


def test_pyav_frame_source_can_retain_native_frames_and_use_stream_time_base() -> None:
    stream = SimpleNamespace(time_base=Fraction(1, 1_000))
    native = _Frame(25, time_base=None)
    source = PyAVFrameSource(
        "flight.ts",
        av_module=_AV(_Container([native], [stream])),
        pixel_format=None,
    )

    frame = next(iter(source))

    assert frame.pts == 2_250
    assert frame.pixels is native
    assert native.formats == []


def test_pyav_frame_source_rounds_and_wraps_transport_pts() -> None:
    stream = SimpleNamespace(time_base=Fraction(1, 180_000))
    frames = [_Frame(1, time_base=stream.time_base), _Frame(-1, time_base=stream.time_base)]
    source = PyAVFrameSource("flight.ts", av_module=_AV(_Container(frames, [stream])))

    decoded = tuple(source)

    assert decoded[0].pts == 1
    assert decoded[1].pts == PTS_MODULUS - 1


def test_pyav_frame_source_closes_container_when_consumer_stops() -> None:
    stream = SimpleNamespace(time_base=Fraction(1, 30))
    container = _Container([_Frame(0), _Frame(1)], [stream])
    frames = iter(PyAVFrameSource("flight.ts", av_module=_AV(container)))

    next(frames)
    frames.close()

    assert container.closed


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"video_stream": -1}, "video_stream"),
        ({"pixel_format": ""}, "pixel_format"),
        ({"program_number": 0}, "program_number"),
        ({"video_pid": 0x2000}, "video_pid"),
        ({"options": {"timeout": 1}}, "options"),
    ],
)
def test_pyav_frame_source_validates_configuration(
    kwargs: dict[str, object], message: str
) -> None:
    backend = _AV(_Container([], []))
    with pytest.raises((TypeError, ValueError), match=message):
        PyAVFrameSource("flight.ts", av_module=backend, **kwargs)  # type: ignore[arg-type]


def test_pyav_frame_source_reports_backend_and_stream_errors() -> None:
    with pytest.raises(TypeError, match="callable open"):
        PyAVFrameSource("flight.ts", av_module=object())

    no_video = PyAVFrameSource("flight.ts", av_module=_AV(_Container([], [])))
    with pytest.raises(ValueError, match="video stream 0"):
        tuple(no_video)

    one_stream = SimpleNamespace(time_base=Fraction(1, 90_000))
    missing_pts = PyAVFrameSource(
        "flight.ts", av_module=_AV(_Container([_Frame(None)], [one_stream]))
    )
    with pytest.raises(ValueError, match="has no PTS"):
        tuple(missing_pts)

    missing_time_base = PyAVFrameSource(
        "flight.ts",
        av_module=_AV(_Container([_Frame(0, time_base=None)], [SimpleNamespace(time_base=None)])),
    )
    with pytest.raises(ValueError, match="time base"):
        tuple(missing_time_base)

    zero_time_base = PyAVFrameSource(
        "flight.ts",
        av_module=_AV(
            _Container([_Frame(0, time_base=Fraction(0))], [one_stream])
        ),
    )
    with pytest.raises(ValueError, match="must be positive"):
        tuple(zero_time_base)


def test_pyav_frame_source_validates_native_container_and_frames() -> None:
    backend = SimpleNamespace(open=lambda _source, *, options=None: object())
    with pytest.raises(TypeError, match="callable close"):
        tuple(PyAVFrameSource("flight.ts", av_module=backend))

    stream = SimpleNamespace(time_base=Fraction(1, 90_000))
    no_decode = SimpleNamespace(
        streams=SimpleNamespace(video=[stream]), close=lambda: None, decode=None
    )
    backend = SimpleNamespace(open=lambda _source, *, options=None: no_decode)
    with pytest.raises(TypeError, match="callable decode"):
        tuple(PyAVFrameSource("flight.ts", av_module=backend))

    for attribute in ("width", "height"):
        frame = _Frame(0)
        setattr(frame, attribute, None)
        source = PyAVFrameSource(
            "flight.ts", av_module=_AV(_Container([frame], [stream]))
        )
        with pytest.raises(TypeError, match=attribute):
            tuple(source)

    frame = _Frame(0)
    frame.to_ndarray = None  # type: ignore[method-assign]
    source = PyAVFrameSource(
        "flight.ts", av_module=_AV(_Container([frame], [stream]))
    )
    with pytest.raises(TypeError, match="to_ndarray"):
        tuple(source)


def test_pyav_frame_source_optional_dependency_error_is_actionable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(_name: str) -> object:
        raise ModuleNotFoundError

    monkeypatch.setattr("stanag4609.sidecar.pyav.importlib.import_module", missing)
    with pytest.raises(RuntimeError, match=r"video-pyav"):
        PyAVFrameSource("flight.ts")


@pytest.mark.integration
def test_real_pyav_frame_source_decodes_video(tmp_path: Path) -> None:
    av = pytest.importorskip("av")
    media = tmp_path / "tiny.mpg"
    container = av.open(media, "w")
    stream = container.add_stream("mpeg2video", rate=30)
    stream.width = 16
    stream.height = 16
    stream.pix_fmt = "yuv420p"
    for index in range(3):
        frame = av.VideoFrame(16, 16, "yuv420p")
        frame.pts = index
        frame.time_base = Fraction(1, 30)
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
    container.close()

    decoded = tuple(PyAVFrameSource(media, pixel_format=None))

    assert len(decoded) == 3
    assert [frame.pts - decoded[0].pts for frame in decoded] == [0, 3_000, 6_000]
    assert all(frame.width == 16 and frame.height == 16 for frame in decoded)
    assert all(type(frame.pixels).__name__ == "VideoFrame" for frame in decoded)
