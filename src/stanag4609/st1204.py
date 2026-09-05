"""MISB ST 1204.3 Motion Imagery Identification System Core Identifiers."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from enum import IntEnum
from uuid import RFC_4122, UUID, uuid1, uuid4, uuid5

from stanag4609.errors import DecodeError, NeedMoreData
from stanag4609.klv.ber import decode_ber_oid, encode_ber_oid


class IdentifierQuality(IntEnum):
    """ST 1204 Identifier Component quality encoded in the Usage Value."""

    NONE = 0
    MANAGED = 1
    VIRTUAL = 2
    PHYSICAL = 3


def validate_miis_uuid(identifier: UUID) -> None:
    """Validate one UUID against normative ST 1204.3 Appendix A rules."""

    if not isinstance(identifier, UUID):
        raise TypeError("identifier must be a UUID")
    if identifier.variant != RFC_4122 or identifier.version not in {1, 4, 5}:
        raise ValueError("ST 1204 UUIDs must use RFC 4122 versions 1, 4, and 5 only")
    if identifier.version == 1 and identifier.node == 0:
        raise ValueError("ST 1204 UUID version 1 must not use the null MAC address")


def generate_miis_uuid1(*, mac_address: int, clock_sequence: int | None = None) -> UUID:
    """Generate an allowed version-1 UUID from an explicit non-null MAC address.

    ST 1204 discourages version 1 and permits it only when the device has a MAC
    address. The caller remains responsible for supplying that device's real,
    stable address rather than an invented node value.
    """

    if (
        isinstance(mac_address, bool)
        or not isinstance(mac_address, int)
        or not 1 <= mac_address < 1 << 48
    ):
        raise ValueError("mac_address must be a nonzero 48-bit integer")
    if clock_sequence is not None and (
        isinstance(clock_sequence, bool)
        or not isinstance(clock_sequence, int)
        or not 0 <= clock_sequence < 1 << 14
    ):
        raise ValueError("clock_sequence must be an integer from 0 to 16383 or None")
    identifier = uuid1(node=mac_address, clock_seq=clock_sequence)
    validate_miis_uuid(identifier)
    return identifier


def generate_miis_uuid4() -> UUID:
    """Generate an allowed random version-4 UUID using the platform CSPRNG."""

    identifier = uuid4()
    validate_miis_uuid(identifier)
    return identifier


def generate_miis_uuid5(namespace: UUID, unique_name: str) -> UUID:
    """Generate the preferred name-based version-5 UUID for unique device text."""

    if not isinstance(namespace, UUID):
        raise TypeError("namespace must be a UUID")
    if not isinstance(unique_name, str):
        raise TypeError("unique_name must be a string")
    if not unique_name:
        raise ValueError("unique_name must be non-empty")
    identifier = uuid5(namespace, unique_name)
    validate_miis_uuid(identifier)
    return identifier


def combine_miis_uuids(identifiers: Iterable[UUID]) -> UUID:
    """Combine two or more UUIDs with ST 1204.3 Appendix A.1's exact algorithm.

    Input order is significant. The SHA-1 input is the concatenated uppercase,
    dash-free hexadecimal *text* encoded as UTF-8, not the UUID binary bytes and
    not an RFC 4122 namespace-prefixed name.
    """

    if not isinstance(identifiers, Iterable):
        raise TypeError("identifiers must be an iterable of UUID values")
    components = tuple(identifiers)
    if len(components) < 2:
        raise ValueError("at least two UUID values are required")
    for identifier in components:
        validate_miis_uuid(identifier)
    source = "".join(identifier.hex.upper() for identifier in components).encode("utf-8")
    combined = bytearray(hashlib.sha1(source).digest()[:16])
    combined[6] = (combined[6] & 0x0F) | 0x50
    combined[8] = (combined[8] & 0x3F) | 0x80
    identifier = UUID(bytes=bytes(combined))
    validate_miis_uuid(identifier)
    return identifier


@dataclass(frozen=True, slots=True)
class MIISCoreIdentifier:
    """A typed ST 1204.3 binary Core Identifier value."""

    version: int
    sensor_quality: IdentifierQuality = IdentifierQuality.NONE
    platform_quality: IdentifierQuality = IdentifierQuality.NONE
    sensor_id: UUID | None = None
    platform_id: UUID | None = None
    window_id: UUID | None = None
    minor_id: UUID | None = None
    raw: bytes = b""

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise TypeError("ST 1204 Core Identifier version must be an integer")
        if self.version != 1:
            raise ValueError(f"unsupported ST 1204 Core Identifier version {self.version}")
        if not isinstance(self.sensor_quality, IdentifierQuality):
            raise TypeError("sensor_quality must be an IdentifierQuality")
        if not isinstance(self.platform_quality, IdentifierQuality):
            raise TypeError("platform_quality must be an IdentifierQuality")
        for name, identifier in (
            ("sensor", self.sensor_id),
            ("platform", self.platform_id),
            ("window", self.window_id),
            ("minor", self.minor_id),
        ):
            if identifier is not None and not isinstance(identifier, UUID):
                raise TypeError(f"{name}_id must be a UUID or None")
            if identifier is not None:
                validate_miis_uuid(identifier)
        if not isinstance(self.raw, bytes):
            raise TypeError("raw must be bytes")

        if (self.sensor_quality is IdentifierQuality.NONE) != (self.sensor_id is None):
            raise ValueError("ST 1204 sensor quality requires a sensor identifier and vice versa")
        if (self.platform_quality is IdentifierQuality.NONE) != (self.platform_id is None):
            raise ValueError(
                "ST 1204 platform quality requires a platform identifier and vice versa"
            )
        foundational = any((self.sensor_id, self.platform_id, self.window_id))
        if self.minor_id is not None and foundational:
            raise ValueError(
                "ST 1204 Minor Identifier cannot combine with Foundational Identifiers"
            )
        if self.minor_id is None and not foundational:
            raise ValueError("ST 1204 Core Identifier requires at least one identifier component")

    @property
    def usage_value(self) -> int:
        """Return the ST 1204.3 Table 3 Usage Value."""
        return (
            int(self.sensor_quality) << 5
            | int(self.platform_quality) << 3
            | int(self.window_id is not None) << 2
            | int(self.minor_id is not None) << 1
        )

    def __bytes__(self) -> bytes:
        return encode_miis_core_identifier(self)


def encode_miis_core_identifier(core: MIISCoreIdentifier) -> bytes:
    """Encode one ST 1204.3 Core Identifier binary value."""
    if not isinstance(core, MIISCoreIdentifier):
        raise TypeError("core must be an MIISCoreIdentifier")
    identifiers = (
        core.sensor_id,
        core.platform_id,
        core.window_id,
        core.minor_id,
    )
    return (
        encode_ber_oid(core.version)
        + bytes((core.usage_value,))
        + b"".join(identifier.bytes for identifier in identifiers if identifier is not None)
    )


def decode_miis_core_identifier(data: bytes) -> MIISCoreIdentifier:
    """Decode and structurally validate one ST 1204.3 binary Core Identifier."""
    if not isinstance(data, bytes):
        raise TypeError("ST 1204 Core Identifier data must be bytes")
    if len(data) < 2:
        raise DecodeError("ST 1204 Core Identifier is truncated before its Usage Value")
    try:
        version, version_length = decode_ber_oid(data, max_octets=4)
    except NeedMoreData as error:
        raise DecodeError("ST 1204 Core Identifier has a truncated BER-OID version") from error
    if version != 1:
        raise DecodeError(f"unsupported ST 1204 Core Identifier version {version}")
    if version_length >= len(data):
        raise DecodeError("ST 1204 Core Identifier is missing its Usage Value")

    usage = data[version_length]
    if usage & 0x81:
        raise DecodeError("ST 1204 Core Identifier Usage Value has nonzero reserved bits")
    sensor_quality = IdentifierQuality((usage >> 5) & 0x03)
    platform_quality = IdentifierQuality((usage >> 3) & 0x03)
    has_window = bool(usage & 0x04)
    has_minor = bool(usage & 0x02)

    component_count = (
        int(sensor_quality is not IdentifierQuality.NONE)
        + int(platform_quality is not IdentifierQuality.NONE)
        + int(has_window)
        + int(has_minor)
    )
    expected_length = version_length + 1 + component_count * 16
    if len(data) != expected_length:
        raise DecodeError(
            f"ST 1204 Core Identifier length is {len(data)} bytes; "
            f"Usage Value requires {expected_length}"
        )

    cursor = version_length + 1

    def take_uuid(included: bool) -> UUID | None:
        nonlocal cursor
        if not included:
            return None
        identifier = UUID(bytes=data[cursor : cursor + 16])
        cursor += 16
        return identifier

    sensor_id = take_uuid(sensor_quality is not IdentifierQuality.NONE)
    platform_id = take_uuid(platform_quality is not IdentifierQuality.NONE)
    window_id = take_uuid(has_window)
    minor_id = take_uuid(has_minor)
    try:
        return MIISCoreIdentifier(
            version=version,
            sensor_quality=sensor_quality,
            platform_quality=platform_quality,
            sensor_id=sensor_id,
            platform_id=platform_id,
            window_id=window_id,
            minor_id=minor_id,
            raw=data,
        )
    except ValueError as error:
        raise DecodeError(str(error)) from error
