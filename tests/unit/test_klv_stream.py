from __future__ import annotations

from io import BytesIO

import pytest

from stanag4609.errors import DecodeError, LimitExceeded, TruncatedData
from stanag4609.klv.ber import encode_ber_length
from stanag4609.klv.stream import KLVStreamParser, iter_klv

KEY = bytes.fromhex("06 0E 2B 34 02 0B 01 01 0E 01 03 01 01 00 00 00")


def make_packet(value: bytes) -> bytes:
    return KEY + encode_ber_length(len(value)) + value


def test_incremental_parser_accepts_every_single_split() -> None:
    raw = make_packet(bytes(range(128)))
    for split in range(len(raw) + 1):
        parser = KLVStreamParser(key_prefix=KEY)
        packets = parser.feed(raw[:split]) + parser.feed(raw[split:]) + parser.finish()
        assert len(packets) == 1
        assert packets[0].raw == raw
        assert packets[0].offset == 0


def test_incremental_parser_accepts_one_byte_chunks_and_tracks_offsets() -> None:
    raw = make_packet(b"one") + make_packet(b"two")
    parser = KLVStreamParser(key_prefix=KEY)
    packets = []
    for octet in raw:
        packets.extend(parser.feed(bytes((octet,))))
    packets.extend(parser.finish())
    assert [packet.value for packet in packets] == [b"one", b"two"]
    assert packets[1].offset == len(make_packet(b"one"))


def test_iter_klv_reads_file_like_stream() -> None:
    raw = make_packet(b"a") + make_packet(b"bc")
    assert [packet.value for packet in iter_klv(BytesIO(raw), chunk_size=2)] == [b"a", b"bc"]


def test_parser_recovery_discards_noise_but_preserves_split_prefix() -> None:
    parser = KLVStreamParser(key_prefix=KEY[:4], recover=True)
    assert parser.feed(b"noise" + KEY[:2]) == []
    packets = parser.feed(KEY[2:] + b"\x01x")
    assert [packet.value for packet in packets] == [b"x"]
    assert packets[0].offset == 5


def test_parser_rejects_noise_in_strict_mode() -> None:
    with pytest.raises(DecodeError):
        KLVStreamParser(key_prefix=KEY).feed(b"not a key")


def test_parser_rejects_oversized_value_before_buffering_it() -> None:
    parser = KLVStreamParser(key_prefix=KEY, max_value_length=10)
    with pytest.raises(LimitExceeded):
        parser.feed(KEY + encode_ber_length(11))


def test_finish_rejects_truncated_packet() -> None:
    parser = KLVStreamParser(key_prefix=KEY)
    parser.feed(make_packet(b"abc")[:-1])
    with pytest.raises(TruncatedData):
        parser.finish()


def test_parser_rejects_smpte_label_as_klv_key() -> None:
    label = bytes.fromhex("06 0E 2B 34 04 01 01 01 0E 01 01 01 01 00 00 00")

    with pytest.raises(DecodeError, match=r"SMPTE Label.*cannot be used as a KLV key"):
        KLVStreamParser().feed(label + b"\x00")


def test_parser_preserves_unrecognized_st336_category() -> None:
    key = bytes.fromhex("06 0E 2B 34 06 01 01 01 0E 01 01 01 01 00 00 00")

    packets = KLVStreamParser().feed(key + b"\x01x")

    assert len(packets) == 1
    assert packets[0].key == key
    assert packets[0].value == b"x"


def test_parser_can_disable_smpte_validation_for_generic_fixed_width_keys() -> None:
    parser = KLVStreamParser(
        key_length=4,
        key_prefix=None,
        validate_smpte_keys=False,
    )

    packets = parser.feed(b"key!\x01x")

    assert [packet.value for packet in packets] == [b"x"]
