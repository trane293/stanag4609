from __future__ import annotations

from datetime import datetime, timezone

import pytest

from stanag4609.errors import DecodeError
from stanag4609.klv.ber import encode_ber_length, encode_ber_oid
from stanag4609.klv.checksum import running_sum_16
from stanag4609.st0102 import decode_security_local_set
from stanag4609.st0601 import (
    DELETE,
    ST0601_KEY,
    AmendLocalSet,
    FieldDecodingMode,
    MetadataSubstreamID,
    RawFieldValue,
    SegmentLocalSet,
    SpecialValue,
    decode_amend_local_set,
    decode_segment_local_set,
    decode_uas_local_set,
    encode_amend_local_set,
    encode_segment_local_set,
    encode_uas_local_set,
)
from stanag4609.st1010 import SDCCFLP, SDCCParseControl, SDCCValueFormat


def _item(tag: int, value: bytes) -> bytes:
    return encode_ber_oid(tag) + encode_ber_length(len(value)) + value


def _packet(items: bytes) -> bytes:
    value = items + _item(1, b"\x00\x00")
    packet = ST0601_KEY + encode_ber_length(len(value)) + value
    return packet[:-2] + running_sum_16(packet[:-2]).to_bytes(2, "big")


def test_segment_local_set_round_trips_and_nests_in_uas() -> None:
    value = encode_segment_local_set({5: 90, 143: MetadataSubstreamID(1)})
    segment = decode_segment_local_set(value)
    assert isinstance(segment, SegmentLocalSet)
    assert segment.substream_id == MetadataSubstreamID(1)
    assert segment.value(5) == pytest.approx(90, abs=0.002)
    assert bytes(segment.local_set) == value

    second_segment = decode_segment_local_set(
        encode_segment_local_set({143: MetadataSubstreamID(2)})
    )
    packet = encode_uas_local_set(
        {
            2: datetime(2024, 1, 1, tzinfo=timezone.utc),
            65: 19,
            100: [segment, second_segment],
        }
    )
    decoded = decode_uas_local_set(packet)
    segments = decoded.getall(100)
    assert len(segments) == 2
    assert segments[0].value == segment
    assert isinstance(segments[1].value, SegmentLocalSet)
    assert segment.getall(5) == (segment.get(5),)
    assert segment.value(999, "missing") == "missing"


def test_segment_and_amend_children_may_duplicate_a_parent_item() -> None:
    """ST 0601.14-35 gives each nested Local Set its own item scope."""
    segment = decode_segment_local_set(
        encode_segment_local_set({5: 90, 143: MetadataSubstreamID(1)})
    )
    amend = decode_amend_local_set(
        encode_amend_local_set({5: 135, 143: MetadataSubstreamID(2)})
    )
    for branch_tag, branch, child_heading in ((100, segment, 90), (101, amend, 135)):
        packet = encode_uas_local_set(
            {
                2: datetime(2024, 1, 1, tzinfo=timezone.utc),
                5: 45,
                65: 19,
                branch_tag: branch,
            }
        )

        decoded = decode_uas_local_set(packet)

        assert decoded.value(5) == pytest.approx(45, abs=0.002)
        assert decoded.value(branch_tag).value(5) == pytest.approx(
            child_heading, abs=0.003
        )


def test_amend_local_set_represents_deletion_with_zli() -> None:
    value = encode_amend_local_set(
        {13: DELETE, 143: MetadataSubstreamID(7), 200: RawFieldValue(b"extension")}
    )
    amend = decode_amend_local_set(value)
    assert isinstance(amend, AmendLocalSet)
    assert amend.value(13) is DELETE
    assert amend.local_set.getall(200)[0].value == b"extension"
    assert bytes(amend.local_set) == value
    assert amend.getall(13) == (amend.get(13),)
    assert amend.value(999, "missing") == "missing"


