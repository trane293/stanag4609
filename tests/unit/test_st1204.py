from __future__ import annotations

from uuid import NAMESPACE_DNS, RFC_4122, UUID, uuid5

import pytest

from stanag4609.errors import DecodeError
from stanag4609.st1204 import (
    IdentifierQuality,
    MIISCoreIdentifier,
    combine_miis_uuids,
    decode_miis_core_identifier,
    encode_miis_core_identifier,
    generate_miis_uuid1,
    generate_miis_uuid4,
    generate_miis_uuid5,
    validate_miis_uuid,
)

FOUNDATIONAL = bytes.fromhex(
    "0170 F592F02373364AF8AA9162C00F2EB2DA "
    "16B74341000841A0BE365B5AB96A3645"
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


def test_decode_and_encode_st1204_official_minor_example() -> None:
    raw = bytes.fromhex("0102 03DD9DEEFB48477B8204B0506F6B2A33")
    core = decode_miis_core_identifier(raw)
    assert core.sensor_quality is IdentifierQuality.NONE
    assert core.platform_quality is IdentifierQuality.NONE
    assert core.minor_id == UUID("03dd9dee-fb48-477b-8204-b0506f6b2a33")
    assert encode_miis_core_identifier(core) == raw


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
            bytes.fromhex("0142")
            + UUID("1ea5de30-e1d3-40fa-b501-2ca5cb58ca25").bytes * 2,
            "cannot combine",
        ),
        (
            bytes.fromhex("0102")
            + UUID("6fa459ea-ee8a-3ca4-894e-db77e160355e").bytes,
            "versions 1, 4, and 5",
        ),
        (
            bytes.fromhex("0102")
            + UUID("f81d4fae-7dec-11d0-a765-000000000000").bytes,
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
