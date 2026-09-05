from __future__ import annotations

import pytest

from stanag4609.errors import DecodeError, TruncatedData
from stanag4609.transport.mpegts import parse_transport_packet
from stanag4609.transport.psi import (
    Descriptor,
    ElementaryStreamInfo,
    KLVCarriage,
    ProgramMapTable,
    PSISectionAssembler,
    find_klv_streams,
    mpeg2_crc32,
    parse_pat,
    parse_pmt,
)

PAT = bytes.fromhex("00 B0 0D 00 01 C1 00 00 00 01 E1 00 E8 F9 5E 7D")
PMT = bytes.fromhex(
    "02 B0 1D 00 01 C1 00 00 E1 01 F0 00 "
    "1B E1 01 F0 00 "
    "06 E1 02 F0 06 05 04 4B 4C 56 41 "
    "59 5D EC 51"
)
SYNC_PMT = bytes.fromhex(
    "02 B0 22 00 01 C1 00 00 E1 01 F0 00 "
    "1B E1 01 F0 00 "
    "15 E1 03 F0 0B 26 09 01 00 FF 4B 4C 56 41 00 0F "
    "C0 26 21 74"
)


def _packet_with_payload(payload: bytes, *, start: bool, cc: int) -> bytes:
    if not 0 < len(payload) <= 184:
        raise ValueError("payload length must be in 1..184")
    byte1 = 0x01 | (0x40 if start else 0)
    if len(payload) == 184:
        return bytes((0x47, byte1, 0x00, 0x10 | cc)) + payload
    adaptation_length = 183 - len(payload)
    adaptation = b"" if adaptation_length == 0 else b"\x00" + b"\xff" * (
        adaptation_length - 1
    )
    return (
        bytes((0x47, byte1, 0x00, 0x30 | cc, adaptation_length))
        + adaptation
        + payload
    )


def _with_crc(data: bytes) -> bytes:
    return data + mpeg2_crc32(data).to_bytes(4, "big")


def _recrc(data: bytearray) -> bytes:
    data[-4:] = mpeg2_crc32(data[:-4]).to_bytes(4, "big")
    return bytes(data)


def test_mpeg2_crc_known_check_value_and_sections() -> None:
    assert mpeg2_crc32(b"123456789") == 0x0376E6E7
    assert mpeg2_crc32(PAT) == 0
    assert mpeg2_crc32(PMT) == 0


def test_parse_program_association_table() -> None:
    pat = parse_pat(PAT)
    assert pat.transport_stream_id == 1
    assert pat.version_number == 0
    assert pat.current_next_indicator
    assert pat.programs[0].program_number == 1
    assert pat.programs[0].program_map_pid == 0x100


def test_parse_program_map_and_discover_asynchronous_klva() -> None:
    pmt = parse_pmt(PMT)
    assert pmt.program_number == 1
    assert pmt.pcr_pid == 0x101
    assert [(stream.stream_type, stream.elementary_pid) for stream in pmt.streams] == [
        (0x1B, 0x101),
        (0x06, 0x102),
    ]
    assert pmt.streams[1].descriptors[0].tag == 0x05
    assert pmt.streams[1].descriptors[0].data == b"KLVA"
    assert pmt.streams[1].descriptors[0].raw == b"\x05\x04KLVA"
    assert find_klv_streams(pmt) == ((0x102, KLVCarriage.ASYNCHRONOUS),)


def test_discover_synchronous_klva_metadata_descriptor() -> None:
    pmt = parse_pmt(SYNC_PMT)
    assert find_klv_streams(pmt) == ((0x103, KLVCarriage.SYNCHRONOUS),)


