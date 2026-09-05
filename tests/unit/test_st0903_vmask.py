from __future__ import annotations

import pytest

from stanag4609.errors import DecodeError, TruncatedData
from stanag4609.st0903 import (
    PixelRun,
    RawVMTIValue,
    VMaskLocalSet,
    VTargetData,
    decode_vmask_local_set,
    decode_vmti_local_set,
    encode_vmask_local_set,
    encode_vmti_local_set,
    encode_vtarget,
)


def test_vmask_official_pixel_contour_vector() -> None:
    mask = VMaskLocalSet(pixel_contour=(14_762, 14_783, 15_115))
    encoded = encode_vmask_local_set(mask)
    assert encoded == bytes.fromhex("01 09 02 39 AA 02 39 BF 02 3B 0B")
    assert decode_vmask_local_set(encoded) == mask


def test_vmask_official_bit_mask_series_vector() -> None:
    mask = VMaskLocalSet(
        bit_mask_series=(PixelRun(74, 2), PixelRun(89, 4), PixelRun(106, 2))
    )
    encoded = encode_vmask_local_set(mask)
    assert encoded == bytes.fromhex(
        "02 0C 03 01 4A 02 03 01 59 04 03 01 6A 02"
    )
    assert decode_vmask_local_set(encoded) == mask

    long_run = VMaskLocalSet(bit_mask_series=(PixelRun(74, 200),))
    assert encode_vmask_local_set(long_run) == bytes.fromhex("02 05 04 01 4A 81 C8")
    assert decode_vmask_local_set(bytes.fromhex("02 05 04 01 4A 81 C8")) == long_run


def test_vmask_supports_both_representations_extensions_and_preservation() -> None:
    raw = bytes.fromhex("02 04 03 01 4A 02 01 09 02 39AA 02 39BF 02 3B0B 09 01 FF")
    decoded = decode_vmask_local_set(raw)
    assert decoded.pixel_contour == (14_762, 14_783, 15_115)
    assert decoded.bit_mask_series == (PixelRun(74, 2),)
    assert decoded.extensions == {9: RawVMTIValue(b"\xff")}
    assert encode_vmask_local_set(decoded, preserve=True) == raw
    assert encode_vmask_local_set(decoded) == bytes.fromhex(
        "01 09 02 39AA 02 39BF 02 3B0B 02 04 03 01 4A 02 09 01 FF"
    )


def test_pixel_run_and_contour_helpers_validate_frame_geometry() -> None:
    run = PixelRun(11, 2)
    assert run.end_pixel == 12
    mask = VMaskLocalSet(
        pixel_contour=(1, 4, 12, 9),
        bit_mask_series=(run,),
    )
    assert mask.is_clockwise(4)
    mask.validate_for_frame(4, 3)

    with pytest.raises(ValueError, match="clockwise"):
        VMaskLocalSet(pixel_contour=(1, 9, 12, 4)).validate_for_frame(4, 3)
    with pytest.raises(ValueError, match="pixel count"):
        VMaskLocalSet(pixel_contour=(1, 4, 13)).validate_for_frame(4, 3)
    with pytest.raises(ValueError, match="pixel count"):
        VMaskLocalSet(bit_mask_series=(PixelRun(11, 3),)).validate_for_frame(4, 3)


@pytest.mark.parametrize(
    ("kwargs", "error", "message"),
    [
        ({}, ValueError, "representation"),
        ({"pixel_contour": (1, 2)}, ValueError, "at least three"),
        ({"pixel_contour": (1, True, 3)}, TypeError, "pixelContour"),
        ({"pixel_contour": (1, 0, 3)}, ValueError, "positive"),
        ({"bit_mask_series": ("bad",)}, TypeError, "PixelRun"),
    ],
)
def test_vmask_model_rejects_invalid_values(
    kwargs: dict[str, object], error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        VMaskLocalSet(**kwargs)  # type: ignore[arg-type]


def test_vmask_encoder_requires_typed_value() -> None:
    with pytest.raises(TypeError, match="VMaskLocalSet"):
        encode_vmask_local_set(object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("args", "error", "message"),
    [
        ((True, 1), TypeError, "start_pixel"),
        ((0, 1), ValueError, "positive"),
        ((1, 0), ValueError, "positive"),
    ],
)
def test_pixel_run_rejects_invalid_values(
    args: tuple[object, object], error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        PixelRun(*args)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw", "error", "message"),
    [
        (b"", DecodeError, "representation"),
        (bytes.fromhex("01 06 01 01 01 02"), TruncatedData, "only"),
        (bytes.fromhex("01 04 01 01 01 02"), DecodeError, "at least three"),
        (bytes.fromhex("01 06 01 01 01 00 01 03"), DecodeError, "positive"),
        (bytes.fromhex("02 04 03 01 4A 00"), DecodeError, "run_length.*positive"),
        (bytes.fromhex("02 05 04 02 00 4A 02"), DecodeError, "minimal"),
        (bytes.fromhex("02 05 04 01 4A 02 00"), DecodeError, "exactly one"),
    ],
)
def test_vmask_rejects_malformed_wire_values(
    raw: bytes, error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        decode_vmask_local_set(raw)


def test_vtarget_item_101_embeds_typed_vmask() -> None:
    mask = VMaskLocalSet(pixel_contour=(1, 4, 12, 9))
    packet = encode_vmti_local_set(
        {4: 6, 8: 4, 9: 3},
        targets=(VTargetData(7, {1: 1, 101: mask}),),
    )
    target = decode_vmti_local_set(packet, standalone=False).targets[0]
    assert target.value(101) == mask
    assert encode_vtarget(VTargetData(7, {101: mask})).hex().startswith("0d0765")

    with pytest.raises(ValueError, match="clockwise"):
        encode_vmti_local_set(
            {4: 6, 8: 4, 9: 3},
            targets=(
                VTargetData(
                    7,
                    {101: VMaskLocalSet(pixel_contour=(1, 9, 12, 4))},
                ),
            ),
        )

    invalid_target = encode_vtarget(
        VTargetData(7, {101: VMaskLocalSet(pixel_contour=(1, 9, 12, 4))})
    )
    invalid_packet = (
        bytes.fromhex("04 01 06 06 01 01 08 01 04 09 01 03 65")
        + bytes((len(invalid_target),))
        + invalid_target
    )
    with pytest.raises(DecodeError, match="clockwise"):
        decode_vmti_local_set(invalid_packet, standalone=False)


def test_vtarget_vmask_requires_typed_value() -> None:
    with pytest.raises(TypeError, match="vMask requires"):
        encode_vtarget(VTargetData(1, {101: RawVMTIValue(b"x")}))


def test_vmask_frame_validation_rejects_invalid_dimensions() -> None:
    mask = VMaskLocalSet(pixel_contour=(1, 2, 3))
    with pytest.raises(ValueError, match="frame_width"):
        mask.validate_for_frame(0, 1)
    with pytest.raises(ValueError, match="frame_height"):
        mask.validate_for_frame(1, True)
    assert not VMaskLocalSet(bit_mask_series=(PixelRun(1, 1),)).is_clockwise(1)
