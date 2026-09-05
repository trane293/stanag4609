from __future__ import annotations

import pytest

from stanag4609.errors import DecodeError, TruncatedData
from stanag4609.st0903 import (
    RawVMTIValue,
    VChipLocalSet,
    VTargetData,
    decode_vchip_local_set,
    decode_vmti_local_set,
    encode_vchip_local_set,
    encode_vmti_local_set,
    encode_vtarget,
)


def test_vchip_official_image_type_vector_and_round_trip() -> None:
    encoded = encode_vchip_local_set(VChipLocalSet("jpeg"))
    assert encoded == bytes.fromhex("01 04 6A 70 65 67")
    decoded = decode_vchip_local_set(encoded)
    assert decoded.image_type == "jpeg"
    assert decoded.image_iri is None
    assert decoded.embedded_image is None
    assert encode_vchip_local_set(decoded, preserve=True) == encoded


def test_vchip_encodes_external_iri_embedded_image_and_extensions() -> None:
    chip = VChipLocalSet(
        "png",
        "https://example.test/chips/42.png",
        b"\x89PNG\r\n\x1a\nimage",
        {9: RawVMTIValue(b"extension")},
    )
    decoded = decode_vchip_local_set(encode_vchip_local_set(chip))
    assert decoded.image_type == "png"
    assert decoded.image_iri == "https://example.test/chips/42.png"
    assert decoded.embedded_image == b"\x89PNG\r\n\x1a\nimage"
    assert decoded.extensions == {9: RawVMTIValue(b"extension")}


def test_vchip_preserve_retains_noncanonical_item_order() -> None:
    raw = bytes.fromhex("03 04 FF D8 FF D9 01 04 6A 70 65 67")
    decoded = decode_vchip_local_set(raw)
    assert encode_vchip_local_set(decoded, preserve=True) == raw
    assert encode_vchip_local_set(decoded) == bytes.fromhex(
        "01 04 6A 70 65 67 03 04 FF D8 FF D9"
    )


@pytest.mark.parametrize(
    ("raw", "error", "message"),
    [
        (b"", DecodeError, "mandatory imageType"),
        (bytes.fromhex("01 03 67 69 66"), DecodeError, "jpeg.*png"),
        (bytes.fromhex("01 04 6A 70 65 67 01 03 70 6E 67"), DecodeError, "occurs twice"),
        (bytes.fromhex("01 04 6A 70 65 67 02 05 6E 6F 70 65 21"), DecodeError, "absolute IRI"),
        (bytes.fromhex("01 04 6A 70 65 67 03 05 61"), TruncatedData, "only 1 remain"),
    ],
)
def test_vchip_rejects_malformed_local_sets(
    raw: bytes, error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        decode_vchip_local_set(raw)


@pytest.mark.parametrize(
    ("kwargs", "error", "message"),
    [
        ({"image_type": "gif"}, ValueError, "jpeg.*png"),
        ({"image_type": "jpeg", "image_iri": "relative.jpg"}, ValueError, "absolute IRI"),
        ({"image_type": "jpeg", "embedded_image": "bytes"}, TypeError, "embeddedImage"),
    ],
)
def test_vchip_model_rejects_invalid_values(
    kwargs: dict[str, object], error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        VChipLocalSet(**kwargs)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="VChipLocalSet"):
        encode_vchip_local_set(object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("image_type", "payload"),
    [
        ("jpeg", b""),
        ("jpeg", b"not-a-jpeg"),
        ("jpeg", b"\x89PNG\r\n\x1a\nimage"),
        ("png", b"not-a-png"),
        ("png", b"\xff\xd8\xff\xd9"),
    ],
)
def test_vchip_rejects_embedded_image_with_wrong_declared_type(
    image_type: str,
    payload: bytes,
) -> None:
    with pytest.raises(ValueError, match="embeddedImage does not match imageType"):
        VChipLocalSet(image_type, embedded_image=payload)


def test_vchip_decode_rejects_embedded_image_with_wrong_declared_type() -> None:
    wire = bytes.fromhex("01 04 6A 70 65 67 03 08 89504E470D0A1A0A")
    with pytest.raises(DecodeError, match="embeddedImage does not match imageType"):
        decode_vchip_local_set(wire)


def test_vtarget_embeds_one_vchip_and_a_vchip_series() -> None:
    chip = VChipLocalSet("jpeg", embedded_image=b"\xff\xd8\xff\xd9")
    single_packet = encode_vmti_local_set(
        {4: 6, 8: 1},
        targets=(VTargetData(42, {1: 1, 105: chip}),),
    )
    single = decode_vmti_local_set(single_packet, standalone=False).targets[0]
    assert single.value(105) == chip

    alternate = VChipLocalSet("png", image_iri="https://example.test/42.png")
    series_packet = encode_vmti_local_set(
        {4: 6, 8: 1},
        targets=(VTargetData(42, {1: 1, 106: (chip, alternate)}),),
    )
    series = decode_vmti_local_set(series_packet, standalone=False).targets[0]
    assert series.value(106) == (chip, alternate)


def test_vtarget_vchip_series_uses_ber_length_framing() -> None:
    chip = VChipLocalSet("jpeg", embedded_image=b"\xff\xd8\xff\xd9")
    encoded = encode_vtarget(VTargetData(1, {106: (chip,)}))
    # Pack length, target ID, Item 106, series length, chip length, chip LS.
    assert encoded == bytes.fromhex(
        "10 01 6A 0D 0C 01 04 6A 70 65 67 03 04 FF D8 FF D9"
    )


def test_vtarget_vchip_fields_validate_types_and_series_shape() -> None:
    with pytest.raises(TypeError, match="vChip requires"):
        encode_vtarget(VTargetData(1, {105: RawVMTIValue(b"x")}))
    with pytest.raises(TypeError, match="vChipSeries requires"):
        encode_vtarget(VTargetData(1, {106: (VChipLocalSet("jpeg"), b"bad")}))
    with pytest.raises(ValueError, match="at least one"):
        encode_vtarget(VTargetData(1, {106: ()}))
