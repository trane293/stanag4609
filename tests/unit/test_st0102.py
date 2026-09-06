from __future__ import annotations

from datetime import date, datetime

import pytest

from stanag4609.errors import DecodeError
from stanag4609.klv.ber import encode_ber_length
from stanag4609.klv.model import KLVPacket
from stanag4609.st0102 import (
    SECURITY_LOCAL_SET_KEY,
    SECURITY_UNIVERSAL_SET_KEY,
    CountryCodingMethod,
    ObjectCountryCodingMethod,
    SecurityClassification,
    SecurityMarkingContext,
    SecuritySpecialValue,
    decode_security_local_set,
    decode_security_universal_set,
    encode_security_local_set,
    encode_security_universal_set,
)

MINIMUM = {
    1: 1,
    2: CountryCodingMethod.GENC_THREE_LETTER,
    3: "//USA",
    12: ObjectCountryCodingMethod.GENC_THREE_LETTER,
    13: "USA;CAN",
    22: 12,
}


def test_security_local_set_round_trip_with_required_fields() -> None:
    raw = encode_security_local_set(MINIMUM, standalone=False)
    security = decode_security_local_set(raw, standalone=False)
    assert security.packet is None
    assert security.value(1) == 1
    assert security.value(2) == 14
    assert security.value(3) == "//USA"
    assert security.value(12) == 14
    assert security.value(13) == "USA;CAN"
    assert security.value(22) == 12
    assert bytes(security.local_set) == raw
    assert encode_security_local_set(
        {field.tag: field.value for field in security.fields}, standalone=False
    ) == raw


def test_security_local_set_standalone_key_and_lossless_unknown_item() -> None:
    embedded = encode_security_local_set(MINIMUM, standalone=False) + bytes.fromhex("6301AA")
    standalone = SECURITY_LOCAL_SET_KEY + bytes((len(embedded),)) + embedded
    security = decode_security_local_set(standalone, standalone=True)
    assert security.packet is not None
    assert bytes(security.packet) == standalone
    assert security.local_set.getall(99)[0].value == b"\xaa"


def test_security_local_set_encoder_can_emit_standalone_klv() -> None:
    raw = encode_security_local_set(MINIMUM, standalone=True)
    security = decode_security_local_set(raw)
    assert raw.startswith(SECURITY_LOCAL_SET_KEY)
    assert security.packet is not None
    assert security.value(999, "missing") == "missing"


def test_security_universal_set_round_trip_and_string_conversions() -> None:
    raw = encode_security_universal_set(MINIMUM)
    security = decode_security_universal_set(raw)

    assert raw.startswith(SECURITY_UNIVERSAL_SET_KEY)
    assert security.packet.key == SECURITY_UNIVERSAL_SET_KEY
    assert security.value(1) is SecurityClassification.UNCLASSIFIED
    assert security.value(2) is CountryCodingMethod.GENC_THREE_LETTER
    assert security.value(3) == "//USA"
    assert security.value(12) is ObjectCountryCodingMethod.GENC_THREE_LETTER
    assert security.value(13) == "USA;CAN"
    assert security.value(22) == 12
    assert security.version == 12
    assert encode_security_universal_set(
        {field.tag: field.value for field in security.fields}
    ) == raw

    classification = security.get(1)
    country_method = security.get(2)
    object_method = security.get(12)
    assert classification is not None and classification.raw == b"UNCLASSIFIED//"
    assert country_method is not None and country_method.raw == b"GENC Three Letter"
    assert object_method is not None and object_method.raw == b"GENC Three Letter"


@pytest.mark.parametrize(
    ("classification", "symbol"),
    [
        (SecurityClassification.UNCLASSIFIED, "UNCLASSIFIED//"),
        (SecurityClassification.RESTRICTED, "RESTRICTED//"),
        (SecurityClassification.CONFIDENTIAL, "CONFIDENTIAL//"),
        (SecurityClassification.SECRET, "SECRET//"),
        (SecurityClassification.TOP_SECRET, "TOP SECRET//"),
    ],
)
def test_security_universal_classification_symbols(
    classification: SecurityClassification, symbol: str
) -> None:
    security = decode_security_universal_set(
        encode_security_universal_set({**MINIMUM, 1: classification})
    )

    field = security.get(1)
    assert field is not None
    assert field.value is classification
    assert field.raw.decode("ascii") == symbol


