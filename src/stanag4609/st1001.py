"""MISB ST 1001.1 audio profile helpers for MPEG-2 Transport Streams.

The standard selects three compressed-audio formats.  This module validates
their MPEG-TS signaling and inspects the fixed frame headers needed by live
applications to route audio streams and expose basic channel configuration.
It deliberately does not decode compressed samples.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from fractions import Fraction
from typing import TYPE_CHECKING, TypeAlias

from stanag4609.errors import DecodeError, LimitExceeded, TruncatedData

if TYPE_CHECKING:
    from stanag4609.transport.psi import ElementaryStreamInfo, ProgramMapTable


class AudioCodec(Enum):
    """Compressed audio formats permitted by MISB ST 1001.1 Table 1."""

    MPEG1_LAYER_II = "mpeg-1-layer-ii"
    MPEG2_LAYER_II = "mpeg-2-layer-ii"
    MPEG2_AAC_LC = "mpeg-2-aac-lc"


class MPEGAudioChannelMode(IntEnum):
    """Two-bit channel-mode value in an MPEG Layer II frame header."""

    STEREO = 0
    JOINT_STEREO = 1
    DUAL_CHANNEL = 2
    SINGLE_CHANNEL = 3


class AACChannelConfiguration(IntEnum):
    """ADTS channel_configuration values defined for MPEG-2 AAC."""

    PROGRAM_CONFIG_ELEMENT = 0
    MONO = 1
    STEREO = 2
    THREE_CHANNEL = 3
    FOUR_CHANNEL = 4
    FIVE_CHANNEL = 5
    FIVE_ONE = 6
    SEVEN_ONE = 7

    @property
    def channel_count(self) -> int | None:
        """Return the channel count, or ``None`` when an in-band PCE defines it."""
        return (None, 1, 2, 3, 4, 5, 6, 8)[int(self)]


@dataclass(frozen=True, slots=True)
class AudioStream:
    """One ST 1001-compliant elementary stream advertised by a PMT."""

    stream: ElementaryStreamInfo
    codec: AudioCodec

    @property
    def pid(self) -> int:
        return self.stream.elementary_pid

    @property
    def stream_type(self) -> int:
        return self.stream.stream_type


@dataclass(frozen=True, slots=True)
class ST1001ValidationIssue:
    """One ST 1001.1 profile violation in an MPEG-TS program map."""

    code: str
    requirement: str
    message: str
    elementary_pid: int | None
    stream_type: int | None


@dataclass(frozen=True, slots=True)
class MPEGLayerIIHeader:
    """Decoded MPEG-1 or MPEG-2 Layer II fixed frame header."""

    codec: AudioCodec
    has_crc: bool
    bitrate: int | None
    sample_rate: int
    padding: bool
    channel_mode: MPEGAudioChannelMode
    frame_length: int | None

    @property
    def channel_count(self) -> int:
        """Return one for single-channel mode and two for the other base modes."""
        return 1 if self.channel_mode is MPEGAudioChannelMode.SINGLE_CHANNEL else 2


@dataclass(frozen=True, slots=True)
class AACADTSHeader:
    """Decoded MPEG-2 AAC-LC ADTS fixed and variable header fields."""

    codec: AudioCodec
    has_crc: bool
    sample_rate: int
    channel_configuration: AACChannelConfiguration
    frame_length: int
    header_length: int
    raw_data_blocks: int

    @property
    def channel_count(self) -> int | None:
        return self.channel_configuration.channel_count


AudioFrameHeader: TypeAlias = MPEGLayerIIHeader | AACADTSHeader


@dataclass(frozen=True, slots=True)
class CompressedAudioFrame:
    """One complete ST 1001 compressed audio frame and parsed header."""

    raw: bytes
    offset: int
    header: AudioFrameHeader

    @property
    def codec(self) -> AudioCodec:
        return self.header.codec

    @property
    def sample_rate(self) -> int:
        return self.header.sample_rate

    @property
    def channel_count(self) -> int | None:
        return self.header.channel_count

    @property
    def sample_count(self) -> int:
        if isinstance(self.header, MPEGLayerIIHeader):
            return 1_152
        return 1_024 * self.header.raw_data_blocks

    @property
    def duration_seconds(self) -> Fraction:
        return Fraction(self.sample_count, self.sample_rate)


_CODECS_BY_STREAM_TYPE = {
    0x03: AudioCodec.MPEG1_LAYER_II,
    0x04: AudioCodec.MPEG2_LAYER_II,
    0x0F: AudioCodec.MPEG2_AAC_LC,
}

# Audio stream types recognized by the transport router but outside ST 1001.1.
_NON_PROFILE_AUDIO_STREAM_TYPES = frozenset({0x11, 0x1C, 0x2D})

_MPEG1_LAYER_II_BITRATES = (
    None,
    32_000,
    48_000,
    56_000,
    64_000,
    80_000,
    96_000,
    112_000,
    128_000,
    160_000,
    192_000,
    224_000,
    256_000,
    320_000,
    384_000,
)
_MPEG2_LAYER_II_BITRATES = (
    None,
    8_000,
    16_000,
    24_000,
    32_000,
    40_000,
    48_000,
    56_000,
    64_000,
    80_000,
    96_000,
    112_000,
    128_000,
    144_000,
    160_000,
)
_MPEG1_SAMPLE_RATES = (44_100, 48_000, 32_000)
_MPEG2_SAMPLE_RATES = (22_050, 24_000, 16_000)
_AAC_SAMPLE_RATES = (
    96_000,
    88_200,
    64_000,
    48_000,
    44_100,
    32_000,
    24_000,
    22_050,
    16_000,
    12_000,
    11_025,
    8_000,
    7_350,
)


def audio_codec_for_stream_type(stream_type: int) -> AudioCodec | None:
    """Map an MPEG-TS ``stream_type`` to an ST 1001 codec, if permitted."""
    if isinstance(stream_type, bool) or not isinstance(stream_type, int):
        raise TypeError("stream_type must be an integer")
    if not 0 <= stream_type <= 0xFF:
        raise ValueError("stream_type must be between 0 and 255")
    return _CODECS_BY_STREAM_TYPE.get(stream_type)


def find_st1001_audio_streams(pmt: ProgramMapTable) -> tuple[AudioStream, ...]:
    """Return every ST 1001-compliant audio elementary stream in PMT order."""
    streams: list[AudioStream] = []
    for stream in pmt.streams:
        codec = audio_codec_for_stream_type(stream.stream_type)
        if codec is not None:
            streams.append(AudioStream(stream, codec))
    return tuple(streams)


def validate_st1001_audio_profile(
    pmt: ProgramMapTable,
    *,
    require_audio: bool = False,
) -> tuple[ST1001ValidationIssue, ...]:
    """Validate the audio codec selection in one program map.

    ``require_audio`` is an application constraint rather than an ST 1001
    requirement: the standard constrains audio when present but does not demand
    that a motion-imagery stream contain audio.
    """
    if not isinstance(require_audio, bool):
        raise TypeError("require_audio must be a boolean")
    issues: list[ST1001ValidationIssue] = []
    permitted = 0
    for stream in pmt.streams:
        if audio_codec_for_stream_type(stream.stream_type) is not None:
            permitted += 1
        elif stream.stream_type in _NON_PROFILE_AUDIO_STREAM_TYPES:
            issues.append(
                ST1001ValidationIssue(
                    "ST1001_AUDIO_CODEC",
                    "ST 1001.1-01",
                    (
                        f"audio PID 0x{stream.elementary_pid:04X} uses stream_type "
                        f"0x{stream.stream_type:02X}, which is outside ST 1001.1 Table 1"
                    ),
                    stream.elementary_pid,
                    stream.stream_type,
                )
            )
    if require_audio and not permitted:
        issues.append(
            ST1001ValidationIssue(
                "ST1001_AUDIO_REQUIRED",
                "application profile",
                "the program does not advertise an ST 1001.1 audio stream",
                None,
                None,
            )
        )
    return tuple(issues)


def parse_mpeg_layer_ii_header(data: bytes | bytearray | memoryview) -> MPEGLayerIIHeader:
    """Decode an MPEG-1/2 Layer II four-byte frame header."""
    raw = bytes(data)
    if len(raw) < 4:
        raise TruncatedData("MPEG Layer II frame header requires four bytes")
    word = int.from_bytes(raw[:4], "big")
    if word >> 21 != 0x7FF:
        raise DecodeError("MPEG Layer II frame sync is invalid")
    version = (word >> 19) & 0x03
    if version == 1:
        raise DecodeError("MPEG Layer II header uses the reserved MPEG version")
    if version == 0:
        raise DecodeError("MPEG-2.5 Layer II is outside ST 1001.1")
    if (word >> 17) & 0x03 != 2:
        raise DecodeError("audio frame is not MPEG Layer II")

    bitrate_index = (word >> 12) & 0x0F
    if bitrate_index == 0x0F:
        raise DecodeError("MPEG Layer II bitrate index 15 is reserved")
    sample_rate_index = (word >> 10) & 0x03
    if sample_rate_index == 3:
        raise DecodeError("MPEG Layer II sample-rate index 3 is reserved")

    if version == 3:
        codec = AudioCodec.MPEG1_LAYER_II
        bitrate = _MPEG1_LAYER_II_BITRATES[bitrate_index]
        sample_rate = _MPEG1_SAMPLE_RATES[sample_rate_index]
    else:
        codec = AudioCodec.MPEG2_LAYER_II
        bitrate = _MPEG2_LAYER_II_BITRATES[bitrate_index]
        sample_rate = _MPEG2_SAMPLE_RATES[sample_rate_index]
    padding = bool((word >> 9) & 1)
    frame_length = (
        None if bitrate is None else (144 * bitrate) // sample_rate + int(padding)
    )
    return MPEGLayerIIHeader(
        codec,
        not bool((word >> 16) & 1),
        bitrate,
        sample_rate,
        padding,
        MPEGAudioChannelMode((word >> 6) & 0x03),
        frame_length,
    )


def parse_aac_adts_header(data: bytes | bytearray | memoryview) -> AACADTSHeader:
    """Decode an MPEG-2 AAC-LC ADTS header, including optional CRC presence."""
    raw = bytes(data)
    if len(raw) < 7:
        raise TruncatedData("AAC ADTS frame header requires at least seven bytes")
    if raw[0] != 0xFF or raw[1] & 0xF0 != 0xF0:
        raise DecodeError("AAC ADTS frame sync is invalid")
    if not raw[1] & 0x08:
        raise DecodeError("AAC ADTS header signals MPEG-4 rather than MPEG-2")
    if raw[1] & 0x06:
        raise DecodeError("AAC ADTS layer must be zero")
    profile = raw[2] >> 6
    if profile != 1:
        raise DecodeError("AAC ADTS profile is not AAC-LC")
    sample_rate_index = (raw[2] >> 2) & 0x0F
    if sample_rate_index >= len(_AAC_SAMPLE_RATES):
        raise DecodeError("AAC ADTS sample-rate index is reserved")

    protection_absent = bool(raw[1] & 1)
    header_length = 7 if protection_absent else 9
    if len(raw) < header_length:
        raise TruncatedData("AAC ADTS header declares a CRC but fewer than nine bytes remain")
    channel_configuration = ((raw[2] & 1) << 2) | (raw[3] >> 6)
    frame_length = ((raw[3] & 3) << 11) | (raw[4] << 3) | (raw[5] >> 5)
    if frame_length < header_length:
        raise DecodeError("AAC ADTS frame length is shorter than its header")
    return AACADTSHeader(
        AudioCodec.MPEG2_AAC_LC,
        not protection_absent,
        _AAC_SAMPLE_RATES[sample_rate_index],
        AACChannelConfiguration(channel_configuration),
        frame_length,
        header_length,
        (raw[6] & 0x03) + 1,
    )


class AudioFrameParser:
    """Incrementally reconstruct ST 1001 Layer II or AAC-LC frames.

    Input chunks and PES payloads may split a frame at any byte. Completed
    frames are released immediately and the retained partial frame is bounded.
    Each audio elementary PID should use an independent parser instance.
    """

    def __init__(self, codec: AudioCodec, *, max_frame_length: int = 64 * 1024) -> None:
        if not isinstance(codec, AudioCodec):
            raise TypeError("codec must be an AudioCodec")
        minimum_header = 7 if codec is AudioCodec.MPEG2_AAC_LC else 4
        if (
            isinstance(max_frame_length, bool)
            or not isinstance(max_frame_length, int)
            or max_frame_length < minimum_header
        ):
            raise ValueError(
                f"max_frame_length must be an integer of at least {minimum_header}"
            )
        self.codec = codec
        self.max_frame_length = max_frame_length
        self._buffer = bytearray()
        self._offset = 0

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    @property
    def stream_offset(self) -> int:
        return self._offset

    def feed(
        self,
        data: bytes | bytearray | memoryview,
    ) -> list[CompressedAudioFrame]:
        """Consume bytes and return every newly completed compressed frame."""

        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("audio frame input must be bytes-like")
        self._buffer.extend(data)
        frames: list[CompressedAudioFrame] = []
        minimum_header = 7 if self.codec is AudioCodec.MPEG2_AAC_LC else 4
        while len(self._buffer) >= minimum_header:
            if self.codec is AudioCodec.MPEG2_AAC_LC:
                try:
                    aac_header = parse_aac_adts_header(self._buffer)
                except TruncatedData:
                    break
                header: AudioFrameHeader = aac_header
                frame_length: int = aac_header.frame_length
            else:
                layer_header = parse_mpeg_layer_ii_header(self._buffer)
                if layer_header.codec is not self.codec:
                    raise DecodeError(
                        f"audio frame is {layer_header.codec.value}, expected {self.codec.value}"
                    )
                if layer_header.frame_length is None:
                    raise DecodeError(
                        "free-format MPEG Layer II frame length cannot be inferred"
                    )
                header = layer_header
                frame_length = layer_header.frame_length
            if frame_length > self.max_frame_length:
                raise LimitExceeded(
                    f"audio frame length {frame_length} exceeds configured limit "
                    f"{self.max_frame_length}"
                )
            if len(self._buffer) < frame_length:
                break
            raw = bytes(self._buffer[:frame_length])
            frames.append(CompressedAudioFrame(raw, self._offset, header))
            del self._buffer[:frame_length]
            self._offset += frame_length
        return frames

    def finish(self) -> list[CompressedAudioFrame]:
        """Signal end of stream and reject an incomplete final audio frame."""

        frames = self.feed(b"")
        if self._buffer:
            raise TruncatedData(
                f"audio stream ended with {len(self._buffer)} incomplete byte(s) "
                f"at offset {self._offset}"
            )
        return frames
