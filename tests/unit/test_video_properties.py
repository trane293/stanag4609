from __future__ import annotations

from dataclasses import replace
from fractions import Fraction

import pytest

from stanag4609 import H262VideoPropertiesParser as PublicH262VideoPropertiesParser
from stanag4609 import HEVCVideoPropertiesParser as PublicHEVCVideoPropertiesParser
from stanag4609.errors import DecodeError, TruncatedData
from stanag4609.video import (
    AVCVideoPropertiesParser,
    H262VideoPropertiesParser,
    HEVCVideoPropertiesParser,
    VideoProperties,
    _avc_level_name,
)


def _bits(fields: tuple[tuple[int, int], ...]) -> bytes:
    value = 0
    width = 0
    for field, bits in fields:
        assert 0 <= field < 1 << bits
        value = (value << bits) | field
        width += bits
    assert width % 8 == 0
    return value.to_bytes(width // 8, "big")


def _ue_bits(value: int) -> str:
    code = f"{value + 1:b}"
    return "0" * (len(code) - 1) + code


def _se_bits(value: int) -> str:
    return _ue_bits(2 * abs(value) - int(value > 0))


def _hevc_ebsp(bits: str) -> bytes:
    bits += "1"
    bits += "0" * (-len(bits) % 8)
    rbsp = int(bits, 2).to_bytes(len(bits) // 8, "big")
    escaped = bytearray()
    zero_count = 0
    for byte in rbsp:
        if zero_count >= 2 and byte <= 3:
            escaped.append(3)
            zero_count = 0
        escaped.append(byte)
        zero_count = zero_count + 1 if byte == 0 else 0
    return bytes(escaped)


def _synthetic_hevc_sps(
    *,
    log2_poc_minus4: int = 4,
    short_term_sets: int = 2,
    long_term_pictures: int = 1,
    aspect_ratio_idc: int = 1,
    sar: tuple[int, int] = (1, 1),
    timing: tuple[int, int] = (1001, 60000),
    level_idc: int = 63,
) -> bytes:
    bits = "0000" "000" "1"  # VPS id, one temporal layer, temporal nesting
    bits += "00" "0" "00010"  # profile space, tier, Main 10 profile
    bits += f"{1 << 29:032b}"  # Main 10 compatibility
    bits += "1" "0" "0" "1" + "0" * 44  # progressive, frame-only source
    bits += f"{level_idc:08b}"
    bits += _ue_bits(0) + _ue_bits(1)  # SPS id, 4:2:0
    bits += _ue_bits(640) + _ue_bits(360) + "0"  # size, no crop
    bits += _ue_bits(2) + _ue_bits(2) + _ue_bits(log2_poc_minus4)
    bits += "0" + _ue_bits(4) + _ue_bits(2) + _ue_bits(0)  # ordering
    bits += _ue_bits(0) * 6  # coding/transform block sizes and hierarchy depths
    bits += "11"  # scaling lists enabled and present
    for size_id in range(4):
        for _ in range(2 if size_id == 3 else 6):
            bits += "1"  # explicit scaling list
            if size_id > 1:
                bits += _se_bits(0)
            bits += _se_bits(0) * min(64, 1 << (4 + (size_id << 1)))
    bits += "11"  # AMP and sample-adaptive offset
    bits += "1" + "0111" + "0111" + _ue_bits(0) + _ue_bits(0) + "0"  # PCM
    bits += _ue_bits(short_term_sets)
    if short_term_sets == 2:
        bits += _ue_bits(1) + _ue_bits(1)
        bits += _ue_bits(0) + "1" + _ue_bits(0) + "1"
        bits += "1" + "0" + _ue_bits(0)  # predicted from set zero
        bits += "1" + "01" + "00"  # used/use-delta decisions for three entries
    bits += "1" + _ue_bits(long_term_pictures)
    for index in range(long_term_pictures):
        bits += f"{index:0{log2_poc_minus4 + 4}b}" + "1"
    bits += "11"  # temporal MVP and strong intra smoothing
    bits += "1"  # VUI present
    bits += "1" + f"{aspect_ratio_idc:08b}"
    if aspect_ratio_idc == 255:
        bits += f"{sar[0]:016b}{sar[1]:016b}"
    bits += "11"  # overscan present and appropriate
    bits += "1" + "101" + "1" + "1" + f"{1:08b}{1:08b}{1:08b}"
    bits += "1" + _ue_bits(2) + _ue_bits(3)  # chroma locations
    bits += "0" "0" "1"  # neutral chroma, frames, frame/field information
    bits += "1" + _ue_bits(0) * 4  # default display window
    bits += "1" + f"{timing[0]:032b}" + f"{timing[1]:032b}" + "1" + _ue_bits(1)
    bits += "0"  # no HRD parameters
    return b"\x42\x01" + _hevc_ebsp(bits)


def _sequence_header(
    *, width: int = 720, height: int = 480, aspect: int = 3, frame_rate: int = 4
) -> bytes:
    prefix = _bits(((width, 12), (height, 12), (aspect, 4), (frame_rate, 4)))
    # bit_rate_value, marker_bit, vbv_buffer_size_value, constrained flag
    suffix = _bits(((50_000, 18), (1, 1), (112, 10), (0, 1), (0, 1), (0, 1)))
    return b"\x00\x00\x01\xb3" + prefix + suffix


def _sequence_extension(
    *,
    profile: int = 4,
    level: int = 8,
    progressive: int = 1,
    chroma: int = 1,
    horizontal_extension: int = 0,
    vertical_extension: int = 0,
    frame_rate_n: int = 0,
    frame_rate_d: int = 0,
) -> bytes:
    payload = _bits(
        (
            (1, 4),
            ((profile << 4) | level, 8),
            (progressive, 1),
            (chroma, 2),
            (horizontal_extension, 2),
            (vertical_extension, 2),
            (0xFFF, 12),
            (1, 1),
            (0xFF, 8),
            (0, 1),
            (frame_rate_n, 2),
            (frame_rate_d, 5),
        )
    )
    return b"\x00\x00\x01\xb5" + payload


def test_h262_properties_decode_main_profile_progressive_sequence() -> None:
    assert PublicH262VideoPropertiesParser is H262VideoPropertiesParser
    parser = H262VideoPropertiesParser()
    source = _sequence_header() + _sequence_extension() + b"\x00\x00\x01\x00picture"

    properties = []
    for index in range(0, len(source), 3):
        properties.extend(parser.feed(source[index : index + 3]))
    properties.extend(parser.finish())

    assert properties == [
        VideoProperties(
            stream_type=0x02,
            codec="H.262/MPEG-2 Video",
            width=720,
            height=480,
            display_aspect_ratio=Fraction(16, 9),
            frame_rate=Fraction(30_000, 1_001),
            progressive=True,
            profile="Main",
            level="Main",
            chroma_format="4:2:0",
            profile_code=4,
            level_code=8,
            bit_depth_luma=8,
            bit_depth_chroma=8,
        )
    ]
    assert properties[0].level_picture_size_conforms is True
    assert properties[0].level_sample_rate_conforms is True


def test_h262_main_level_enforces_sampling_density() -> None:
    parser = H262VideoPropertiesParser()
    result = parser.feed(
        _sequence_header(width=721)
        + _sequence_extension()
        + b"\x00\x00\x01\xb7"
    ) + parser.finish()

    assert result[0].misp_profile_level is True
    assert result[0].level_picture_size_conforms is False


def test_h262_main_level_enforces_frame_and_luma_sample_rate() -> None:
    parser = H262VideoPropertiesParser()
    result = parser.feed(
        _sequence_header(width=320, height=240, frame_rate=8)
        + _sequence_extension()
        + b"\x00\x00\x01\xb7"
    ) + parser.finish()

    assert result[0].frame_rate == 60
    assert result[0].level_picture_size_conforms is True
    assert result[0].level_sample_rate_conforms is False


def test_h262_high_level_accepts_common_1080p30_resources() -> None:
    parser = H262VideoPropertiesParser()
    result = parser.feed(
        _sequence_header(width=1920, height=1080, frame_rate=5)
        + _sequence_extension(level=4)
        + b"\x00\x00\x01\xb7"
    ) + parser.finish()

    assert result[0].level == "High"
    assert result[0].level_picture_size_conforms is True
    assert result[0].level_sample_rate_conforms is True


def test_h262_properties_apply_size_and_frame_rate_extensions() -> None:
    parser = H262VideoPropertiesParser()
    source = (
        _sequence_header(width=1, height=2, aspect=1, frame_rate=3)
        + _sequence_extension(
            horizontal_extension=1,
            vertical_extension=2,
            frame_rate_n=1,
            frame_rate_d=3,
        )
        + b"\x00\x00\x01\xb7"
    )

    result = parser.feed(source) + parser.finish()

    assert len(result) == 1
    assert result[0].width == 4097
    assert result[0].height == 8194
    assert result[0].display_aspect_ratio == Fraction(4097, 8194)
    assert result[0].frame_rate == Fraction(25, 2)


def test_h262_properties_report_interlaced_and_non_misp_profile() -> None:
    parser = H262VideoPropertiesParser()
    result = parser.feed(
        _sequence_header() + _sequence_extension(profile=5, level=10, progressive=0)
        + b"\x00\x00\x01\xb7"
    ) + parser.finish()

    assert result[0].progressive is False
    assert result[0].profile == "Simple"
    assert result[0].level == "Low"
    assert result[0].misp_profile_level is False


def test_h262_properties_validate_headers_bounds_and_lifecycle() -> None:
    parser = H262VideoPropertiesParser(max_unit_size=16)
    with pytest.raises(TruncatedData, match="sequence header"):
        parser.feed(b"\x00\x00\x01\xb3\x01\x02\x00\x00\x01\x00")

    parser.reset()
    bad_marker = bytearray(_sequence_header())
    bad_marker[10] &= ~0x20
    with pytest.raises(DecodeError, match="marker_bit"):
        parser.feed(bytes(bad_marker) + b"\x00\x00\x01\x00")

    bounded = H262VideoPropertiesParser(max_unit_size=16)
    with pytest.raises(DecodeError, match="exceeds"):
        bounded.feed(b"\x00\x00\x01\xb2" + b"x" * 20)

    complete = H262VideoPropertiesParser()
    complete.feed(b"junk")
    assert complete.finish() == ()
    assert complete.finish() == ()
    with pytest.raises(RuntimeError, match="finished"):
        complete.feed(b"")
    complete.reset()
    assert complete.buffered_bytes == 0


def test_h262_sequence_extension_requires_prior_header_and_valid_codes() -> None:
    parser = H262VideoPropertiesParser()
    assert parser.feed(_sequence_extension() + b"\x00\x00\x01\x00") == ()

    bad_aspect = H262VideoPropertiesParser()
    with pytest.raises(DecodeError, match="aspect_ratio_information"):
        bad_aspect.feed(
            _sequence_header(aspect=0) + _sequence_extension() + b"\x00\x00\x01\x00"
        )

    bad_rate = H262VideoPropertiesParser()
    with pytest.raises(DecodeError, match="frame_rate_code"):
        bad_rate.feed(
            _sequence_header(frame_rate=0)
            + _sequence_extension()
            + b"\x00\x00\x01\x00"
        )


def test_avc_properties_decode_constrained_baseline_vector_across_chunks() -> None:
    # FFmpeg/libx264 1280x720p30 Constrained Baseline Level 4.0 SPS.
    sps = bytes.fromhex("6742c028d9005005bb0110000003001000000303c0f183248000")
    source = b"garbage\x00\x00\x00\x01" + sps + b"\x00\x00\x01\x68\x00"
    parser = AVCVideoPropertiesParser()
    result = []
    for offset in range(0, len(source), 2):
        result.extend(parser.feed(source[offset : offset + 2]))
    result.extend(parser.finish())

    assert len(result) == 1
    properties = result[0]
    assert properties.width == 1280
    assert properties.height == 720
    assert properties.display_aspect_ratio == Fraction(16, 9)
    assert properties.frame_rate == Fraction(30, 1)
    assert properties.frame_rate_is_fixed is False
    assert properties.progressive is True
    assert properties.profile == "Constrained Baseline"
    assert properties.level == "4.0"
    assert properties.chroma_format == "4:2:0"
    assert properties.misp_profile_level is True
    assert properties.level_picture_size_conforms is True
    assert properties.level_sample_rate_conforms is True


def test_avc_properties_expose_real_fmv_profile_violation() -> None:
    # SPS extracted from the checksum-pinned public Esri Truck.ts fixture.
    sps = bytes.fromhex(
        "674200298c680780227e5ffc00040004400000fa40003a98250000000000000000"
    )
    parser = AVCVideoPropertiesParser()
    result = parser.feed(b"\x00\x00\x01" + sps + b"\x00\x00\x01\x68")

    assert len(result) == 1
    assert result[0].width == 1920
    assert result[0].height == 1080
    assert result[0].profile == "Baseline"
    assert result[0].level == "4.1"
    assert result[0].misp_profile_level is False


def test_avc_properties_reject_reserved_level_code_inside_numeric_range() -> None:
    sps = bytearray.fromhex("6742c028d9005005bb0110000003001000000303c0f183248000")
    sps[3] = 14
    parser = AVCVideoPropertiesParser()
    result = parser.feed(b"\x00\x00\x01" + sps + b"\x00\x00\x01\x68")

    assert result[0].level == "1.4"
    assert result[0].misp_profile_level is False
    assert result[0].level_picture_size_conforms is None


def test_avc_properties_enforce_signalled_level_size_and_sample_rate() -> None:
    sps = bytearray.fromhex("6742c028d9005005bb0110000003001000000303c0f183248000")
    sps[3] = 10
    parser = AVCVideoPropertiesParser()
    properties = parser.feed(b"\x00\x00\x01" + sps + b"\x00\x00\x01\x68")[0]

    assert properties.coded_width == 1280
    assert properties.coded_height == 720
    assert properties.level_picture_size_conforms is False
    assert properties.level_sample_rate_conforms is False


def test_level_dimension_limits_apply_independently_of_total_picture_size() -> None:
    avc_parser = AVCVideoPropertiesParser()
    avc = avc_parser.feed(
        bytes.fromhex(
            "0000016742c028d9005005bb0110000003001000000303c0f18324800000000168"
        )
    )[0]
    narrow_avc = replace(
        avc,
        width=912,
        height=16,
        coded_width=912,
        coded_height=16,
        level="1.2",
        level_code=12,
    )
    assert narrow_avc.level_picture_size_conforms is False

    hevc_parser = HEVCVideoPropertiesParser()
    hevc = hevc_parser.feed(
        b"\x00\x00\x01"
        + _synthetic_hevc_sps(level_idc=30)
        + b"\x00\x00\x01\x44\x01"
    )[0]
    narrow_hevc = replace(
        hevc,
        width=544,
        height=16,
        coded_width=544,
        coded_height=16,
    )
    assert narrow_hevc.level_picture_size_conforms is False


def test_avc_level_1b_uses_profile_specific_signalling() -> None:
    assert _avc_level_name(100, 9, 0) == "1b"
    assert _avc_level_name(66, 11, 0x10) == "1b"
    assert _avc_level_name(100, 11, 0x10) == "1.1"


def test_avc_level_1b_uses_its_stricter_level_limits() -> None:
    parser = AVCVideoPropertiesParser()
    source = bytes.fromhex(
        "0000016742c028d9005005bb0110000003001000000303c0f18324800000000168"
    )
    base = parser.feed(source)[0]
    level_1b = replace(
        base,
        width=320,
        height=180,
        coded_width=320,
        coded_height=192,
        frame_rate=Fraction(10, 1),
        level="1b",
        level_code=11,
    )

    assert level_1b.level_picture_size_conforms is False
    assert level_1b.level_sample_rate_conforms is False
    level_1_1 = replace(level_1b, level="1.1")
    assert level_1_1.level_picture_size_conforms is True
    assert level_1_1.level_sample_rate_conforms is True


def test_avc_properties_parser_is_bounded_and_reports_malformed_sps() -> None:
    parser = AVCVideoPropertiesParser(max_unit_size=8)
    with pytest.raises(DecodeError, match="exceeds"):
        parser.feed(b"\x00\x00\x01\x67" + b"x" * 12)

    malformed = AVCVideoPropertiesParser()
    with pytest.raises(TruncatedData, match="bit field"):
        malformed.feed(b"\x00\x00\x01\x67\x42\x00\x28\x00\x00\x01\x68")

    finished = AVCVideoPropertiesParser()
    finished.feed(b"junk")
    assert finished.finish() == ()
    with pytest.raises(RuntimeError, match="finished"):
        finished.feed(b"")


def test_hevc_properties_decode_main10_vector_across_chunks() -> None:
    assert PublicHEVCVideoPropertiesParser is HEVCVideoPropertiesParser
    # FFmpeg/libx265 640x360p30 Main 10 SPS; the encoder selected Level 2.1.
    sps = bytes.fromhex(
        "42010102200000030090000003000003003fa005020169365959a4932bc05a02"
        "0000030002000003003c10"
    )
    source = b"garbage\x00\x00\x00\x01" + sps + b"\x00\x00\x01\x44\x01"
    parser = HEVCVideoPropertiesParser()
    result = []
    for offset in range(0, len(source), 3):
        result.extend(parser.feed(source[offset : offset + 3]))
    result.extend(parser.finish())

    assert len(result) == 1
    properties = result[0]
    assert properties.width == 640
    assert properties.height == 360
    assert properties.progressive is True
    assert properties.profile == "Main 10"
    assert properties.level == "2.1"
    assert properties.chroma_format == "4:2:0"
    assert properties.bit_depth_luma == 10
    assert properties.bit_depth_chroma == 10
    assert properties.profile_code == 2
    assert properties.level_code == 63
    assert properties.misp_profile_level is True
    assert properties.level_picture_size_conforms is True
    assert properties.level_sample_rate_conforms is True


def test_hevc_properties_decode_vui_aspect_ratio_and_timing() -> None:
    # FFmpeg/libx265 640x360p30000/1001 Main 10 SPS with square-pixel VUI.
    sps = bytes.fromhex(
        "42010102200000030090000003000003003fa005020169365959a4932bc05a02"
        "000007d20000ea6010"
    )
    parser = HEVCVideoPropertiesParser()
    result = parser.feed(b"\x00\x00\x01" + sps + b"\x00\x00\x01\x44\x01")

    assert len(result) == 1
    assert result[0].display_aspect_ratio == Fraction(16, 9)
    assert result[0].frame_rate == Fraction(30_000, 1_001)


def test_hevc_properties_walk_optional_sps_syntax_before_vui() -> None:
    parser = HEVCVideoPropertiesParser()
    sps = _synthetic_hevc_sps()
    result = parser.feed(b"\x00\x00\x01" + sps + b"\x00\x00\x01\x44\x01")

    assert len(result) == 1
    assert result[0].display_aspect_ratio == Fraction(16, 9)
    assert result[0].frame_rate == Fraction(30_000, 1_001)


def test_hevc_properties_decode_extended_sample_aspect_ratio() -> None:
    parser = HEVCVideoPropertiesParser()
    sps = _synthetic_hevc_sps(aspect_ratio_idc=255, sar=(4, 3))
    result = parser.feed(b"\x00\x00\x01" + sps + b"\x00\x00\x01\x44\x01")

    assert result[0].display_aspect_ratio == Fraction(64, 27)


@pytest.mark.parametrize("timing", [(0, 60_000), (1_001, 0)])
def test_hevc_properties_reject_zero_vui_timing_values(timing: tuple[int, int]) -> None:
    parser = HEVCVideoPropertiesParser()
    sps = _synthetic_hevc_sps(timing=timing)
    with pytest.raises(DecodeError, match="timing values must be non-zero"):
        parser.feed(b"\x00\x00\x01" + sps + b"\x00\x00\x01\x44\x01")


@pytest.mark.parametrize(
    ("sps", "message"),
    [
        (_synthetic_hevc_sps(log2_poc_minus4=13), "pic_order_cnt"),
        (_synthetic_hevc_sps(short_term_sets=65), "short_term_ref_pic_sets"),
        (_synthetic_hevc_sps(short_term_sets=0, long_term_pictures=33), "long_term_ref"),
    ],
)
def test_hevc_properties_reject_out_of_range_sps_counts(sps: bytes, message: str) -> None:
    parser = HEVCVideoPropertiesParser()
    with pytest.raises(DecodeError, match=message):
        parser.feed(b"\x00\x00\x01" + sps + b"\x00\x00\x01\x44\x01")


def test_hevc_properties_reject_main_profile_for_adopted_misp() -> None:
    sps = bytes.fromhex(
        "42010101600000030090000003000003003ca00a080f165959a4932bc05a0200"
        "00030002000003003c10"
    )
    parser = HEVCVideoPropertiesParser()
    result = parser.feed(b"\x00\x00\x01" + sps + b"\x00\x00\x01\x44\x01")

    assert len(result) == 1
    assert result[0].profile == "Main"
    assert result[0].level == "2"
    assert result[0].misp_profile_level is False


def test_hevc_properties_reject_reserved_level_code_inside_numeric_range() -> None:
    parser = HEVCVideoPropertiesParser()
    sps = _synthetic_hevc_sps(level_idc=64)
    result = parser.feed(b"\x00\x00\x01" + sps + b"\x00\x00\x01\x44\x01")

    assert result[0].level == "Level IDC 64"
    assert result[0].misp_profile_level is False
    assert result[0].level_picture_size_conforms is None


def test_hevc_properties_enforce_signalled_level_size_and_sample_rate() -> None:
    parser = HEVCVideoPropertiesParser()
    sps = _synthetic_hevc_sps(level_idc=30)
    properties = parser.feed(
        b"\x00\x00\x01" + sps + b"\x00\x00\x01\x44\x01"
    )[0]

    assert properties.coded_width == 640
    assert properties.coded_height == 360
    assert properties.level_picture_size_conforms is False
    assert properties.level_sample_rate_conforms is False


def test_hevc_properties_parser_validates_header_bounds_and_lifecycle() -> None:
    bounded = HEVCVideoPropertiesParser(max_unit_size=8)
    with pytest.raises(DecodeError, match="exceeds"):
        bounded.feed(b"\x00\x00\x01\x42\x01" + b"x" * 12)

    temporal_id = HEVCVideoPropertiesParser()
    with pytest.raises(DecodeError, match="temporal_id_plus1"):
        temporal_id.feed(b"\x00\x00\x01\x42\x00abcd\x00\x00\x01\x44\x01")

    malformed = HEVCVideoPropertiesParser()
    with pytest.raises(TruncatedData, match="bit field"):
        malformed.feed(b"\x00\x00\x01\x42\x01\x00\x00\x01\x44\x01")

    finished = HEVCVideoPropertiesParser()
    assert finished.finish() == ()
    with pytest.raises(RuntimeError, match="finished"):
        finished.feed(b"")
