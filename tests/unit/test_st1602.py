from __future__ import annotations

from datetime import datetime, timezone

import pytest

from stanag4609.errors import DecodeError
from stanag4609.klv.checksum import crc16_ccitt
from stanag4609.st0601 import decode_uas_local_set, encode_uas_local_set
from stanag4609.st1602 import (
    COMPOSITE_IMAGING_LOCAL_SET_KEY,
    CompositeImagingLocalSet,
    RawCompositeValue,
    decode_composite_imaging_local_set,
    encode_composite_imaging_local_set,
)

WHEN = datetime(2023, 3, 2, 1, 2, 3, tzinfo=timezone.utc)


def _minimal(**changes: object) -> CompositeImagingLocalSet:
    values: dict[str, object] = {
        "document_version": 2,
        "sub_image_rows": 480,
        "sub_image_columns": 640,
        "sub_image_position_x": -10,
        "sub_image_position_y": 20,
        "z_order": 1,
    }
    values.update(changes)
    return CompositeImagingLocalSet(**values)  # type: ignore[arg-type]


def test_key_and_minimal_round_trip() -> None:
    assert crc16_ccitt(COMPOSITE_IMAGING_LOCAL_SET_KEY) == 666
    encoded = encode_composite_imaging_local_set(_minimal())
    decoded = decode_composite_imaging_local_set(encoded)
    assert decoded == _minimal()
    assert decoded.transparency == 0
    assert decoded.sub_image_rectangle == (-10, 20, 640, 480)
    assert encode_composite_imaging_local_set(decoded, preserve=True) == encoded


def test_all_fields_and_active_rectangle() -> None:
    value = _minimal(
        timestamp=WHEN,
        source_image_rows=1080,
        source_image_columns=1920,
        source_aoi_rows=540,
        source_aoi_columns=960,
        source_aoi_position_x=100,
        source_aoi_position_y=200,
        active_rows=400,
        active_columns=600,
        active_offset_x=2,
        active_offset_y=3,
        transparency=178,
    )
    decoded = decode_composite_imaging_local_set(encode_composite_imaging_local_set(value))
    assert decoded == value
    assert decoded.active_rectangle == (-8, 23, 600, 400)


def test_missing_and_duplicate_items_are_rejected() -> None:
    with pytest.raises(DecodeError, match="missing mandatory"):
        decode_composite_imaging_local_set(b"\x02\x01\x02")
    wire = encode_composite_imaging_local_set(_minimal())
    with pytest.raises(DecodeError, match="duplicate"):
        decode_composite_imaging_local_set(wire + b"\x12\x01\x01")


def test_integer_encodings_are_minimal() -> None:
    wire = encode_composite_imaging_local_set(_minimal())
    with pytest.raises(DecodeError, match="minimal unsigned"):
        decode_composite_imaging_local_set(wire.replace(b"\x02\x01\x02", b"\x02\x02\x00\x02"))
    with pytest.raises(DecodeError, match="minimal signed"):
        decode_composite_imaging_local_set(wire.replace(b"\x0b\x01\xf6", b"\x0b\x02\xff\xf6"))


@pytest.mark.parametrize("z_order", [0, 256])
def test_z_order_domain(z_order: int) -> None:
    with pytest.raises(ValueError, match="Z-Order"):
        encode_composite_imaging_local_set(_minimal(z_order=z_order))


@pytest.mark.parametrize("transparency", [-1, 256])
def test_transparency_domain(transparency: int) -> None:
    with pytest.raises(ValueError, match="Transparency"):
        encode_composite_imaging_local_set(_minimal(transparency=transparency))


def test_dimensions_and_active_fields_are_validated() -> None:
    with pytest.raises(ValueError, match="positive"):
        encode_composite_imaging_local_set(_minimal(sub_image_rows=0))
    with pytest.raises(ValueError, match="occur together"):
        encode_composite_imaging_local_set(_minimal(active_rows=10))
    with pytest.raises(ValueError, match="greater than zero"):
        encode_composite_imaging_local_set(
            _minimal(active_rows=10, active_columns=10, active_offset_x=0)
        )


def test_timestamp_requires_eight_bytes_and_timezone() -> None:
    wire = encode_composite_imaging_local_set(_minimal())
    with pytest.raises(DecodeError, match="eight bytes"):
        decode_composite_imaging_local_set(b"\x01\x01\x00" + wire)
    with pytest.raises(ValueError, match="timezone-aware"):
        encode_composite_imaging_local_set(_minimal(timestamp=datetime(2023, 1, 1)))


def test_unknown_extension_is_lossless() -> None:
    value = _minimal(extensions={30: RawCompositeValue(b"future")})
    encoded = encode_composite_imaging_local_set(value)
    decoded = decode_composite_imaging_local_set(encoded)
    assert decoded.extensions[30].data == b"future"
    assert encode_composite_imaging_local_set(decoded, preserve=True) == encoded


def test_st0601_item_99_bridge() -> None:
    value = decode_composite_imaging_local_set(
        encode_composite_imaging_local_set(_minimal())
    )
    packet = encode_uas_local_set({2: WHEN, 65: 19, 99: value})
    assert decode_uas_local_set(packet).value(99) == value


def test_input_types_and_inactive_rectangle() -> None:
    assert _minimal().active_rectangle is None
    with pytest.raises(TypeError):
        decode_composite_imaging_local_set(bytearray())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        encode_composite_imaging_local_set(object())  # type: ignore[arg-type]


def test_additional_model_and_wire_validation() -> None:
    with pytest.raises(TypeError):
        RawCompositeValue(bytearray())  # type: ignore[arg-type]
    wire = encode_composite_imaging_local_set(_minimal())
    with pytest.raises(DecodeError, match="Item 2 is empty"):
        decode_composite_imaging_local_set(wire.replace(b"\x02\x01\x02", b"\x02\x00"))
    with pytest.raises(DecodeError, match="one byte"):
        decode_composite_imaging_local_set(wire.replace(b"\x12\x01\x01", b"\x12\x02\x00\x01"))
    with pytest.raises(ValueError, match="Document Version"):
        encode_composite_imaging_local_set(_minimal(document_version=0))
    with pytest.raises(ValueError, match="non-negative"):
        encode_composite_imaging_local_set(_minimal(source_image_rows=-1))
    with pytest.raises(TypeError, match="Item 3"):
        encode_composite_imaging_local_set(_minimal(source_image_rows="bad"))
    with pytest.raises(TypeError, match="preserve"):
        encode_composite_imaging_local_set(_minimal(), preserve=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="greater than 18"):
        encode_composite_imaging_local_set(
            _minimal(extensions={18: RawCompositeValue(b"x")})
        )
    with pytest.raises(TypeError, match="RawCompositeValue"):
        encode_composite_imaging_local_set(_minimal(extensions={30: b"x"}))


def test_timestamp_type_and_range_validation() -> None:
    with pytest.raises(TypeError, match="datetime"):
        encode_composite_imaging_local_set(_minimal(timestamp="today"))
    with pytest.raises(ValueError, match="uint64"):
        encode_composite_imaging_local_set(
            _minimal(timestamp=datetime(1960, 1, 1, tzinfo=timezone.utc))
        )
