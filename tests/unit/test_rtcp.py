from __future__ import annotations

from collections.abc import Callable
from fractions import Fraction

import pytest

import stanag4609 as public_api
from stanag4609.errors import DecodeError, TruncatedData
from stanag4609.transport.rtcp import (
    RTCP_CNAME_ITEM_TYPE,
    RTCP_RECEIVER_REPORT_TYPE,
    RTCP_SDES_PACKET_TYPE,
    RTCP_SENDER_REPORT_TYPE,
    RTCP_VERSION,
    RTCPPacket,
    RTCPReceiverReport,
    RTCPReceptionReport,
    RTCPSDESChunk,
    RTCPSDESItem,
    RTCPSDESPacket,
    RTCPSenderReport,
    RTPNTPClockMapping,
    encode_rtcp_sender_compound,
    parse_rtcp_packets,
    parse_rtcp_sdes_packets,
    parse_rtcp_sender_reports,
    validate_rtcp_compound,
)
from stanag4609.transport.rtp import RTPMPEG2TransportPacketizer

SENDER_REPORT_WITHOUT_BLOCKS = bytes.fromhex(
    "80 c8 00 0601 02 03 04e0 00 00 00 80 00 00 0012 34 56 7800 00 00 0200 00 05 78"
)
SENDER_COMPOUND = SENDER_REPORT_WITHOUT_BLOCKS + bytes.fromhex(
    "81 ca 00 03 01 02 03 04 01 03 61 40 62 00 00 00"
)


def test_sender_report_decodes_and_reencodes_exact_wire_vector() -> None:
    reports = parse_rtcp_sender_reports(SENDER_REPORT_WITHOUT_BLOCKS)

    assert reports == (
        RTCPSenderReport(
            ssrc=0x01020304,
            ntp_seconds=0xE0000000,
            ntp_fraction=0x80000000,
            rtp_timestamp=0x12345678,
            sender_packet_count=2,
            sender_octet_count=1400,
        ),
    )
    assert reports[0].ntp_timestamp == Fraction(0xE0000000 * 2 + 1, 2)
    assert reports[0].encode() == SENDER_REPORT_WITHOUT_BLOCKS
    assert parse_rtcp_sender_reports(reports[0].encode(padding_size=4)) == reports
    assert RTCP_VERSION == 2
    assert RTCP_SENDER_REPORT_TYPE == 200


def test_sender_report_round_trips_signed_reception_report_block() -> None:
    block = RTCPReceptionReport(
        ssrc=0xA0A1A2A3,
        fraction_lost=17,
        cumulative_packets_lost=-2,
        extended_highest_sequence=0x0001FFFF,
        interarrival_jitter=900,
        last_sender_report=0x12345678,
        delay_since_last_sender_report=0x00008000,
    )
    report = RTCPSenderReport(
        ssrc=0x01020304,
        ntp_seconds=3,
        ntp_fraction=4,
        rtp_timestamp=5,
        sender_packet_count=6,
        sender_octet_count=7,
        reception_reports=(block,),
    )

    encoded = report.encode()

    assert encoded[:4] == bytes.fromhex("81 c8 00 0c")
    assert encoded[33:36] == bytes.fromhex("ff ff fe")
    assert parse_rtcp_sender_reports(encoded) == (report,)


def test_receiver_report_round_trips_reception_blocks() -> None:
    block = RTCPReceptionReport(9, 1, 2, 3, 4, 5, 6)
    report = RTCPReceiverReport(ssrc=7, reception_reports=(block,))

    encoded = report.encode()

    assert encoded[:4] == bytes.fromhex("81 c9 00 07")
    assert RTCPReceiverReport.from_packet(parse_rtcp_packets(encoded)[0]) == report
    assert RTCP_RECEIVER_REPORT_TYPE == 201

    receiver_compound = encoded + RTCPSDESPacket(
        (RTCPSDESChunk(7, (RTCPSDESItem.from_text(1, "receiver.example"),)),)
    ).encode()
    assert validate_rtcp_compound(receiver_compound)


def test_sdes_cname_decodes_and_sender_compound_matches_exact_vector() -> None:
    packets = validate_rtcp_compound(SENDER_COMPOUND)
    descriptions = parse_rtcp_sdes_packets(SENDER_COMPOUND)

    assert len(packets) == 2
    assert descriptions == (
        RTCPSDESPacket(
            chunks=(
                RTCPSDESChunk(
                    ssrc=0x01020304,
                    items=(RTCPSDESItem.from_text(RTCP_CNAME_ITEM_TYPE, "a@b"),),
                ),
            ),
        ),
    )
    assert descriptions[0].cname_for_ssrc(0x01020304) == "a@b"
    assert descriptions[0].encode() == SENDER_COMPOUND[len(SENDER_REPORT_WITHOUT_BLOCKS) :]
    assert public_api.RTCPSDESPacket is RTCPSDESPacket
    assert (
        encode_rtcp_sender_compound(
            parse_rtcp_sender_reports(SENDER_COMPOUND)[0],
            cname="a@b",
        )
        == SENDER_COMPOUND
    )
    assert RTCP_SDES_PACKET_TYPE == 202


