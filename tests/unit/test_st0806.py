from __future__ import annotations

from datetime import datetime, timezone
from fractions import Fraction

import pytest

from stanag4609.errors import ChecksumError, DecodeError
from stanag4609.klv.ber import encode_ber_length
from stanag4609.klv.checksum import mpeg2_crc32
from stanag4609.klv.model import KLVPacket
from stanag4609.st0806 import (
    AOI_LOCAL_SET_KEY,
    POI_LOCAL_SET_KEY,
    RVT_LOCAL_SET_KEY,
    USER_DEFINED_LOCAL_SET_KEY,
    RawRVTValue,
    RVTAreaOfInterest,
    RVTErrorValue,
    RVTLocalSet,
    RVTPointOfInterest,
    RVTUserDataType,
    RVTUserDefinedData,
    RVTValidationContext,
    decode_area_of_interest,
    decode_point_of_interest,
    decode_rvt_local_set,
    decode_user_defined_data,
    encode_area_of_interest,
    encode_point_of_interest,
    encode_rvt_local_set,
    encode_user_defined_data,
)


def test_st0806_registered_keys() -> None:
    assert bytes.fromhex("060E2B34020B01010E01030102000000") == RVT_LOCAL_SET_KEY
    assert bytes.fromhex("060E2B34020B01010E0103010C000000") == POI_LOCAL_SET_KEY
    assert bytes.fromhex("060E2B34020B01010E0103010D000000") == AOI_LOCAL_SET_KEY
    assert bytes.fromhex("060E2B34020B01010E0103010F000000") == USER_DEFINED_LOCAL_SET_KEY


def test_point_of_interest_typed_round_trip_and_error_sentinel() -> None:
    encoded = encode_point_of_interest(
        {
            1: 42,
            2: 45.0,
            3: -90.0,
            4: 1000.0,
            5: 3,
            6: "truck",
            7: "SFGPUCI----K",
            8: "detector-1",
            9: "TARGET-000000001",
            10: "operation-alpha",
            200: RawRVTValue(b"extension"),
        }
    )
    poi = decode_point_of_interest(encoded)
    assert isinstance(poi, RVTPointOfInterest)
    assert poi.value(1) == 42
    assert poi.value(2) == pytest.approx(45.0)
    assert poi.value(3) == pytest.approx(-90.0)
    assert poi.value(4) == pytest.approx(1000.0, abs=0.16)
    assert poi.value(5) == 3
    assert poi.value(6) == "truck"
    assert poi.local_set.getone(200).value == b"extension"
    reencoded = {field.tag: field.value for field in poi.fields}
    reencoded[200] = RawRVTValue(b"extension")
    assert encode_point_of_interest(reencoded) == encoded

    error = decode_point_of_interest(bytes.fromhex("01020001 020480000000 030480000000"))
    assert error.value(2) is RVTErrorValue.ERROR
    assert error.value(3) is RVTErrorValue.ERROR
    assert poi.value(199, "missing") == "missing"
    assert poi.getall(6)[0].value == "truck"


def test_point_of_interest_requires_mandatory_unique_fields() -> None:
    with pytest.raises(DecodeError, match="requires tags 1, 2, and 3"):
        decode_point_of_interest(bytes.fromhex("01020001 020400000000"))
    with pytest.raises(DecodeError, match="occurs twice"):
        decode_point_of_interest(bytes.fromhex("01020001 01020002 020400000000 030400000000"))
    with pytest.raises(ValueError, match="requires tags 1, 2, and 3"):
        encode_point_of_interest({1: 1, 2: 0.0})


