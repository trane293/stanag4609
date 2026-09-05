"""Optional direct FFmpeg audio decoding through PyAV codec contexts."""

from __future__ import annotations

import importlib
from collections.abc import Iterable
from typing import Any

from stanag4609.st1001 import AudioCodec, CompressedAudioFrame


class PyAVAudioDecoder:
    """Decode complete ST 1001 frames to native PyAV ``AudioFrame`` objects.

    PyAV is imported only when an instance is created, so the package core
    remains dependency-free. The returned native frames retain FFmpeg's sample
    format, planes, channel layout, and timing; callers can resample them with
    PyAV or pass them directly to an audio output pipeline.
    """

    __slots__ = (
        "_codec_context",
        "_decoded_frames",
        "_finished",
        "_packet_factory",
        "_submitted_frames",
        "codec",
    )

    def __init__(
        self,
        codec: AudioCodec,
        *,
        thread_count: int | None = None,
        av_module: Any | None = None,
    ) -> None:
        if not isinstance(codec, AudioCodec):
            raise TypeError("codec must be an AudioCodec")
        if thread_count is not None and (
            isinstance(thread_count, bool)
            or not isinstance(thread_count, int)
            or thread_count < 1
        ):
            raise ValueError("thread_count must be a positive integer or None")
        if av_module is None:
            try:
                av_module = importlib.import_module("av")
            except ModuleNotFoundError as error:
                raise RuntimeError(
                    "PyAV is not installed; install stanag4609[audio-pyav]"
                ) from error
        codec_context_type = getattr(av_module, "CodecContext", None)
        create = getattr(codec_context_type, "create", None)
        packet_factory = getattr(av_module, "Packet", None)
        if not callable(create) or not callable(packet_factory):
            raise TypeError("PyAV backend must expose CodecContext.create and Packet")

        codec_name = "aac" if codec is AudioCodec.MPEG2_AAC_LC else "mp2"
        context = create(codec_name, "r")
        if not callable(getattr(context, "decode", None)):
            raise TypeError("PyAV CodecContext must expose a callable decode method")
        if thread_count is not None:
            context.thread_count = thread_count
        self.codec = codec
        self._codec_context = context
        self._packet_factory = packet_factory
        self._submitted_frames = 0
        self._decoded_frames = 0
        self._finished = False

    @property
    def codec_context(self) -> Any:
        """Return the native PyAV codec context for advanced configuration."""

        return self._codec_context

    @property
    def submitted_frames(self) -> int:
        return self._submitted_frames

    @property
    def decoded_frames(self) -> int:
        return self._decoded_frames

    @property
    def finished(self) -> bool:
        return self._finished

    def decode(self, frame: CompressedAudioFrame) -> tuple[Any, ...]:
        """Submit one compressed frame and return available native audio frames."""

        if self._finished:
            raise RuntimeError("cannot decode after the PyAV codec context was flushed")
        if not isinstance(frame, CompressedAudioFrame):
            raise TypeError("frame must be a CompressedAudioFrame")
        if frame.codec is not self.codec:
            raise ValueError(
                f"compressed frame codec {frame.codec.value} does not match decoder codec "
                f"{self.codec.value}"
            )
        packet = self._packet_factory(frame.raw)
        result = self._decode_native(packet)
        self._submitted_frames += 1
        self._decoded_frames += len(result)
        return result

    def decode_many(
        self,
        frames: Iterable[CompressedAudioFrame],
    ) -> tuple[Any, ...]:
        """Submit compressed frames in order and flatten available output."""

        output: list[Any] = []
        for frame in frames:
            output.extend(self.decode(frame))
        return tuple(output)

    def flush(self) -> tuple[Any, ...]:
        """Flush delayed native frames and make the decoder immutable."""

        if self._finished:
            return ()
        result = self._decode_native(None)
        self._decoded_frames += len(result)
        self._finished = True
        return result

    def _decode_native(self, packet: Any | None) -> tuple[Any, ...]:
        result = self._codec_context.decode(packet)
        if isinstance(result, (str, bytes)) or not isinstance(result, Iterable):
            raise TypeError("PyAV CodecContext.decode must return an iterable of frames")
        return tuple(result)
