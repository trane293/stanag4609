from __future__ import annotations

import pytest

from stanag4609.errors import DecodeError, TruncatedData
from stanag4609.st0604 import (
    MISP_HEVC_MICROSECOND_UUID,
    MISP_HEVC_NANOSECOND_UUID,
    MISP_MICROSECOND_IDENTIFIER,
    EmbeddedVideoTimestamp,
    TimestampedVideoAccessUnit,
    TimestampResolution,
    VideoTimestampedAccessUnitParser,
    VideoTimestampStreamParser,
    decode_time_status,
    decode_timestamp_payload,
    encode_avc_timestamp_sei,
    encode_h262_timestamp_user_data,
    encode_hevc_timestamp_sei,
    encode_time_status,
    encode_timestamp_payload,
)

TIMESTAMP = 0x0001020304050607
MODIFIED = bytes.fromhex("0001ff0203ff0405ff0607")


def test_st0604_microsecond_payload_matches_tables_one_and_two() -> None:
    payload = encode_timestamp_payload(TIMESTAMP, stream_type=0x1B)

    assert payload == MISP_MICROSECOND_IDENTIFIER + b"\x1f" + MODIFIED
    assert decode_timestamp_payload(payload, stream_type=0x1B) == EmbeddedVideoTimestamp(
        TIMESTAMP,
        TimestampResolution.MICROSECONDS,
        decode_time_status(0x1F),
        0x1B,
    )


def test_st0604_hevc_uses_distinct_microsecond_and_nanosecond_uuids() -> None:
    microseconds = encode_timestamp_payload(TIMESTAMP, stream_type=0x24)
    nanoseconds = encode_timestamp_payload(
        TIMESTAMP,
        stream_type=0x24,
        resolution=TimestampResolution.NANOSECONDS,
    )

    assert microseconds[:16] == MISP_HEVC_MICROSECOND_UUID
    assert nanoseconds[:16] == MISP_HEVC_NANOSECOND_UUID
    assert decode_timestamp_payload(microseconds, stream_type=0x24).resolution is (
        TimestampResolution.MICROSECONDS
    )
    assert decode_timestamp_payload(nanoseconds, stream_type=0x24).resolution is (
        TimestampResolution.NANOSECONDS
    )


def test_st0603_time_status_round_trips_all_defined_bits() -> None:
    raw = encode_time_status(locked=False, discontinuity=True, reverse=True)
    status = decode_time_status(raw)

    assert raw == 0xFF
    assert status.locked is False
    assert status.discontinuity is True
    assert status.reverse is True
    with pytest.raises(ValueError, match="reverse requires"):
        encode_time_status(reverse=True)
    with pytest.raises(DecodeError, match="reserved bits"):
        decode_time_status(0)


def test_st0604_rejects_truncated_bad_emulation_and_codec_resolution() -> None:
    payload = encode_timestamp_payload(TIMESTAMP, stream_type=0x02)
    with pytest.raises(TruncatedData, match="28 bytes"):
        decode_timestamp_payload(payload[:-1], stream_type=0x02)
    malformed = bytearray(payload)
    malformed[19] = 0
    with pytest.raises(DecodeError, match="0xFF"):
        decode_timestamp_payload(bytes(malformed), stream_type=0x02)
    with pytest.raises(ValueError, match=r"only for H\.265"):
        encode_timestamp_payload(
            TIMESTAMP,
            stream_type=0x1B,
            resolution=TimestampResolution.NANOSECONDS,
        )


@pytest.mark.parametrize(
    ("stream_type", "encoded"),
    [
        (0x02, encode_h262_timestamp_user_data(TIMESTAMP)),
        (0x1B, encode_avc_timestamp_sei(TIMESTAMP)),
        (0x24, encode_hevc_timestamp_sei(TIMESTAMP)),
        (
            0x24,
            encode_hevc_timestamp_sei(
                TIMESTAMP,
                resolution=TimestampResolution.NANOSECONDS,
            ),
        ),
    ],
)
def test_video_timestamp_parser_recovers_all_supported_codec_forms_across_chunks(
    stream_type: int,
    encoded: bytes,
) -> None:
    parser = VideoTimestampStreamParser(stream_type)
    timestamps = []
    source = b"garbage" + encoded + b"\x00\x00\x01\x09\xf0"
    for index in range(0, len(source), 5):
        timestamps.extend(parser.feed(source[index : index + 5]))
    timestamps.extend(parser.finish())

    assert len(timestamps) == 1
    assert timestamps[0].value == TIMESTAMP
    assert timestamps[0].stream_type == stream_type
    expected = (
        TimestampResolution.NANOSECONDS
        if encoded.find(MISP_HEVC_NANOSECOND_UUID) >= 0
        else TimestampResolution.MICROSECONDS
    )
    assert timestamps[0].resolution is expected


def test_video_timestamp_parser_ignores_unrelated_user_data_and_sei_messages() -> None:
    mpeg = VideoTimestampStreamParser(0x02)
    assert mpeg.feed(b"\x00\x00\x01\xb2hello\x00\x00\x01\x00") == ()
    avc = VideoTimestampStreamParser(0x1B)
    assert avc.feed(b"\x00\x00\x01\x06\x01\x01x\x80\x00\x00\x01\x09") == ()

    literal_three = VideoTimestampStreamParser(0x1B)
    assert literal_three.feed(
        b"\x00\x00\x01\x06\x01\x04\x00\x00\x03\x04\x80"
        b"\x00\x00\x01\x09"
    ) == ()