@pytest.mark.parametrize(
    ("values", "error", "message"),
    [
        ({1: 1, 2: object(), 3: 0.0}, TypeError, "must be numeric"),
        ({1: 1, 2: 91.0, 3: 0.0}, ValueError, "outside"),
        ({1: 1, 2: Fraction(45), 3: Fraction(-90)}, None, None),
        ({1: 1, 2: 0.0, 3: 0.0, 4: RVTErrorValue.ERROR}, ValueError, "no error"),
        ({1: 1, 2: 0.0, 3: 0.0, 5: 0}, ValueError, "permitted range"),
        ({1: 1, 2: 0.0, 3: 0.0, 5: True}, TypeError, "integer"),
        ({1: 1, 2: 0.0, 3: 0.0, 6: b"text"}, TypeError, "string"),
        ({1: 1, 2: 0.0, 3: 0.0, 6: "café"}, ValueError, "ISO-7"),
        ({1: 1, 2: 0.0, 3: 0.0, 7: "x" * 128}, ValueError, "exceeds 127"),
        ({1: 1, 2: 0.0, 3: 0.0, 9: "short"}, ValueError, "requires 16"),
        ({1: 1, 2: 0.0, 3: 0.0, 200: b"raw"}, TypeError, "RawRVTValue"),
    ],
)
def test_point_of_interest_encoder_rejects_invalid_values(
    values: dict[int, object],
    error: type[Exception] | None,
    message: str | None,
) -> None:
    if error is None:
        assert decode_point_of_interest(encode_point_of_interest(values)).value(2) == pytest.approx(
            45.0
        )
        return
    with pytest.raises(error, match=message):
        encode_point_of_interest(values)


def test_point_of_interest_decoder_rejects_bad_wire_values() -> None:
    with pytest.raises(DecodeError, match="requires 2 bytes"):
        decode_point_of_interest(bytes.fromhex("010101 020400000000 030400000000"))
    with pytest.raises(DecodeError, match="outside its range"):
        decode_point_of_interest(bytes.fromhex("01020001 020400000000 030400000000 050100"))
    with pytest.raises(DecodeError, match="not ISO-7"):
        decode_point_of_interest(bytes.fromhex("01020001 020400000000 030400000000 0601FF"))
    with pytest.raises(DecodeError, match="requires 16 bytes"):
        decode_point_of_interest(bytes.fromhex("01020001 020400000000 030400000000 090178"))
    with pytest.raises(TypeError, match="data must be bytes"):
        decode_point_of_interest(bytearray())  # type: ignore[arg-type]


def test_raw_rvt_value_requires_bytes() -> None:
    with pytest.raises(TypeError, match="must be bytes"):
        RawRVTValue(bytearray())  # type: ignore[arg-type]


def test_area_of_interest_typed_round_trip() -> None:
    encoded = encode_area_of_interest(
        {
            1: 7,
            2: 45.0,
            3: -120.0,
            4: 44.0,
            5: -119.0,
            6: 2,
            7: "search box",
            8: "operator",
            9: "AREA-00000000001",
            10: "operation-alpha",
        }
    )
    aoi = decode_area_of_interest(encoded)
    assert isinstance(aoi, RVTAreaOfInterest)
    assert aoi.value(1) == 7
    assert aoi.value(2) == pytest.approx(45.0)
    assert aoi.value(5) == pytest.approx(-119.0)
    assert aoi.value(6) == 2
    assert encode_area_of_interest({field.tag: field.value for field in aoi.fields}) == encoded


def test_area_of_interest_requires_six_mandatory_unique_fields() -> None:
    with pytest.raises(DecodeError, match="requires tags 1 through 6"):
        decode_area_of_interest(bytes.fromhex("01020001"))
    with pytest.raises(ValueError, match="requires tags 1 through 6"):
        encode_area_of_interest({1: 1})


@pytest.mark.parametrize(
    "record",
    [
        RVTUserDefinedData(3, RVTUserDataType.STRING, "hello"),
        RVTUserDefinedData(4, RVTUserDataType.SIGNED_INTEGER, -257, value_length=2),
        RVTUserDefinedData(5, RVTUserDataType.UNSIGNED_INTEGER, 65535, value_length=2),
        RVTUserDefinedData(6, RVTUserDataType.EXPERIMENTAL, bytes.fromhex("010203")),
    ],
)
def test_user_defined_data_typed_round_trip(record: RVTUserDefinedData) -> None:
    encoded = encode_user_defined_data(record)
    assert decode_user_defined_data(encoded) == record


def test_user_defined_data_enforces_exact_two_item_order() -> None:
    with pytest.raises(DecodeError, match="exactly tags 1 and 2"):
        decode_user_defined_data(bytes.fromhex("020141 010103"))
    with pytest.raises(ValueError, match="between 0 and 63"):
        encode_user_defined_data(RVTUserDefinedData(64, RVTUserDataType.STRING, "x"))
    with pytest.raises(TypeError, match="string"):
        encode_user_defined_data(
            RVTUserDefinedData(1, RVTUserDataType.STRING, b"x")  # type: ignore[arg-type]
        )


