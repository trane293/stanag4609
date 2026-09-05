from __future__ import annotations

from dataclasses import replace
from io import BytesIO

import pytest

from stanag4609 import encode_transport_packet as public_encode_transport_packet
from stanag4609 import rebuild_transport_packet as public_rebuild_transport_packet
from stanag4609.errors import DecodeError, TruncatedData
from stanag4609.transport.mpegts import (
    TS_PACKET_SIZE,
    AdaptationField,
    AdaptationFieldExtension,
    ProgramClockReference,
    TransportStreamParser,
    encode_adaptation_field,
    encode_adaptation_field_extension,
    encode_program_clock_reference,
    encode_transport_packet,
    iter_transport_stream,
    parse_transport_packet,
    rebuild_transport_packet,
)


def _adaptation_packet(adaptation: bytes) -> bytes:
    payload = bytes(TS_PACKET_SIZE - 5 - len(adaptation))
    return (
        _header(0x101, adaptation_field_control=3)
        + bytes((len(adaptation),))
        + adaptation
        + payload
    )


def _seamless_splice(splice_type: int, timestamp: int) -> bytes:
    return bytes(
        (
            (splice_type << 4) | (((timestamp >> 30) & 0x07) << 1) | 1,
            (timestamp >> 22) & 0xFF,
            (((timestamp >> 15) & 0x7F) << 1) | 1,
            (timestamp >> 7) & 0xFF,
            ((timestamp & 0x7F) << 1) | 1,
        )
    )


def _header(
    pid: int,
    *,
    payload_unit_start: bool = False,
    adaptation_field_control: int = 1,
    continuity_counter: int = 0,
) -> bytes:
    return bytes(
        (
            0x47,
            (0x40 if payload_unit_start else 0) | (pid >> 8),
            pid & 0xFF,
            (adaptation_field_control << 4) | continuity_counter,
        )
    )


def _payload_packet(pid: int, fill: int, *, start: bool = False, cc: int = 0) -> bytes:
    return _header(pid, payload_unit_start=start, continuity_counter=cc) + bytes((fill,)) * 184


def test_parse_payload_only_transport_packet() -> None:
    raw = _payload_packet(0x123, 0xA5, start=True, cc=9)
    packet = parse_transport_packet(raw, offset=376)
    assert len(raw) == TS_PACKET_SIZE
    assert packet.pid == 0x123
    assert packet.payload_unit_start
    assert packet.continuity_counter == 9
    assert packet.payload == bytes((0xA5,)) * 184
    assert packet.adaptation_field == b""
    assert packet.offset == 376
    assert bytes(packet) == raw


def test_parse_adaptation_and_payload_packet() -> None:
    payload = bytes(range(181))
    raw = _header(0x44, adaptation_field_control=3) + b"\x02\x40\xff" + payload
    packet = parse_transport_packet(raw)
    assert packet.random_access_indicator
    assert packet.adaptation_field == b"\x40\xff"
    assert packet.payload == payload


def test_parse_pcr_and_opcr_from_adaptation_field_exactly() -> None:
    pcr = ProgramClockReference(base=0x1ABCDEFFF, extension=299)
    opcr = ProgramClockReference(base=90_000, extension=17)
    adaptation = b"\x18" + encode_program_clock_reference(pcr) + encode_program_clock_reference(
        opcr
    )
    payload = bytes(188 - 5 - len(adaptation))
    raw = (
        _header(0x101, adaptation_field_control=3)
        + bytes((len(adaptation),))
        + adaptation
        + payload
    )
    packet = parse_transport_packet(raw)
    assert packet.pcr == pcr
    assert packet.opcr == opcr
    assert packet.pcr.ticks == (0x1ABCDEFFF * 300) + 299
    assert packet.pcr.seconds == pytest.approx(packet.pcr.ticks / 27_000_000)
    assert not packet.discontinuity_indicator


