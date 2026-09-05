from __future__ import annotations

from datetime import datetime, timezone

import pytest

from stanag4609.errors import DecodeError
from stanag4609.imap import IMAPSpecialKind, IMAPSpecialValue
from stanag4609.klv.checksum import crc16_ccitt
from stanag4609.klv.model import KLVPacket
from stanag4609.st0601 import decode_uas_local_set, encode_uas_local_set
from stanag4609.st1206 import (
    SAR_MOTION_IMAGERY_LOCAL_SET_KEY,
    ImagePlane,
    LookDirection,
    RawSARValue,
    SARMotionImageryLocalSet,
    decode_sar_motion_imagery_local_set,
    encode_sar_motion_imagery_local_set,
)
from stanag4609.st1303 import MDAP, MDAPAlgorithm, MDAPElementType

WHEN = datetime(2024, 5, 6, 7, 8, 9, tzinfo=timezone.utc)


def _minimal(**changes: object) -> SARMotionImageryLocalSet:
    values: dict[str, object] = {
        "ground_plane_squint_angle": -12.5,
        "look_direction": LookDirection.RIGHT,
        "document_version": 1,
    }
    values.update(changes)
    return SARMotionImageryLocalSet(**values)  # type: ignore[arg-type]


def _polynomial() -> MDAP:
    return MDAP(
        dimensions=(2, 2),
        element_size=4,
        algorithm=MDAPAlgorithm.IMAP,
        elements=(0.0, 1.0, 10.0, 100.0),
        element_type=MDAPElementType.IMAP,
        imap_bounds=(0.0, 1_000_000.0),
        imap_parameter_size=4,
    )


def test_key_and_standalone_minimal_round_trip() -> None:
    assert crc16_ccitt(SAR_MOTION_IMAGERY_LOCAL_SET_KEY) == 54900
    wire = encode_sar_motion_imagery_local_set(_minimal())
    decoded = decode_sar_motion_imagery_local_set(wire)
    assert decoded.ground_plane_squint_angle == pytest.approx(-12.5, abs=0.01)
    assert decoded.look_direction is LookDirection.RIGHT
    assert decoded.document_version == 1
    assert decoded.standalone
    assert encode_sar_motion_imagery_local_set(decoded, preserve=True) == wire


def test_all_fields_embedded_round_trip() -> None:
    value = _minimal(
        grazing_angle=45.0,
        image_plane=ImagePlane.GROUND,
        range_resolution=0.5,
        cross_range_resolution=0.75,
        range_pixel_size=0.25,
        cross_range_pixel_size=0.375,
        image_rows=1080,
        image_columns=1920,
        range_direction_angle=10.0,
        true_north_direction=20.0,
        range_layover_angle=30.0,
        ground_aperture_angular_extent=5.0,
        aperture_duration=123_456,
        ground_track_angle=40.0,
        minimum_detectable_velocity=2.5,
        true_pulse_repetition_frequency=10_000.0,
        pulse_repetition_frequency_scale_factor=0.5,
        transmit_rf_center_frequency=9_600_000_000.0,
        transmit_rf_bandwidth=250_000_000.0,
        radar_cross_section_scale_factor_polynomial=_polynomial(),
        reference_frame_timestamp=WHEN,
        reference_frame_grazing_angle=44.0,
        reference_frame_ground_plane_squint_angle=-10.0,
        reference_frame_range_direction_angle=11.0,
        reference_frame_range_layover_angle=31.0,
    )
    wire = encode_sar_motion_imagery_local_set(value, standalone=False)
    decoded = decode_sar_motion_imagery_local_set(wire, standalone=False)
    assert decoded.look_direction is LookDirection.RIGHT
    assert decoded.image_plane is ImagePlane.GROUND
    assert decoded.image_rows == 1080
    assert decoded.reference_frame_timestamp == WHEN
    assert decoded.radar_cross_section_scale_factor_polynomial is not None
    assert decoded.radar_cross_section_scale_factor_polynomial.dimensions == (2, 2)
    assert decoded.radar_cross_section_scale_factor(1.0, 2.0) == pytest.approx(212.0, abs=0.1)
    assert not decoded.standalone


def test_mandatory_and_duplicate_items_are_rejected() -> None:
    with pytest.raises(DecodeError, match="mandatory"):
        decode_sar_motion_imagery_local_set(b"\x1c\x01\x01", standalone=False)
    wire = encode_sar_motion_imagery_local_set(_minimal(), standalone=False)
    with pytest.raises(DecodeError, match="duplicate"):
        decode_sar_motion_imagery_local_set(wire + b"\x03\x01\x01", standalone=False)


def test_fixed_width_and_enumeration_validation() -> None:
    wire = encode_sar_motion_imagery_local_set(_minimal(), standalone=False)
    with pytest.raises(DecodeError, match=r"Item 2.*two bytes"):
        decode_sar_motion_imagery_local_set(
            wire.replace(b"\x02\x02", b"\x02\x01", 1), standalone=False
        )
    with pytest.raises(DecodeError, match="Look Direction"):
        decode_sar_motion_imagery_local_set(
            wire.replace(b"\x03\x01\x01", b"\x03\x01\x02"), standalone=False
        )
    with pytest.raises(ValueError, match="Image Plane"):
        encode_sar_motion_imagery_local_set(_minimal(image_plane=3))  # type: ignore[arg-type]


