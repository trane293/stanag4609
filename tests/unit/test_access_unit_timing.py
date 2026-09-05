from __future__ import annotations

import pytest

from stanag4609.transport.access_unit_timing import VideoAccessUnitPTSValidator
from stanag4609.transport.demux import PESStreamEvent, StreamKind
from stanag4609.transport.mpegts import ProgramClockReference, parse_transport_packet
from stanag4609.transport.mux import encode_pcr_packet, encode_pes_packet
from stanag4609.transport.pes import parse_pes_packet
from stanag4609.transport.psi import ElementaryStreamInfo


def _event(
    payload: bytes,
    *,
    pts: int | None = None,
    stream_type: int = 0x1B,
    program_number: int = 1,
    pid: int = 0x101,
    offset: int = 0,
    kind: StreamKind = StreamKind.VIDEO,
    discontinuity: bool = False,
) -> PESStreamEvent:
    packets = ()
    if discontinuity:
        packets = (
            parse_transport_packet(
                encode_pcr_packet(
                    pid=pid,
                    pcr=ProgramClockReference(0, 0),
                    discontinuity=True,
                ),
                offset=offset,
            ),
        )
    return PESStreamEvent(
        program_number,
        ElementaryStreamInfo(stream_type, pid, ()),
        kind,
        None,
        parse_pes_packet(
            encode_pes_packet(payload, stream_id=0xE0, pts=pts),
            offset=offset,
            transport_packets=packets,
        ),
    )


@pytest.mark.parametrize(
    ("stream_type", "marker"),
    [
        (0x01, b"\x00\x00\x01\x00"),
        (0x02, b"\x00\x00\x01\x00"),
        (0x1B, b"\x00\x00\x01\x09"),
        (0x24, b"\x00\x00\x01\x46"),
    ],
)
def test_first_video_access_unit_requires_pts_for_common_codecs(
    stream_type: int,
    marker: bytes,
) -> None:
    validator = VideoAccessUnitPTSValidator()
    assert validator.observe(_event(b"header", stream_type=stream_type)) == ()

    issue = validator.observe(_event(marker + b"frame", stream_type=stream_type, offset=188))[0]

    assert issue.code == "first_access_unit"
    assert issue.requirement == "ITU-T H.222.0 (10/2014) §2.7.5"
    assert issue.program_number == 1
    assert issue.pid == 0x101
    assert issue.stream_type == stream_type
    assert issue.source_offset == 188
    assert "without PTS" in issue.message
    assert validator.completed_streams == ((1, 0x101),)


def test_first_video_access_unit_accepts_pts_and_ignores_later_units() -> None:
    validator = VideoAccessUnitPTSValidator()
    marker = b"\x00\x00\x01\x09"
    assert validator.observe(_event(marker, pts=0)) == ()
    assert validator.observe(_event(marker, offset=188)) == ()
    assert validator.finish() == ()


def test_pts_requires_access_unit_start_in_same_pes() -> None:
    validator = VideoAccessUnitPTSValidator()
    assert validator.observe(_event(b"not-an-access-unit", pts=0, offset=100)) == ()

    issue = validator.observe(_event(b"next-payload", offset=200))[0]

    assert issue.code == "pts_without_access_unit"
    assert issue.source_offset == 100
    assert "carries PTS" in issue.message


def test_pts_alignment_waits_for_a_split_access_unit_prefix() -> None:
    validator = VideoAccessUnitPTSValidator()
    assert validator.observe(_event(b"payload\x00\x00", pts=0, offset=100)) == ()
    assert validator.observe(_event(b"\x01\x09frame", offset=200)) == ()
    assert validator.finish() == ()


def test_finish_rejects_unresolved_pts_and_is_idempotent() -> None:
    validator = VideoAccessUnitPTSValidator()
    assert validator.observe(_event(b"payload", pts=0, offset=100)) == ()

    issue = validator.finish()[0]

    assert issue.code == "pts_without_access_unit"
    assert issue.source_offset == 100
    assert validator.finish() == ()


def test_empty_pts_pes_is_rejected_immediately() -> None:
    validator = VideoAccessUnitPTSValidator()

    issue = validator.observe(_event(b"", pts=0, offset=100))[0]

    assert issue.code == "pts_without_access_unit"
    assert issue.source_offset == 100


def test_split_start_code_is_attributed_to_pes_containing_its_first_byte() -> None:
    marker_prefix = b"\x00\x00"

    missing = VideoAccessUnitPTSValidator()
    assert missing.observe(_event(b"payload" + marker_prefix, offset=100)) == ()
    issue = missing.observe(_event(b"\x01\x09frame", pts=90_000, offset=200))[0]
    assert issue.source_offset == 100

    timestamped = VideoAccessUnitPTSValidator()
    assert timestamped.observe(_event(marker_prefix, pts=0, offset=100)) == ()
    assert timestamped.observe(_event(b"\x01\x09frame", offset=200)) == ()


def test_scanner_skips_non_access_unit_start_codes_and_unrelated_streams() -> None:
    validator = VideoAccessUnitPTSValidator()
    assert validator.observe(_event(b"\x00\x00\x01\x67sps")) == ()
    assert validator.observe(_event(b"\x00\x00\x01\x09", kind=StreamKind.DATA)) == ()
    assert validator.observe(_event(b"\x00\x00\x01\x09", stream_type=0x42)) == ()
    assert validator.streams == ((1, 0x101),)


def test_discontinuity_and_stream_type_change_establish_new_first_unit() -> None:
    validator = VideoAccessUnitPTSValidator()
    assert validator.observe(_event(b"\x00\x00\x01\x09", pts=0)) == ()

    issue = validator.observe(
        _event(b"\x00\x00\x01\x09", offset=188, discontinuity=True)
    )[0]
    assert issue.source_offset == 188

    mpeg_issue = validator.observe(
        _event(b"\x00\x00\x01\x00", stream_type=0x02, offset=376)
    )[0]
    assert mpeg_issue.stream_type == 0x02


def test_first_video_access_unit_validator_reset_and_validation() -> None:
    validator = VideoAccessUnitPTSValidator()
    validator.observe(_event(b"prefix", program_number=1, pid=0x101))
    validator.observe(_event(b"prefix", program_number=1, pid=0x102))
    validator.observe(_event(b"prefix", program_number=2, pid=0x101))

    validator.reset(program_number=1, pid=0x101)
    assert validator.streams == ((1, 0x102), (2, 0x101))
    validator.reset(program_number=1)
    assert validator.streams == ((2, 0x101),)
    validator.reset()
    assert validator.streams == ()

    with pytest.raises(TypeError, match="PESStreamEvent"):
        validator.observe(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="requires program_number"):
        validator.reset(pid=0x101)
    with pytest.raises(ValueError, match="program_number"):
        validator.reset(program_number=0)
    with pytest.raises(ValueError, match="pid"):
        validator.reset(program_number=1, pid=0x2000)
