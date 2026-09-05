from __future__ import annotations

import pytest

from stanag4609.errors import DecodeError, TruncatedData
from stanag4609.st1001 import (
    AACChannelConfiguration,
    AudioCodec,
    AudioFrameParser,
    MPEGAudioChannelMode,
    audio_codec_for_stream_type,
    find_st1001_audio_streams,
    parse_aac_adts_header,
    parse_mpeg_layer_ii_header,
    validate_st1001_audio_profile,
)
from stanag4609.transport.demux import PESStreamEvent, StreamKind
from stanag4609.transport.pes import PESPacket
from stanag4609.transport.psi import ElementaryStreamInfo, ProgramMapTable


def _pmt(*stream_types: int) -> ProgramMapTable:
    streams = tuple(
        ElementaryStreamInfo(stream_type, 0x101 + index, ())
        for index, stream_type in enumerate(stream_types)
    )
    return ProgramMapTable(1, 0, True, 0, 0, 0x100, (), streams, b"")


@pytest.mark.parametrize(
    ("stream_type", "codec"),
    [
        (0x03, AudioCodec.MPEG1_LAYER_II),
        (0x04, AudioCodec.MPEG2_LAYER_II),
        (0x0F, AudioCodec.MPEG2_AAC_LC),
    ],
)
def test_st1001_maps_each_permitted_transport_stream_type(
    stream_type: int, codec: AudioCodec
) -> None:
    assert audio_codec_for_stream_type(stream_type) is codec


def test_st1001_profile_finds_every_permitted_audio_elementary_stream() -> None:
    streams = find_st1001_audio_streams(_pmt(0x1B, 0x03, 0x04, 0x0F, 0x06))
    assert [(stream.pid, stream.codec) for stream in streams] == [
        (0x102, AudioCodec.MPEG1_LAYER_II),
        (0x103, AudioCodec.MPEG2_LAYER_II),
        (0x104, AudioCodec.MPEG2_AAC_LC),
    ]
    assert [stream.stream_type for stream in streams] == [0x03, 0x04, 0x0F]
    assert validate_st1001_audio_profile(_pmt(0x03, 0x04, 0x0F)) == ()


def test_st1001_profile_reports_non_profile_audio_and_optional_absence() -> None:
    issues = validate_st1001_audio_profile(_pmt(0x11, 0x1B), require_audio=True)
    assert [(issue.code, issue.elementary_pid, issue.stream_type) for issue in issues] == [
        ("ST1001_AUDIO_CODEC", 0x101, 0x11),
        ("ST1001_AUDIO_REQUIRED", None, None),
    ]
    assert validate_st1001_audio_profile(_pmt(0x1B)) == ()


def test_st1001_rejects_invalid_arguments() -> None:
    with pytest.raises(TypeError, match="stream_type"):
        audio_codec_for_stream_type("0x03")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="stream_type"):
        audio_codec_for_stream_type(True)
    with pytest.raises(ValueError, match="stream_type"):
        audio_codec_for_stream_type(256)
    with pytest.raises(TypeError, match="require_audio"):
        validate_st1001_audio_profile(_pmt(0x03), require_audio=1)  # type: ignore[arg-type]


def test_mpeg1_layer_ii_header_exposes_crc_bitrate_rate_and_channels() -> None:
    # MPEG-1, Layer II, CRC present, 128 kbit/s, 48 kHz, joint stereo.
    header = parse_mpeg_layer_ii_header(bytes.fromhex("FFFC8444"))
    assert header.codec is AudioCodec.MPEG1_LAYER_II
    assert header.has_crc
    assert header.bitrate == 128_000
    assert header.sample_rate == 48_000
    assert header.channel_mode is MPEGAudioChannelMode.JOINT_STEREO
    assert header.channel_count == 2
    assert header.frame_length == 384
    padded = parse_mpeg_layer_ii_header(bytes.fromhex("FFFC8644"))
    assert padded.padding
    assert padded.frame_length == 385


def test_mpeg2_layer_ii_header_exposes_mono_and_crc_recommendation() -> None:
    # MPEG-2, Layer II, no CRC, 64 kbit/s, 24 kHz, single channel.
    header = parse_mpeg_layer_ii_header(bytes.fromhex("FFF584C0"))
    assert header.codec is AudioCodec.MPEG2_LAYER_II
    assert not header.has_crc
    assert header.bitrate == 64_000
    assert header.sample_rate == 24_000
    assert header.channel_mode is MPEGAudioChannelMode.SINGLE_CHANNEL
    assert header.channel_count == 1
    assert header.frame_length == 384


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"\xff\xfc\x84", "four bytes"),
        (bytes.fromhex("7FFC8444"), "sync"),
        (bytes.fromhex("FFEC8444"), "reserved MPEG version"),
        (bytes.fromhex("FFFA8444"), "Layer II"),
        (bytes.fromhex("FFFCF444"), "bitrate index"),
        (bytes.fromhex("FFFC8C44"), "sample-rate index"),
    ],
)
def test_mpeg_layer_ii_header_rejects_invalid_headers(raw: bytes, message: str) -> None:
    error = TruncatedData if len(raw) < 4 else DecodeError
    with pytest.raises(error, match=message):
        parse_mpeg_layer_ii_header(raw)


def test_mpeg_layer_ii_header_rejects_mpeg_2_5() -> None:
    with pytest.raises(DecodeError, match=r"MPEG-2\.5"):
        parse_mpeg_layer_ii_header(bytes.fromhex("FFE684C0"))


def test_mpeg_layer_ii_header_reports_free_format_without_inventing_rate() -> None:
    header = parse_mpeg_layer_ii_header(bytes.fromhex("FFFC0444"))
    assert header.bitrate is None
    assert header.frame_length is None