def test_polynomial_contract_and_evaluation() -> None:
    wrong_dimensions = MDAP(
        (4,),
        4,
        MDAPAlgorithm.IMAP,
        (0.0, 1.0, 2.0, 3.0),
        MDAPElementType.IMAP,
        imap_bounds=(0.0, 1_000_000.0),
        imap_parameter_size=4,
    )
    with pytest.raises(ValueError, match="two-dimensional"):
        encode_sar_motion_imagery_local_set(
            _minimal(radar_cross_section_scale_factor_polynomial=wrong_dimensions)
        )
    with pytest.raises(LookupError, match="polynomial"):
        _minimal().radar_cross_section_scale_factor(0, 0)

    wrong_algorithm = MDAP(
        (2, 2),
        4,
        MDAPAlgorithm.NATURAL,
        (0.0, 1.0, 2.0, 3.0),
        MDAPElementType.IEEE,
    )
    with pytest.raises(ValueError, match="IMAP algorithm"):
        _minimal(radar_cross_section_scale_factor_polynomial=wrong_algorithm)
    wrong_bounds = MDAP(
        (2, 2),
        4,
        MDAPAlgorithm.IMAP,
        (0.0, 1.0, 2.0, 3.0),
        MDAPElementType.IMAP,
        imap_bounds=(0.0, 100.0),
        imap_parameter_size=4,
    )
    with pytest.raises(ValueError, match="IMAP bounds"):
        _minimal(radar_cross_section_scale_factor_polynomial=wrong_bounds)
    special = IMAPSpecialValue(IMAPSpecialKind.POSITIVE_QUIET_NAN, b"\xd0\x00\x00\x00")
    special_polynomial = MDAP(
        (1, 1),
        4,
        MDAPAlgorithm.IMAP,
        (special,),
        MDAPElementType.IMAP,
        imap_bounds=(0.0, 1_000_000.0),
        imap_parameter_size=4,
    )
    with pytest.raises(ValueError, match="non-numeric"):
        _minimal(
            radar_cross_section_scale_factor_polynomial=special_polynomial
        ).radar_cross_section_scale_factor(0, 0)


def test_effective_pulse_repetition_frequency_applies_equation_18() -> None:
    sar = _minimal(
        true_pulse_repetition_frequency=20_000.0,
        pulse_repetition_frequency_scale_factor=0.25,
    )
    assert sar.effective_pulse_repetition_frequency() == pytest.approx(5_000.0)
    decoded = decode_sar_motion_imagery_local_set(
        encode_sar_motion_imagery_local_set(sar, standalone=False),
        standalone=False,
    )
    assert decoded.effective_pulse_repetition_frequency() == pytest.approx(
        5_000.0,
        abs=0.4,
    )

    with pytest.raises(LookupError, match="true pulse repetition frequency"):
        _minimal(
            pulse_repetition_frequency_scale_factor=0.25
        ).effective_pulse_repetition_frequency()
    with pytest.raises(LookupError, match="scale factor"):
        _minimal(
            true_pulse_repetition_frequency=20_000.0
        ).effective_pulse_repetition_frequency()

    special = IMAPSpecialValue(IMAPSpecialKind.POSITIVE_QUIET_NAN, b"\xd0\x00")
    with pytest.raises(ValueError, match="numeric IMAP"):
        _minimal(
            true_pulse_repetition_frequency=special,
            pulse_repetition_frequency_scale_factor=0.25,
        ).effective_pulse_repetition_frequency()


def test_radar_cross_section_applies_equation_20_with_pixel_validation() -> None:
    sar = _minimal(
        image_rows=2,
        image_columns=3,
        radar_cross_section_scale_factor_polynomial=_polynomial(),
    )
    assert sar.radar_cross_section(1.0, 2.0, pixel_power=3.5) == pytest.approx(742.0)
    decoded = decode_sar_motion_imagery_local_set(
        encode_sar_motion_imagery_local_set(sar, standalone=False),
        standalone=False,
    )
    assert decoded.radar_cross_section(1.0, 2.0, pixel_power=3.5) == pytest.approx(
        742.0,
        abs=0.01,
    )

    for row, column, message in (
        (-1.0, 0.0, "row"),
        (0.0, -1.0, "column"),
        (2.0, 0.0, "row"),
        (0.0, 3.0, "column"),
        (float("nan"), 0.0, "row"),
    ):
        with pytest.raises(ValueError, match=message):
            sar.radar_cross_section_scale_factor(row, column)

    with pytest.raises(TypeError, match="row"):
        sar.radar_cross_section_scale_factor(True, 0.0)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="pixel power"):
        sar.radar_cross_section(0.0, 0.0, pixel_power=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="pixel power"):
        sar.radar_cross_section(0.0, 0.0, pixel_power=-1.0)