def test_parse_complete_adaptation_field_structure() -> None:
    timestamp = 0x1ABCDEFFF
    extension = (
        b"\xff"
        + (0x8000 | 0x1234).to_bytes(2, "big")
        + (0xC00000 | 0x23456).to_bytes(3, "big")
        + _seamless_splice(10, timestamp)
        + b"\xff"
    )
    adaptation = b"\x27\xfe\x03abc" + bytes((len(extension),)) + extension + b"\xff"

    packet = parse_transport_packet(_adaptation_packet(adaptation))

    assert packet.elementary_stream_priority_indicator
    assert packet.splice_countdown == -2
    assert packet.transport_private_data == b"abc"
    assert packet.adaptation_field_extension == AdaptationFieldExtension(
        legal_time_window_valid=True,
        legal_time_window_offset=0x1234,
        piecewise_rate=0x23456,
        splice_type=10,
        dts_next_access_unit=timestamp,
    )


def test_encode_complete_adaptation_field_exactly_and_round_trip() -> None:
    extension = AdaptationFieldExtension(
        legal_time_window_valid=True,
        legal_time_window_offset=0x1234,
        piecewise_rate=0x23456,
        splice_type=10,
        dts_next_access_unit=0x1ABCDEFFF,
    )
    field = AdaptationField(
        random_access_indicator=False,
        elementary_stream_priority_indicator=True,
        splice_countdown=-2,
        transport_private_data=b"abc",
        extension=extension,
    )

    encoded_extension = encode_adaptation_field_extension(
        extension, stuffing_length=1
    )
    encoded = encode_adaptation_field(
        field, stuffing_length=1, extension_stuffing_length=1
    )
    assert encoded_extension.hex() == "ff9234c23456adaf37dfffff"
    assert encoded.hex() == "27fe036162630cff9234c23456adaf37dfffffff"

    packet = parse_transport_packet(_adaptation_packet(encoded))
    assert packet.adaptation == field
    assert (
        encode_adaptation_field(
            packet.adaptation,
            stuffing_length=1,
            extension_stuffing_length=1,
        )
        == encoded
    )


def test_adaptation_writer_rejects_invalid_models_and_packet_overflow() -> None:
    with pytest.raises(ValueError, match="appear together"):
        AdaptationFieldExtension(legal_time_window_valid=True)
    with pytest.raises(ValueError, match="positive 22-bit"):
        AdaptationFieldExtension(piecewise_rate=0)
    with pytest.raises(ValueError, match="requires splice_countdown"):
        AdaptationField(
            extension=AdaptationFieldExtension(
                splice_type=0,
                dts_next_access_unit=0,
            )
        )
    with pytest.raises(ValueError, match="requires an adaptation extension"):
        encode_adaptation_field(
            AdaptationField(),
            extension_stuffing_length=1,
        )
    with pytest.raises(ValueError, match="cannot contain flags"):
        AdaptationField(empty=True, random_access_indicator=True)
    with pytest.raises(ValueError, match="cannot contain stuffing"):
        encode_adaptation_field(AdaptationField(empty=True), stuffing_length=1)
    with pytest.raises(ValueError, match="183"):
        encode_adaptation_field(AdaptationField(), stuffing_length=183)


def test_empty_adaptation_field_round_trips() -> None:
    packet = parse_transport_packet(_adaptation_packet(b""))
    assert packet.adaptation == AdaptationField(empty=True)
    assert encode_adaptation_field(packet.adaptation) == b""


def test_encode_payload_only_transport_packet_exactly() -> None:
    assert public_encode_transport_packet is encode_transport_packet
    assert public_rebuild_transport_packet is rebuild_transport_packet
    payload = b"\xA5" * 184
    raw = encode_transport_packet(
        pid=0x123,
        payload=payload,
        transport_error_indicator=True,
        payload_unit_start=True,
        transport_priority=True,
        scrambling_control=2,
        continuity_counter=9,
    )

    assert raw[:4] == bytes.fromhex("47e12399")
    packet = parse_transport_packet(raw)
    assert packet.transport_error_indicator
    assert packet.payload_unit_start
    assert packet.transport_priority
    assert packet.scrambling_control == 2
    assert packet.continuity_counter == 9
    assert packet.payload == payload
    assert packet.adaptation is None


