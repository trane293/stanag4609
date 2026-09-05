from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from stanag4609.errors import ChecksumError, DecodeError
from stanag4609.klv.ber import encode_ber_length, encode_ber_oid
from stanag4609.klv.checksum import running_sum_16
from stanag4609.klv.model import KLVPacket
from stanag4609.st0102 import SecurityLocalSet, encode_security_local_set
from stanag4609.st0601 import (
    DELETE,
    FIELD_DEFINITIONS,
    ST0601_KEY,
    FieldDecodingMode,
    GenericFlagData,
    IcingDetected,
    IMAPFieldValue,
    LaserPRFCode,
    OperationalMode,
    PlatformStatus,
    PositioningMethodSource,
    RawFieldValue,
    SensorControlMode,
    SensorFieldOfViewName,
    SpecialValue,
    ST0601Semantic,
    ST0601ValidationContext,
    WeaponFired,
    WeaponLoad,
    decode_uas_local_set,
    encode_field_value,
    encode_uas_local_set,
    misp_timestamp_to_utc,
    update_uas_local_set,
    utc_to_misp_timestamp,
)
from stanag4609.st0806 import RVTLocalSet, decode_rvt_local_set, encode_rvt_local_set
from stanag4609.st0903 import (
    VMTILocalSet,
    VMTIValidationContext,
    VTargetData,
    encode_vmti_local_set,
)
from stanag4609.st1010 import SDCCFLP, SDCCParseControl, SDCCValueFormat
from stanag4609.st1204 import IdentifierQuality, MIISCoreIdentifier

# MISB ST 0902.8 Annex C "Dynamic Only" example, copied byte-for-byte from the
# official publication downloaded from the NGA MISB registry.
DYNAMIC_ONLY = bytes.fromhex(
    "060e2b34020b01010e0103010100000061"
    "020800046050584e0180050271c20602fd3d070208b8"
    "0d045595b66d0e045b5360c40f02c2211002cd9c1102d917"
    "1204724a0a20130487f84b8614047dc55ece150403830926"
    "160212811704f101a229180414bc082b190234f3410106"
    "01025c2b"
)


def test_st0601_active_root_item_registry_is_complete() -> None:
    """ST 0601.19 Items 1-142 are active at root except deprecated Item 66."""
    assert set(FIELD_DEFINITIONS) == set(range(1, 143)) - {66}
    assert 143 not in FIELD_DEFINITIONS  # MSID is restricted to Segment/Amend Local Sets.


def _packet_with_items(items: bytes) -> bytes:
    value = items + b"\x01\x02\x00\x00"
    packet = ST0601_KEY + encode_ber_length(len(value)) + value
    return packet[:-2] + running_sum_16(packet[:-2]).to_bytes(2, "big")


def test_running_sum_reference_packet() -> None:
    assert running_sum_16(DYNAMIC_ONLY[:-2]) == 0x5C2B


def test_decode_dynamic_only_reference_packet_losslessly() -> None:
    uas = decode_uas_local_set(DYNAMIC_ONLY)
    assert uas.packet.key == ST0601_KEY
    assert bytes(uas.packet) == DYNAMIC_ONLY
    assert bytes(uas.local_set) == uas.packet.value
    assert uas.value(2) == datetime(2009, 1, 12, 22, 8, 22, tzinfo=timezone.utc)
    assert uas.value(5) == pytest.approx(159.9743648432)
    assert uas.value(6) == pytest.approx(-0.4315317240)
    assert uas.value(7) == pytest.approx(3.4058656575)
    assert uas.value(13) == pytest.approx(60.1768229660)
    assert uas.value(14) == pytest.approx(128.4267590421)
    assert uas.value(65) == 6


def test_misp_timestamp_utc_conversion_applies_leap_seconds_and_correction() -> None:
    utc = datetime(2017, 1, 1, tzinfo=timezone.utc)
    corrected_misp_microseconds = 1_483_228_829_125_000

    assert misp_timestamp_to_utc(
        corrected_misp_microseconds - 125_000,
        leap_seconds=29,
        correction_offset=125_000,
    ) == utc + timedelta(microseconds=125_000)
    assert utc_to_misp_timestamp(
        utc + timedelta(microseconds=125_000),
        leap_seconds=29,
        correction_offset=125_000,
    ) == corrected_misp_microseconds - 125_000


def test_uas_local_set_converts_timestamp_with_packet_time_adjustments() -> None:
    packet = decode_uas_local_set(
        encode_uas_local_set(
            {
                2: 1_483_228_800_000_000,
                65: 19,
                136: 29,
                137: 125_000,
            }
        )
    )

    assert packet.misp_timestamp_microseconds == 1_483_228_800_000_000
    assert packet.utc_timestamp() == datetime(
        2016, 12, 31, 23, 59, 31, 125_000, tzinfo=timezone.utc
    )
    assert packet.utc_timestamp(leap_seconds=28) == datetime(
        2016, 12, 31, 23, 59, 32, 125_000, tzinfo=timezone.utc
    )


def test_utc_conversion_requires_explicit_or_packet_leap_seconds() -> None:
    packet = decode_uas_local_set(
        encode_uas_local_set({2: 1_483_228_800_000_000, 65: 19})
    )

    with pytest.raises(ValueError, match="leap_seconds"):
        packet.utc_timestamp()
    assert packet.utc_timestamp(leap_seconds=29) == datetime(
        2016, 12, 31, 23, 59, 31, tzinfo=timezone.utc
    )


@pytest.mark.parametrize(
    ("call", "match"),
    [
        (lambda: misp_timestamp_to_utc(0, leap_seconds=True), "integer"),
        (lambda: misp_timestamp_to_utc(0, leap_seconds=0, correction_offset=1.5), "integer"),
        (lambda: misp_timestamp_to_utc(0, leap_seconds=2**31), "outside"),
        (
            lambda: misp_timestamp_to_utc(
                0, leap_seconds=0, correction_offset=2**63
            ),
            "outside",
        ),
        (
            lambda: utc_to_misp_timestamp(
                datetime(2017, 1, 1), leap_seconds=29
            ),
            "timezone-aware",
        ),
        (
            lambda: utc_to_misp_timestamp(
                datetime(1970, 1, 1, tzinfo=timezone.utc),
                leap_seconds=-1,
            ),
            "unsigned 64-bit",
        ),
    ],
)
def test_misp_utc_conversion_rejects_ambiguous_or_unrepresentable_values(
    call: object,
    match: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=match):
        call()  # type: ignore[operator]


@pytest.mark.parametrize(
    ("semantic", "legacy_tag", "preferred_tag", "legacy_value", "preferred_value"),
    [
        (ST0601Semantic.PLATFORM_PITCH, 6, 90, 5.0, 45.0),
        (ST0601Semantic.PLATFORM_ROLL, 7, 91, 6.0, 46.0),
        (ST0601Semantic.PLATFORM_ANGLE_OF_ATTACK, 50, 92, 7.0, 47.0),
        (ST0601Semantic.PLATFORM_SIDESLIP, 52, 93, 8.0, 98.0),
        (ST0601Semantic.TARGET_WIDTH, 22, 96, 100.0, IMAPFieldValue(200.0, 3)),
        (ST0601Semantic.DENSITY_ALTITUDE, 38, 103, 1_000.0, IMAPFieldValue(2_000.0, 3)),
        (ST0601Semantic.FRAME_CENTER_HEIGHT, 25, 78, 300.0, 400.0),
    ],
)
def test_preferred_scalar_representations_follow_st0601_requirements(
    semantic: ST0601Semantic,
    legacy_tag: int,
    preferred_tag: int,
    legacy_value: object,
    preferred_value: object,
) -> None:
    uas = decode_uas_local_set(
        encode_uas_local_set(
            {
                2: datetime(2025, 1, 1, tzinfo=timezone.utc),
                legacy_tag: legacy_value,
                preferred_tag: preferred_value,
                65: 19,
            }
        )
    )

    resolved = uas.preferred_field(semantic)

    assert resolved is not None
    assert resolved.field.definition.tag == preferred_tag
    assert resolved.value == pytest.approx(
        preferred_value.value if isinstance(preferred_value, IMAPFieldValue) else preferred_value,
        abs=1.0,
    )
    assert tuple(field.definition.tag for field in resolved.ignored) == (legacy_tag,)
    assert preferred_tag in {field.definition.tag for field in uas.effective_fields}
    assert legacy_tag not in {field.definition.tag for field in uas.effective_fields}


@pytest.mark.parametrize(
    ("semantic", "values", "selected_tag", "ignored_tags"),
    [
        (
            ST0601Semantic.SENSOR_HEIGHT,
            {15: 100.0, 75: 200.0, 104: IMAPFieldValue(300.0, 3)},
            104,
            (75, 15),
        ),
        (
            ST0601Semantic.ALTERNATE_PLATFORM_HEIGHT,
            {69: 400.0, 76: 500.0, 105: IMAPFieldValue(600.0, 3)},
            105,
            (76, 69),
        ),
    ],
)
def test_extended_hae_representation_wins_complete_height_priority_chain(
    semantic: ST0601Semantic,
    values: dict[int, object],
    selected_tag: int,
    ignored_tags: tuple[int, ...],
) -> None:
    uas = decode_uas_local_set(
        encode_uas_local_set(
            {2: datetime(2025, 1, 1, tzinfo=timezone.utc), **values, 65: 19}
        )
    )

    resolved = uas.preferred_field(semantic)

    assert resolved is not None
    assert resolved.field.definition.tag == selected_tag
    assert tuple(field.definition.tag for field in resolved.ignored) == ignored_tags