def test_video_timestamp_parser_reports_malformed_matching_payload_and_bounds() -> None:
    parser = VideoTimestampStreamParser(0x1B)
    malformed = b"\x00\x00\x01\x06\x05\x1c" + MISP_MICROSECOND_IDENTIFIER
    with pytest.raises(TruncatedData, match="declared payload"):
        parser.feed(malformed + b"\x00\x00\x01\x09")

    bounded = VideoTimestampStreamParser(0x02, max_unit_size=28)
    with pytest.raises(DecodeError, match="exceeds"):
        bounded.feed(b"\x00\x00\x01\xb2" + b"x" * 33)


def test_video_timestamp_parser_lifecycle_and_configuration() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        VideoTimestampStreamParser(0x42)
    with pytest.raises(ValueError, match="at least 28"):
        VideoTimestampStreamParser(0x1B, max_unit_size=27)

    parser = VideoTimestampStreamParser(0x1B)
    parser.feed(encode_avc_timestamp_sei(TIMESTAMP))
    assert len(parser.finish()) == 1
    assert parser.finish() == ()
    with pytest.raises(RuntimeError, match="finished"):
        parser.feed(b"")
    parser.reset()
    assert parser.feed(b"") == ()


@pytest.mark.parametrize(
    ("stream_type", "access_unit"),
    [
        (0x02, b"\x00\x00\x01\x00picture"),
        (0x1B, b"\x00\x00\x01\x65\x80slice"),
        (0x24, b"\x00\x00\x01\x02\x01\x80slice"),
    ],
)
def test_video_timestamp_parser_counts_common_codec_access_units(
    stream_type: int,
    access_unit: bytes,
) -> None:
    parser = VideoTimestampStreamParser(stream_type)
    parser.feed(access_unit + b"\x00\x00\x01\xb7")
    parser.finish()

    assert parser.access_units == 1


@pytest.mark.parametrize(
    ("stream_type", "source"),
    [
        (
            0x02,
            b"\x00\x00\x01\x00picture-header"
            + encode_h262_timestamp_user_data(TIMESTAMP)
            + b"\x00\x00\x01\x01slice"
            + b"\x00\x00\x01\x00next-picture",
        ),
        (
            0x1B,
            encode_avc_timestamp_sei(TIMESTAMP)
            + b"\x00\x00\x01\x65\x80first-slice"
            + b"\x00\x00\x01\x41\x80next-slice",
        ),
        (
            0x24,
            encode_hevc_timestamp_sei(TIMESTAMP)
            + b"\x00\x00\x01\x02\x01\x80first-slice"
            + b"\x00\x00\x01\x02\x01\x80next-slice",
        ),
    ],
)
def test_timestamped_access_unit_parser_associates_codec_timestamp_placement(
    stream_type: int,
    source: bytes,
) -> None:
    parser = VideoTimestampedAccessUnitParser(stream_type)
    access_units = []
    for index in range(0, len(source), 5):
        access_units.extend(parser.feed(source[index : index + 5]))
    access_units.extend(parser.finish())

    assert access_units == [
        TimestampedVideoAccessUnit(
            index=0,
            timestamps=(
                EmbeddedVideoTimestamp(
                    TIMESTAMP,
                    TimestampResolution.MICROSECONDS,
                    decode_time_status(0x1F),
                    stream_type,
                ),
            ),
        ),
        TimestampedVideoAccessUnit(index=1, timestamps=()),
    ]
    assert parser.access_units == 2
    assert parser.unassociated_timestamps == ()


def test_hevc_suffix_timestamp_associates_with_current_access_unit() -> None:
    suffix = bytearray(encode_hevc_timestamp_sei(TIMESTAMP))
    suffix[3] = 0x50  # NAL unit type 40, suffix SEI.
    source = (
        b"\x00\x00\x01\x02\x01\x80first-slice"
        + bytes(suffix)
        + b"\x00\x00\x01\x02\x01\x80next-slice"
    )
    parser = VideoTimestampedAccessUnitParser(0x24)

    access_units = (*parser.feed(source), *parser.finish())

    assert [len(access_unit.timestamps) for access_unit in access_units] == [1, 0]
    assert access_units[0].timestamps[0].value == TIMESTAMP


def test_h262_timestamp_after_picture_data_is_not_frame_associated() -> None:
    source = (
        b"\x00\x00\x01\x00picture-header"
        b"\x00\x00\x01\x01slice"
        + encode_h262_timestamp_user_data(TIMESTAMP)
    )
    parser = VideoTimestampedAccessUnitParser(0x02)

    access_units = (*parser.feed(source), *parser.finish())

    assert access_units == (TimestampedVideoAccessUnit(index=0, timestamps=()),)
    assert [timestamp.value for timestamp in parser.unassociated_timestamps] == [TIMESTAMP]


def test_timestamped_access_unit_parser_retains_orphan_timestamp_and_lifecycle() -> None:
    parser = VideoTimestampedAccessUnitParser(0x1B)

    assert parser.feed(encode_avc_timestamp_sei(TIMESTAMP)) == ()
    assert parser.finish() == ()
    assert [timestamp.value for timestamp in parser.unassociated_timestamps] == [TIMESTAMP]
    assert parser.finish() == ()
    with pytest.raises(RuntimeError, match="finished"):
        parser.feed(b"")

    parser.reset()
    assert parser.access_units == 0
    assert parser.unassociated_timestamps == ()
