"""Exact presentation timing for ST 1001 audio carried in PES packets."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from stanag4609.errors import DecodeError
from stanag4609.st1001 import AudioCodec, AudioFrameParser, CompressedAudioFrame
from stanag4609.transport.demux import PESStreamEvent, StreamKind
from stanag4609.transport.timing import PTS_CLOCK_RATE, PTSTimeline


@dataclass(frozen=True, slots=True)
class TimedCompressedAudioFrame:
    """One compressed audio frame with an exact unwrapped presentation time."""

    program_number: int
    pid: int
    frame: CompressedAudioFrame
    presentation_ticks: Fraction | None
    explicit_pts: bool

    @property
    def presentation_seconds(self) -> Fraction | None:
        """Return the presentation time in seconds, if a PTS anchor is known."""

        if self.presentation_ticks is None:
            return None
        return self.presentation_ticks / PTS_CLOCK_RATE


class AudioPESFrameParser:
    """Reconstruct and timestamp one ST 1001 audio elementary stream.

    H.222.0 defines an audio PES PTS as referring to the first audio access unit
    whose first byte occurs in that PES packet. This parser preserves that rule
    when a compressed frame straddles PES boundaries and derives subsequent
    presentation times from each frame's exact rational sample duration.

    Instances bind to the program, PID, and codec of their first event. Keep one
    instance per audio PID in a live demultiplexing pipeline.
    """

    __slots__ = (
        "_anchors",
        "_codec",
        "_frames_emitted",
        "_max_frame_length",
        "_next_presentation_ticks",
        "_parser",
        "_pid",
        "_program_number",
        "_timeline",
    )

    def __init__(self, *, max_frame_length: int = 64 * 1024) -> None:
        if (
            isinstance(max_frame_length, bool)
            or not isinstance(max_frame_length, int)
            or max_frame_length < 7
        ):
            raise ValueError("max_frame_length must be an integer of at least seven")
        self._max_frame_length = max_frame_length
        self._program_number: int | None = None
        self._pid: int | None = None
        self._codec: AudioCodec | None = None
        self._parser: AudioFrameParser | None = None
        self._timeline = PTSTimeline()
        self._anchors: dict[int, Fraction] = {}
        self._next_presentation_ticks: Fraction | None = None
        self._frames_emitted = 0

    @property
    def program_number(self) -> int | None:
        return self._program_number

    @property
    def pid(self) -> int | None:
        return self._pid

    @property
    def codec(self) -> AudioCodec | None:
        return self._codec

    @property
    def buffered_bytes(self) -> int:
        return 0 if self._parser is None else self._parser.buffered_bytes

    @property
    def frames_emitted(self) -> int:
        return self._frames_emitted

    def feed(self, event: PESStreamEvent) -> list[TimedCompressedAudioFrame]:
        """Consume one audio PES event and return newly completed timed frames."""

        if not isinstance(event, PESStreamEvent):
            raise TypeError("event must be a PESStreamEvent")
        if event.kind is not StreamKind.AUDIO:
            raise ValueError("event must describe an audio PES")
        codec = event.audio_codec
        if codec is None:
            raise ValueError("audio PES does not use an ST 1001 codec")
        self._bind_or_validate(event, codec)
        assert self._parser is not None

        if any(packet.discontinuity_indicator for packet in event.pes.transport_packets):
            self._timeline.reset()
            self._anchors.clear()
            self._next_presentation_ticks = None

        pes_payload_offset = self._parser.stream_offset + self._parser.buffered_bytes
        frames = self._parser.feed(event.pes.payload)
        if event.pes.pts is not None:
            anchor_offset = self._first_access_unit_offset(frames, pes_payload_offset)
            if anchor_offset is None:
                raise DecodeError(
                    "audio PES carries PTS but no audio access unit commences in its payload"
                )
            self._anchors[anchor_offset] = Fraction(self._timeline.observe(event.pes.pts))
        return self._time_frames(frames)

    def finish(self) -> list[TimedCompressedAudioFrame]:
        """Signal end of stream and reject incomplete compressed state."""

        if self._parser is None:
            return []
        frames = self._parser.finish()
        timed = self._time_frames(frames)
        if self._anchors:
            raise DecodeError("audio PTS refers to an access unit absent at end of stream")
        return timed

    def _bind_or_validate(self, event: PESStreamEvent, codec: AudioCodec) -> None:
        if self._parser is None:
            self._program_number = event.program_number
            self._pid = event.pid
            self._codec = codec
            self._parser = AudioFrameParser(codec, max_frame_length=self._max_frame_length)
            return
        if event.program_number != self._program_number:
            raise ValueError(
                f"audio PES program {event.program_number} does not match bound program "
                f"{self._program_number}"
            )
        if event.pid != self._pid:
            raise ValueError(f"audio PES PID {event.pid} does not match bound PID {self._pid}")
        if codec is not self._codec:
            assert self._codec is not None
            raise ValueError(
                f"audio PES codec {codec.value} does not match bound codec "
                f"{self._codec.value}"
            )

    def _first_access_unit_offset(
        self,
        frames: list[CompressedAudioFrame],
        pes_payload_offset: int,
    ) -> int | None:
        candidates = [
            frame.offset for frame in frames if frame.offset >= pes_payload_offset
        ]
        assert self._parser is not None
        if (
            self._parser.buffered_bytes
            and self._parser.stream_offset >= pes_payload_offset
        ):
            candidates.append(self._parser.stream_offset)
        return min(candidates, default=None)

    def _time_frames(
        self,
        frames: list[CompressedAudioFrame],
    ) -> list[TimedCompressedAudioFrame]:
        output: list[TimedCompressedAudioFrame] = []
        assert self._program_number is not None
        assert self._pid is not None
        for frame in frames:
            anchor = self._anchors.pop(frame.offset, None)
            explicit = anchor is not None
            if anchor is not None:
                self._next_presentation_ticks = anchor
            presentation = self._next_presentation_ticks
            output.append(
                TimedCompressedAudioFrame(
                    self._program_number,
                    self._pid,
                    frame,
                    presentation,
                    explicit,
                )
            )
            if presentation is not None:
                self._next_presentation_ticks = (
                    presentation + frame.duration_seconds * PTS_CLOCK_RATE
                )
        self._frames_emitted += len(output)
        return output