def test_unknown_extension_and_embedded_preservation() -> None:
    value = _minimal(extensions={40: RawSARValue(b"future")})
    wire = encode_sar_motion_imagery_local_set(value, standalone=False)
    decoded = decode_sar_motion_imagery_local_set(wire, standalone=False)
    assert decoded.extensions[40].data == b"future"
    assert encode_sar_motion_imagery_local_set(decoded, standalone=False, preserve=True) == wire


def test_st0601_item_95_bridge() -> None:
    sar = decode_sar_motion_imagery_local_set(
        encode_sar_motion_imagery_local_set(_minimal(), standalone=False),
        standalone=False,
    )
    packet = encode_uas_local_set({2: WHEN, 65: 19, 95: sar})
    assert decode_uas_local_set(packet).value(95) == sar


def test_model_and_api_validation() -> None:
    with pytest.raises(TypeError):
        RawSARValue(bytearray())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="look_direction"):
        _minimal(look_direction=1)
    with pytest.raises(ValueError, match="Document Version"):
        _minimal(document_version=0)
    with pytest.raises(TypeError, match="Document Version"):
        _minimal(document_version="1")
    with pytest.raises(TypeError, match="numeric"):
        _minimal(grazing_angle="high")
    with pytest.raises(TypeError, match="Image Rows"):
        _minimal(image_rows=1.5)
    with pytest.raises(ValueError, match="does not fit"):
        _minimal(image_rows=65_536)
    with pytest.raises(TypeError):
        decode_sar_motion_imagery_local_set(bytearray())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        encode_sar_motion_imagery_local_set(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="booleans"):
        encode_sar_motion_imagery_local_set(
            _minimal(),
            standalone=1,  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="standalone"):
        decode_sar_motion_imagery_local_set(
            b"",
            standalone=1,  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="embedded"):
        decode_sar_motion_imagery_local_set(
            KLVPacket(SAR_MOTION_IMAGERY_LOCAL_SET_KEY, b"", b"\x00"),
            standalone=False,
        )
    with pytest.raises(DecodeError, match="unexpected Universal Key"):
        decode_sar_motion_imagery_local_set(KLVPacket(bytes(16), b"", b"\x00"))
    with pytest.raises(DecodeError, match="expected one"):
        decode_sar_motion_imagery_local_set(b"")
    with pytest.raises(ValueError, match="outside"):
        encode_sar_motion_imagery_local_set(_minimal(grazing_angle=91.0))
    with pytest.raises(ValueError, match="greater than 28"):
        encode_sar_motion_imagery_local_set(_minimal(extensions={28: RawSARValue(b"x")}))
    with pytest.raises(TypeError, match="extension tags"):
        encode_sar_motion_imagery_local_set(
            _minimal(
                extensions={"future": RawSARValue(b"x")}  # type: ignore[dict-item]
            )
        )
    with pytest.raises(TypeError, match="RawSARValue"):
        encode_sar_motion_imagery_local_set(
            _minimal(extensions={40: b"x"})  # type: ignore[dict-item]
        )


def test_malformed_optional_wire_fields() -> None:
    wire = encode_sar_motion_imagery_local_set(_minimal(), standalone=False)
    with pytest.raises(DecodeError, match="Item 28 must contain"):
        decode_sar_motion_imagery_local_set(
            wire.replace(b"\x1c\x01\x01", b"\x1c\x02\x00\x01"), standalone=False
        )
    with pytest.raises(DecodeError, match="Look Direction must contain"):
        decode_sar_motion_imagery_local_set(
            wire.replace(b"\x03\x01\x01", b"\x03\x02\x00\x01"), standalone=False
        )
    with pytest.raises(DecodeError, match="Image Plane must contain"):
        decode_sar_motion_imagery_local_set(b"\x04\x02\x00\x01" + wire, standalone=False)
    with pytest.raises(DecodeError, match="unknown ST 1206 Image Plane"):
        decode_sar_motion_imagery_local_set(b"\x04\x01\x03" + wire, standalone=False)
    with pytest.raises(DecodeError, match="eight bytes"):
        decode_sar_motion_imagery_local_set(b"\x17\x01\x00" + wire, standalone=False)
    with pytest.raises(DecodeError, match="Document Version"):
        decode_sar_motion_imagery_local_set(
            wire.replace(b"\x1c\x01\x01", b"\x1c\x01\x00"), standalone=False
        )


def test_reference_timestamp_validation() -> None:
    with pytest.raises(TypeError, match="datetime"):
        _minimal(reference_frame_timestamp="today")
    with pytest.raises(ValueError, match="timezone-aware"):
        _minimal(reference_frame_timestamp=datetime(2024, 1, 1))
    with pytest.raises(ValueError, match="uint64"):
        _minimal(reference_frame_timestamp=datetime(1960, 1, 1, tzinfo=timezone.utc))
