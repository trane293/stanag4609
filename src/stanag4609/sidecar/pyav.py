"""Optional PyAV video decoding into model-neutral sidecar frames."""

from __future__ import annotations

import importlib
from collections.abc import Iterator, Mapping
from fractions import Fraction
from typing import Any

from stanag4609.sidecar.model import FrameEnvelope
from stanag4609.transport.timing import PTS_CLOCK_RATE, PTS_MODULUS


def _rescale_pts(pts: int, time_base: Any) -> int:
    try:
        seconds_per_tick = Fraction(time_base)
    except (TypeError, ValueError, ZeroDivisionError) as error:
        raise ValueError("decoded video frame has an invalid time base") from error
    if seconds_per_tick <= 0:
        raise ValueError("decoded video frame time base must be positive")
    ticks = pts * seconds_per_tick * PTS_CLOCK_RATE
    numerator = ticks.numerator
    denominator = ticks.denominator
    if numerator < 0:
        rounded = -((-2 * numerator + denominator) // (2 * denominator))
    else:
        rounded = (2 * numerator + denominator) // (2 * denominator)
    return rounded % PTS_MODULUS


class PyAVFrameSource:
    """Decode one PyAV video stream into :class:`FrameEnvelope` values.

    PyAV is imported only when the source is constructed, preserving the
    dependency-free library core. Decoded timestamps are rescaled from the
    native stream time base to the 90 kHz, unsigned 33-bit clock used by
    MPEG-2 transport PTS. Half-tick values round away from zero before normal
    PTS wrapping.

    The default ``bgr24`` pixel format is directly consumable by common OpenCV,
    Ultralytics, and NumPy pipelines. Pass ``pixel_format=None`` to retain each
    native PyAV ``VideoFrame`` instead. Absolute UTC is intentionally not
    inferred from media-relative PTS; use :class:`FrameMetadataCorrelator` to
    attach synchronous KLV and derive it from ST 0601 Item 2.
    """

    __slots__ = (
        "_av",
        "_decoded_frames",
        "_options",
        "pixel_format",
        "program_number",
        "source",
        "video_pid",
        "video_stream",
    )

    def __init__(
        self,
        source: Any,
        *,
        video_stream: int = 0,
        pixel_format: str | None = "bgr24",
        program_number: int = 1,
        video_pid: int | None = None,
        options: Mapping[str, str] | None = None,
        av_module: Any | None = None,
    ) -> None:
        if isinstance(video_stream, bool) or not isinstance(video_stream, int) or video_stream < 0:
            raise ValueError("video_stream must be a nonnegative integer")
        if pixel_format is not None and (not isinstance(pixel_format, str) or not pixel_format):
            raise ValueError("pixel_format must be a non-empty string or None")
        if (
            isinstance(program_number, bool)
            or not isinstance(program_number, int)
            or not 1 <= program_number <= 0xFFFF
        ):
            raise ValueError("program_number must be between 1 and 65535")
        if video_pid is not None and (
            isinstance(video_pid, bool)
            or not isinstance(video_pid, int)
            or not 0 <= video_pid <= 0x1FFF
        ):
            raise ValueError("video_pid must be an integer from 0 to 8191 or None")
        if options is not None and (
            not isinstance(options, Mapping)
            or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in options.items()
            )
        ):
            raise TypeError("options must map strings to strings or be None")
        if av_module is None:
            try:
                av_module = importlib.import_module("av")
            except ModuleNotFoundError as error:
                raise RuntimeError(
                    "PyAV is not installed; install stanag4609[video-pyav]"
                ) from error
        if not callable(getattr(av_module, "open", None)):
            raise TypeError("PyAV backend must expose a callable open function")

        self.source = source
        self.video_stream = video_stream
        self.pixel_format = pixel_format
        self.program_number = program_number
        self.video_pid = video_pid
        self._options = None if options is None else dict(options)
        self._av = av_module
        self._decoded_frames = 0

    @property
    def decoded_frames(self) -> int:
        """Return the number of envelopes yielded across iterations."""

        return self._decoded_frames

    def __iter__(self) -> Iterator[FrameEnvelope]:
        return self.frames()

    def frames(self) -> Iterator[FrameEnvelope]:
        """Open the source, yield decoded frames, and always close the container."""

        container = self._av.open(self.source, options=self._options)
        close = getattr(container, "close", None)
        if not callable(close):
            raise TypeError("PyAV container must expose a callable close method")
        try:
            streams = getattr(getattr(container, "streams", None), "video", ())
            try:
                stream = streams[self.video_stream]
            except (IndexError, KeyError, TypeError) as error:
                raise ValueError(
                    f"source does not contain video stream {self.video_stream}"
                ) from error
            decode = getattr(container, "decode", None)
            if not callable(decode):
                raise TypeError("PyAV container must expose a callable decode method")
            for sequence_number, frame in enumerate(decode(stream)):
                pts = getattr(frame, "pts", None)
                if isinstance(pts, bool) or not isinstance(pts, int):
                    raise ValueError(
                        f"decoded video frame {sequence_number} has no PTS"
                    )
                time_base = getattr(frame, "time_base", None)
                if time_base is None:
                    time_base = getattr(stream, "time_base", None)
                transport_pts = _rescale_pts(pts, time_base)
                width = getattr(frame, "width", None)
                height = getattr(frame, "height", None)
                if isinstance(width, bool) or not isinstance(width, int):
                    raise TypeError("PyAV video frame width must be an integer")
                if isinstance(height, bool) or not isinstance(height, int):
                    raise TypeError("PyAV video frame height must be an integer")
                if self.pixel_format is None:
                    pixels = frame
                else:
                    to_ndarray = getattr(frame, "to_ndarray", None)
                    if not callable(to_ndarray):
                        raise TypeError(
                            "PyAV video frame must expose callable to_ndarray for pixel conversion"
                        )
                    pixels = to_ndarray(format=self.pixel_format)
                envelope = FrameEnvelope(
                    sequence_number=sequence_number,
                    pts=transport_pts,
                    width=width,
                    height=height,
                    pixels=pixels,
                    program_number=self.program_number,
                    video_pid=self.video_pid,
                )
                self._decoded_frames += 1
                yield envelope
        finally:
            close()