def test_child_security_accepts_only_object_country_items() -> None:
    raw_security = bytes.fromhex("0C 01 0E 0D 06 00 55 00 53 00 41")
    security = decode_security_local_set(
        raw_security,
        standalone=False,
        require_required=False,
    )
    segment = decode_segment_local_set(
        encode_segment_local_set({48: security, 143: MetadataSubstreamID(1)})
    )
    child_security = segment.value(48)
    assert child_security.value(12) == 14
    assert child_security.value(13) == "USA"
    with pytest.raises(DecodeError, match="required tag 1"):
        decode_uas_local_set(
            encode_uas_local_set(
                {
                    2: datetime(2024, 1, 1, tzinfo=timezone.utc),
                    48: security,
                    65: 19,
                }
            )
        )


def test_legal_recursive_branch_shapes() -> None:
    leaf_amend = decode_amend_local_set(
        encode_amend_local_set({13: 20, 143: MetadataSubstreamID(3)})
    )
    parent_amend = decode_amend_local_set(
        encode_amend_local_set({101: [leaf_amend], 143: MetadataSubstreamID(2)})
    )
    root_segment = decode_segment_local_set(
        encode_segment_local_set({101: [parent_amend], 143: MetadataSubstreamID(1)})
    )
    assert isinstance(root_segment.value(101), AmendLocalSet)

    leaf_segment = decode_segment_local_set(
        encode_segment_local_set({143: MetadataSubstreamID(4)})
    )
    parent_segment = decode_segment_local_set(
        encode_segment_local_set({100: leaf_segment, 143: MetadataSubstreamID(5)})
    )
    assert isinstance(parent_segment.value(100), SegmentLocalSet)


def test_branch_decoder_bounds_recursive_depth() -> None:
    leaf = _item(143, b"\x03")
    child = _item(101, leaf) + _item(143, b"\x02")
    root = _item(101, child) + _item(143, b"\x01")
    with pytest.raises(DecodeError, match="maximum depth 1"):
        decode_segment_local_set(root, max_depth=1)
    with pytest.raises(ValueError, match="cannot be negative"):
        decode_segment_local_set(_item(143, b"\x01"), max_depth=-1)
    with pytest.raises(TypeError, match="data must be bytes"):
        decode_segment_local_set(bytearray(_item(143, b"\x01")))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="field_decoding"):
        decode_amend_local_set(
            _item(143, b"\x01"), field_decoding="strict"  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="max_depth"):
        decode_amend_local_set(_item(143, b"\x01"), max_depth=True)


def test_branch_preserve_mode_retains_malformed_known_item() -> None:
    value = _item(5, b"\x00") + _item(143, b"\x01")
    with pytest.raises(DecodeError, match="requires 2 byte"):
        decode_segment_local_set(value)
    segment = decode_segment_local_set(
        value, field_decoding=FieldDecodingMode.PRESERVE
    )
    assert not segment.fields
    assert segment.issues[0].tag == 5


@pytest.mark.parametrize(
    ("decoder", "value", "message"),
    [
        (decode_segment_local_set, b"", "requires exactly one"),
        (
            decode_segment_local_set,
            _item(143, b"\x01") + _item(143, b"\x02"),
            "requires exactly one",
        ),
        (
            decode_segment_local_set,
            _item(1, b"\x00\x00") + _item(143, b"\x01"),
            "checksum",
        ),
        (
            decode_segment_local_set,
            _item(100, _item(143, b"\x02"))
            + _item(101, _item(143, b"\x03"))
            + _item(143, b"\x01"),
            "both Segment and Amend",
        ),
        (
            decode_amend_local_set,
            _item(100, _item(143, b"\x02")) + _item(143, b"\x01"),
            "cannot contain Segment",
        ),
    ],
)
def test_branch_decoder_rejects_invalid_hierarchies(
    decoder: object, value: bytes, message: str
) -> None:
    with pytest.raises(DecodeError, match=message):
        decoder(value)  # type: ignore[operator]


