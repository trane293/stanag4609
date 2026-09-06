from __future__ import annotations

from uuid import NAMESPACE_DNS, RFC_4122, UUID, uuid5

import pytest

from stanag4609.errors import DecodeError
from stanag4609.st1204 import (
    MIIS_CORE_IDENTIFIER_KEY,
    IdentifierQuality,
    MIISCoreIdentifier,
    combine_miis_sensor_identifiers,
    combine_miis_uuids,
    decode_miis_core_identifier,
    decode_miis_core_identifier_klv,
    decode_miis_core_identifier_xml,
    encode_miis_core_identifier,
    encode_miis_core_identifier_klv,
    encode_miis_core_identifier_xml,
    format_miis_core_identifier,
    generate_miis_uuid1,
    generate_miis_uuid4,
    generate_miis_uuid5,
    miis_text_check_value,
    parse_miis_core_identifier,
    validate_miis_uuid,
    with_miis_window_identifier,
)

FOUNDATIONAL = bytes.fromhex(
    "0170 F592F02373364AF8AA9162C00F2EB2DA "
    "16B74341000841A0BE365B5AB96A3645"
)
FOUNDATIONAL_TEXT = (
    "0170:F592-F023-7336-4AF8-AA91-62C0-0F2E-B2DA/16B7-4341-0008-41A0-BE36-5B5A-B96A-3645:D3"
)
APPENDIX_E_TEXT_VALUES = (
    "0154:C7D1-6253-98A2-41C2-BA6E-90F8-FCC7-3914/"
    "E047-AB3E-81BE-41ED-9664-09B0-2F44-5FAB/"
    "5E71-B0DC-20FE-4920-8216-26D6-4F61-D863:C8",
    "0150:08CE-252E-D0FA-49E3-B1A0-65E6-1D57-20DC/EAF1-8A27-A086-4019-A586-EAAF-9715-7BFA:DA",
    "0168:F354-666E-D552-4C0D-9168-A745-4CEB-A073/840A-4799-BBC0-4BD5-97A7-56F6-4092-AF7B:AA",
    "0178:865E-FD9C-EF8A-41C3-8244-B885-AFCC-40BF/ED8A-9AB8-72E2-4165-9979-7E5A-F54A-5B9A:25",
    "0144:340F-463B-AEC2-4F8C-BD45-92AE-DE80-E1C6/F0AF-8673-3A17-424C-B060-3EE4-A86B-38F9:D9",
    "0140:BC76-CFEF-0BEE-41A4-9618-EB4B-010D-2F08:B6",
    "0114:3D50-2DB6-4A93-44C3-B56E-94AD-7C4E-E476/45E2-8FAF-C2D3-4A6E-815B-5FE6-B0A9-6ABD:F1",
    "0110:1AB8-231E-17E8-4748-A133-CE93-89A7-A060:25",
    "0104:C2A7-D724-96F7-47DB-A23D-A297-3007-5876:55",
    "0102:03DD-9DEE-FB48-477B-8204-B050-6F6B-2A33:25",
)


def test_decode_and_encode_st1204_official_foundational_example() -> None:
    core = decode_miis_core_identifier(FOUNDATIONAL)
    assert core.version == 1
    assert core.usage_value == 0x70
    assert core.sensor_quality is IdentifierQuality.PHYSICAL
    assert core.platform_quality is IdentifierQuality.VIRTUAL
    assert core.sensor_id == UUID("f592f023-7336-4af8-aa91-62c00f2eb2da")
    assert core.platform_id == UUID("16b74341-0008-41a0-be36-5b5ab96a3645")
    assert core.window_id is None
    assert core.minor_id is None
    assert core.raw == FOUNDATIONAL
    assert encode_miis_core_identifier(core) == FOUNDATIONAL


def test_st1204_standalone_klv_matches_official_example() -> None:
    expected = MIIS_CORE_IDENTIFIER_KEY + bytes((len(FOUNDATIONAL),)) + FOUNDATIONAL
    core = decode_miis_core_identifier_klv(expected)

    assert core == decode_miis_core_identifier(FOUNDATIONAL)
    assert encode_miis_core_identifier_klv(core) == expected


def test_st1204_standalone_klv_rejects_wrong_key_and_trailing_packet() -> None:
    packet = encode_miis_core_identifier_klv(decode_miis_core_identifier(FOUNDATIONAL))
    with pytest.raises(DecodeError, match="unexpected Universal Key"):
        decode_miis_core_identifier_klv(bytes(16) + packet[16:])
    with pytest.raises(DecodeError, match="expected one"):
        decode_miis_core_identifier_klv(packet + packet)


