from __future__ import annotations

from fractions import Fraction

import pytest

from stanag4609.audio.timing import AudioPESFrameParser
from stanag4609.errors import DecodeError, TruncatedData
from stanag4609.st1001 import AudioCodec
from stanag4609.transport.demux import PESStreamEvent, StreamKind
from stanag4609.transport.pes import PESPacket
from stanag4609.transport.psi import ElementaryStreamInfo
from stanag4609.transport.timing import PTS_MODULUS


def _layer_frame(*, sample_rate: int = 48_000) -> bytes:
    if sample_rate == 48_000:
        header = bytes.fromhex("FFFC8444")
        return header + bytes(384 - len(header))
    if sample_rate == 44_100:
        header = bytes.fromhex("FFFC8044")
        return header + bytes(417 - len(header))
    raise ValueError


def _event(
    payload: bytes,
    *,
    pts: int | None = None,
    pid: int = 0x104,
    program_number: int = 1,
    stream_type: int = 0x03,
) -> PESStreamEvent:
    pes = PESPacket(b"", 0, 0xC0, 0, False, pts, None, b"", payload)
    return PESStreamEvent(
        program_number,
        ElementaryStreamInfo(stream_type, pid, ()),
        StreamKind.AUDIO,
        None,
        pes,
    )


def test_audio_pes_frame_parser_derives_exact_frame_presentation_times() -> None:
    frame = _layer_frame()
    parser = AudioPESFrameParser()
    timed = parser.feed(_event(frame + frame, pts=90_000))

    assert [item.presentation_ticks for item in timed] == [
        Fraction(90_000),
        Fraction(92_160),
    ]
    assert [item.presentation_seconds for item in timed] == [Fraction(1), Fraction(128, 125)]
    assert [item.explicit_pts for item in timed] == [True, False]
    assert all(item.program_number == 1 and item.pid == 0x104 for item in timed)
    assert parser.program_number == 1
    assert parser.pid == 0x104
    assert parser.codec is AudioCodec.MPEG1_LAYER_II
    assert parser.frames_emitted == 2
    assert parser.finish() == []


def test_pts_follows_frame_start_across_pes_boundaries() -> None:
    frame = _layer_frame()
    parser = AudioPESFrameParser()

    assert parser.feed(_event(frame[:100], pts=45_000)) == []
    timed = parser.feed(_event(frame[100:] + frame))

    assert [item.frame.offset for item in timed] == [0, 384]
    assert [item.presentation_ticks for item in timed] == [45_000, 47_160]
    assert [item.explicit_pts for item in timed] == [True, False]


def test_pts_targets_first_frame_commencing_in_pes_not_continued_frame() -> None:
    frame = _layer_frame()
    parser = AudioPESFrameParser()

    assert parser.feed(_event(frame[:100])) == []
    timed = parser.feed(_event(frame[100:] + frame, pts=90_000))

    assert timed[0].frame.offset == 0
    assert timed[0].presentation_ticks is None
    assert not timed[0].explicit_pts
    assert timed[1].frame.offset == 384
    assert timed[1].presentation_ticks == 90_000
    assert timed[1].explicit_pts


def test_pts_is_rejected_when_no_audio_access_unit_commences_in_pes() -> None:
    frame = _layer_frame()
    parser = AudioPESFrameParser()
    assert parser.feed(_event(frame[:100])) == []
    with pytest.raises(DecodeError, match="no audio access unit commences"):
        parser.feed(_event(frame[100:200], pts=90_000))


def test_audio_timing_preserves_fractional_ticks_and_unwraps_pts() -> None:
    frame = _layer_frame(sample_rate=44_100)
    parser = AudioPESFrameParser()
    first = parser.feed(_event(frame, pts=PTS_MODULUS - 1))[0]
    second = parser.feed(_event(frame, pts=2_350))[0]

    assert first.presentation_ticks == PTS_MODULUS - 1
    assert second.presentation_ticks == PTS_MODULUS + 2_350
    assert second.frame.duration_seconds == Fraction(192, 7_350)
    assert second.presentation_seconds == Fraction(PTS_MODULUS + 2_350, 90_000)


def test_audio_pes_frame_parser_validates_route_codec_and_finish() -> None:
    frame = _layer_frame()
    parser = AudioPESFrameParser(max_frame_length=1_000)
    parser.feed(_event(frame))

    with pytest.raises(ValueError, match="PID"):
        parser.feed(_event(frame, pid=0x105))
    with pytest.raises(ValueError, match="program"):
        parser.feed(_event(frame, program_number=2))
    with pytest.raises(ValueError, match="codec"):
        parser.feed(_event(bytes.fromhex("FFF94C800C9FFC") + bytes(93), stream_type=0x0F))
    with pytest.raises(TypeError, match="PESStreamEvent"):
        parser.feed(frame)  # type: ignore[arg-type]

    partial = AudioPESFrameParser()
    partial.feed(_event(frame[:10]))
    with pytest.raises(TruncatedData, match="incomplete"):
        partial.finish()


def test_audio_pes_frame_parser_rejects_non_audio_and_unknown_audio() -> None:
    pes = PESPacket(b"", 0, 0xE0, 0, False, None, None, b"", b"video")
    video = PESStreamEvent(
        1,
        ElementaryStreamInfo(0x1B, 0x101, ()),
        StreamKind.VIDEO,
        None,
        pes,
    )
    with pytest.raises(ValueError, match="audio PES"):
        AudioPESFrameParser().feed(video)

    unknown = _event(b"audio", stream_type=0x11)
    with pytest.raises(ValueError, match="ST 1001"):
        AudioPESFrameParser().feed(unknown)