def test_preferred_field_falls_back_and_validates_semantic_name() -> None:
    uas = decode_uas_local_set(
        encode_uas_local_set(
            {2: datetime(2025, 1, 1, tzinfo=timezone.utc), 15: 123.0, 65: 19}
        )
    )

    resolved = uas.preferred_field("sensor_height")

    assert resolved is not None
    assert resolved.field.definition.tag == 15
    assert resolved.ignored == ()
    assert uas.preferred_field(ST0601Semantic.TARGET_WIDTH) is None
    with pytest.raises(ValueError, match="unknown ST 0601 semantic"):
        uas.preferred_field("not_a_semantic")


def test_checksum_corruption_is_detected_but_can_be_inspected_explicitly() -> None:
    corrupted = bytearray(DYNAMIC_ONLY)
    corrupted[30] ^= 1
    with pytest.raises(ChecksumError):
        decode_uas_local_set(bytes(corrupted))
    decoded = decode_uas_local_set(bytes(corrupted), verify_checksum=False)
    assert decoded.packet.raw == bytes(corrupted)


def test_checksum_must_be_final_and_two_bytes() -> None:
    value = bytes.fromhex("020800046050584e01800101ff")
    packet = ST0601_KEY + bytes((len(value),)) + value
    with pytest.raises(ChecksumError):
        decode_uas_local_set(packet, verify_checksum=False)


@pytest.mark.parametrize(
    "tag",
    [
        tag
        for tag, definition in FIELD_DEFINITIONS.items()
        if tag != 1
        and definition.kind in {"mapped", "timestamp", "uint", "sint"}
        and definition.length is not None
    ],
)
def test_every_fixed_length_numeric_item_rejects_wrong_wire_length(tag: int) -> None:
    """ST 0107.3-06 is enforced for the complete ST 0601 root registry."""
    definition = FIELD_DEFINITIONS[tag]
    assert definition.length is not None
    malformed = _item(tag, bytes(definition.length + 1))
    timestamp = bytes.fromhex("020800046050584e0180")
    version = bytes.fromhex("410113")
    if tag == 2:
        items = malformed + version
    elif tag == 65:
        items = timestamp + malformed
    else:
        items = timestamp + malformed + version

    with pytest.raises(DecodeError, match=rf"tag {tag}.*requires"):
        decode_uas_local_set(_packet_with_items(items))


@pytest.mark.parametrize(
    "tag",
    [
        tag
        for tag, definition in FIELD_DEFINITIONS.items()
        if definition.maximum_length is not None
    ],
)
def test_every_bounded_item_rejects_values_above_its_maximum_length(tag: int) -> None:
    """ST 0107.3-07 is enforced before field-specific decoding."""
    definition = FIELD_DEFINITIONS[tag]
    assert definition.maximum_length is not None
    malformed = _item(tag, bytes(definition.maximum_length + 1))
    timestamp = bytes.fromhex("020800046050584e0180")
    version = bytes.fromhex("410113")

    with pytest.raises(DecodeError, match=r"maximum|between"):
        decode_uas_local_set(_packet_with_items(timestamp + malformed + version))


@pytest.mark.parametrize(
    "tag, maximum_length",
    [
        (3, 127),
        (4, 127),
        (10, 127),
        (11, 127),
        (12, 127),
        (59, 127),
        (70, 127),
        (106, 127),
        (107, 127),
        (108, 127),
        (129, 32),
        (135, 127),
    ],
)
def test_every_root_text_item_enforces_its_st0601_maximum_length(
    tag: int,
    maximum_length: int,
) -> None:
    """ST 0107.3-07 applies to every ST 0601.19 root UTF-8 item."""
    definition = FIELD_DEFINITIONS[tag]
    assert definition.kind == "text"
    assert definition.maximum_length == maximum_length
    assert encode_field_value(tag, "X" * maximum_length) == b"X" * maximum_length

    with pytest.raises(ValueError, match=rf"{maximum_length}-byte maximum"):
        encode_field_value(tag, "X" * (maximum_length + 1))

    timestamp = bytes.fromhex("020800046050584e0180")
    version = bytes.fromhex("410113")
    oversized = _item(tag, b"X" * (maximum_length + 1))
    with pytest.raises(DecodeError, match=rf"tag {tag}.*{maximum_length}-byte maximum"):
        decode_uas_local_set(_packet_with_items(timestamp + oversized + version))


def test_timestamp_is_required_in_strict_decode() -> None:
    value = bytes.fromhex("01020000")
    packet = ST0601_KEY + bytes((len(value),)) + value
    with pytest.raises(DecodeError, match="Time Stamp"):
        decode_uas_local_set(packet, verify_checksum=False)


def test_timestamp_must_be_first_and_version_is_required() -> None:
    timestamp = bytes.fromhex("020800046050584e0180")
    with pytest.raises(DecodeError, match="first item"):
        decode_uas_local_set(
            _packet_with_items(bytes.fromhex("05020000") + timestamp + bytes.fromhex("410113"))
        )
    with pytest.raises(DecodeError, match="Version Number"):
        decode_uas_local_set(_packet_with_items(timestamp))


def test_metadata_birth_context_validates_timestamp_on_encode_and_decode() -> None:
    birth = datetime(2025, 1, 2, 3, 4, 5, 678_901, tzinfo=timezone.utc)
    context = ST0601ValidationContext(metadata_birth_timestamp=birth)
    packet = encode_uas_local_set({2: birth, 65: 19}, context=context)

    assert decode_uas_local_set(packet, context=context).value(2) == birth

    later = ST0601ValidationContext(metadata_birth_timestamp=birth.replace(microsecond=678_902))
    with pytest.raises(ValueError, match="time of birth"):
        encode_uas_local_set({2: birth, 65: 19}, context=later)
    with pytest.raises(DecodeError, match="time of birth"):
        decode_uas_local_set(packet, context=later)


def test_lossless_update_can_validate_metadata_birth_timestamp() -> None:
    original_birth = datetime(2025, 1, 2, tzinfo=timezone.utc)
    updated_birth = datetime(2025, 1, 2, 0, 0, 1, tzinfo=timezone.utc)
    source = encode_uas_local_set({2: original_birth, 65: 19})
    context = ST0601ValidationContext(metadata_birth_timestamp=updated_birth)

    with pytest.raises(DecodeError, match="time of birth"):
        update_uas_local_set(source, {5: 90.0}, context=context)

    updated = update_uas_local_set(source, {2: updated_birth, 5: 90.0}, context=context)
    assert decode_uas_local_set(updated, context=context).value(2) == updated_birth


