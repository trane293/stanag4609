"""MISB ST 0102.12 Security Metadata Local Set codec."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum, IntEnum
from typing import Any, Literal

from stanag4609.errors import DecodeError
from stanag4609.klv.ber import encode_ber_length, encode_ber_oid
from stanag4609.klv.local_set import parse_local_set
from stanag4609.klv.model import KLVPacket, LocalSet, LocalSetItem
from stanag4609.klv.stream import KLVStreamParser

SECURITY_LOCAL_SET_KEY = bytes.fromhex(
    "06 0E 2B 34 02 03 01 01 0E 01 03 03 02 00 00 00"
)


class SecurityClassification(IntEnum):
    """ST 0102 Local Set Tag 1 security-classification values."""

    UNCLASSIFIED = 0x01
    RESTRICTED = 0x02
    CONFIDENTIAL = 0x03
    SECRET = 0x04
    TOP_SECRET = 0x05


class CountryCodingMethod(IntEnum):
    """ST 0102 Tag 2 country-code vocabularies."""

    ISO_3166_TWO_LETTER = 0x01
    ISO_3166_THREE_LETTER = 0x02
    FIPS_10_4_TWO_LETTER = 0x03
    FIPS_10_4_FOUR_LETTER = 0x04
    ISO_3166_NUMERIC = 0x05
    STANAG_1059_TWO_LETTER = 0x06
    STANAG_1059_THREE_LETTER = 0x07
    OMITTED_8 = 0x08
    OMITTED_9 = 0x09
    FIPS_10_4_MIXED = 0x0A
    ISO_3166_MIXED = 0x0B
    STANAG_1059_MIXED = 0x0C
    GENC_TWO_LETTER = 0x0D
    GENC_THREE_LETTER = 0x0E
    GENC_NUMERIC = 0x0F
    GENC_MIXED = 0x10


class ObjectCountryCodingMethod(IntEnum):
    """ST 0102 Tag 12 object-country-code vocabularies."""

    ISO_3166_TWO_LETTER = 0x01
    ISO_3166_THREE_LETTER = 0x02
    ISO_3166_NUMERIC = 0x03
    FIPS_10_4_TWO_LETTER = 0x04
    FIPS_10_4_FOUR_LETTER = 0x05
    STANAG_1059_TWO_LETTER = 0x06
    STANAG_1059_THREE_LETTER = 0x07
    OMITTED_8 = 0x08
    OMITTED_9 = 0x09
    OMITTED_10 = 0x0A
    OMITTED_11 = 0x0B
    OMITTED_12 = 0x0C
    GENC_TWO_LETTER = 0x0D
    GENC_THREE_LETTER = 0x0E
    GENC_NUMERIC = 0x0F
    GENC_ADMINISTRATIVE_SUBDIVISION = 0x40


class SecuritySpecialValue(Enum):
    """ST 0107 zero-length Unknown value within a Security Local Set."""

    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SecurityMarkingContext:
    """External marking facts that make ST 0102 fields mandatory by context."""

    sci_shi: bool = False
    caveats: bool = False
    releasing_instructions: bool = False

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, bool)
            for value in (self.sci_shi, self.caveats, self.releasing_instructions)
        ):
            raise TypeError("SecurityMarkingContext fields must be booleans")

    @property
    def required_tags(self) -> tuple[int, ...]:
        """Return the Local Set tags required by the declared marking context."""
        return tuple(
            tag
            for tag, required in (
                (4, self.sci_shi),
                (5, self.caveats),
                (6, self.releasing_instructions),
            )
            if required
        )


@dataclass(frozen=True, slots=True)
class _SecurityDefinition:
    tag: int
    name: str
    kind: Literal["uint", "ascii", "utf16", "date8", "date10"]
    length: int | None = None
    minimum: int | None = None
    maximum: int | None = None
    allowed: frozenset[int] | None = None


@dataclass(frozen=True, slots=True)
class SecurityField:
    tag: int
    name: str
    value: Any
    raw: bytes
    item: LocalSetItem


@dataclass(frozen=True, slots=True)
class SecurityLocalSet:
    """Decoded standalone or ST 0601-embedded Security Metadata Local Set."""

    packet: KLVPacket | None
    local_set: LocalSet
    fields: tuple[SecurityField, ...]
    standalone: bool

    def getall(self, tag: int) -> tuple[SecurityField, ...]:
        return tuple(field for field in self.fields if field.tag == tag)

    def get(self, tag: int) -> SecurityField | None:
        matches = self.getall(tag)
        if not matches:
            return None
        if len(matches) > 1:
            raise ValueError(f"ST 0102 tag {tag} occurs {len(matches)} times")
        return matches[0]

    def value(self, tag: int, default: Any = None) -> Any:
        field = self.get(tag)
        return default if field is None else field.value

    @property
    def version(self) -> int:
        """Return the declared version, or the mandated legacy default of 3."""
        value = self.value(22, 3)
        if not isinstance(value, int):
            raise DecodeError("ST 0102 Security Metadata Version is Unknown")
        return value


_DEFINITIONS = {
    1: _SecurityDefinition(
        1, "Security Classification", "uint", 1, minimum=1, maximum=5
    ),
    2: _SecurityDefinition(
        2,
        "Classifying Country and Releasing Instructions Country Coding Method",
        "uint",
        1,
        minimum=1,
        maximum=16,
    ),
    3: _SecurityDefinition(3, "Classifying Country", "ascii"),
    4: _SecurityDefinition(4, "Security-SCI/SHI Information", "ascii"),
    5: _SecurityDefinition(5, "Caveats", "ascii"),
    6: _SecurityDefinition(6, "Releasing Instructions", "ascii"),
    7: _SecurityDefinition(7, "Classified By", "ascii"),
    8: _SecurityDefinition(8, "Derived From", "ascii"),
    9: _SecurityDefinition(9, "Classification Reason", "ascii"),
    10: _SecurityDefinition(10, "Declassification Date", "date8", 8),
    11: _SecurityDefinition(11, "Classification and Marking System", "ascii"),
    12: _SecurityDefinition(
        12,
        "Object Country Coding Method",
        "uint",
        1,
        allowed=frozenset((*range(1, 16), 64)),
    ),
    13: _SecurityDefinition(13, "Object Country Codes", "utf16"),
    14: _SecurityDefinition(14, "Classification Comments", "ascii"),
    22: _SecurityDefinition(22, "Security Metadata Version", "uint", 2),
    23: _SecurityDefinition(23, "Country Coding Method Version Date", "date10", 10),
    24: _SecurityDefinition(24, "Object Country Coding Method Version Date", "date10", 10),
}

_COUNTRY_METHODS: dict[int, tuple[str, str, int] | tuple[str, str, None]] = {
    0x01: ("ISO 3166 two-letter", "alpha", 2),
    0x02: ("ISO 3166 three-letter", "alpha", 3),
    0x03: ("FIPS 10-4 two-letter", "alpha", 2),
    0x04: ("FIPS 10-4 four-letter", "alpha", 4),
    0x05: ("ISO 3166 numeric", "numeric", 3),
    0x06: ("STANAG 1059 two-letter", "alpha", 2),
    0x07: ("STANAG 1059 three-letter", "alpha", 3),
    0x0A: ("FIPS 10-4 mixed", "mixed", None),
    0x0B: ("ISO 3166 mixed", "mixed", None),
    0x0C: ("STANAG 1059 mixed", "mixed", None),
    0x0D: ("GENC two-letter", "alpha", 2),
    0x0E: ("GENC three-letter", "alpha", 3),
    0x0F: ("GENC numeric", "numeric", 3),
    0x10: ("GENC mixed", "mixed", None),
}

_OBJECT_COUNTRY_METHODS: dict[
    int, tuple[str, str, int] | tuple[str, str, None]
] = {
    0x01: ("ISO 3166 two-letter", "alpha", 2),
    0x02: ("ISO 3166 three-letter", "alpha", 3),
    0x03: ("ISO 3166 numeric", "numeric", 3),
    0x04: ("FIPS 10-4 two-letter", "alpha", 2),
    0x05: ("FIPS 10-4 four-letter", "alpha", 4),
    0x06: ("STANAG 1059 two-letter", "alpha", 2),
    0x07: ("STANAG 1059 three-letter", "alpha", 3),
    0x0D: ("GENC two-letter", "alpha", 2),
    0x0E: ("GENC three-letter", "alpha", 3),
    0x0F: ("GENC numeric", "numeric", 3),
    0x40: ("GENC administrative-subdivision", "subdivision", None),
}


def _validate_ascii(value: str, *, tag: int) -> bytes:
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError(f"ST 0102 tag {tag} requires ISO/IEC 646 ASCII text") from error
    if any(octet < 0x20 or octet > 0x7E for octet in encoded):
        raise ValueError(f"ST 0102 tag {tag} requires printable ISO/IEC 646 ASCII text")
    return encoded


def _validate_semantics(tag: int, value: str) -> None:
    if tag == 3 and not value.startswith("//"):
        raise ValueError("ST 0102 Classifying Country starts with //")
    if tag == 4:
        if not value.endswith("//"):
            raise ValueError("ST 0102 SCI/SHI Information ends with //")
        entries = value[:-2].split("/")
        if not entries or any(not entry for entry in entries):
            raise ValueError(
                "ST 0102 SCI/SHI Information requires non-empty entries separated "
                "by one / and terminated by //"
            )
    if tag == 6:
        entries = value.split(" ")
        if any(not entry for entry in entries) or "_" in value or ";" in value:
            raise ValueError(
                "ST 0102 Releasing Instructions requires country codes separated "
                "by one blank space, not underscores or semicolons"
            )
    if tag == 13:
        entries = value.split(";")
        if any(not entry for entry in entries) or any(
            character.isspace() for character in value
        ):
            raise ValueError(
                "ST 0102 Object Country Codes requires non-empty codes separated "
                "by semicolons with no spaces"
            )


def _marking_context(value: SecurityMarkingContext | None) -> SecurityMarkingContext:
    context = SecurityMarkingContext() if value is None else value
    if not isinstance(context, SecurityMarkingContext):
        raise TypeError("context must be a SecurityMarkingContext")
    return context


def _validate_country_code(
    code: str,
    *,
    method: tuple[str, str, int] | tuple[str, str, None],
    field_name: str,
) -> None:
    method_name, kind, length = method
    ascii_upper = code.isascii() and code == code.upper()
    if kind == "alpha":
        valid = ascii_upper and code.isalpha() and len(code) == length
    elif kind == "numeric":
        valid = code.isascii() and code.isdigit() and len(code) == length
    elif kind == "mixed":
        valid = ascii_upper and code.isalpha() and len(code) in (2, 3, 4)
    else:
        country, separator, subdivision = code.partition("-")
        valid = (
            separator == "-"
            and len(country) == 2
            and country.isascii()
            and country.isalpha()
            and country == country.upper()
            and 1 <= len(subdivision) <= 3
            and subdivision.isascii()
            and subdivision.isalnum()
            and subdivision == subdivision.upper()
        )
    if not valid:
        raise ValueError(
            f"ST 0102 {method_name} coding method is inconsistent with "
            f"{field_name} code {code!r}"
        )


def _validate_mixed_code_lengths(
    codes: tuple[str, ...],
    *,
    method_name: str,
) -> None:
    graph_lengths = {len(code) for code in codes if len(code) in (2, 3)}
    if len(graph_lengths) > 1:
        raise ValueError(
            f"ST 0102 {method_name} coding method must not mix digraphs and trigraphs"
        )


def _validate_country_fields(values: Mapping[int, Any]) -> None:
    country_method = values.get(2)
    if isinstance(country_method, int) and not isinstance(country_method, bool):
        method = _COUNTRY_METHODS.get(country_method)
        if method is not None:
            field_codes: list[tuple[str, str]] = []
            classifying_country = values.get(3)
            if isinstance(classifying_country, str) and classifying_country.startswith("//"):
                field_codes.append((classifying_country[2:], "Classifying Country"))
            releasing = values.get(6)
            if isinstance(releasing, str):
                field_codes.extend(
                    (code, "Releasing Instructions") for code in releasing.split(" ")
                )
            for code, field_name in field_codes:
                _validate_country_code(code, method=method, field_name=field_name)
            if method[1] == "mixed":
                _validate_mixed_code_lengths(
                    tuple(code for code, _ in field_codes),
                    method_name=method[0],
                )

    object_method = values.get(12)
    object_countries = values.get(13)
    if (
        isinstance(object_method, int)
        and not isinstance(object_method, bool)
        and isinstance(object_countries, str)
        and (method := _OBJECT_COUNTRY_METHODS.get(object_method)) is not None
    ):
        for code in object_countries.split(";"):
            _validate_country_code(code, method=method, field_name="Object Country Codes")


def _decode_date(raw: bytes, *, separated: bool, tag: int) -> date:
    try:
        text = raw.decode("ascii")
        if separated:
            return date.fromisoformat(text)
        if len(text) != 8 or not text.isdigit():
            raise ValueError
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except (UnicodeDecodeError, ValueError) as error:
        form = "YYYY-MM-DD" if separated else "YYYYMMDD"
        raise DecodeError(f"ST 0102 tag {tag} requires a valid {form} date") from error


def _decode_value(item: LocalSetItem, definition: _SecurityDefinition) -> SecurityField:
    if not item.value:
        return SecurityField(
            item.tag, definition.name, SecuritySpecialValue.UNKNOWN, item.value, item
        )
    if definition.length is not None and len(item.value) != definition.length:
        raise DecodeError(
            f"ST 0102 tag {item.tag} ({definition.name}) requires "
            f"{definition.length} byte(s), observed {len(item.value)}"
        )
    if definition.kind == "uint":
        value: Any = int.from_bytes(item.value, "big")
        if definition.allowed is not None and value not in definition.allowed:
            raise DecodeError(f"ST 0102 {definition.name.lower()} value {value} is not allowed")
        if definition.minimum is not None and value < definition.minimum:
            raise DecodeError(
                f"ST 0102 {definition.name.lower()} must be between "
                f"{definition.minimum} and {definition.maximum}"
            )
        if definition.maximum is not None and value > definition.maximum:
            raise DecodeError(
                f"ST 0102 {definition.name.lower()} must be between "
                f"{definition.minimum} and {definition.maximum}"
            )
        if item.tag == 1:
            value = SecurityClassification(value)
    elif definition.kind == "ascii":
        try:
            value = item.value.decode("ascii")
        except UnicodeDecodeError as error:
            raise DecodeError(
                f"ST 0102 tag {item.tag} requires ISO/IEC 646 ASCII text"
            ) from error
        try:
            _validate_ascii(value, tag=item.tag)
            _validate_semantics(item.tag, value)
        except ValueError as error:
            raise DecodeError(str(error)) from error
    elif definition.kind == "utf16":
        if len(item.value) % 2:
            raise DecodeError(f"ST 0102 tag {item.tag} UTF-16 value has an odd byte length")
        try:
            if item.value.startswith(b"\xff\xfe"):
                value = item.value[2:].decode("utf-16-le")
            elif item.value.startswith(b"\xfe\xff"):
                value = item.value[2:].decode("utf-16-be")
            else:
                value = item.value.decode("utf-16-be")
        except UnicodeDecodeError as error:
            raise DecodeError(f"ST 0102 tag {item.tag} is not valid UTF-16") from error
        try:
            _validate_semantics(item.tag, value)
        except ValueError as error:
            raise DecodeError(str(error)) from error
    else:
        value = _decode_date(
            item.value,
            separated=definition.kind == "date10",
            tag=item.tag,
        )
    return SecurityField(item.tag, definition.name, value, item.value, item)


def _encode_value(tag: int, value: Any) -> bytes:
    try:
        definition = _DEFINITIONS[tag]
    except KeyError as error:
        raise ValueError(f"ST 0102 tag {tag} is not supported for typed encoding") from error
    if value is SecuritySpecialValue.UNKNOWN:
        return b""
    if definition.kind == "uint":
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"ST 0102 tag {tag} requires int")
        if definition.allowed is not None and value not in definition.allowed:
            raise ValueError(f"ST 0102 {definition.name.lower()} value {value} is not allowed")
        if definition.minimum is not None and not definition.minimum <= value <= (
            definition.maximum or definition.minimum
        ):
            raise ValueError(
                f"ST 0102 {definition.name.lower()} must be between "
                f"{definition.minimum} and {definition.maximum}"
            )
        assert definition.length is not None
        try:
            return value.to_bytes(definition.length, "big")
        except OverflowError as error:
            raise ValueError(f"ST 0102 tag {tag} integer is out of range") from error
    if definition.kind == "ascii":
        if not isinstance(value, str):
            raise TypeError(f"ST 0102 tag {tag} requires str")
        if not value:
            raise ValueError(
                f"ST 0102 tag {tag} empty text is ambiguous; use SecuritySpecialValue.UNKNOWN"
            )
        _validate_semantics(tag, value)
        return _validate_ascii(value, tag=tag)
    if definition.kind == "utf16":
        if not isinstance(value, str):
            raise TypeError(f"ST 0102 tag {tag} requires str")
        if not value:
            raise ValueError(
                f"ST 0102 tag {tag} empty text is ambiguous; use SecuritySpecialValue.UNKNOWN"
            )
        _validate_semantics(tag, value)
        return value.encode("utf-16-be")
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"ST 0102 tag {tag} requires datetime.date")
    return value.strftime("%Y-%m-%d" if definition.kind == "date10" else "%Y%m%d").encode()


def encode_security_local_set(
    values: Mapping[int, Any],
    *,
    standalone: bool = False,
    context: SecurityMarkingContext | None = None,
) -> bytes:
    """Encode an ST 0102.12 Security Metadata Local Set."""
    if not isinstance(standalone, bool):
        raise TypeError("standalone must be a boolean")
    active_context = _marking_context(context)
    declared_version = values.get(22, 3)
    if isinstance(declared_version, bool) or not isinstance(declared_version, int):
        raise TypeError("ST 0102 Security Metadata Version must be an integer")
    required_tags = {1, 2, 3, 13}
    if declared_version >= 4:
        required_tags.add(22)
    if declared_version >= 6:
        required_tags.add(12)
    for tag in required_tags:
        if tag not in values or values[tag] is SecuritySpecialValue.UNKNOWN or values[tag] == "":
            raise ValueError(f"ST 0102 required tag {tag} is missing or Unknown")
    for tag in active_context.required_tags:
        if tag not in values or values[tag] is SecuritySpecialValue.UNKNOWN or values[tag] == "":
            raise ValueError(f"ST 0102 context-required tag {tag} is missing or Unknown")
    _validate_country_fields(values)
    encoded_items = []
    for tag in sorted(values):
        raw = _encode_value(tag, values[tag])
        encoded_items.append(encode_ber_oid(tag) + encode_ber_length(len(raw)) + raw)
    local_value = b"".join(encoded_items)
    if not standalone:
        return local_value
    return SECURITY_LOCAL_SET_KEY + encode_ber_length(len(local_value)) + local_value


def _parse_single_packet(data: bytes) -> KLVPacket:
    parser = KLVStreamParser(key_prefix=SECURITY_LOCAL_SET_KEY, max_value_length=1024 * 1024)
    packets = parser.feed(data)
    packets.extend(parser.finish())
    if len(packets) != 1:
        raise DecodeError(f"expected exactly one ST 0102 packet, observed {len(packets)}")
    return packets[0]


def decode_security_local_set(
    data: bytes | KLVPacket,
    *,
    standalone: bool = True,
    require_required: bool = True,
    context: SecurityMarkingContext | None = None,
) -> SecurityLocalSet:
    """Decode one standalone or nested ST 0102.12 Security Metadata Local Set."""
    if not isinstance(standalone, bool) or not isinstance(require_required, bool):
        raise TypeError("standalone and require_required must be booleans")
    active_context = _marking_context(context)
    if standalone:
        packet = data if isinstance(data, KLVPacket) else _parse_single_packet(data)
        if packet.key != SECURITY_LOCAL_SET_KEY:
            raise DecodeError("unexpected Universal Key for ST 0102 Security Local Set")
        local_value = packet.value
    else:
        if isinstance(data, KLVPacket):
            raise TypeError("embedded ST 0102 data must be raw Local Set bytes")
        packet = None
        local_value = data
    local_set = parse_local_set(local_value)
    for tag in _DEFINITIONS:
        if len(local_set.getall(tag)) > 1:
            raise DecodeError(f"ST 0102 singleton tag {tag} occurs twice")
    version_items = local_set.getall(22)
    if version_items and not version_items[0].value:
        raise DecodeError("ST 0102 required tag 22 (Security Metadata Version) is Unknown")
    declared_version = (
        int.from_bytes(version_items[0].value, "big") if version_items else 3
    )
    if require_required:
        required_tags = {1, 2, 3, 13}
        if declared_version >= 4:
            required_tags.add(22)
        if declared_version >= 6:
            required_tags.add(12)
        for tag in required_tags:
            definition = _DEFINITIONS[tag]
            items = local_set.getall(tag)
            if not items or not items[0].value:
                raise DecodeError(
                    f"ST 0102 required tag {definition.tag} ({definition.name}) "
                    "is missing or Unknown"
                )
        for tag in active_context.required_tags:
            definition = _DEFINITIONS[tag]
            items = local_set.getall(tag)
            if not items or not items[0].value:
                raise DecodeError(
                    f"ST 0102 context-required tag {definition.tag} ({definition.name}) "
                    "is missing or Unknown"
                )
    fields = tuple(
        _decode_value(item, item_definition)
        for item in local_set.items
        if (item_definition := _DEFINITIONS.get(item.tag)) is not None
    )
    try:
        _validate_country_fields({field.tag: field.value for field in fields})
    except ValueError as error:
        raise DecodeError(str(error)) from error
    return SecurityLocalSet(packet, local_set, fields, standalone)