def test_security_universal_set_covers_every_table_one_element() -> None:
    values = {
        **MINIMUM,
        4: "SI/TK//",
        5: "FOUO",
        6: "USA CAN",
        7: "Original Classification Authority",
        8: "Source Guidance",
        9: "Mission reason",
        10: date(2030, 12, 31),
        11: "CAPCO",
        14: "Non-essential security note",
        23: date(2024, 1, 2),
        24: date(2025, 3, 4),
    }
    security = decode_security_universal_set(encode_security_universal_set(values))

    assert {field.tag for field in security.fields} == {
        *range(1, 15),
        22,
        23,
        24,
    }
    assert len(security.items) == 17
    assert len({item.key for item in security.items}) == 17


def test_security_local_and_universal_sets_have_equivalent_typed_values() -> None:
    values = {
        **MINIMUM,
        4: "SI/TK//",
        5: "FOUO",
        6: "USA CAN",
        10: date(2030, 12, 31),
        23: date(2024, 1, 2),
        24: date(2025, 3, 4),
    }
    local = decode_security_local_set(
        encode_security_local_set(values, standalone=False), standalone=False
    )
    universal = decode_security_universal_set(encode_security_universal_set(values))

    assert {field.tag: field.value for field in universal.fields} == {
        field.tag: field.value for field in local.fields
    }


@pytest.mark.parametrize(
    ("method", "code", "symbol"),
    [
        (CountryCodingMethod.ISO_3166_TWO_LETTER, "US", "ISO-3166 Two Letter"),
        (CountryCodingMethod.ISO_3166_THREE_LETTER, "USA", "ISO-3166 Three Letter"),
        (CountryCodingMethod.FIPS_10_4_TWO_LETTER, "US", "FIPS 10-4 Two Letter"),
        (CountryCodingMethod.FIPS_10_4_FOUR_LETTER, "USAA", "FIPS 10-4 Four Letter"),
        (CountryCodingMethod.ISO_3166_NUMERIC, "840", "ISO-3166 Numeric"),
        (CountryCodingMethod.STANAG_1059_TWO_LETTER, "US", "1059 Two Letter"),
        (CountryCodingMethod.STANAG_1059_THREE_LETTER, "USA", "1059 Three Letter"),
        (CountryCodingMethod.FIPS_10_4_MIXED, "NATO", "FIPS 10-4 Mixed"),
        (CountryCodingMethod.ISO_3166_MIXED, "NATO", "ISO 3166 Mixed"),
        (CountryCodingMethod.STANAG_1059_MIXED, "NATO", "STANAG 1059 Mixed"),
        (CountryCodingMethod.GENC_TWO_LETTER, "US", "GENC Two Letter"),
        (CountryCodingMethod.GENC_THREE_LETTER, "USA", "GENC Three Letter"),
        (CountryCodingMethod.GENC_NUMERIC, "840", "GENC Numeric"),
        (CountryCodingMethod.GENC_MIXED, "NATO", "GENC Mixed"),
    ],
)
def test_security_universal_country_method_symbols(
    method: CountryCodingMethod, code: str, symbol: str
) -> None:
    security = decode_security_universal_set(
        encode_security_universal_set({**MINIMUM, 2: method, 3: f"//{code}"})
    )

    field = security.get(2)
    assert field is not None
    assert field.value is method
    assert field.raw.decode("ascii") == symbol


@pytest.mark.parametrize(
    ("method", "code", "symbol"),
    [
        (ObjectCountryCodingMethod.ISO_3166_TWO_LETTER, "US", "ISO-3166 Two Letter"),
        (ObjectCountryCodingMethod.ISO_3166_THREE_LETTER, "USA", "ISO-3166 Three Letter"),
        (ObjectCountryCodingMethod.ISO_3166_NUMERIC, "840", "ISO-3166 Numeric"),
        (ObjectCountryCodingMethod.FIPS_10_4_TWO_LETTER, "US", "FIPS 10-4 Two Letter"),
        (ObjectCountryCodingMethod.FIPS_10_4_FOUR_LETTER, "USAA", "FIPS 10-4 Four Letter"),
        (ObjectCountryCodingMethod.STANAG_1059_TWO_LETTER, "US", "1059 Two Letter"),
        (ObjectCountryCodingMethod.STANAG_1059_THREE_LETTER, "USA", "1059 Three Letter"),
        (ObjectCountryCodingMethod.GENC_TWO_LETTER, "US", "GENC Two Letter"),
        (ObjectCountryCodingMethod.GENC_THREE_LETTER, "USA", "GENC Three Letter"),
        (ObjectCountryCodingMethod.GENC_NUMERIC, "840", "GENC Numeric"),
        (
            ObjectCountryCodingMethod.GENC_ADMINISTRATIVE_SUBDIVISION,
            "US-CA",
            "GENC AdminSub",
        ),
    ],
)
def test_security_universal_object_country_method_symbols(
    method: ObjectCountryCodingMethod, code: str, symbol: str
) -> None:
    security = decode_security_universal_set(
        encode_security_universal_set({**MINIMUM, 12: method, 13: code})
    )

    field = security.get(12)
    assert field is not None
    assert field.value is method
    assert field.raw.decode("ascii") == symbol