def test_user_defined_data_decoder_rejects_invalid_wire_values() -> None:
    with pytest.raises(TypeError, match="must be bytes"):
        decode_user_defined_data(bytearray())  # type: ignore[arg-type]
    with pytest.raises(DecodeError, match="requires one byte"):
        decode_user_defined_data(bytes.fromhex("01020001 020178"))
    with pytest.raises(DecodeError, match="not ISO-7"):
        decode_user_defined_data(bytes.fromhex("010100 0201FF"))
    with pytest.raises(DecodeError, match="cannot be empty"):
        decode_user_defined_data(bytes.fromhex("010140 0200"))
    with pytest.raises(DecodeError, match="cannot be empty"):
        decode_user_defined_data(bytes.fromhex("010180 0200"))


@pytest.mark.parametrize(
    ("record", "error", "message"),
    [
        (object(), TypeError, "record"),
        (RVTUserDefinedData(True, RVTUserDataType.STRING, "x"), TypeError, "identifier"),
        (RVTUserDefinedData(64, RVTUserDataType.STRING, "x"), ValueError, "between 0 and 63"),
        (RVTUserDefinedData(1, "string", "x"), TypeError, "RVTUserDataType"),
        (
            RVTUserDefinedData(1, RVTUserDataType.STRING, "x", value_length=-1),
            ValueError,
            "non-negative",
        ),
        (RVTUserDefinedData(1, RVTUserDataType.STRING, "café"), ValueError, "ISO-7"),
        (RVTUserDefinedData(1, RVTUserDataType.SIGNED_INTEGER, True), TypeError, "integer"),
        (
            RVTUserDefinedData(1, RVTUserDataType.SIGNED_INTEGER, 128, value_length=1),
            ValueError,
            "does not fit",
        ),
        (RVTUserDefinedData(1, RVTUserDataType.UNSIGNED_INTEGER, -1), ValueError, "does not fit"),
        (RVTUserDefinedData(1, RVTUserDataType.EXPERIMENTAL, "x"), TypeError, "must be bytes"),
        (
            RVTUserDefinedData(1, RVTUserDataType.EXPERIMENTAL, b"x", value_length=2),
            ValueError,
            "does not match",
        ),
    ],
)
def test_user_defined_data_encoder_rejects_invalid_records(
    record: object, error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        encode_user_defined_data(record)  # type: ignore[arg-type]


def test_user_defined_integer_width_is_minimal_when_unspecified() -> None:
    assert decode_user_defined_data(
        encode_user_defined_data(RVTUserDefinedData(1, RVTUserDataType.SIGNED_INTEGER, 127))
    ).value_length == 1
    assert decode_user_defined_data(
        encode_user_defined_data(RVTUserDefinedData(1, RVTUserDataType.SIGNED_INTEGER, -129))
    ).value_length == 2
    assert decode_user_defined_data(
        encode_user_defined_data(RVTUserDefinedData(1, RVTUserDataType.UNSIGNED_INTEGER, 256))
    ).value_length == 2


def test_standalone_rvt_packet_owns_timestamp_and_crc32() -> None:
    timestamp = datetime(2024, 1, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
    poi1 = decode_point_of_interest(encode_point_of_interest({1: 1, 2: 45.0, 3: -90.0, 6: "truck"}))
    poi2 = decode_point_of_interest(encode_point_of_interest({1: 2, 2: 46.0, 3: -91.0}))
    aoi = decode_area_of_interest(
        encode_area_of_interest({1: 7, 2: 45.0, 3: -90.0, 4: 44.0, 5: -89.0, 6: 2})
    )
    user = RVTUserDefinedData(3, RVTUserDataType.STRING, "model-a")
    packet = encode_rvt_local_set(
        {
            2: timestamp,
            3: 120,
            8: 4,
            10: "H.264",
            11: user,
            12: (poi1, poi2),
            13: aoi,
            14: 11,
            15: "SMU",
            16: 12345,
            17: 54321,
        }
    )
    assert mpeg2_crc32(packet) == 0
    decoded = decode_rvt_local_set(packet)
    assert isinstance(decoded, RVTLocalSet)
    assert decoded.standalone is True
    assert decoded.packet is not None
    assert decoded.value(2) == timestamp
    assert decoded.value(3) == 120
    assert decoded.value(10) == "H.264"
    assert decoded.value(11) == user
    assert decoded.value(13).value(1) == 7
    assert tuple(field.value.value(1) for field in decoded.getall(12)) == (1, 2)
    with pytest.raises(ValueError, match="occurs 2 times"):
        decoded.get(12)
    assert bytes(decoded.packet) == packet


def test_rvt_time_of_birth_context_validates_encode_and_decode() -> None:
    birth = datetime(2024, 1, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
    context = RVTValidationContext(metadata_birth_timestamp=birth)
    packet = encode_rvt_local_set({2: birth, 8: 4}, context=context)
    assert decode_rvt_local_set(packet, context=context).value(2) == birth

    mismatched = RVTValidationContext(
        metadata_birth_timestamp=datetime(2024, 1, 2, 3, 4, 5, 7, tzinfo=timezone.utc)
    )
    with pytest.raises(ValueError, match="time of birth"):
        encode_rvt_local_set({2: birth, 8: 4}, context=mismatched)
    with pytest.raises(DecodeError, match="time of birth"):
        decode_rvt_local_set(packet, context=mismatched)


def test_rvt_time_of_birth_context_requires_a_timestamp_when_requested() -> None:
    context = RVTValidationContext(metadata_birth_timestamp=0)
    with pytest.raises(ValueError, match="requires a Precision Time Stamp"):
        encode_rvt_local_set({3: 120}, standalone=False, context=context)
    with pytest.raises(DecodeError, match="requires a Precision Time Stamp"):
        decode_rvt_local_set(bytes.fromhex("03020078"), standalone=False, context=context)


def test_rvt_validation_context_and_checksum_flag_are_strictly_typed() -> None:
    with pytest.raises(TypeError, match="integer microseconds"):
        RVTValidationContext(metadata_birth_timestamp=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timezone-aware"):
        RVTValidationContext(metadata_birth_timestamp=datetime(2024, 1, 1))
    with pytest.raises(ValueError, match="uint64"):
        RVTValidationContext(metadata_birth_timestamp=-1)
    with pytest.raises(TypeError, match="RVTValidationContext"):
        encode_rvt_local_set({2: 0}, context=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="RVTValidationContext"):
        decode_rvt_local_set(
            encode_rvt_local_set({2: 0}),
            context=object(),  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="verify_checksum"):
        decode_rvt_local_set(
            encode_rvt_local_set({2: 0}),
            verify_checksum=1,  # type: ignore[arg-type]
        )


def test_standalone_rvt_structure_and_crc_are_strict() -> None:
    with pytest.raises(ValueError, match="tag 2"):
        encode_rvt_local_set({3: 10})
    packet = bytearray(encode_rvt_local_set({2: 0, 8: 4}))
    packet[-1] ^= 1
    with pytest.raises(ChecksumError, match="CRC"):
        decode_rvt_local_set(bytes(packet))

    value = bytes.fromhex("080104 02080000000000000000 010400000000")
    malformed = RVT_LOCAL_SET_KEY + encode_ber_length(len(value)) + value
    with pytest.raises(DecodeError, match="first"):
        decode_rvt_local_set(malformed, verify_checksum=False)


def test_embedded_rvt_omits_independent_packet_fields() -> None:
    value = encode_rvt_local_set({3: 120, 8: 4}, standalone=False)
    decoded = decode_rvt_local_set(value, standalone=False)
    assert decoded.standalone is False
    assert decoded.packet is None
    assert decoded.value(3) == 120
    assert bytes(decoded.local_set) == value
    with pytest.raises(ValueError, match="forbids checksum"):
        encode_rvt_local_set({1: 0, 3: 120}, standalone=False)


def test_rvt_encoder_validates_api_and_nested_types() -> None:
    with pytest.raises(TypeError, match="must be a mapping"):
        encode_rvt_local_set([])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="non-negative integers"):
        encode_rvt_local_set({True: 1})
    with pytest.raises(TypeError, match="boolean"):
        encode_rvt_local_set({}, standalone=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="computed automatically"):
        encode_rvt_local_set({1: 0, 2: 0})
    with pytest.raises(ValueError, match="at least one value"):
        encode_rvt_local_set({2: 0, 12: ()})
    with pytest.raises(TypeError, match="RVTUserDefinedData"):
        encode_rvt_local_set({2: 0, 11: "user"})
    with pytest.raises(TypeError, match="RVTPointOfInterest"):
        encode_rvt_local_set({2: 0, 12: "poi"})
    with pytest.raises(TypeError, match="RVTAreaOfInterest"):
        encode_rvt_local_set({2: 0, 13: "aoi"})
    with pytest.raises(TypeError, match="RawRVTValue"):
        encode_rvt_local_set({2: 0, 200: b"raw"})
    extension = encode_rvt_local_set({2: 0, 200: RawRVTValue(b"raw")})
    assert decode_rvt_local_set(extension).local_set.getone(200).value == b"raw"


def test_rvt_decoder_rejects_invalid_api_and_structure() -> None:
    with pytest.raises(TypeError, match="boolean"):
        decode_rvt_local_set(b"", standalone=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="must be bytes"):
        decode_rvt_local_set(KLVPacket(RVT_LOCAL_SET_KEY, b"", b""), standalone=False)
    with pytest.raises(DecodeError, match="exactly one"):
        decode_rvt_local_set(b"")
    with pytest.raises(DecodeError, match="empty"):
        decode_rvt_local_set(b"", standalone=False)
    with pytest.raises(DecodeError, match="requires one Precision"):
        value = bytes.fromhex("03020001 010400000000")
        decode_rvt_local_set(
            RVT_LOCAL_SET_KEY + encode_ber_length(len(value)) + value,
            verify_checksum=False,
        )
    with pytest.raises(ChecksumError, match="final four-byte"):
        value = bytes.fromhex("02080000000000000000 01020000")
        decode_rvt_local_set(
            RVT_LOCAL_SET_KEY + encode_ber_length(len(value)) + value,
            verify_checksum=False,
        )
    with pytest.raises(DecodeError, match="forbids checksum"):
        decode_rvt_local_set(bytes.fromhex("03020001 010400000000"), standalone=False)


def test_rvt_timestamp_and_scalar_constraints() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        encode_rvt_local_set({2: datetime(2024, 1, 1)})
    with pytest.raises(ValueError, match="permitted range"):
        encode_rvt_local_set({2: 0, 14: 0})
    with pytest.raises(ValueError, match="byte range"):
        encode_rvt_local_set({2: 0, 3: 65536})
    with pytest.raises(TypeError, match="integer"):
        encode_rvt_local_set({2: 0, 3: 1.5})
    with pytest.raises(DecodeError, match="date value out of range"):
        decode_rvt_local_set(bytes.fromhex("0208FFFFFFFFFFFFFFFF"), standalone=False)


@pytest.mark.parametrize("tag", [15, 19])
@pytest.mark.parametrize("value", ["123", "sMU", "IMU", "S1U", "SIU"])
def test_rvt_mgrs_band_and_grid_square_rejects_invalid_text(
    tag: int, value: str
) -> None:
    with pytest.raises(ValueError, match=r"MGRS.*latitude band.*grid square"):
        encode_rvt_local_set({tag: value}, standalone=False)

    with pytest.raises(DecodeError, match=r"MGRS.*latitude band.*grid square"):
        decode_rvt_local_set(bytes((tag, 3)) + value.encode("ascii"), standalone=False)


@pytest.mark.parametrize(("tag", "value"), [(15, "SMU"), (19, "ZAA")])
def test_rvt_mgrs_band_and_grid_square_accepts_valid_text(tag: int, value: str) -> None:
    encoded = encode_rvt_local_set({tag: value}, standalone=False)
    assert decode_rvt_local_set(encoded, standalone=False).value(tag) == value


def test_wrong_rvt_key_and_duplicate_scalar_are_rejected() -> None:
    wrong = KLVPacket(bytes(16), b"\x02\x08" + bytes(8) + b"\x01\x04" + bytes(4), b"\x0e")
    with pytest.raises(DecodeError, match="Universal Key"):
        decode_rvt_local_set(wrong, verify_checksum=False)
    value = bytes.fromhex("03020001 03020002")
    with pytest.raises(DecodeError, match="occurs twice"):
        decode_rvt_local_set(value, standalone=False)