def test_decode_and_encode_st1204_official_minor_example() -> None:
    raw = bytes.fromhex("0102 03DD9DEEFB48477B8204B0506F6B2A33")
    core = decode_miis_core_identifier(raw)
    assert core.sensor_quality is IdentifierQuality.NONE
    assert core.platform_quality is IdentifierQuality.NONE
    assert core.minor_id == UUID("03dd9dee-fb48-477b-8204-b0506f6b2a33")
    assert encode_miis_core_identifier(core) == raw


def test_st1204_text_format_matches_official_example_and_check_algorithm() -> None:
    core = decode_miis_core_identifier(FOUNDATIONAL)

    assert miis_text_check_value("031FA3") == 0x79
    assert format_miis_core_identifier(core) == FOUNDATIONAL_TEXT
    assert parse_miis_core_identifier(FOUNDATIONAL_TEXT) == core
    assert parse_miis_core_identifier(FOUNDATIONAL_TEXT.lower()) == core


def test_st1204_minor_text_round_trip() -> None:
    core = decode_miis_core_identifier(bytes.fromhex("0102 03DD9DEEFB48477B8204B0506F6B2A33"))
    rendered = format_miis_core_identifier(core)

    assert rendered.startswith("0102:03DD-9DEE-FB48-477B-8204-B050-6F6B-2A33:")
    assert parse_miis_core_identifier(rendered) == core


@pytest.mark.parametrize("text_value", APPENDIX_E_TEXT_VALUES)
def test_st1204_all_normative_appendix_e_text_vectors(text_value: str) -> None:
    core = parse_miis_core_identifier(text_value)

    assert format_miis_core_identifier(core) == text_value
    assert decode_miis_core_identifier_klv(encode_miis_core_identifier_klv(core)) == core


@pytest.mark.parametrize(
    "value, message",
    [
        (FOUNDATIONAL_TEXT[:-2] + "D2", "check value"),
        (FOUNDATIONAL_TEXT.replace("-", "", 1), "format"),
        ("0170:<bad>:00", "format"),
        ("", "format"),
    ],
)
def test_st1204_text_parser_rejects_corruption_and_noncanonical_shape(
    value: str, message: str
) -> None:
    with pytest.raises(DecodeError, match=message):
        parse_miis_core_identifier(value)


def test_st1204_xml_matches_normative_namespace_and_round_trips() -> None:
    core = decode_miis_core_identifier(FOUNDATIONAL)
    encoded = encode_miis_core_identifier_xml(core)

    assert b'<MiisCoreId xmlns="http://www.nga.gov/MiisSchema/">' in encoded
    assert FOUNDATIONAL_TEXT.encode() in encoded
    assert decode_miis_core_identifier_xml(encoded) == core


@pytest.mark.parametrize(
    "xml, message",
    [
        (b'<MiisCoreId xmlns="urn:wrong">0170:bad:00</MiisCoreId>', "root element"),
        (
            b'<MiisCoreId xmlns="http://www.nga.gov/MiisSchema/"><x/></MiisCoreId>',
            "child elements",
        ),
        (b'<!DOCTYPE x><MiisCoreId xmlns="http://www.nga.gov/MiisSchema/"/>', "DTD"),
    ],
)
def test_st1204_xml_decoder_rejects_wrong_or_unsafe_documents(xml: bytes, message: str) -> None:
    with pytest.raises(DecodeError, match=message):
        decode_miis_core_identifier_xml(xml)


def test_construct_and_validate_st1204_core_identifier() -> None:
    sensor_id = UUID("bc76cfef-0bee-41a4-9618-eb4b010d2f08")
    core = MIISCoreIdentifier(
        version=1,
        sensor_quality=IdentifierQuality.VIRTUAL,
        sensor_id=sensor_id,
    )
    assert core.usage_value == 0x40
    assert encode_miis_core_identifier(core) == bytes.fromhex(
        "0140 BC76CFEF0BEE41A49618EB4B010D2F08"
    )
    with pytest.raises(ValueError, match="requires a sensor"):
        MIISCoreIdentifier(version=1, sensor_quality=IdentifierQuality.PHYSICAL)
    with pytest.raises(ValueError, match="cannot combine"):
        MIISCoreIdentifier(
            version=1,
            sensor_quality=IdentifierQuality.PHYSICAL,
            sensor_id=sensor_id,
            minor_id=sensor_id,
        )