def test_security_universal_set_preserves_unknown_items() -> None:
    known = encode_security_universal_set(MINIMUM)
    decoded = decode_security_universal_set(known)
    unknown = bytes.fromhex("060E2B34010101010E0101017F00000001AA")
    value = b"".join(bytes(item) for item in decoded.items) + unknown
    raw = SECURITY_UNIVERSAL_SET_KEY + encode_ber_length(len(value)) + value

    reparsed = decode_security_universal_set(raw)

    assert len(reparsed.fields) == len(decoded.fields)
    assert reparsed.items[-1].key == unknown[:16]
    assert reparsed.items[-1].value == b"\xaa"


@pytest.mark.parametrize(
    ("raw_value", "message"),
    [
        (b"unclassified//", "Security Classification"),
        (b"Omitted Value", "Country Coding Method"),
    ],
)
def test_security_universal_set_rejects_unknown_symbolic_values(
    raw_value: bytes, message: str
) -> None:
    encoded = encode_security_universal_set(MINIMUM)
    security = decode_security_universal_set(encoded)
    item = security.get(1 if message.startswith("Security") else 2)
    assert item is not None
    assert isinstance(item.item, KLVPacket)
    replacement = item.item.key + encode_ber_length(len(raw_value)) + raw_value
    body = b"".join(
        replacement if packet is item.item else bytes(packet)
        for packet in security.items
    )
    replaced = SECURITY_UNIVERSAL_SET_KEY + encode_ber_length(len(body)) + body

    with pytest.raises(DecodeError, match=message):
        decode_security_universal_set(replaced)


def test_security_universal_set_rejects_duplicates_and_missing_context() -> None:
    security = decode_security_universal_set(encode_security_universal_set(MINIMUM))
    duplicated_body = b"".join(bytes(item) for item in security.items) + bytes(
        security.items[0]
    )
    duplicated = (
        SECURITY_UNIVERSAL_SET_KEY
        + encode_ber_length(len(duplicated_body))
        + duplicated_body
    )

    with pytest.raises(DecodeError, match="occurs twice"):
        decode_security_universal_set(duplicated)
    with pytest.raises(DecodeError, match="context-required tag 5"):
        decode_security_universal_set(
            bytes(security.packet), context=SecurityMarkingContext(caveats=True)
        )


def test_security_universal_set_partial_and_option_contracts() -> None:
    security = decode_security_universal_set(encode_security_universal_set(MINIMUM))
    classification = security.get(1)
    assert classification is not None
    assert isinstance(classification.item, KLVPacket)
    body = bytes(classification.item)
    partial = SECURITY_UNIVERSAL_SET_KEY + encode_ber_length(len(body)) + body

    decoded = decode_security_universal_set(partial, require_required=False)
    assert decoded.value(1) is SecurityClassification.UNCLASSIFIED
    with pytest.raises(DecodeError, match="required tag 2"):
        decode_security_universal_set(partial)
    with pytest.raises(TypeError, match="require_required"):
        decode_security_universal_set(partial, require_required=1)  # type: ignore[arg-type]
    with pytest.raises(DecodeError, match="unexpected Universal Key"):
        decode_security_universal_set(KLVPacket(bytes(16), b"", b"\x00"))


def test_security_universal_set_rejects_omitted_local_method_values() -> None:
    with pytest.raises(ValueError, match="no Universal Set symbolic form"):
        encode_security_universal_set(
            {
                **MINIMUM,
                2: CountryCodingMethod.OMITTED_8,
            }
        )