def test_encode_transport_packet_adds_canonical_adaptation_stuffing() -> None:
    clock = ProgramClockReference(90_000, 17)
    adaptation = AdaptationField(random_access_indicator=True, pcr=clock)
    payload = bytes(range(170))

    raw = encode_transport_packet(
        pid=0x101,
        payload=payload,
        payload_unit_start=True,
        continuity_counter=15,
        adaptation=adaptation,
    )

    assert len(raw) == TS_PACKET_SIZE
    assert raw[:5] == bytes.fromhex("4741013f0d")
    packet = parse_transport_packet(raw)
    assert packet.adaptation == adaptation
    assert packet.adaptation_field.endswith(b"\xFF" * 6)
    assert packet.payload == payload


def test_encode_adaptation_only_and_zero_length_adaptation_packets() -> None:
    adaptation_only = encode_transport_packet(
        pid=0x101,
        continuity_counter=3,
        adaptation=AdaptationField(discontinuity_indicator=True),
    )
    assert adaptation_only[:6] == bytes.fromhex("47010123b780")
    assert adaptation_only[6:] == b"\xFF" * 182
    assert parse_transport_packet(adaptation_only).adaptation == AdaptationField(
        discontinuity_indicator=True
    )

    empty = encode_transport_packet(
        pid=0x101,
        payload=b"\x22" * 183,
        adaptation=AdaptationField(empty=True),
    )
    assert empty[:5] == bytes.fromhex("4701013000")
    assert parse_transport_packet(empty).adaptation == AdaptationField(empty=True)