def test_mpeg2_aac_lc_adts_header_exposes_channel_configuration() -> None:
    # MPEG-2 AAC-LC, 48 kHz, stereo, CRC present, 100-byte ADTS frame.
    header = parse_aac_adts_header(bytes.fromhex("FFF84C800C9FFC0000"))
    assert header.codec is AudioCodec.MPEG2_AAC_LC
    assert header.has_crc
    assert header.sample_rate == 48_000
    assert header.channel_configuration is AACChannelConfiguration.STEREO
    assert header.channel_count == 2
    assert header.frame_length == 100
    assert header.header_length == 9
    assert header.raw_data_blocks == 1


def test_aac_adts_header_supports_program_config_element_channel_layout() -> None:
    header = parse_aac_adts_header(bytes.fromhex("FFF94C0000FFFC"))
    assert not header.has_crc
    assert header.channel_configuration is AACChannelConfiguration.PROGRAM_CONFIG_ELEMENT
    assert header.channel_count is None
    assert header.header_length == 7


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"\xff\xf8\x4c\x80\x0c\x9f", "seven bytes"),
        (bytes.fromhex("7FF84C800C9FFC"), "sync"),
        (bytes.fromhex("FFF04C800C9FFC"), "MPEG-2"),
        (bytes.fromhex("FFFA4C800C9FFC"), "layer"),
        (bytes.fromhex("FFF80C800C9FFC"), "AAC-LC"),
        (bytes.fromhex("FFF87C800C9FFC"), "sample-rate index"),
        (bytes.fromhex("FFF84C8000FFFC0000"), "frame length"),
        (bytes.fromhex("FFF84C800C9FFC"), "CRC"),
    ],
)
def test_aac_adts_header_rejects_invalid_headers(raw: bytes, message: str) -> None:
    error = TruncatedData if len(raw) < 7 or message == "CRC" else DecodeError
    with pytest.raises(error, match=message):
        parse_aac_adts_header(raw)


def test_audio_pes_event_exposes_st1001_codec_without_decoding_payload() -> None:
    stream = ElementaryStreamInfo(0x0F, 0x104, ())
    pes = PESPacket(b"", 0, 0xC0, 0, False, None, None, b"", b"aac")
    event = PESStreamEvent(1, stream, StreamKind.AUDIO, None, pes)
    assert event.audio_codec is AudioCodec.MPEG2_AAC_LC

    video = PESStreamEvent(
        1,
        ElementaryStreamInfo(0x1B, 0x101, ()),
        StreamKind.VIDEO,
        None,
        pes,
    )
    assert video.audio_codec is None


def test_layer_ii_frame_parser_reconstructs_arbitrary_chunks_and_duration() -> None:
    raw = bytes.fromhex("FFFC8444") + bytes(380)
    parser = AudioFrameParser(AudioCodec.MPEG1_LAYER_II)
    assert parser.feed(raw[:1]) == []
    assert parser.feed(raw[1:17]) == []
    frames = parser.feed(raw[17:])
    assert len(frames) == 1
    frame = frames[0]
    assert frame.raw == raw
    assert frame.offset == 0
    assert frame.codec is AudioCodec.MPEG1_LAYER_II
    assert frame.sample_rate == 48_000
    assert frame.channel_count == 2
    assert frame.sample_count == 1_152
    assert frame.duration_seconds.numerator == 3
    assert frame.duration_seconds.denominator == 125
    assert parser.buffered_bytes == 0
    assert parser.stream_offset == len(raw)
    assert parser.finish() == []


def test_aac_frame_parser_emits_multiple_frames_and_raw_data_blocks() -> None:
    # MPEG-2 AAC-LC, 48 kHz stereo, no CRC, 100-byte frame, two raw blocks.
    header = bytes.fromhex("FFF94C800C9FFD")
    raw = header + bytes(100 - len(header))
    parser = AudioFrameParser(AudioCodec.MPEG2_AAC_LC)
    frames = parser.feed(raw + raw)
    assert [frame.offset for frame in frames] == [0, 100]
    assert all(frame.raw == raw for frame in frames)
    assert all(frame.sample_count == 2_048 for frame in frames)
    assert all(frame.duration_seconds.numerator == 16 for frame in frames)
    assert all(frame.duration_seconds.denominator == 375 for frame in frames)


def test_audio_frame_parser_rejects_wrong_codec_free_format_and_bounds() -> None:
    layer = bytes.fromhex("FFFC8444") + bytes(380)
    with pytest.raises(DecodeError, match="AAC ADTS"):
        AudioFrameParser(AudioCodec.MPEG2_AAC_LC).feed(layer)
    with pytest.raises(DecodeError, match="free-format"):
        AudioFrameParser(AudioCodec.MPEG1_LAYER_II).feed(bytes.fromhex("FFFC0444"))

    aac = bytes.fromhex("FFF94C800C9FFC") + bytes(93)
    with pytest.raises(DecodeError, match="configured limit"):
        AudioFrameParser(AudioCodec.MPEG2_AAC_LC, max_frame_length=99).feed(aac)


def test_audio_frame_parser_validates_configuration_and_trailing_frame() -> None:
    with pytest.raises(TypeError, match="AudioCodec"):
        AudioFrameParser("mp2")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_frame_length"):
        AudioFrameParser(AudioCodec.MPEG1_LAYER_II, max_frame_length=3)
    parser = AudioFrameParser(AudioCodec.MPEG1_LAYER_II)
    with pytest.raises(TypeError, match="bytes-like"):
        parser.feed("audio")  # type: ignore[arg-type]
    parser.feed(bytes.fromhex("FFFC8444") + bytes(10))
    with pytest.raises(TruncatedData, match="incomplete"):
        parser.finish()