def test_security_classification_and_coding_domains_are_strict() -> None:
    assert tuple(SecurityClassification) == (
        SecurityClassification.UNCLASSIFIED,
        SecurityClassification.RESTRICTED,
        SecurityClassification.CONFIDENTIAL,
        SecurityClassification.SECRET,
        SecurityClassification.TOP_SECRET,
    )
    for classification in range(1, 6):
        values = {**MINIMUM, 1: classification}
        assert decode_security_local_set(
            encode_security_local_set(values, standalone=False), standalone=False
        ).value(1) is SecurityClassification(classification)
    with pytest.raises(ValueError, match=r"classification.*between 1 and 5"):
        encode_security_local_set({**MINIMUM, 1: 6}, standalone=False)
    with pytest.raises(ValueError, match="coding method"):
        encode_security_local_set({**MINIMUM, 12: 16}, standalone=False)
    assert encode_security_local_set(
        {**MINIMUM, 12: 64, 13: "US-CA"}, standalone=False
    )


def test_security_text_encodings_and_dates() -> None:
    values = {
        **MINIMUM,
        4: "SI/TK//",
        5: "FOUO",
        6: "USA CAN",
        10: date(2030, 12, 31),
        23: date(2024, 1, 2),
        24: date(2025, 3, 4),
    }
    security = decode_security_local_set(
        encode_security_local_set(values, standalone=False), standalone=False
    )
    assert security.value(4) == "SI/TK//"
    assert security.value(10) == date(2030, 12, 31)
    assert security.value(23) == date(2024, 1, 2)
    assert security.value(24) == date(2025, 3, 4)
    with pytest.raises(ValueError, match="ASCII"):
        encode_security_local_set({**MINIMUM, 4: "café//"}, standalone=False)
    with pytest.raises(ValueError, match="starts with //"):
        encode_security_local_set({**MINIMUM, 3: "USA"}, standalone=False)
    with pytest.raises(ValueError, match="ends with //"):
        encode_security_local_set({**MINIMUM, 4: "SI/TK"}, standalone=False)
    with pytest.raises(ValueError, match="printable"):
        encode_security_local_set({**MINIMUM, 5: "line\nbreak"}, standalone=False)


@pytest.mark.parametrize("value", ["SI//", "SI/TK//", "SI/TK/COMPARTMENT NAME//"])
def test_security_sci_shi_uses_slash_delimiters_and_terminal_double_slash(
    value: str,
) -> None:
    raw = encode_security_local_set({**MINIMUM, 4: value}, standalone=False)

    assert decode_security_local_set(raw, standalone=False).value(4) == value


@pytest.mark.parametrize(
    "value",
    ["//", "/SI//", "SI///", "SI//TK//", "SI/TK/", "SI//extra"],
)
def test_security_sci_shi_rejects_invalid_entry_framing(value: str) -> None:
    with pytest.raises(ValueError, match="SCI/SHI"):
        encode_security_local_set({**MINIMUM, 4: value}, standalone=False)


@pytest.mark.parametrize("value", ["USA", "USA CAN", "USA CAN GBR"])
def test_releasing_instructions_use_single_space_separators(value: str) -> None:
    raw = encode_security_local_set({**MINIMUM, 6: value}, standalone=False)

    assert decode_security_local_set(raw, standalone=False).value(6) == value


@pytest.mark.parametrize(
    "value",
    [" USA", "USA ", "USA  CAN", "USA_CAN", "USA;CAN"],
)
def test_releasing_instructions_reject_invalid_separators(value: str) -> None:
    with pytest.raises(ValueError, match="Releasing Instructions"):
        encode_security_local_set({**MINIMUM, 6: value}, standalone=False)


@pytest.mark.parametrize("value", ["USA", "USA;CAN", "USA;CAN;GBR"])
def test_object_country_codes_use_semicolon_without_spaces(value: str) -> None:
    raw = encode_security_local_set({**MINIMUM, 13: value}, standalone=False)

    assert decode_security_local_set(raw, standalone=False).value(13) == value


@pytest.mark.parametrize(
    "value",
    [" USA", "USA ", "USA CAN", "USA; CAN", "USA;;CAN", ";USA", "USA;"],
)
def test_object_country_codes_reject_invalid_separators(value: str) -> None:
    with pytest.raises(ValueError, match="Object Country Codes"):
        encode_security_local_set({**MINIMUM, 13: value}, standalone=False)