@pytest.mark.parametrize(
    "value, error, message",
    [
        (True, TypeError, "integer microseconds or an aware datetime"),
        (datetime(2025, 1, 1), ValueError, "timezone-aware"),
        (-1, ValueError, "unsigned 64-bit"),
    ],
)
def test_metadata_birth_context_rejects_invalid_values(
    value: object, error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        ST0601ValidationContext(metadata_birth_timestamp=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("tag", [103, 104, 105])
def test_imap_precision_context_enforces_sub_metre_altitude_length(tag: int) -> None:
    context = ST0601ValidationContext(imap_system_precisions={tag: 0.5})
    assert context.required_imap_length(tag) == 3
    values = {
        2: datetime(2025, 1, 2, tzinfo=timezone.utc),
        65: 19,
        tag: IMAPFieldValue(1_000.0, 3),
    }
    packet = encode_uas_local_set(values, context=context)
    assert decode_uas_local_set(packet, context=context).value(tag) == pytest.approx(
        1_000.0, abs=0.1
    )

    for wrong_length in (2, 4):
        with pytest.raises(ValueError, match=r"system precision.*requires 3 bytes"):
            encode_uas_local_set(
                {**values, tag: IMAPFieldValue(1_000.0, wrong_length)},
                context=context,
            )

    shorter = encode_uas_local_set(
        {**values, tag: IMAPFieldValue(1_000.0, 2)},
    )
    with pytest.raises(DecodeError, match=r"system precision.*requires 3 bytes"):
        decode_uas_local_set(shorter, context=context)


def test_imap_precision_context_is_immutable_and_validates_entries() -> None:
    precisions = {96: 0.25}
    context = ST0601ValidationContext(imap_system_precisions=precisions)
    precisions[96] = 64.0
    assert context.required_imap_length(96) == 3
    with pytest.raises(TypeError):
        context.imap_system_precisions[96] = 1.0  # type: ignore[index]
    assert context.required_imap_length(103) is None

    with pytest.raises(TypeError, match="mapping"):
        ST0601ValidationContext(imap_system_precisions=[])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="not a variable-length IMAP"):
        ST0601ValidationContext(imap_system_precisions={22: 0.25})
    with pytest.raises(TypeError, match="tag"):
        ST0601ValidationContext(imap_system_precisions={True: 0.25})
    with pytest.raises(ValueError, match="allows at most"):
        ST0601ValidationContext(imap_system_precisions={120: 1e-20})


def test_metadata_substream_id_is_forbidden_at_root_level() -> None:
    timestamp = bytes.fromhex("020800046050584e0180")
    version = bytes.fromhex("410113")
    root_msid = bytes.fromhex("810F 02 01 23")
    with pytest.raises(DecodeError, match=r"Metadata Substream ID.*root"):
        decode_uas_local_set(_packet_with_items(timestamp + version + root_msid))
    source = _packet_with_items(timestamp + version)
    with pytest.raises(ValueError, match=r"Metadata Substream ID.*root"):
        update_uas_local_set(source, {143: RawFieldValue(b"\x01")})


@pytest.mark.parametrize("tag", [2, 5, 65])
def test_singleton_fields_cannot_be_duplicated(tag: int) -> None:
    timestamp = bytes.fromhex("020800046050584e0180")
    heading = bytes.fromhex("05020000")
    version = bytes.fromhex("410113")
    duplicate = {
        2: timestamp,
        5: heading,
        65: version,
    }[tag]
    with pytest.raises(DecodeError, match="occurs twice"):
        decode_uas_local_set(_packet_with_items(timestamp + heading + version + duplicate))


@pytest.mark.parametrize(
    "tag",
    [tag for tag, definition in FIELD_DEFINITIONS.items() if not definition.multiple],
)
def test_every_registered_singleton_is_rejected_when_duplicated(tag: int) -> None:
    """ST 0601.13-24 is registry-driven, not a hand-picked tag allowlist."""
    timestamp = bytes.fromhex("020800046050584e0180")
    version = bytes.fromhex("410113")
    if tag == 1:
        items = timestamp + version + _item(1, b"\x00\x00")
    elif tag == 2:
        items = timestamp + timestamp + version
    elif tag == 65:
        items = timestamp + version + version
    else:
        item = _item(tag, b"")
        items = timestamp + item + item + version

    with pytest.raises(DecodeError, match=rf"singleton tag {tag} occurs twice"):
        decode_uas_local_set(_packet_with_items(items))


def test_non_mandatory_items_decode_in_arbitrary_wire_order() -> None:
    """ST 0601.13-23 permits arbitrary order between timestamp and checksum."""
    timestamp = bytes.fromhex("020800046050584e0180")
    items = b"".join(
        (
            timestamp,
            _item(65, b"\x13"),
            _item(200, b"extension"),
            _item(14, b"\x00\x00\x00\x00"),
            _item(3, b"MISSION"),
            _item(5, b"\x20\x00"),
        )
    )

    decoded = decode_uas_local_set(_packet_with_items(items))

    assert tuple(item.tag for item in decoded.local_set.items) == (2, 65, 200, 14, 3, 5, 1)
    assert decoded.value(3) == "MISSION"


@pytest.mark.parametrize(
    "tag",
    [tag for tag, definition in FIELD_DEFINITIONS.items() if definition.special_raw is not None],
)
def test_every_registered_integer_special_value_round_trips(tag: int) -> None:
    """ST 0601.13-27/-28 special values retain their semantic identity."""
    definition = FIELD_DEFINITIONS[tag]
    assert definition.length is not None
    assert definition.special_raw is not None
    assert definition.special_value is not None
    raw = definition.special_raw.to_bytes(
        definition.length,
        "big",
        signed=definition.signed,
    )
    packet = _packet_with_items(
        bytes.fromhex("020800046050584e0180")
        + _item(tag, raw)
        + bytes.fromhex("410113")
    )

    decoded = decode_uas_local_set(packet)

    assert decoded.value(tag) is definition.special_value
    assert encode_field_value(tag, definition.special_value) == raw


def test_wrong_universal_key_is_rejected() -> None:
    packet = KLVPacket(b"\x06\x0e\x2b\x34" + b"\0" * 12, b"\x01\x02\x00\x00", b"\x04")
    with pytest.raises(DecodeError):
        decode_uas_local_set(packet, verify_checksum=False, require_timestamp=False)


def test_known_fields_encode_back_to_reference_packet() -> None:
    decoded = decode_uas_local_set(DYNAMIC_ONLY)
    values = {
        field.definition.tag: field.value for field in decoded.fields if field.definition.tag != 1
    }
    assert encode_uas_local_set(values) == DYNAMIC_ONLY


def test_encode_field_validation() -> None:
    assert encode_field_value(3, "MISSION01") == b"MISSION01"
    assert encode_field_value(13, SpecialValue.RESERVED) == bytes.fromhex("80000000")
    assert encode_field_value(6, SpecialValue.OUT_OF_RANGE) == bytes.fromhex("8000")
    assert encode_field_value(23, SpecialValue.OFF_EARTH) == bytes.fromhex("80000000")
    assert encode_field_value(5, SpecialValue.UNKNOWN) == b""
    with pytest.raises(ValueError, match="does not define"):
        encode_field_value(5, SpecialValue.RESERVED)
    with pytest.raises(ValueError, match="outside"):
        encode_field_value(13, 91.0)
    with pytest.raises(ValueError, match="not supported"):
        encode_field_value(999, 1)
    with pytest.raises(ValueError, match="checksum"):
        encode_field_value(1, 0)
    with pytest.raises(TypeError):
        encode_field_value(8, 1.5)
    with pytest.raises(ValueError):
        encode_field_value(8, 256)


@pytest.mark.parametrize("tag", [6, 7, 50, 51, 52, 79, 80, 90, 91, 92, 93])
@pytest.mark.parametrize("boundary", ["below", "above"])
def test_encoder_uses_defined_out_of_range_value_for_unrepresentable_measurement(
    tag: int,
    boundary: str,
) -> None:
    definition = FIELD_DEFINITIONS[tag]
    assert definition.physical_min is not None
    assert definition.physical_max is not None
    assert definition.special_raw is not None
    assert definition.length is not None
    value = (
        definition.physical_min - 1
        if boundary == "below"
        else definition.physical_max + 1
    )

    encoded = encode_field_value(tag, value)

    assert encoded == definition.special_raw.to_bytes(
        definition.length,
        "big",
        signed=definition.signed,
    )
    decoded = decode_uas_local_set(
        _packet_with_items(
            bytes.fromhex("020800046050584e0180")
            + bytes((tag, len(encoded)))
            + encoded
            + bytes.fromhex("410113")
        )
    )
    assert decoded.value(tag) is SpecialValue.OUT_OF_RANGE


def test_zero_length_and_empty_text_have_distinct_values() -> None:
    timestamp = bytes.fromhex("020800046050584e0180")
    version = bytes.fromhex("410113")
    decoded = decode_uas_local_set(
        _packet_with_items(timestamp + bytes.fromhex("0300 040100 0D00") + version)
    )
    assert decoded.value(3) is SpecialValue.UNKNOWN
    assert decoded.value(4) == ""
    assert decoded.value(13) is SpecialValue.UNKNOWN
    assert encode_field_value(3, SpecialValue.UNKNOWN) == b""
    assert encode_field_value(3, "") == b"\x00"


@pytest.mark.parametrize(
    "tag",
    [
        tag
        for tag, definition in FIELD_DEFINITIONS.items()
        if tag not in {1, 2, 65} and not definition.multiple
    ],
)
def test_every_optional_singleton_supports_zero_length_unknown(tag: int) -> None:
    """ST 0107.4-17 ZLI semantics apply registry-wide, not by field kind."""

    timestamp = bytes.fromhex("020800046050584e0180")
    version = bytes.fromhex("410113")
    decoded = decode_uas_local_set(
        _packet_with_items(timestamp + _item(tag, b"") + version)
    )
    assert decoded.value(tag) is SpecialValue.UNKNOWN
    assert encode_field_value(tag, SpecialValue.UNKNOWN) == b""


def test_numeric_special_values_decode_without_collapsing_meaning() -> None:
    timestamp = bytes.fromhex("020800046050584e0180")
    specials = bytes.fromhex("06028000 0D0480000000 170480000000")
    version = bytes.fromhex("410113")
    decoded = decode_uas_local_set(_packet_with_items(timestamp + specials + version))
    assert decoded.value(6) is SpecialValue.OUT_OF_RANGE
    assert decoded.value(13) is SpecialValue.RESERVED
    assert decoded.value(23) is SpecialValue.OFF_EARTH


@pytest.mark.parametrize(
    "tag, raw, expected",
    [
        (26, "1750", 0.0136602540),
        (27, "063F", 0.0036602540),
        (28, "F9C1", -0.0036602540),
        (29, "1750", 0.0136602540),
        (30, "ED1F", -0.0110621778),
        (31, "F732", -0.0051602540),
        (32, "01D0", 0.0010621778),
        (33, "EB3F", -0.0121602540),
        (35, "A7C4", 235.924010),
        (36, "B2", 69.8039216),
        (37, "BEBA", 3725.18502),
        (38, "CA35", 14818.6770),
        (40, "8F695262", -79.1638500519),
        (41, "765457F2", 166.4008129604),
        (42, "F823", 18389.0471),
        (43, "03", 6.0),
        (44, "0F", 30.0),
        (45, "1A95", 425.215152),
        (46, "2611", 608.9231),
    ],
)
def test_st0601_items_26_through_46_official_examples(tag: int, raw: str, expected: float) -> None:
    timestamp = bytes.fromhex("020800046050584e0180")
    value = bytes.fromhex(raw)
    item = bytes((tag, len(value))) + value
    version = bytes.fromhex("410113")
    decoded = decode_uas_local_set(_packet_with_items(timestamp + item + version))
    # Published software values are rounded decimal presentations. The largest
    # quantization step in this table is about 2.3 microdegrees (Items 26-33).
    assert decoded.value(tag) == pytest.approx(expected, rel=2e-7, abs=1.2e-6)
    assert encode_field_value(tag, expected) == value
    assert encode_field_value(tag, decoded.value(tag)) == value


def test_st0601_corner_offsets_preserve_off_earth_identity() -> None:
    timestamp = bytes.fromhex("020800046050584e0180")
    items = b"".join(bytes((tag, 2, 0x80, 0x00)) for tag in range(26, 34))
    version = bytes.fromhex("410113")
    decoded = decode_uas_local_set(_packet_with_items(timestamp + items + version))
    for tag in range(26, 34):
        assert decoded.value(tag) is SpecialValue.OFF_EARTH
        assert encode_field_value(tag, SpecialValue.OFF_EARTH) == b"\x80\x00"


def test_st0601_target_location_preserves_off_earth_identity() -> None:
    timestamp = bytes.fromhex("020800046050584e0180")
    items = b"".join(bytes((tag, 4)) + bytes.fromhex("80000000") for tag in (40, 41))
    version = bytes.fromhex("410113")
    decoded = decode_uas_local_set(_packet_with_items(timestamp + items + version))
    for tag in (40, 41):
        assert decoded.value(tag) is SpecialValue.OFF_EARTH
        assert encode_field_value(tag, SpecialValue.OFF_EARTH) == bytes.fromhex("80000000")


def test_st0601_items_34_39_and_47_integer_domains() -> None:
    timestamp = bytes.fromhex("020800046050584e0180")
    items = bytes.fromhex("220102 270154 2F0131")
    version = bytes.fromhex("410113")
    decoded = decode_uas_local_set(_packet_with_items(timestamp + items + version))
    assert decoded.value(34) == 2
    assert decoded.value(39) == 84
    assert decoded.value(47) == 49
    assert encode_field_value(34, 2) == b"\x02"
    assert encode_field_value(39, -128) == b"\x80"
    assert encode_field_value(47, 63) == b"\x3f"
    with pytest.raises(ValueError, match="range"):
        encode_field_value(34, 3)
    with pytest.raises(ValueError, match="range"):
        encode_field_value(47, 64)


def test_invalid_coded_integer_is_strict_or_diagnostic_by_policy() -> None:
    timestamp = bytes.fromhex("020800046050584e0180")
    version = bytes.fromhex("410113")
    packet = _packet_with_items(timestamp + bytes.fromhex("220103") + version)
    with pytest.raises(DecodeError, match=r"tag 34.*range"):
        decode_uas_local_set(packet)
    decoded = decode_uas_local_set(packet, field_decoding=FieldDecodingMode.PRESERVE)
    assert decoded.issues[0].tag == 34


@pytest.mark.parametrize(
    "tag, raw, expected",
    [
        (49, "3D07", 1191.95850),
        (50, "C883", -8.67030854),
        (51, "D3FE", -61.8878750),
        (52, "DF79", -5.08255257),
        (53, "6AF4", 2088.96010),
        (54, "7670", 8306.80552),
        (55, "81", 50.5882353),
        (57, "B38EACF1", 3506979.03160634),
        (58, "A45D", 6420.53864),
        (64, "DDC5", 311.868162),
    ],
)
def test_st0601_items_49_through_64_mapped_official_examples(
    tag: int, raw: str, expected: float
) -> None:
    timestamp = bytes.fromhex("020800046050584e0180")
    value = bytes.fromhex(raw)
    version = bytes.fromhex("410113")
    decoded = decode_uas_local_set(
        _packet_with_items(timestamp + bytes((tag, len(value))) + value + version)
    )
    assert decoded.value(tag) == pytest.approx(expected, rel=2e-7, abs=2e-7)
    assert encode_field_value(tag, expected) == value
    assert encode_field_value(tag, decoded.value(tag)) == value


def test_st0601_items_49_through_64_direct_official_examples() -> None:
    timestamp = bytes.fromhex("020800046050584e0180")
    items = bytes.fromhex("38 01 8C 3B 07 544F502047554E 3C 02 AFD8 3D 01 BA 3E 02 06CF 3F 01 02")
    version = bytes.fromhex("410113")
    decoded = decode_uas_local_set(_packet_with_items(timestamp + items + version))
    assert decoded.value(56) == 140
    assert decoded.value(59) == "TOP GUN"
    weapon_load = decoded.value(60)
    assert isinstance(weapon_load, WeaponLoad)
    assert weapon_load == 45016
    assert (
        weapon_load.station_number,
        weapon_load.substation_number,
        weapon_load.weapon_type,
        weapon_load.weapon_variant,
    ) == (10, 15, 13, 8)
    weapon_fired = decoded.value(61)
    assert isinstance(weapon_fired, WeaponFired)
    assert weapon_fired == 186
    assert (weapon_fired.station_number, weapon_fired.substation_number) == (11, 10)
    assert decoded.value(62) == LaserPRFCode(1743)
    assert decoded.value(63) is SensorFieldOfViewName.MEDIUM


def test_st0601_coded_fields_decode_to_integer_compatible_semantic_types() -> None:
    timestamp = bytes.fromhex("020800046050584e0180")
    items = bytes.fromhex("22 01 02 2F 01 31 4D 01 01 7C 01 03 7D 01 09 7E 01 05")
    version = bytes.fromhex("410113")
    decoded = decode_uas_local_set(_packet_with_items(timestamp + items + version))

    assert decoded.value(34) is IcingDetected.ICING_DETECTED
    flags = decoded.value(47)
    assert flags == 49
    assert flags & GenericFlagData.LASER_RANGE
    assert flags & GenericFlagData.SLANT_RANGE_MEASURED
    assert flags & GenericFlagData.IMAGE_INVALID
    assert decoded.value(77) is OperationalMode.OPERATIONAL
    positioning = decoded.value(124)
    assert positioning == PositioningMethodSource.INS | PositioningMethodSource.GPS
    assert decoded.value(125) is PlatformStatus.EGRESS
    assert decoded.value(126) is SensorControlMode.AUTO_HOLDING_POSITION


def test_legacy_weapon_component_constructors_preserve_wire_integer_compatibility() -> None:
    load = WeaponLoad.from_components(10, 15, 13, 8)
    fired = WeaponFired.from_components(11, 10)
    assert load == 45016
    assert fired == 186
    assert encode_field_value(60, load) == bytes.fromhex("AFD8")
    assert encode_field_value(61, fired) == bytes.fromhex("BA")
    with pytest.raises(ValueError, match="four-bit"):
        WeaponLoad.from_components(16, 0, 0, 0)
    with pytest.raises(ValueError, match="four-bit"):
        WeaponFired.from_components(0, -1)


@pytest.mark.parametrize("value", [111, 888, 1111, 1743, 8888])
def test_laser_prf_code_accepts_only_st0601_digit_profile(value: int) -> None:
    assert int(LaserPRFCode(value)) == value
    assert int.from_bytes(encode_field_value(62, value), "big") == value


@pytest.mark.parametrize("value", [0, 11, 110, 999, 1000, 1809, 8889, 11111])
def test_laser_prf_code_rejects_nonconforming_values_on_encode_and_decode(
    value: int,
) -> None:
    with pytest.raises(ValueError, match="three or four digits"):
        encode_field_value(62, value)
    if value > 0xFFFF:
        return
    timestamp = bytes.fromhex("020800046050584e0180")
    bad_prf = _item(62, value.to_bytes(2, "big"))
    version = bytes.fromhex("410113")
    packet = _packet_with_items(timestamp + bad_prf + version)
    with pytest.raises(DecodeError, match="three or four digits"):
        decode_uas_local_set(packet)
    preserved = decode_uas_local_set(packet, field_decoding=FieldDecodingMode.PRESERVE)
    assert preserved.get(62) is None
    assert preserved.issues[0].tag == 62


def test_st0601_new_coded_and_text_domains_are_validated() -> None:
    assert encode_field_value(63, 8) == b"\x08"
    with pytest.raises(ValueError, match="range"):
        encode_field_value(63, 9)
    assert encode_field_value(59, "X" * 127) == b"X" * 127
    with pytest.raises(ValueError, match="127"):
        encode_field_value(59, "X" * 128)

    timestamp = bytes.fromhex("020800046050584e0180")
    version = bytes.fromhex("410113")
    oversized = bytes((59, 0x81, 128)) + b"X" * 128
    with pytest.raises(DecodeError, match=r"tag 59.*127"):
        decode_uas_local_set(_packet_with_items(timestamp + oversized + version))


@pytest.mark.parametrize(
    ("tag", "reserved"),
    [(34, 3), (47, 64), (63, 9), (77, 6), (124, 0), (125, 13), (126, 7)],
)
def test_every_bounded_coded_item_rejects_reserved_values_on_encode_and_decode(
    tag: int,
    reserved: int,
) -> None:
    with pytest.raises(ValueError, match="permitted range"):
        encode_field_value(tag, reserved)
    timestamp = bytes.fromhex("020800046050584e0180")
    version = bytes.fromhex("410113")
    with pytest.raises(DecodeError, match="permitted range"):
        decode_uas_local_set(
            _packet_with_items(timestamp + _item(tag, bytes((reserved,))) + version)
        )


@pytest.mark.parametrize("tag", [50, 51, 52])
def test_st0601_platform_attitude_out_of_range_identity(tag: int) -> None:
    timestamp = bytes.fromhex("020800046050584e0180")
    version = bytes.fromhex("410113")
    item = bytes((tag, 2)) + bytes.fromhex("8000")
    decoded = decode_uas_local_set(_packet_with_items(timestamp + item + version))
    assert decoded.value(tag) is SpecialValue.OUT_OF_RANGE
    assert encode_field_value(tag, SpecialValue.OUT_OF_RANGE) == bytes.fromhex("8000")


@pytest.mark.parametrize(
    "tag, raw, expected",
    [
        (67, "85A15A39", -86.04120734894704),
        (68, "001C501C", 0.15552755452484243),
        (69, "0BB3", 9.44533455),
        (71, "172F", 32.6024262),
        (75, "C221", 14190.7195),
        (76, "0BB3", 9.44533455),
        (78, "0BB3", 9.44533455),
        (79, "09FB", 25.4977569),
        (80, "04BC", 12.1),
    ],
)
def test_st0601_items_67_through_80_mapped_official_examples(
    tag: int, raw: str, expected: float
) -> None:
    timestamp = bytes.fromhex("020800046050584e0180")
    value = bytes.fromhex(raw)
    version = bytes.fromhex("410113")
    decoded = decode_uas_local_set(
        _packet_with_items(timestamp + bytes((tag, len(value))) + value + version)
    )
    # Item 80's published 12.1 m/s example is rounded to one decimal place;
    # half of its wire quantization step is about 0.005 m/s.
    assert decoded.value(tag) == pytest.approx(expected, rel=2e-7, abs=0.0051)
    assert encode_field_value(tag, expected) == value
    assert encode_field_value(tag, decoded.value(tag)) == value


def test_st0601_items_70_72_and_77_official_examples() -> None:
    timestamp = bytes.fromhex("020800046050584e0180")
    items = bytes.fromhex("46 06 415041434845 48 08 0002D5D024660180 4D 01 01")
    version = bytes.fromhex("410113")
    decoded = decode_uas_local_set(_packet_with_items(timestamp + items + version))
    assert decoded.value(70) == "APACHE"
    assert decoded.value(72) == datetime(1995, 4, 16, 13, 44, 54, tzinfo=timezone.utc)
    assert decoded.value(77) == 1
    assert encode_field_value(70, "APACHE") == b"APACHE"
    assert encode_field_value(
        72, datetime(1995, 4, 16, 13, 44, 54, tzinfo=timezone.utc)
    ) == bytes.fromhex("0002D5D024660180")


@pytest.mark.parametrize(
    "tag, special, length",
    [
        (67, SpecialValue.RESERVED, 4),
        (68, SpecialValue.RESERVED, 4),
        (79, SpecialValue.OUT_OF_RANGE, 2),
        (80, SpecialValue.OUT_OF_RANGE, 2),
    ],
)
def test_st0601_items_67_through_80_special_value_identity(
    tag: int, special: SpecialValue, length: int
) -> None:
    timestamp = bytes.fromhex("020800046050584e0180")
    raw = (-(2 ** (length * 8 - 1))).to_bytes(length, "big", signed=True)
    version = bytes.fromhex("410113")
    decoded = decode_uas_local_set(
        _packet_with_items(timestamp + bytes((tag, length)) + raw + version)
    )
    assert decoded.value(tag) is special
    assert encode_field_value(tag, special) == raw


def test_st0601_operational_mode_and_alternate_name_domains() -> None:
    assert encode_field_value(77, 5) == b"\x05"
    with pytest.raises(ValueError, match="range"):
        encode_field_value(77, 6)
    assert encode_field_value(70, "X" * 127) == b"X" * 127
    with pytest.raises(ValueError, match="127"):
        encode_field_value(70, "X" * 128)


@pytest.mark.parametrize(
    "tag, raw, expected",
    [
        (82, "F1069B63", -10.528728379108287),
        (83, "14BCB2C0", 29.161550376960857),
        (84, "F1004D00", -10.546048887183977),
        (85, "14BE84C8", 29.171550376960860),
        (86, "F0FD9B17", -10.553450810972622),
        (87, "14BB17AF", 29.152729868885170),
        (88, "F102052A", -10.541326455319641),
        (89, "14B9D176", 29.145729868885170),
        (90, "FF62E2F2", -0.43152510208614414),
        (91, "04D804DF", 3.4058139815022304),
        (92, "F3AB48EF", -8.6701769841230370),
        (93, "DE179323", -47.683),
    ],
)
def test_st0601_items_82_through_93_official_examples(tag: int, raw: str, expected: float) -> None:
    timestamp = bytes.fromhex("020800046050584e0180")
    value = bytes.fromhex(raw)
    version = bytes.fromhex("410113")
    decoded = decode_uas_local_set(
        _packet_with_items(timestamp + bytes((tag, len(value))) + value + version)
    )
    assert decoded.value(tag) == pytest.approx(expected, rel=2e-7, abs=2e-7)
    assert encode_field_value(tag, expected) == value
    assert encode_field_value(tag, decoded.value(tag)) == value


@pytest.mark.parametrize(
    "tag, special",
    [
        *[(tag, SpecialValue.OFF_EARTH) for tag in range(82, 90)],
        *[(tag, SpecialValue.OUT_OF_RANGE) for tag in range(90, 94)],
    ],
)
def test_st0601_items_82_through_93_special_value_identity(tag: int, special: SpecialValue) -> None:
    timestamp = bytes.fromhex("020800046050584e0180")
    raw = bytes.fromhex("80000000")
    version = bytes.fromhex("410113")
    decoded = decode_uas_local_set(
        _packet_with_items(timestamp + bytes((tag, len(raw))) + raw + version)
    )
    assert decoded.value(tag) is special
    assert encode_field_value(tag, special) == raw


def test_st0601_item_94_embeds_typed_miis_core_identifier() -> None:
    timestamp = bytes.fromhex("020800046050584e0180")
    value = bytes.fromhex("0170 F592F02373364AF8AA9162C00F2EB2DA 16B74341000841A0BE365B5AB96A3645")
    version = bytes.fromhex("410113")
    decoded = decode_uas_local_set(
        _packet_with_items(timestamp + bytes((94, len(value))) + value + version)
    )
    core = decoded.value(94)
    assert isinstance(core, MIISCoreIdentifier)
    assert core.sensor_quality is IdentifierQuality.PHYSICAL
    assert core.platform_quality is IdentifierQuality.VIRTUAL
    assert encode_field_value(94, core) == value


def test_st0601_item_48_embeds_typed_security_local_set() -> None:
    timestamp = bytes.fromhex("020800046050584e0180")
    value = encode_security_local_set(
        {1: 1, 2: 14, 3: "//USA", 12: 14, 13: "USA", 22: 12}, standalone=False
    )
    version = bytes.fromhex("410113")
    decoded = decode_uas_local_set(
        _packet_with_items(timestamp + bytes((48, len(value))) + value + version)
    )
    security = decoded.value(48)
    assert isinstance(security, SecurityLocalSet)
    assert security.value(3) == "//USA"
    assert encode_field_value(48, security) == value


@pytest.mark.parametrize(
    "tag, raw, expected, encoded_value",
    [
        (105, "2F921E", 23456.24, IMAPFieldValue(23456.24, 3)),
        (109, "0001A0", 1.625, IMAPFieldValue(1.625, 3)),
        (112, "1F40", 125.0, IMAPFieldValue(125.0, 2)),
        (113, "05F500", 2150.0, IMAPFieldValue(2150.0, 3)),
        (114, "05F740", 2154.5, IMAPFieldValue(2154.5, 3)),
    ],
)
def test_st0601_items_105_through_114_imap_official_examples(
    tag: int,
    raw: str,
    expected: float,
    encoded_value: IMAPFieldValue,
) -> None:
    timestamp = bytes.fromhex("020800046050584e0180")
    value = bytes.fromhex(raw)
    version = bytes.fromhex("410113")
    decoded = decode_uas_local_set(
        _packet_with_items(timestamp + bytes((tag, len(value))) + value + version)
    )
    assert decoded.value(tag) == pytest.approx(expected)
    assert encode_field_value(tag, encoded_value) == value


def test_st0601_items_106_through_111_direct_official_examples() -> None:
    timestamp = bytes.fromhex("020800046050584e0180")
    items = bytes.fromhex("6A 04 424C5545 6B 06 424153453031 6C 04 484F4D45 6E 02 4DAF 6F 02 0BB8")
    version = bytes.fromhex("410113")
    decoded = decode_uas_local_set(_packet_with_items(timestamp + items + version))
    assert decoded.value(106) == "BLUE"
    assert decoded.value(107) == "BASE01"
    assert decoded.value(108) == "HOME"
    assert decoded.value(110) == 19887
    assert decoded.value(111) == 3000
    assert encode_field_value(110, 19887) == bytes.fromhex("4DAF")
    assert encode_field_value(111, 3000) == bytes.fromhex("0BB8")


def test_st0601_items_105_through_114_length_domains() -> None:
    for tag in (106, 107, 108):
        assert encode_field_value(tag, "X" * 127) == b"X" * 127
        with pytest.raises(ValueError, match="127"):
            encode_field_value(tag, "X" * 128)
    for tag in (109, 113, 114):
        with pytest.raises(ValueError, match="4 byte"):
            encode_field_value(tag, IMAPFieldValue(1, 5))
    with pytest.raises(ValueError, match="range"):
        encode_field_value(110, 2**32)
    assert encode_field_value(110, 0) == b"\x00"

    timestamp = bytes.fromhex("020800046050584e0180")
    version = bytes.fromhex("410113")
    for tag in (109, 110, 113, 114):
        with pytest.raises(DecodeError, match=r"maximum|between 1 and 4"):
            decode_uas_local_set(
                _packet_with_items(timestamp + bytes((tag, 5)) + b"\x00" * 5 + version)
            )


@pytest.mark.parametrize(
    "tag, raw, expected, encoded_value",
    [
        (117, "3E90", 1.0, IMAPFieldValue(1.0, 2)),
        (118, "3E8011", 0.004176, IMAPFieldValue(0.004176, 3)),
        (119, "3B60", -50.0, IMAPFieldValue(-50.0, 2)),
        (120, "4800", 72.0, IMAPFieldValue(72.0, 2)),
    ],
)
def test_st0601_items_117_through_120_imap_official_examples(
    tag: int,
    raw: str,
    expected: float,
    encoded_value: IMAPFieldValue,
) -> None:
    timestamp = bytes.fromhex("020800046050584e0180")
    value = bytes.fromhex(raw)
    version = bytes.fromhex("410113")
    decoded = decode_uas_local_set(
        _packet_with_items(timestamp + bytes((tag, len(value))) + value + version)
    )
    # Published values are decimal presentations; the three-byte rate mapping
    # has an approximately 0.000244 degree/second quantization step.
    assert decoded.value(tag) == pytest.approx(expected, abs=0.00013)
    assert encode_field_value(tag, encoded_value) == value


def test_st0601_items_123_through_126_official_examples_and_domains() -> None:
    timestamp = bytes.fromhex("020800046050584e0180")
    items = bytes.fromhex("7B 01 07 7C 01 03 7D 01 09 7E 01 05")
    version = bytes.fromhex("410113")
    decoded = decode_uas_local_set(_packet_with_items(timestamp + items + version))
    assert decoded.value(123) == 7
    assert decoded.value(124) == 3
    assert decoded.value(125) == 9
    assert decoded.value(126) == 5
    assert encode_field_value(124, 255) == b"\xff"
    with pytest.raises(ValueError, match="range"):
        encode_field_value(124, 0)
    with pytest.raises(ValueError, match="range"):
        encode_field_value(125, 13)
    with pytest.raises(ValueError, match="range"):
        encode_field_value(126, 7)


def _item(tag: int, value: bytes) -> bytes:
    return encode_ber_oid(tag) + encode_ber_length(len(value)) + value


def test_st0601_items_129_and_131_through_137_official_examples() -> None:
    timestamp = bytes.fromhex("020800046050584e0180")
    items = b"".join(
        (
            _item(129, b"A123"),
            _item(131, bytes.fromhex("056F271B5E41B7")),
            _item(132, bytes.fromhex("0257C0")),
            _item(133, bytes.fromhex("2710")),
            _item(134, bytes.fromhex("3700")),
            _item(135, b"Frequency Modulation"),
            _item(136, bytes.fromhex("1E")),
            _item(137, bytes.fromhex("012B8DC635")),
        )
    )
    version = bytes.fromhex("410113")
    decoded = decode_uas_local_set(_packet_with_items(timestamp + items + version))
    assert decoded.value(129) == "A123"
    assert decoded.value(131) == datetime(2018, 6, 21, 13, 43, 57, 122999, tzinfo=timezone.utc)
    assert decoded.value(132) == pytest.approx(2400.0)
    assert decoded.value(133) == 10_000
    assert decoded.value(134) == pytest.approx(55.0)
    assert decoded.value(135) == "Frequency Modulation"
    assert decoded.value(136) == 30
    assert decoded.value(137) == 5_025_678_901

    assert encode_field_value(129, "A123") == b"A123"
    assert encode_field_value(131, decoded.value(131)) == bytes.fromhex("056F271B5E41B7")
    assert encode_field_value(132, IMAPFieldValue(2400, 3)) == bytes.fromhex("0257C0")
    assert encode_field_value(133, 10_000) == bytes.fromhex("2710")
    assert encode_field_value(134, IMAPFieldValue(55, 2)) == bytes.fromhex("3700")
    assert encode_field_value(136, 30) == b"\x1e"
    assert encode_field_value(137, 5_025_678_901) == bytes.fromhex("012B8DC635")


def test_st0601_variable_signed_integer_boundaries() -> None:
    assert encode_field_value(136, -1) == b"\xff"
    assert encode_field_value(136, -129) == bytes.fromhex("ff7f")
    assert encode_field_value(136, 128) == bytes.fromhex("0080")
    assert encode_field_value(137, -(2**63)) == bytes.fromhex("8000000000000000")
    with pytest.raises(ValueError, match="range"):
        encode_field_value(136, 2**31)
    with pytest.raises(ValueError, match="range"):
        encode_field_value(137, -(2**63) - 1)


@pytest.mark.parametrize(
    ("tag", "value", "encoded"),
    [
        (110, 0, b"\x00"),
        (110, 128, b"\x80"),
        (111, 65_535, b"\xff\xff"),
        (131, 1, b"\x01"),
        (133, 65_536, b"\x01\x00\x00"),
        (136, -1, b"\xff"),
        (136, 128, b"\x00\x80"),
        (137, -129, b"\xff\x7f"),
    ],
)
def test_variable_integer_encoders_use_minimal_wire_lengths(
    tag: int,
    value: int,
    encoded: bytes,
) -> None:
    assert encode_field_value(tag, value) == encoded


@pytest.mark.parametrize(
    ("tag", "raw"),
    [
        (110, b"\x00\x01"),
        (111, b"\x00\x80"),
        (131, b"\x00\x01"),
        (133, b"\x00\x7f"),
        (136, b"\x00\x7f"),
        (136, b"\xff\xff"),
        (137, b"\xff\x80"),
    ],
)
def test_variable_integer_decoders_reject_leading_extensions(
    tag: int,
    raw: bytes,
) -> None:
    timestamp = bytes.fromhex("020800046050584e0180")
    version = bytes.fromhex("410113")

    with pytest.raises(DecodeError, match="minimal"):
        decode_uas_local_set(
            _packet_with_items(timestamp + _item(tag, raw) + version)
        )


def test_variable_imap_length_can_be_selected_from_system_precision() -> None:
    selected = IMAPFieldValue.for_precision(96, 250.0, 0.25)
    assert selected == IMAPFieldValue(250.0, 3)
    assert len(encode_field_value(96, selected)) == 3

    with pytest.raises(TypeError, match="tag"):
        IMAPFieldValue.for_precision(True, 1.0, 0.25)
    with pytest.raises(ValueError, match="not a variable-length IMAP"):
        IMAPFieldValue.for_precision(22, 1.0, 0.25)
    with pytest.raises(ValueError, match="allows at most"):
        IMAPFieldValue.for_precision(120, 1.0, 1e-20)
    with pytest.raises(ValueError, match="outside"):
        IMAPFieldValue.for_precision(96, 1_500_001.0, 0.25)


def test_st0601_late_text_and_imap_length_domains() -> None:
    assert encode_field_value(129, "X" * 32) == b"X" * 32
    with pytest.raises(ValueError, match="32"):
        encode_field_value(129, "X" * 33)
    assert encode_field_value(135, "X" * 127) == b"X" * 127
    with pytest.raises(ValueError, match="127"):
        encode_field_value(135, "X" * 128)
    with pytest.raises(ValueError, match="4 byte"):
        encode_field_value(132, IMAPFieldValue(2400, 5))


@pytest.mark.parametrize("value", [" padded", "padded ", "bad\x00value", "bad\x7fvalue"])
def test_noncanonical_utf8_is_rejected_on_encode(value: str) -> None:
    with pytest.raises(ValueError, match="ST 0107"):
        encode_field_value(3, value)


def test_noncanonical_utf8_is_rejected_on_decode() -> None:
    timestamp = bytes.fromhex("020800046050584e0180")
    version = bytes.fromhex("410113")
    with pytest.raises(DecodeError, match="ST 0107"):
        decode_uas_local_set(
            _packet_with_items(timestamp + bytes.fromhex("030720626164696420") + version)
        )


@pytest.mark.parametrize(
    "tag",
    [tag for tag, definition in FIELD_DEFINITIONS.items() if definition.kind == "text"],
)
def test_every_st0601_utf8_item_enforces_common_text_rules(tag: int) -> None:
    with pytest.raises(ValueError, match="ST 0107"):
        encode_field_value(tag, " padded")

    timestamp = bytes.fromhex("020800046050584e0180")
    version = bytes.fromhex("410113")
    with pytest.raises(DecodeError, match="ST 0107"):
        decode_uas_local_set(
            _packet_with_items(timestamp + _item(tag, b"padded ") + version)
        )


def test_preserve_mode_reports_nonconforming_known_field_without_losing_wire_data() -> None:
    timestamp = bytes.fromhex("020800046050584e0180")
    # ST 0601 Target Width (tag 22) is a two-byte mapped value. Some deployed
    # FMV producers emit four bytes while still producing a valid checksum.
    malformed_target_width = bytes.fromhex("1604000001c9")
    version = bytes.fromhex("410101")
    packet = _packet_with_items(timestamp + malformed_target_width + version)

    with pytest.raises(DecodeError, match=r"tag 22.*requires 2 byte"):
        decode_uas_local_set(packet)

    decoded = decode_uas_local_set(packet, field_decoding=FieldDecodingMode.PRESERVE)
    assert decoded.value(22) is None
    assert len(decoded.issues) == 1
    issue = decoded.issues[0]
    assert issue.tag == 22
    assert issue.name == "Target Width"
    assert issue.raw == bytes.fromhex("000001c9")
    assert "requires 2 byte" in issue.message
    assert bytes(decoded.packet) == packet


def test_preserve_mode_can_losslessly_update_other_fields() -> None:
    timestamp = bytes.fromhex("020800046050584e0180")
    malformed_target_width = bytes.fromhex("1604000001c9")
    version = bytes.fromhex("410101")
    packet = _packet_with_items(timestamp + malformed_target_width + version)
    decoded = decode_uas_local_set(packet, field_decoding=FieldDecodingMode.PRESERVE)

    updated = update_uas_local_set(
        decoded,
        {3: "AI-DEMO"},
        field_decoding=FieldDecodingMode.PRESERVE,
    )
    result = decode_uas_local_set(updated, field_decoding=FieldDecodingMode.PRESERVE)
    assert result.value(3) == "AI-DEMO"
    assert result.local_set.getall(22)[0].value == bytes.fromhex("000001c9")
    assert result.issues[0].tag == 22


def test_encode_timestamp_is_exact_and_requires_aware_datetime() -> None:
    instant = datetime(2008, 10, 24, 0, 13, 29, 913000, tzinfo=timezone.utc)
    assert encode_field_value(2, instant) == bytes.fromhex("000459F4A6AA4AA8")
    with pytest.raises(ValueError, match="timezone-aware"):
        encode_field_value(2, instant.replace(tzinfo=None))
    with pytest.raises(TypeError):
        encode_field_value(2, 1.5)


def test_encode_packet_requires_timestamp_and_owns_checksum() -> None:
    with pytest.raises(ValueError, match="tag 2"):
        encode_uas_local_set({5: 180.0})
    with pytest.raises(ValueError, match="tag 1"):
        encode_uas_local_set({1: 0, 2: 0})
    with pytest.raises(ValueError, match="tag 65"):
        encode_uas_local_set({2: 0})


@pytest.mark.parametrize(
    "tag, value, encoded, decoded",
    [
        (96, 13_898.5463, "00D92A", 13_898.5),
        (103, 23_456.24, "2F921E", 23_456.234375),
        (104, 23_456.24, "2F921E", 23_456.234375),
    ],
)
def test_st0601_imap_extended_field_official_examples(
    tag: int, value: float, encoded: str, decoded: float
) -> None:
    raw = bytes.fromhex(encoded)
    assert encode_field_value(tag, IMAPFieldValue(value, length=3)) == raw
    timestamp = bytes.fromhex("020800046050584e0180")
    version = bytes.fromhex("410113")
    item = bytes((tag, len(raw))) + raw
    uas = decode_uas_local_set(_packet_with_items(timestamp + item + version))
    assert uas.value(tag) == decoded


def test_st0601_imap_field_requires_explicit_valid_length_and_range() -> None:
    with pytest.raises(TypeError, match="IMAPFieldValue"):
        encode_field_value(96, 1.0)
    with pytest.raises(ValueError, match="between 1 and 8"):
        encode_field_value(96, IMAPFieldValue(1.0, length=9))
    with pytest.raises(ValueError, match="outside"):
        encode_field_value(96, IMAPFieldValue(1_500_001.0, length=3))
    timestamp = bytes.fromhex("020800046050584e0180")
    version = bytes.fromhex("410113")
    with pytest.raises(DecodeError, match="between 1 and 8"):
        decode_uas_local_set(
            _packet_with_items(timestamp + bytes.fromhex("6009") + bytes(9) + version)
        )


def test_st0601_imap_field_round_trips_through_packet_encoder() -> None:
    packet = encode_uas_local_set(
        {
            2: 1_700_000_000_000_000,
            65: 19,
            96: IMAPFieldValue(13_898.5463, length=3),
            104: IMAPFieldValue(23_456.24, length=3),
        }
    )
    decoded = decode_uas_local_set(packet)
    assert decoded.value(96) == 13_898.5
    assert decoded.value(104) == 23_456.234375


def test_st0601_imap_field_rejects_disallowed_special_pattern() -> None:
    timestamp = bytes.fromhex("020800046050584e0180")
    version = bytes.fromhex("410113")
    with pytest.raises(DecodeError, match="does not permit IMAP special"):
        decode_uas_local_set(_packet_with_items(timestamp + bytes.fromhex("6003E00000") + version))


def test_st0601_item_74_decodes_and_reencodes_embedded_vmti() -> None:
    vmti_value = bytes.fromhex("040106060100")
    packet = encode_uas_local_set(
        {
            2: 1_700_000_000_000_000,
            65: 19,
            74: vmti_value,
        }
    )
    vmti = decode_uas_local_set(packet).value(74)
    assert isinstance(vmti, VMTILocalSet)
    assert vmti.standalone is False
    assert vmti.value(4) == 6
    assert vmti.targets == ()
    assert encode_field_value(74, vmti) == vmti_value
    assert encode_field_value(74, vmti_value) == vmti_value


def test_st0601_item_74_inherits_parent_timestamp_validation_context() -> None:
    parent_time = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    frame_time = datetime(2025, 1, 2, 3, 4, 6, tzinfo=timezone.utc)
    vmti_without_timestamp = encode_vmti_local_set({4: 6}, standalone=False)
    context = ST0601ValidationContext(
        vmti_context=VMTIValidationContext(vmti_frame_timestamp=frame_time)
    )

    with pytest.raises(ValueError, match="embedded ST 0903 requires precisionTimeStamp"):
        encode_uas_local_set(
            {2: parent_time, 65: 19, 74: vmti_without_timestamp},
            context=context,
        )

    unchecked = encode_uas_local_set(
        {2: parent_time, 65: 19, 74: vmti_without_timestamp}
    )
    with pytest.raises(DecodeError, match="embedded ST 0903 requires precisionTimeStamp"):
        decode_uas_local_set(unchecked, context=context)
    with pytest.raises(DecodeError, match="embedded ST 0903 requires precisionTimeStamp"):
        update_uas_local_set(unchecked, {}, context=context)

    preserved = decode_uas_local_set(
        unchecked,
        context=context,
        field_decoding=FieldDecodingMode.PRESERVE,
    )
    assert preserved.value(74) is None
    assert [(issue.tag, issue.name) for issue in preserved.issues] == [(74, "VMTI Local Set")]

    vmti_with_timestamp = encode_vmti_local_set(
        {2: frame_time, 4: 6},
        standalone=False,
    )
    checked = encode_uas_local_set(
        {2: parent_time, 65: 19, 74: vmti_with_timestamp},
        context=context,
    )
    assert decode_uas_local_set(checked, context=context).value(74).value(2) == frame_time


def test_st0601_item_74_accepts_matching_parent_and_vmti_frame_time() -> None:
    frame_time = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    vmti_without_timestamp = encode_vmti_local_set({4: 6}, standalone=False)
    context = ST0601ValidationContext(
        vmti_context=VMTIValidationContext(vmti_frame_timestamp=frame_time)
    )

    packet = encode_uas_local_set(
        {2: frame_time, 65: 19, 74: vmti_without_timestamp},
        context=context,
    )
    assert isinstance(decode_uas_local_set(packet, context=context).value(74), VMTILocalSet)


def test_st0601_item_74_context_rejects_conflicting_explicit_parent_time() -> None:
    parent_time = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    vmti_without_timestamp = encode_vmti_local_set({4: 6}, standalone=False)
    context = ST0601ValidationContext(
        vmti_context=VMTIValidationContext(
            parent_timestamp=parent_time.replace(second=4),
            vmti_frame_timestamp=parent_time,
        )
    )

    with pytest.raises(ValueError, match=r"parent_timestamp.*ST 0601"):
        encode_uas_local_set(
            {2: parent_time, 65: 19, 74: vmti_without_timestamp},
            context=context,
        )

    packet_without_vmti = encode_uas_local_set(
        {2: parent_time, 65: 19},
        context=context,
    )
    assert decode_uas_local_set(packet_without_vmti, context=context).value(74) is None


def test_st0601_item_74_context_enforces_two_frame_offset_age() -> None:
    parent_time = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    frame_time = parent_time.replace(microsecond=100_001)
    target = VTargetData(1, {10: 0.0, 11: 0.0})
    vmti = encode_vmti_local_set(
        {2: frame_time, 4: 6},
        targets=(target,),
        standalone=False,
    )
    context = ST0601ValidationContext(
        vmti_context=VMTIValidationContext(
            vmti_frame_timestamp=frame_time,
            frame_period_microseconds=50_000,
        )
    )

    with pytest.raises(ValueError, match="more than two frames"):
        encode_uas_local_set({2: parent_time, 65: 19, 74: vmti}, context=context)
    unchecked = encode_uas_local_set({2: parent_time, 65: 19, 74: vmti})
    with pytest.raises(DecodeError, match="more than two frames"):
        decode_uas_local_set(unchecked, context=context)


def test_st0601_validation_context_rejects_invalid_vmti_context() -> None:
    with pytest.raises(TypeError, match="vmti_context"):
        ST0601ValidationContext(vmti_context=object())  # type: ignore[arg-type]


def test_st0601_item_74_context_preserves_malformed_parent_timestamp() -> None:
    frame_time = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    context = ST0601ValidationContext(
        vmti_context=VMTIValidationContext(vmti_frame_timestamp=frame_time)
    )
    packet = _packet_with_items(
        _item(2, b"\x00")
        + _item(65, b"\x13")
        + _item(74, encode_vmti_local_set({4: 6}, standalone=False))
    )

    preserved = decode_uas_local_set(
        packet,
        context=context,
        field_decoding=FieldDecodingMode.PRESERVE,
    )

    assert [issue.tag for issue in preserved.issues] == [2, 74]
    assert preserved.value(2) is None
    assert preserved.value(74) is None


def test_st0601_item_73_decodes_and_reencodes_embedded_rvt() -> None:
    rvt_value = encode_rvt_local_set({3: 120, 8: 4, 10: "H.264"}, standalone=False)
    packet = encode_uas_local_set(
        {
            2: 1_700_000_000_000_000,
            65: 19,
            73: decode_rvt_local_set(rvt_value, standalone=False),
        }
    )
    rvt = decode_uas_local_set(packet).value(73)
    assert isinstance(rvt, RVTLocalSet)
    assert rvt.standalone is False
    assert rvt.value(3) == 120
    assert rvt.value(10) == "H.264"
    assert encode_field_value(73, rvt) == rvt_value


def test_st0601_item_73_rejects_standalone_or_wrong_typed_values() -> None:
    standalone = decode_rvt_local_set(encode_rvt_local_set({2: 0, 8: 4}))
    with pytest.raises(ValueError, match="embedded RVT"):
        encode_field_value(73, standalone)
    with pytest.raises(TypeError, match="RVTLocalSet"):
        encode_field_value(73, b"not typed")


def test_st0601_item_73_rejects_invalid_embedded_rvt_mgrs_grid() -> None:
    packet = _packet_with_items(
        _item(2, (1_700_000_000_000_000).to_bytes(8, "big"))
        + _item(65, b"\x13")
        + _item(73, _item(15, b"S1U"))
    )

    with pytest.raises(DecodeError, match=r"MGRS.*latitude band.*grid square"):
        decode_uas_local_set(packet)


def test_st0601_item_74_rejects_invalid_embedded_vmti() -> None:
    with pytest.raises(TypeError, match="VMTI"):
        encode_field_value(74, "not VMTI")
    with pytest.raises(DecodeError, match="numTargetsReported"):
        encode_field_value(74, bytes.fromhex("040106"))


def _uas_sdcc(*, source_tags: tuple[int, ...] = (13, 14)) -> SDCCFLP:
    return SDCCFLP(
        matrix_size=2,
        parse_control=SDCCParseControl(
            mode=2,
            sparse=False,
            standard_deviation_length=4,
            correlation_coefficient_length=2,
            standard_deviation_format=SDCCValueFormat.IEEE,
            correlation_coefficient_format=SDCCValueFormat.IMAP,
        ),
        standard_deviations=(1.0, 2.0),
        correlation_coefficients=(0.0,),
        source_tags=source_tags,
    )


def test_st0601_item_102_decodes_mode_2_and_binds_refined_source_order() -> None:
    timestamp = bytes.fromhex("020800046050584e0180")
    sensor_latitude = bytes.fromhex("0D0400000000")
    sensor_longitude = bytes.fromhex("0E0400000000")
    sdcc = _item(102, bytes.fromhex("02 92 04 3F800000 40000000 4000"))
    version = bytes.fromhex("410113")
    decoded = decode_uas_local_set(
        _packet_with_items(timestamp + sensor_latitude + sensor_longitude + sdcc + version)
    )
    value = decoded.value(102)
    assert isinstance(value, SDCCFLP)
    assert value.source_tags == (13, 14)
    assert value.standard_deviations == (1.0, 2.0)
    assert value.correlation_coefficients == pytest.approx((0.0,))
    assert encode_field_value(102, value) == bytes.fromhex("02 92 04 3F800000 40000000 4000")


def test_st0601_item_102_requires_mode_2_and_eligible_immediate_sources() -> None:
    timestamp = bytes.fromhex("020800046050584e0180")
    version = bytes.fromhex("410113")
    mode_1 = _item(102, bytes.fromhex("01 40 3F800000"))
    with pytest.raises(DecodeError, match="Mode 2"):
        decode_uas_local_set(
            _packet_with_items(timestamp + bytes.fromhex("0D0400000000") + mode_1 + version)
        )

    sdcc = _item(102, bytes.fromhex("02 92 04 3F800000 40000000 4000"))
    with pytest.raises(DecodeError, match="Refined Source List"):
        decode_uas_local_set(
            _packet_with_items(timestamp + bytes.fromhex("030141 0D0400000000") + sdcc + version)
        )


def test_st0601_encoder_orders_item_102_after_its_declared_sources() -> None:
    packet = encode_uas_local_set(
        {
            2: 1_700_000_000_000_000,
            13: 10.0,
            14: 20.0,
            65: 19,
            102: _uas_sdcc(source_tags=(14, 13)),
        }
    )
    decoded = decode_uas_local_set(packet)
    assert tuple(item.tag for item in decoded.local_set.items) == (2, 65, 14, 13, 102, 1)
    assert decoded.value(102).source_tags == (14, 13)


def test_st0601_encoder_supports_multiple_disjoint_sdcc_groups() -> None:
    packet = encode_uas_local_set(
        {
            2: 1_700_000_000_000_000,
            13: 10.0,
            14: 20.0,
            65: 19,
            79: 3.0,
            80: 4.0,
            102: (
                _uas_sdcc(source_tags=(13, 14)),
                _uas_sdcc(source_tags=(80, 79)),
            ),
        }
    )
    decoded = decode_uas_local_set(packet)
    assert tuple(item.tag for item in decoded.local_set.items) == (
        2,
        65,
        13,
        14,
        102,
        80,
        79,
        102,
        1,
    )
    assert tuple(value.value.source_tags for value in decoded.getall(102)) == (
        (13, 14),
        (80, 79),
    )


def test_st0601_encoder_rejects_invalid_sdcc_source_declarations() -> None:
    base = {
        2: 1_700_000_000_000_000,
        13: 10.0,
        14: 20.0,
        65: 19,
    }
    with pytest.raises(ValueError, match="two source tags"):
        encode_uas_local_set({**base, 102: _uas_sdcc(source_tags=(13,))})
    with pytest.raises(ValueError, match="not present"):
        encode_uas_local_set({**base, 102: _uas_sdcc(source_tags=(13, 79))})
    with pytest.raises(ValueError, match="eligible"):
        encode_uas_local_set({**base, 3: "MISSION", 102: _uas_sdcc(source_tags=(3, 13))})
    with pytest.raises(ValueError, match="more than one"):
        encode_uas_local_set(
            {
                **base,
                79: 3.0,
                102: (
                    _uas_sdcc(source_tags=(13, 14)),
                    _uas_sdcc(source_tags=(13, 79)),
                ),
            }
        )


def test_lossless_uas_update_noop_is_byte_exact() -> None:
    assert update_uas_local_set(DYNAMIC_ONLY, {}) == DYNAMIC_ONLY
    decoded = decode_uas_local_set(DYNAMIC_ONLY)
    assert update_uas_local_set(decoded, {}) == DYNAMIC_ONLY


def test_uas_update_replaces_adds_and_deletes_fields_with_new_checksum() -> None:
    updated = update_uas_local_set(
        DYNAMIC_ONLY,
        {
            3: "TRUCK-DETECTOR",
            5: 180.0,
            6: DELETE,
            200: RawFieldValue(b"extension"),
        },
    )
    decoded = decode_uas_local_set(updated)
    assert decoded.value(3) == "TRUCK-DETECTOR"
    assert decoded.value(5) == pytest.approx(180.0, abs=0.01)
    assert decoded.value(6) is None
    assert decoded.local_set.getone(200).value == b"extension"
    assert running_sum_16(updated[:-2]) == int.from_bytes(updated[-2:], "big")


def test_uas_update_protects_checksum_and_mandatory_structure() -> None:
    with pytest.raises(ValueError, match="checksum"):
        update_uas_local_set(DYNAMIC_ONLY, {1: RawFieldValue(b"bad")})
    with pytest.raises(DecodeError, match="Time Stamp"):
        update_uas_local_set(DYNAMIC_ONLY, {2: DELETE})
    with pytest.raises(TypeError, match="RawFieldValue"):
        update_uas_local_set(DYNAMIC_ONLY, {200: b"extension"})