def test_sdes_round_trips_multiple_chunks_and_unknown_items() -> None:
    packet = RTCPSDESPacket(
        chunks=(
            RTCPSDESChunk(
                1,
                (
                    RTCPSDESItem.from_text(1, "sender.example"),
                    RTCPSDESItem.from_text(6, "stanag4609"),
                ),
            ),
            RTCPSDESChunk(2, (RTCPSDESItem(8, b"\x01x"),)),
        )
    )

    encoded = packet.encode()

    assert parse_rtcp_sdes_packets(encoded) == (packet,)
    assert packet.chunks[0].items[1].text == "stanag4609"
    assert packet.cname_for_ssrc(2) is None


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (SENDER_REPORT_WITHOUT_BLOCKS, "at least two"),
        (
            RTCPPacket(204, 0, b"").encode() + SENDER_COMPOUND[len(SENDER_REPORT_WITHOUT_BLOCKS) :],
            "begin with",
        ),
        (
            SENDER_REPORT_WITHOUT_BLOCKS + RTCPPacket(204, 0, b"").encode(),
            "CNAME",
        ),
        (
            SENDER_REPORT_WITHOUT_BLOCKS
            + RTCPSDESPacket((RTCPSDESChunk(99, (RTCPSDESItem.from_text(1, "other"),)),)).encode(),
            "sender SSRC",
        ),
    ],
)
def test_compound_validator_requires_report_first_and_matching_cname(
    data: bytes,
    message: str,
) -> None:
    with pytest.raises(DecodeError, match=message):
        validate_rtcp_compound(data)


@pytest.mark.parametrize(
    ("payload", "count", "padding_size", "message"),
    [
        (b"", 1, 0, "source identifier"),
        (b"\x00\x00\x00\x01\x01", 1, 3, "length"),
        (b"\x00\x00\x00\x01\x01\x02x", 1, 1, "value"),
        (b"\x00\x00\x00\x01\x00\x01\x00\x00", 1, 0, "padding"),
        (b"\x00\x00\x00\x01\x00\x00\x00\x00", 2, 0, "source identifier"),
    ],
)
def test_sdes_parser_rejects_truncated_items_bad_padding_and_count(
    payload: bytes,
    count: int,
    padding_size: int,
    message: str,
) -> None:
    packet = RTCPPacket(RTCP_SDES_PACKET_TYPE, count, payload, padding_size)
    with pytest.raises((DecodeError, TruncatedData), match=message):
        parse_rtcp_sdes_packets(packet.encode())


def test_cname_requires_nonempty_utf8_at_most_255_octets() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        RTCPSDESItem.from_text(RTCP_CNAME_ITEM_TYPE, "")
    with pytest.raises(ValueError, match="255"):
        RTCPSDESItem.from_text(RTCP_CNAME_ITEM_TYPE, "é" * 128)
    invalid = RTCPSDESPacket((RTCPSDESChunk(1, (RTCPSDESItem(1, b"\xff"),)),))
    with pytest.raises(DecodeError, match="UTF-8"):
        invalid.cname_for_ssrc(1)

    duplicate = RTCPSDESPacket(
        (
            RTCPSDESChunk(
                1,
                (
                    RTCPSDESItem.from_text(1, "first.example"),
                    RTCPSDESItem.from_text(1, "second.example"),
                ),
            ),
        )
    )
    with pytest.raises(DecodeError, match="multiple CNAME"):
        duplicate.cname_for_ssrc(1)


def test_generic_compound_parser_preserves_unknown_packets_and_last_padding() -> None:
    sender = RTCPSenderReport(1, 2, 3, 4, 5, 6).encode()
    application_packet = RTCPPacket(
        packet_type=204,
        count=7,
        payload=b"TESTdata",
        padding_size=4,
    )
    compound = sender + application_packet.encode()

    packets = parse_rtcp_packets(compound)

    assert len(packets) == 2
    assert packets[0].packet_type == RTCP_SENDER_REPORT_TYPE
    assert packets[1] == application_packet
    assert b"".join(packet.encode() for packet in packets) == compound
    assert parse_rtcp_sender_reports(compound)[0].ssrc == 1


@pytest.mark.parametrize(
    ("data", "error", "message"),
    [
        (b"", DecodeError, "empty"),
        (b"\x80", TruncatedData, "header"),
        (b"\x40\xc8\x00\x00", DecodeError, "version"),
        (b"\x80\xc8\x00\x06" + b"\x00" * 20, TruncatedData, "declares"),
        (b"\xa0\xcc\x00\x01abcd", DecodeError, "padding length"),
        (b"\xa0\xcc\x00\x01abc\x00", DecodeError, "padding length"),
        (
            RTCPPacket(204, 0, b"", padding_size=4).encode() + RTCPPacket(204, 0, b"").encode(),
            DecodeError,
            "final packet",
        ),
    ],
)
def test_compound_parser_rejects_malformed_packets(
    data: bytes,
    error: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error, match=message):
        parse_rtcp_packets(data)