def test_country_codes_match_the_declared_coding_methods() -> None:
    with pytest.raises(ValueError, match=r"GENC two-letter.*Classifying Country"):
        encode_security_local_set(
            {
                **MINIMUM,
                2: CountryCodingMethod.GENC_TWO_LETTER,
                3: "//USA",
            },
            standalone=False,
        )
    with pytest.raises(ValueError, match=r"GENC numeric.*Object Country Codes"):
        encode_security_local_set(
            {
                **MINIMUM,
                12: ObjectCountryCodingMethod.GENC_NUMERIC,
                13: "USA",
            },
            standalone=False,
        )

    valid = encode_security_local_set(
        {
            **MINIMUM,
            2: CountryCodingMethod.GENC_NUMERIC,
            3: "//840",
            6: "840 124",
            12: ObjectCountryCodingMethod.GENC_ADMINISTRATIVE_SUBDIVISION,
            13: "US-CA;CA-BC",
        },
        standalone=False,
    )
    assert decode_security_local_set(valid, standalone=False).value(13) == "US-CA;CA-BC"


@pytest.mark.parametrize("value", ["US GB NATO", "USA CAN NATO"])
def test_mixed_country_method_accepts_consistent_graphs_and_tetragraphs(
    value: str,
) -> None:
    raw = encode_security_local_set(
        {
            **MINIMUM,
            2: CountryCodingMethod.GENC_MIXED,
            3: "//NATO",
            6: value,
        },
        standalone=False,
    )

    assert decode_security_local_set(raw, standalone=False).value(6) == value


def test_mixed_country_method_rejects_combined_digraphs_and_trigraphs() -> None:
    with pytest.raises(ValueError, match="must not mix digraphs and trigraphs"):
        encode_security_local_set(
            {
                **MINIMUM,
                2: CountryCodingMethod.GENC_MIXED,
                3: "//USA",
                6: "US CAN NATO",
            },
            standalone=False,
        )


def test_decoder_rejects_country_code_inconsistent_with_method() -> None:
    raw = bytes.fromhex("02010D03052F2F555341")

    with pytest.raises(DecodeError, match=r"GENC two-letter.*Classifying Country"):
        decode_security_local_set(raw, standalone=False, require_required=False)


def test_security_marking_context_requires_applicable_fields() -> None:
    context = SecurityMarkingContext(
        sci_shi=True,
        caveats=True,
        releasing_instructions=True,
    )
    assert context.required_tags == (4, 5, 6)

    with pytest.raises(ValueError, match="context-required tag 4"):
        encode_security_local_set(MINIMUM, standalone=False, context=context)

    complete = {
        **MINIMUM,
        4: "SI/TK//",
        5: "FOUO",
        6: "USA CAN",
    }
    raw = encode_security_local_set(complete, standalone=False, context=context)
    assert decode_security_local_set(
        raw,
        standalone=False,
        context=context,
    ).value(6) == "USA CAN"


def test_security_marking_context_rejects_unknown_or_missing_values_on_decode() -> None:
    context = SecurityMarkingContext(caveats=True)
    raw = encode_security_local_set(
        {**MINIMUM, 5: SecuritySpecialValue.UNKNOWN},
        standalone=False,
    )

    with pytest.raises(DecodeError, match="context-required tag 5"):
        decode_security_local_set(raw, standalone=False, context=context)

    with pytest.raises(TypeError, match="SecurityMarkingContext"):
        encode_security_local_set(MINIMUM, context=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="booleans"):
        SecurityMarkingContext(sci_shi=1)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("tag", "value", "message"),
    [
        (4, "SI//TK//", "SCI/SHI"),
        (6, "USA_CAN", "Releasing Instructions"),
        (13, "USA; CAN", "Object Country Codes"),
    ],
)
def test_security_decoder_rejects_malformed_delimited_text(
    tag: int,
    value: str,
    message: str,
) -> None:
    encoded = value.encode("utf-16-be") if tag == 13 else value.encode("ascii")
    raw = bytes((tag, len(encoded))) + encoded

    with pytest.raises(DecodeError, match=message):
        decode_security_local_set(raw, standalone=False, require_required=False)