def test_table_crc_and_shape_errors_are_rejected() -> None:
    corrupted = PAT[:-1] + bytes((PAT[-1] ^ 1,))
    with pytest.raises(DecodeError, match="CRC"):
        parse_pat(corrupted)
    with pytest.raises(DecodeError, match="PAT"):
        parse_pat(PMT)
    with pytest.raises(DecodeError, match="PMT"):
        parse_pmt(PAT)
    with pytest.raises(TruncatedData):
        parse_pmt(PMT[:-1])
    with pytest.raises(TruncatedData, match="header"):
        parse_pat(b"")
    with pytest.raises(DecodeError, match="syntax"):
        parse_pat(_with_crc(bytes.fromhex("00 30 09 00 01 C1 00 00")))
    with pytest.raises(DecodeError, match="1021"):
        parse_pat(bytes.fromhex("00 B4 00"))
    with pytest.raises(DecodeError, match="trailing"):
        parse_pat(PAT + b"\x00")

    invalid_section_number = bytearray(PAT)
    invalid_section_number[6:8] = b"\x02\x01"
    invalid_section_number[-4:] = mpeg2_crc32(
        invalid_section_number[:-4]
    ).to_bytes(4, "big")
    with pytest.raises(DecodeError, match="section_number"):
        parse_pat(bytes(invalid_section_number))

    invalid_pmt_section = bytearray(PMT)
    invalid_pmt_section[6:8] = b"\x01\x01"
    invalid_pmt_section[-4:] = mpeg2_crc32(invalid_pmt_section[:-4]).to_bytes(
        4, "big"
    )
    with pytest.raises(DecodeError, match=r"PMT.*section_number"):
        parse_pmt(bytes(invalid_pmt_section))


def test_pat_and_pmt_inner_loop_errors_are_rejected() -> None:
    with pytest.raises(DecodeError, match="too short"):
        parse_pat(_with_crc(bytes.fromhex("00 B0 05 00")))
    with pytest.raises(DecodeError, match="multiple"):
        parse_pat(_with_crc(bytes.fromhex("00 B0 0A 00 01 C1 00 00 01")))

    program_descriptor_header_only = _with_crc(
        bytes.fromhex("02 B0 0E 00 01 C1 00 00 E1 00 F0 01 05")
    )
    with pytest.raises(TruncatedData, match="descriptor header"):
        parse_pmt(program_descriptor_header_only)
    program_descriptor_overrun = _with_crc(
        bytes.fromhex("02 B0 0F 00 01 C1 00 00 E1 00 F0 02 05 04")
    )
    with pytest.raises(TruncatedData, match="descriptor overruns"):
        parse_pmt(program_descriptor_overrun)
    stream_header_partial = _with_crc(
        bytes.fromhex("02 B0 10 00 01 C1 00 00 E1 00 F0 00 1B E1 00")
    )
    with pytest.raises(TruncatedData, match="stream header"):
        parse_pmt(stream_header_partial)
    stream_info_overrun = _with_crc(
        bytes.fromhex("02 B0 12 00 01 C1 00 00 E1 00 F0 00 06 E1 02 F0 04")
    )
    with pytest.raises(TruncatedData, match="descriptors overrun"):
        parse_pmt(stream_info_overrun)


@pytest.mark.parametrize(
    "index,mask,match",
    [
        (1, 0x40, "zero bit"),
        (1, 0x20, "reserved"),
        (5, 0x80, "reserved"),
        (10, 0x80, "reserved"),
    ],
)
def test_pat_rejects_nonconformant_fixed_bits(
    index: int,
    mask: int,
    match: str,
) -> None:
    malformed = bytearray(PAT)
    if index == 1 and mask == 0x40:
        malformed[index] |= mask
    else:
        malformed[index] &= ~mask

    with pytest.raises(DecodeError, match=match):
        parse_pat(_recrc(malformed))


@pytest.mark.parametrize("index", [1, 5, 8, 10, 13, 15])
def test_pmt_rejects_nonconformant_reserved_bits(index: int) -> None:
    malformed = bytearray(PMT)
    malformed[index] &= ~0x10 if index in {1, 10, 15} else ~0x80

    with pytest.raises(DecodeError, match="reserved"):
        parse_pmt(_recrc(malformed))