def test_sender_report_parser_rejects_wrong_type_and_report_count_length() -> None:
    wrong_type = bytearray(SENDER_REPORT_WITHOUT_BLOCKS)
    wrong_type[1] = 201
    with pytest.raises(DecodeError, match="does not contain a Sender Report"):
        parse_rtcp_sender_reports(wrong_type)

    wrong_count = bytearray(SENDER_REPORT_WITHOUT_BLOCKS)
    wrong_count[0] = 0x81
    with pytest.raises(DecodeError, match="reception report"):
        parse_rtcp_sender_reports(wrong_count)


def test_clock_mapping_is_exact_and_wrap_safe_in_both_directions() -> None:
    report = RTCPSenderReport(
        ssrc=9,
        ntp_seconds=100,
        ntp_fraction=0x80000000,
        rtp_timestamp=0xFFFFFFF0,
        sender_packet_count=0,
        sender_octet_count=0,
    )
    mapping = RTPNTPClockMapping.from_sender_report(report, clock_rate=90_000)

    assert mapping.ntp_timestamp(0x10) == Fraction(201, 2) + Fraction(32, 90_000)
    assert mapping.ntp_timestamp(0xFFFFFF00) == Fraction(201, 2) - Fraction(240, 90_000)
    assert mapping.rtp_timestamp(mapping.ntp_timestamp(0x10)) == 0x10
    assert public_api.RTPNTPClockMapping is RTPNTPClockMapping


def test_independent_video_and_metadata_clocks_align_on_sender_report_ntp() -> None:
    video = RTPNTPClockMapping.from_sender_report(
        RTCPSenderReport(1, 100, 0, 9_000, 0, 0),
        clock_rate=90_000,
    )
    metadata = RTPNTPClockMapping.from_sender_report(
        RTCPSenderReport(2, 100, 0x80000000, 500, 0, 0),
        clock_rate=1_000,
    )

    assert video.ntp_timestamp(99_000) == Fraction(101)
    assert metadata.ntp_timestamp(1_000) == Fraction(101)
    assert metadata.rtp_timestamp(video.ntp_timestamp(99_000)) == 1_000


def test_rtp_packetizer_builds_compound_reports_from_emitted_payload_counts() -> None:
    ts_packet = bytes.fromhex("47 01 00 10") + b"\xff" * 184
    packetizer = RTPMPEG2TransportPacketizer(ssrc=0x01020304, sequence_number=1)
    assert packetizer.sender_packet_count == 0
    assert packetizer.sender_octet_count == 0

    packetizer.packetize(ts_packet, timestamp=90_000)
    packetizer.packetize(ts_packet * 2, timestamp=180_000)
    compound = packetizer.compound_sender_report(
        ntp_seconds=0xE0000000,
        ntp_fraction=0x80000000,
        rtp_timestamp=180_000,
        cname="sensor@example.test",
    )

    report = parse_rtcp_sender_reports(compound)[0]
    assert report.ssrc == 0x01020304
    assert report.sender_packet_count == 2
    assert report.sender_octet_count == 3 * 188
    assert parse_rtcp_sdes_packets(compound)[0].cname_for_ssrc(report.ssrc) == (
        "sensor@example.test"
    )
    assert validate_rtcp_compound(compound)


def test_rtp_packetizer_sender_counters_wrap_as_rfc_3550_uint32_values() -> None:
    ts_packet = bytes.fromhex("47 01 00 10") + b"\xff" * 184
    packetizer = RTPMPEG2TransportPacketizer(ssrc=1, sequence_number=1)
    packetizer.sender_packet_count = 0xFFFFFFFF
    packetizer.sender_octet_count = 0xFFFFFF80

    packetizer.packetize(ts_packet, timestamp=1)

    assert packetizer.sender_packet_count == 0
    assert packetizer.sender_octet_count == 60


@pytest.mark.parametrize(
    "factory",
    [
        lambda: RTCPPacket(256, 0, b""),
        lambda: RTCPPacket(200, 32, b""),
        lambda: RTCPPacket(200, 0, b"x"),
        lambda: RTCPPacket(200, 0, b"", padding_size=3),
        lambda: RTCPReceptionReport(0, 0, -(1 << 23) - 1, 0, 0, 0, 0),
        lambda: RTCPSenderReport(
            0,
            0,
            0,
            0,
            0,
            0,
            tuple(RTCPReceptionReport(0, 0, 0, 0, 0, 0, 0) for _ in range(32)),
        ),
        lambda: RTPNTPClockMapping(0, 0, 0, 0, 0),
    ],
)
def test_rtcp_models_reject_out_of_range_or_unaligned_values(
    factory: Callable[[], object],
) -> None:
    with pytest.raises(ValueError):
        factory()