def test_security_utf16_decoder_accepts_bom_and_default_byte_order() -> None:
    little_endian = decode_security_local_set(
        b"\x0d\x06\xff\xfeU\x00S\x00", standalone=False, require_required=False
    )
    big_endian = decode_security_local_set(
        b"\x0d\x06\xfe\xff\x00U\x00S", standalone=False, require_required=False
    )
    default_big_endian = decode_security_local_set(
        b"\x0d\x04\x00U\x00S", standalone=False, require_required=False
    )
    assert little_endian.value(13) == "US"
    assert big_endian.value(13) == "US"
    assert default_big_endian.value(13) == "US"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"\x01\x02\x00\x01", "requires 1 byte"),
        (b"\x02\x01\x00", "between 1 and 16"),
        (b"\x0c\x01\x10", "is not allowed"),
        (b"\x05\x01\xff", "ASCII"),
        (b"\x0d\x01A", "odd byte length"),
        (b"\x0d\x02\xd8\x00", "not valid UTF-16"),
        (b"\x0a\x08" + b"20241340", "valid YYYYMMDD"),
        (b"\x17\x0a" + b"2024-13-40", "valid YYYY-MM-DD"),
    ],
)
def test_security_malformed_wire_values_are_rejected(raw: bytes, message: str) -> None:
    with pytest.raises(DecodeError, match=message):
        decode_security_local_set(raw, standalone=False, require_required=False)


@pytest.mark.parametrize(
    ("values", "error", "message"),
    [
        ({**MINIMUM, 99: b"raw"}, ValueError, "not supported"),
        ({**MINIMUM, 22: True}, TypeError, "Version must be an integer"),
        ({**MINIMUM, 22: 65536}, ValueError, "out of range"),
        ({**MINIMUM, 5: 5}, TypeError, "requires str"),
        ({**MINIMUM, 13: 13}, TypeError, "requires str"),
        ({**MINIMUM, 10: datetime(2024, 1, 1)}, TypeError, "datetime.date"),
    ],
)
def test_security_typed_encoder_rejects_invalid_python_values(
    values: dict[int, object], error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        encode_security_local_set(values, standalone=False)


def test_security_decoder_framing_and_option_contracts() -> None:
    with pytest.raises(TypeError, match="standalone"):
        encode_security_local_set(MINIMUM, standalone=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="must be booleans"):
        decode_security_local_set(b"", standalone=False, require_required=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="raw Local Set bytes"):
        decode_security_local_set(
            KLVPacket(SECURITY_LOCAL_SET_KEY, b"", b"\x00"), standalone=False
        )
    with pytest.raises(DecodeError, match="unexpected Universal Key"):
        decode_security_local_set(KLVPacket(bytes(16), b"", b"\x00"))
    with pytest.raises(DecodeError, match="exactly one"):
        decode_security_local_set(b"")


def test_security_unknown_zli_is_distinct_and_does_not_satisfy_required_field() -> None:
    raw = encode_security_local_set(
        {**MINIMUM, 4: SecuritySpecialValue.UNKNOWN}, standalone=False
    )
    assert decode_security_local_set(raw, standalone=False).value(4) is SecuritySpecialValue.UNKNOWN
    with pytest.raises(ValueError, match="required tag 3"):
        encode_security_local_set(
            {**MINIMUM, 3: SecuritySpecialValue.UNKNOWN}, standalone=False
        )
    with pytest.raises(ValueError, match="ambiguous"):
        encode_security_local_set({**MINIMUM, 4: ""}, standalone=False)


def test_security_legacy_absent_version_defaults_to_three() -> None:
    legacy_values = {1: 1, 2: 1, 3: "//US", 13: "US"}
    raw = encode_security_local_set(legacy_values, standalone=False)
    security = decode_security_local_set(raw, standalone=False)
    assert security.version == 3
    assert security.get(12) is None
    assert security.get(22) is None


def test_security_required_fields_duplicates_and_malformed_values_are_rejected() -> None:
    with pytest.raises(ValueError, match="required tag 13"):
        encode_security_local_set(
            {tag: value for tag, value in MINIMUM.items() if tag != 13}, standalone=False
        )
    valid = encode_security_local_set(MINIMUM, standalone=False)
    with pytest.raises(DecodeError, match="occurs twice"):
        decode_security_local_set(valid + bytes.fromhex("010101"), standalone=False)
    malformed = valid.replace(bytes.fromhex("010101"), bytes.fromhex("010106"), 1)
    with pytest.raises(DecodeError, match="classification"):
        decode_security_local_set(malformed, standalone=False)