def test_branch_encoder_contracts() -> None:
    with pytest.raises(TypeError, match="values must be a mapping"):
        encode_segment_local_set(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="omit the checksum"):
        encode_segment_local_set({1: RawFieldValue(b"xx"), 143: MetadataSubstreamID(1)})
    with pytest.raises(ValueError, match="requires exactly one"):
        encode_segment_local_set({5: 20})
    with pytest.raises(ValueError, match="both Segment and Amend"):
        encode_segment_local_set(
            {
                100: RawFieldValue(_item(143, b"\x02")),
                101: RawFieldValue(_item(143, b"\x03")),
                143: MetadataSubstreamID(1),
            }
        )
    with pytest.raises(ValueError, match="cannot contain Segment"):
        encode_amend_local_set(
            {100: RawFieldValue(_item(143, b"\x02")), 143: MetadataSubstreamID(1)}
        )
    with pytest.raises(ValueError, match=r"DELETE.*Amend"):
        encode_segment_local_set({5: DELETE, 143: MetadataSubstreamID(1)})
    with pytest.raises(TypeError, match="non-negative integers"):
        encode_segment_local_set({"x": RawFieldValue(b"x"), 143: MetadataSubstreamID(1)})  # type: ignore[dict-item]
    with pytest.raises(TypeError, match="requires RawFieldValue"):
        encode_segment_local_set({143: MetadataSubstreamID(1), 200: b"raw"})


def test_root_level_cannot_mix_segment_and_amend() -> None:
    segment = decode_segment_local_set(
        encode_segment_local_set({143: MetadataSubstreamID(1)})
    )
    amend = decode_amend_local_set(
        encode_amend_local_set({143: MetadataSubstreamID(2)})
    )
    with pytest.raises(ValueError, match="both Segment and Amend"):
        encode_uas_local_set(
            {
                2: datetime(2024, 1, 1, tzinfo=timezone.utc),
                65: 19,
                100: segment,
                101: amend,
            }
        )
    with pytest.raises(ValueError, match="cannot be Unknown"):
        encode_uas_local_set(
            {
                2: datetime(2024, 1, 1, tzinfo=timezone.utc),
                65: 19,
                100: SpecialValue.UNKNOWN,
            }
        )

    timestamp_value = int(
        datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1_000_000
    )
    timestamp = _item(2, timestamp_value.to_bytes(8, "big"))
    version = _item(65, b"\x13")
    with pytest.raises(DecodeError, match="both Segment and Amend"):
        decode_uas_local_set(
            _packet(
                timestamp
                + version
                + _item(100, bytes(segment.local_set))
                + _item(101, bytes(amend.local_set))
            )
        )


def test_amend_local_set_nests_in_uas() -> None:
    amend = decode_amend_local_set(
        encode_amend_local_set({13: DELETE, 143: MetadataSubstreamID(9)})
    )
    packet = encode_uas_local_set(
        {
            2: datetime(2024, 1, 1, tzinfo=timezone.utc),
            65: 19,
            101: amend,
        }
    )
    assert decode_uas_local_set(packet).value(101) == amend


def test_segment_preserves_sdcc_refined_source_order() -> None:
    sdcc = SDCCFLP(
        2,
        SDCCParseControl(
            2,
            False,
            4,
            2,
            SDCCValueFormat.IEEE,
            SDCCValueFormat.IMAP,
        ),
        (1.0, 2.0),
        (0.0,),
        source_tags=(14, 13),
    )
    encoded = encode_segment_local_set(
        {
            13: 10.0,
            14: 20.0,
            102: sdcc,
            143: MetadataSubstreamID(7),
        }
    )
    segment = decode_segment_local_set(encoded)
    assert tuple(item.tag for item in segment.local_set.items) == (143, 14, 13, 102)
    assert segment.value(102).source_tags == (14, 13)