def test_rebuild_transport_packet_preserves_header_payload_and_noop_bytes() -> None:
    source = parse_transport_packet(
        encode_transport_packet(
            pid=0x144,
            payload=b"source" * 28,
            payload_unit_start=True,
            transport_priority=True,
            continuity_counter=7,
            adaptation=AdaptationField(
                pcr=ProgramClockReference(123_456, 12),
                transport_private_data=b"private",
            ),
        )
    )
    assert rebuild_transport_packet(source) == source.raw
    with pytest.raises(ValueError, match="non-negative integer"):
        rebuild_transport_packet(source, extension_stuffing_length=False)

    assert source.adaptation is not None
    changed = replace(source.adaptation, random_access_indicator=True)
    rebuilt = parse_transport_packet(
        rebuild_transport_packet(source, adaptation=changed)
    )
    assert rebuilt.pid == source.pid
    assert rebuilt.payload_unit_start == source.payload_unit_start
    assert rebuilt.transport_priority == source.transport_priority
    assert rebuilt.continuity_counter == source.continuity_counter
    assert rebuilt.payload == source.payload
    assert rebuilt.adaptation == changed


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"pid": -1, "payload": b"x" * 184}, "pid"),
        ({"pid": 0, "payload": b"x" * 184, "continuity_counter": 16}, "continuity"),
        ({"pid": 0, "payload": b"x" * 184, "scrambling_control": 4}, "scrambling"),
        ({"pid": 0, "payload": b"x" * 183}, "exactly 184"),
        (
            {
                "pid": 0,
                "payload": b"x" * 184,
                "adaptation": AdaptationField(),
            },
            "do not fit",
        ),
        (
            {
                "pid": 0,
                "adaptation": AdaptationField(empty=True),
            },
            "empty adaptation field",
        ),
    ],
)
def test_encode_transport_packet_rejects_invalid_layouts(
    kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        encode_transport_packet(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "adaptation, message",
    [
        (b"\x02\x03ab", "private data"),
        (b"\x01\x02\xff", "extension"),
        (b"\x01\x01\xe0", "reserved"),
        (b"\x01\x04\x5f\x00\x00\x00", "piecewise-rate reserved"),
        (b"\x01\x04\x5f\xc0\x00\x00", "must be positive"),
        (b"\x01\x06\x3f" + _seamless_splice(0, 0), "splicing_point_flag"),
        (b"\x04", "splice countdown"),
        (b"\x00\x00", "stuffing"),
    ],
)
def test_adaptation_optional_fields_are_bounded_and_validated(
    adaptation: bytes, message: str
) -> None:
    with pytest.raises(DecodeError, match=message):
        parse_transport_packet(_adaptation_packet(adaptation))


def test_adaptation_flags_and_clock_reference_encoding_are_validated() -> None:
    discontinuity = b"\xb7\x80" + b"\xff" * 182
    packet = parse_transport_packet(
        _header(0x101, adaptation_field_control=2) + discontinuity
    )
    assert packet.discontinuity_indicator

    with pytest.raises(ValueError, match="base"):
        ProgramClockReference(base=2**33, extension=0)
    with pytest.raises(ValueError, match="extension"):
        ProgramClockReference(base=0, extension=300)
    malformed_pcr = b"\xb7\x10" + b"\x00" * 6 + b"\xff" * 176
    with pytest.raises(DecodeError, match="reserved"):
        parse_transport_packet(
            _header(0x101, adaptation_field_control=2) + malformed_pcr
        )
    truncated_pcr = b"\x01\x10" + b"\xff" * 182
    with pytest.raises(DecodeError, match="PCR"):
        parse_transport_packet(
            _header(0x101, adaptation_field_control=3) + truncated_pcr
        )


def test_parse_adaptation_only_packet_has_no_payload() -> None:
    raw = _header(0x1FFF, adaptation_field_control=2) + b"\xb7\x00" + b"\xff" * 182
    packet = parse_transport_packet(raw)
    assert packet.pid == 0x1FFF
    assert packet.has_adaptation_field
    assert not packet.has_payload
    assert packet.payload == b""


@pytest.mark.parametrize(
    "raw, message",
    [
        (b"\x46" + b"\0" * 187, "sync"),
        (_header(1, adaptation_field_control=0) + b"\0" * 184, "reserved"),
        (_header(1, adaptation_field_control=3) + b"\xb8" + b"\0" * 183, "adaptation"),
        (b"\x47" * 187, "188"),
    ],
)
def test_invalid_transport_packets_are_rejected(raw: bytes, message: str) -> None:
    with pytest.raises(DecodeError, match=message):
        parse_transport_packet(raw)


def test_incremental_parser_accepts_every_split_and_tracks_offsets() -> None:
    raw = _payload_packet(0x100, 1, cc=0) + _payload_packet(0x100, 2, cc=1)
    for split in range(len(raw) + 1):
        parser = TransportStreamParser()
        packets = parser.feed(raw[:split]) + parser.feed(raw[split:]) + parser.finish()
        assert [packet.offset for packet in packets] == [0, 188]
        assert [packet.payload[0] for packet in packets] == [1, 2]


def test_recovery_mode_resynchronizes_live_input() -> None:
    raw = _payload_packet(0x100, 7)
    parser = TransportStreamParser(recover=True)
    assert parser.feed(b"garbage" + raw[:20]) == []
    packets = parser.feed(raw[20:])
    assert len(packets) == 1
    assert packets[0].offset == 7
    assert parser.discarded_bytes == 7


def test_strict_mode_and_finish_reject_misaligned_input() -> None:
    with pytest.raises(DecodeError, match="sync"):
        TransportStreamParser().feed(b"bad" + b"\0" * 185)
    parser = TransportStreamParser()
    parser.feed(_payload_packet(1, 0)[:-1])
    with pytest.raises(TruncatedData, match="187"):
        parser.finish()


def test_iter_transport_stream_reads_without_whole_file_buffering() -> None:
    raw = _payload_packet(2, 3) + _payload_packet(3, 4)
    packets = list(iter_transport_stream(BytesIO(raw), chunk_size=17))
    assert [packet.pid for packet in packets] == [2, 3]
    assert [packet.payload[0] for packet in packets] == [3, 4]