@pytest.mark.parametrize(
    "raw, message",
    [
        (bytes.fromhex("0171") + b"\x00" * 32, "reserved"),
        (bytes.fromhex("0100"), "at least one"),
        (bytes.fromhex("0140") + b"\x00" * 15, "length"),
        (bytes.fromhex("0202") + b"\x00" * 16, "version"),
        (
            bytes.fromhex("0142") + UUID("1ea5de30-e1d3-40fa-b501-2ca5cb58ca25").bytes * 2,
            "cannot combine",
        ),
        (
            bytes.fromhex("0102") + UUID("6fa459ea-ee8a-3ca4-894e-db77e160355e").bytes,
            "versions 1, 4, and 5",
        ),
        (
            bytes.fromhex("0102") + UUID("f81d4fae-7dec-11d0-a765-000000000000").bytes,
            "null MAC",
        ),
        (bytes.fromhex("8180"), "truncated"),
    ],
)
def test_invalid_st1204_binary_values_are_rejected(raw: bytes, message: str) -> None:
    with pytest.raises((DecodeError, ValueError), match=message):
        decode_miis_core_identifier(raw)


def test_combine_miis_uuids_matches_normative_appendix_a_table_13() -> None:
    combined = combine_miis_uuids(
        (
            UUID("f81d4fae-7dec-11d0-a765-00a0c91e6bf6"),
            UUID("1ea5de30-e1d3-40fa-b501-2ca5cb58ca25"),
        )
    )
    assert combined == UUID("cd9a0ef2-24a6-5343-aa21-3cd6be881b52")
    assert combined.version == 5
    assert combined.variant == RFC_4122
    assert combine_miis_uuids((combined, NAMESPACE_DNS)) != combine_miis_uuids(
        (NAMESPACE_DNS, combined)
    )


def test_combine_miis_sensor_identifiers_uses_lowest_component_quality() -> None:
    first = UUID("f81d4fae-7dec-11d0-a765-00a0c91e6bf6")
    second = UUID("1ea5de30-e1d3-40fa-b501-2ca5cb58ca25")

    identifier, quality = combine_miis_sensor_identifiers(
        (
            (first, IdentifierQuality.PHYSICAL),
            (second, IdentifierQuality.VIRTUAL),
        )
    )

    assert identifier == UUID("cd9a0ef2-24a6-5343-aa21-3cd6be881b52")
    assert quality is IdentifierQuality.VIRTUAL


def test_window_derivation_preserves_foundational_components() -> None:
    source = decode_miis_core_identifier(FOUNDATIONAL)
    window_id = UUID("bc76cfef-0bee-41a4-9618-eb4b010d2f08")

    derived = with_miis_window_identifier(source, window_id)

    assert derived.sensor_id == source.sensor_id
    assert derived.platform_id == source.platform_id
    assert derived.sensor_quality is source.sensor_quality
    assert derived.platform_quality is source.platform_quality
    assert derived.window_id == window_id
    assert derived.minor_id is None
    with pytest.raises(ValueError, match="Foundational"):
        with_miis_window_identifier(
            decode_miis_core_identifier(bytes.fromhex("0102 03DD9DEEFB48477B8204B0506F6B2A33")),
            window_id,
        )


def test_st1204_generators_only_create_permitted_uuid_versions() -> None:
    version_1 = generate_miis_uuid1(mac_address=0x001122334455, clock_sequence=7)
    version_4 = generate_miis_uuid4()
    version_5 = generate_miis_uuid5(NAMESPACE_DNS, "sensor:maker:model:serial")

    assert version_1.version == 1
    assert version_1.node == 0x001122334455
    assert version_4.version == 4
    assert version_5 == uuid5(NAMESPACE_DNS, "sensor:maker:model:serial")
    for identifier in (version_1, version_4, version_5):
        validate_miis_uuid(identifier)


def test_st1204_rejects_disallowed_versions_and_null_version_1_mac() -> None:
    version_3 = UUID("6fa459ea-ee8a-3ca4-894e-db77e160355e")
    null_mac_version_1 = UUID("f81d4fae-7dec-11d0-a765-000000000000")
    with pytest.raises(ValueError, match="versions 1, 4, and 5"):
        MIISCoreIdentifier(
            version=1,
            sensor_quality=IdentifierQuality.MANAGED,
            sensor_id=version_3,
        )
    with pytest.raises(ValueError, match="null MAC"):
        validate_miis_uuid(null_mac_version_1)
    with pytest.raises(ValueError, match="mac_address"):
        generate_miis_uuid1(mac_address=0)


def test_st1204_generation_inputs_are_explicit_and_bounded() -> None:
    with pytest.raises(ValueError, match="at least two"):
        combine_miis_uuids((NAMESPACE_DNS,))
    with pytest.raises(TypeError, match="UUID"):
        combine_miis_uuids((NAMESPACE_DNS, object()))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-empty"):
        generate_miis_uuid5(NAMESPACE_DNS, "")
    with pytest.raises(ValueError, match="clock_sequence"):
        generate_miis_uuid1(mac_address=1, clock_sequence=0x4000)