def test_pat_rejects_duplicate_program_number_within_section() -> None:
    duplicate = _with_crc(
        bytes.fromhex("00 B0 11 00 01 C1 00 00 00 01 E1 00 00 01 E1 01")
    )

    with pytest.raises(DecodeError, match="program_number 1 occurs twice"):
        parse_pat(duplicate)


def test_non_klva_descriptors_are_not_misidentified() -> None:
    streams = (
        ElementaryStreamInfo(0x06, 10, (Descriptor(0x05, b"NOPE"),)),
        ElementaryStreamInfo(0x15, 11, (Descriptor(0x26, b"\x01"),)),
        ElementaryStreamInfo(0x15, 12, (Descriptor(0x26, b"\xff\xff\0\0\0\0"),)),
        ElementaryStreamInfo(0x15, 13, (Descriptor(0x26, b"\x01\x00\x01"),)),
    )
    pmt = ProgramMapTable(1, 0, True, 0, 0, 1, (), streams, b"")
    assert find_klv_streams(pmt) == ()


def test_section_assembler_handles_arbitrary_transport_boundaries() -> None:
    first = parse_transport_packet(
        _packet_with_payload(b"\x00" + PMT[:8], start=True, cc=0)
    )
    second = parse_transport_packet(_packet_with_payload(PMT[8:], start=False, cc=1))
    assembler = PSISectionAssembler(pid=0x100)
    assert assembler.feed(first) == []
    assert assembler.feed(second) == [PMT]
    assert assembler.buffered_bytes == 0
    assert assembler.finish() == []


def test_section_assembler_extracts_multiple_sections_and_ignores_stuffing() -> None:
    payload = b"\x00" + PAT + PAT + b"\xff" * (184 - 1 - 2 * len(PAT))
    packet = parse_transport_packet(_packet_with_payload(payload, start=True, cc=0))
    assembler = PSISectionAssembler(pid=0x100)
    assert assembler.feed(packet) == [PAT, PAT]


def test_section_assembler_rejects_non_ff_byte_after_stuffing_begins() -> None:
    payload = b"\x00" + PAT + b"\xff\x00" + b"\xff" * (184 - 1 - len(PAT) - 2)
    packet = parse_transport_packet(_packet_with_payload(payload, start=True, cc=0))

    with pytest.raises(DecodeError, match="stuffing"):
        PSISectionAssembler(pid=0x100).feed(packet)


def test_section_assembler_validates_pid_pointer_and_truncation() -> None:
    assembler = PSISectionAssembler(pid=0x100)
    wrong_pid = parse_transport_packet(
        bytes((0x47, 0x40, 0x01, 0x10)) + b"\x00" + b"\xff" * 183
    )
    with pytest.raises(ValueError, match="PID"):
        assembler.feed(wrong_pid)
    bad_pointer = parse_transport_packet(
        _packet_with_payload(b"\x09abc", start=True, cc=0)
    )
    with pytest.raises(DecodeError, match="pointer"):
        assembler.feed(bad_pointer)
    partial = parse_transport_packet(
        _packet_with_payload(b"\x00" + PMT[:8], start=True, cc=0)
    )
    assembler.feed(partial)
    with pytest.raises(TruncatedData):
        assembler.finish()


def test_section_assembler_bounds_and_live_join_behavior() -> None:
    with pytest.raises(ValueError, match="PID"):
        PSISectionAssembler(pid=0x2000)
    with pytest.raises(ValueError, match="max_section_length"):
        PSISectionAssembler(pid=0x100, max_section_length=2)
    assembler = PSISectionAssembler(pid=0x100, max_section_length=8)
    non_start = parse_transport_packet(_packet_with_payload(PAT, start=False, cc=0))
    assert assembler.feed(non_start) == []
    oversized = parse_transport_packet(
        _packet_with_payload(b"\x00\x00\xb0\x09" + b"\0" * 9, start=True, cc=1)
    )
    with pytest.raises(DecodeError, match="configured limit"):
        assembler.feed(oversized)
