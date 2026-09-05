"""Bounded compressed-video property inspection for MISP profiles."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from math import isfinite

from stanag4609.errors import DecodeError, TruncatedData

_H262_FRAME_RATES = {
    1: Fraction(24_000, 1_001),
    2: Fraction(24, 1),
    3: Fraction(25, 1),
    4: Fraction(30_000, 1_001),
    5: Fraction(30, 1),
    6: Fraction(50, 1),
    7: Fraction(60_000, 1_001),
    8: Fraction(60, 1),
}
_H262_DISPLAY_ASPECT_RATIOS = {
    2: Fraction(4, 3),
    3: Fraction(16, 9),
    4: Fraction(221, 100),
}
_H262_PROFILES = {
    1: "High",
    2: "Spatially Scalable",
    3: "SNR Scalable",
    4: "Main",
    5: "Simple",
}
_H262_LEVELS = {
    4: "High",
    6: "High 1440",
    8: "Main",
    10: "Low",
}
_H262_CHROMA_FORMATS = {1: "4:2:0", 2: "4:2:2", 3: "4:4:4"}
_H262_MAIN_PROFILE_LEVEL_LIMITS = {
    4: (1_920, 1_088, Fraction(60, 1), 62_668_800),
    8: (720, 576, Fraction(30, 1), 10_368_000),
}
_H262_MAIN_PROFILE_MAX_BIT_RATES = {4: 80_000_000, 8: 15_000_000}
_H262_MAIN_PROFILE_MAX_VBV_BUFFER_SIZES = {4: 9_781_248, 8: 1_835_008}
_MISP_AVC_LEVEL_CODES = frozenset({9, 10, 11, 12, 13, 20, 21, 22, 30, 31, 32, 40})
_MISP_HEVC_LEVEL_CODES = frozenset({30, 60, 63, 90, 93, 120, 123, 150, 153})
_AVC_LEVEL_LIMITS = {
    9: (1_485, 99),
    10: (1_485, 99),
    11: (3_000, 396),
    12: (6_000, 396),
    13: (11_880, 396),
    20: (11_880, 396),
    21: (19_800, 792),
    22: (20_250, 1_620),
    30: (40_500, 1_620),
    31: (108_000, 3_600),
    32: (216_000, 5_120),
    40: (245_760, 8_192),
}
_HEVC_LEVEL_LIMITS = {
    30: (36_864, 552_960),
    60: (122_880, 3_686_400),
    63: (245_760, 7_372_800),
    90: (552_960, 16_588_800),
    93: (983_040, 33_177_600),
    120: (2_228_224, 66_846_720),
    123: (2_228_224, 133_693_440),
    150: (8_912_896, 267_386_880),
    153: (8_912_896, 534_773_760),
}


@dataclass(frozen=True, slots=True)
class MISPImageContext:
    """Producer-known image facts that cannot be recovered from encoded video.

    ``source_aspect_ratio`` is the image aspect ratio acquired at the imager,
    which MISP distinguishes from an encoded display aspect ratio. Scan facts
    cover the source and any conversion or transcode stages before the stream
    reaches the verifier. ``source_digital`` and ``conversion_digital`` carry
    the corresponding producer-known signal-form history; the verifier's input
    is necessarily the final digital stage.
    """

    source_aspect_ratio: Fraction | int | float | None = None
    source_progressive: bool | None = None
    conversion_progressive: tuple[bool, ...] = ()
    source_digital: bool | None = None
    conversion_digital: tuple[bool, ...] = ()

    def __post_init__(self) -> None:
        ratio = self.source_aspect_ratio
        if ratio is not None:
            if isinstance(ratio, bool) or not isinstance(ratio, (Fraction, int, float)):
                raise TypeError("source_aspect_ratio must be a finite positive number or None")
            if isinstance(ratio, float) and not isfinite(ratio):
                raise ValueError("source_aspect_ratio must be finite")
            normalized = Fraction(ratio)
            if normalized <= 0:
                raise ValueError("source_aspect_ratio must be positive")
            object.__setattr__(self, "source_aspect_ratio", normalized)
        if self.source_progressive is not None and not isinstance(
            self.source_progressive, bool
        ):
            raise TypeError("source_progressive must be a boolean or None")
        if not isinstance(self.conversion_progressive, tuple):
            raise TypeError("conversion_progressive must be a tuple of booleans")
        for index, progressive in enumerate(self.conversion_progressive):
            if not isinstance(progressive, bool):
                raise TypeError(f"conversion_progressive[{index}] must be a boolean")
        if self.source_digital is not None and not isinstance(self.source_digital, bool):
            raise TypeError("source_digital must be a boolean or None")
        if not isinstance(self.conversion_digital, tuple):
            raise TypeError("conversion_digital must be a tuple of booleans")
        for index, digital in enumerate(self.conversion_digital):
            if not isinstance(digital, bool):
                raise TypeError(f"conversion_digital[{index}] must be a boolean")
        if self.conversion_digital and self.source_digital is None:
            raise ValueError("conversion_digital requires source_digital provenance")


@dataclass(frozen=True, slots=True)
class VideoProperties:
    """Properties advertised by one compressed-video sequence parameter set."""

    stream_type: int
    codec: str
    width: int
    height: int
    display_aspect_ratio: Fraction | None
    frame_rate: Fraction | None
    progressive: bool | None
    profile: str
    level: str
    chroma_format: str
    profile_code: int | None = None
    level_code: int | None = None
    frame_rate_is_fixed: bool | None = None
    bit_depth_luma: int | None = None
    bit_depth_chroma: int | None = None
    coded_width: int | None = None
    coded_height: int | None = None
    frame_rate_extension_n: int | None = None
    frame_rate_extension_d: int | None = None
    bit_rate: int | None = None
    vbv_buffer_size: int | None = None

    @property
    def misp_profile_level(self) -> bool:
        """Whether the codec profile/level meets its adopted MISP rule."""

        if self.stream_type == 0x02:
            return self.profile == "Main" and self.level in {"Main", "High"}
        if self.stream_type == 0x1B:
            return (
                self.profile in {"Constrained Baseline", "Main", "High"}
                and self.level_code in _MISP_AVC_LEVEL_CODES
            )
        if self.stream_type == 0x24:
            return (
                self.profile_code == 2
                and self.level_code in _MISP_HEVC_LEVEL_CODES
            )
        return False

    @property
    def level_picture_size_conforms(self) -> bool | None:
        """Whether coded picture dimensions fit the signalled codec level."""

        width = self.coded_width or self.width
        height = self.coded_height or self.height
        if self.level_code is None:
            return None
        if self.stream_type == 0x02:
            h262_limits = _H262_MAIN_PROFILE_LEVEL_LIMITS.get(self.level_code)
            if self.profile != "Main" or h262_limits is None:
                return None
            maximum_width, maximum_height, _, _ = h262_limits
            return width <= maximum_width and height <= maximum_height
        if self.stream_type == 0x1B:
            avc_limits = (
                (1_485, 99)
                if self.level == "1b"
                else _AVC_LEVEL_LIMITS.get(self.level_code)
            )
            if avc_limits is None:
                return None
            _, maximum_frame_size = avc_limits
            width_in_mbs = (width + 15) // 16
            height_in_mbs = (height + 15) // 16
            return (
                width_in_mbs * height_in_mbs <= maximum_frame_size
                and width_in_mbs * width_in_mbs <= maximum_frame_size * 8
                and height_in_mbs * height_in_mbs <= maximum_frame_size * 8
            )
        if self.stream_type == 0x24:
            hevc_limits = _HEVC_LEVEL_LIMITS.get(self.level_code)
            if hevc_limits is None:
                return None
            maximum_luma_picture_size, _ = hevc_limits
            return (
                width * height <= maximum_luma_picture_size
                and width * width <= maximum_luma_picture_size * 8
                and height * height <= maximum_luma_picture_size * 8
            )
        return None

    @property
    def level_sample_rate_conforms(self) -> bool | None:
        """Whether coded size and advertised rate fit the codec level."""

        if self.frame_rate is None:
            return None
        width = self.coded_width or self.width
        height = self.coded_height or self.height
        if self.level_code is None:
            return None
        if self.stream_type == 0x02:
            h262_limits = _H262_MAIN_PROFILE_LEVEL_LIMITS.get(self.level_code)
            if (
                self.profile != "Main"
                or h262_limits is None
                or self.progressive is None
            ):
                return None
            _, _, maximum_frame_rate, maximum_luma_sample_rate = h262_limits
            padded_width = 16 * ((width + 15) // 16)
            vertical_block = 16 if self.progressive else 32
            padded_height = vertical_block * (
                (height + vertical_block - 1) // vertical_block
            )
            return (
                self.frame_rate <= maximum_frame_rate
                and padded_width * padded_height * self.frame_rate
                <= maximum_luma_sample_rate
            )
        if self.stream_type == 0x1B:
            avc_limits = (
                (1_485, 99)
                if self.level == "1b"
                else _AVC_LEVEL_LIMITS.get(self.level_code)
            )
            if avc_limits is None:
                return None
            maximum_macroblocks_per_second, _ = avc_limits
            macroblocks = ((width + 15) // 16) * ((height + 15) // 16)
            return macroblocks * self.frame_rate <= maximum_macroblocks_per_second
        if self.stream_type == 0x24:
            hevc_limits = _HEVC_LEVEL_LIMITS.get(self.level_code)
            if hevc_limits is None:
                return None
            _, maximum_luma_sample_rate = hevc_limits
            return width * height * self.frame_rate <= maximum_luma_sample_rate
        return None

    @property
    def h262_frame_rate_extension_conforms(self) -> bool | None:
        """Whether H.262 uses the zero extensions required by defined profiles."""

        if self.stream_type != 0x02:
            return None
        if self.frame_rate_extension_n is None or self.frame_rate_extension_d is None:
            return None
        return self.frame_rate_extension_n == 0 and self.frame_rate_extension_d == 0

    @property
    def h262_level_bit_rate_conforms(self) -> bool | None:
        """Whether the declared H.262 bit rate fits its Main Profile level."""

        if (
            self.stream_type != 0x02
            or self.profile != "Main"
            or self.level_code is None
        ):
            return None
        maximum = _H262_MAIN_PROFILE_MAX_BIT_RATES.get(self.level_code)
        if maximum is None or self.bit_rate is None:
            return None
        return self.bit_rate <= maximum

    @property
    def h262_level_vbv_buffer_conforms(self) -> bool | None:
        """Whether the declared H.262 VBV size fits its Main Profile level."""

        if (
            self.stream_type != 0x02
            or self.profile != "Main"
            or self.level_code is None
        ):
            return None
        maximum = _H262_MAIN_PROFILE_MAX_VBV_BUFFER_SIZES.get(self.level_code)
        if maximum is None or self.vbv_buffer_size is None:
            return None
        return self.vbv_buffer_size <= maximum

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible representation."""

        return {
            "stream_type": self.stream_type,
            "codec": self.codec,
            "width": self.width,
            "height": self.height,
            "display_aspect_ratio": (
                float(self.display_aspect_ratio)
                if self.display_aspect_ratio is not None
                else None
            ),
            "display_aspect_ratio_fraction": (
                f"{self.display_aspect_ratio.numerator}:"
                f"{self.display_aspect_ratio.denominator}"
                if self.display_aspect_ratio is not None
                else None
            ),
            "frame_rate": float(self.frame_rate) if self.frame_rate is not None else None,
            "frame_rate_fraction": (
                f"{self.frame_rate.numerator}/{self.frame_rate.denominator}"
                if self.frame_rate is not None
                else None
            ),
            "progressive": self.progressive,
            "profile": self.profile,
            "level": self.level,
            "chroma_format": self.chroma_format,
            "profile_code": self.profile_code,
            "level_code": self.level_code,
            "frame_rate_is_fixed": self.frame_rate_is_fixed,
            "bit_depth_luma": self.bit_depth_luma,
            "bit_depth_chroma": self.bit_depth_chroma,
            "coded_width": self.coded_width,
            "coded_height": self.coded_height,
            "frame_rate_extension_n": self.frame_rate_extension_n,
            "frame_rate_extension_d": self.frame_rate_extension_d,
            "bit_rate": self.bit_rate,
            "vbv_buffer_size": self.vbv_buffer_size,
            "misp_profile_level": self.misp_profile_level,
            "level_picture_size_conforms": self.level_picture_size_conforms,
            "level_sample_rate_conforms": self.level_sample_rate_conforms,
            "h262_frame_rate_extension_conforms": (
                self.h262_frame_rate_extension_conforms
            ),
            "h262_level_bit_rate_conforms": self.h262_level_bit_rate_conforms,
            "h262_level_vbv_buffer_conforms": self.h262_level_vbv_buffer_conforms,
        }


@dataclass(frozen=True, slots=True)
class _H262SequenceHeader:
    width: int
    height: int
    aspect_ratio_information: int
    frame_rate_code: int
    bit_rate_value: int
    vbv_buffer_size_value: int


def _read_bits(data: bytes, offset: int, width: int) -> int:
    if offset < 0 or width < 0 or offset + width > len(data) * 8:
        raise TruncatedData("bit field extends beyond compressed-video header")
    result = 0
    for bit in range(offset, offset + width):
        result = (result << 1) | ((data[bit // 8] >> (7 - bit % 8)) & 1)
    return result


def _parse_h262_sequence_header(data: bytes) -> _H262SequenceHeader:
    if len(data) < 8:
        raise TruncatedData("H.262 sequence header needs at least 8 bytes")
    width = _read_bits(data, 0, 12)
    height = _read_bits(data, 12, 12)
    aspect = _read_bits(data, 24, 4)
    frame_rate = _read_bits(data, 28, 4)
    bit_rate_value = _read_bits(data, 32, 18)
    vbv_buffer_size_value = _read_bits(data, 51, 10)
    if width == 0 or height == 0:
        raise DecodeError("H.262 sequence dimensions must be non-zero")
    if aspect not in {1, 2, 3, 4}:
        raise DecodeError(f"unsupported H.262 aspect_ratio_information {aspect}")
    if frame_rate not in _H262_FRAME_RATES:
        raise DecodeError(f"reserved H.262 frame_rate_code {frame_rate}")
    if _read_bits(data, 50, 1) != 1:
        raise DecodeError("H.262 sequence header marker_bit must be one")
    return _H262SequenceHeader(
        width,
        height,
        aspect,
        frame_rate,
        bit_rate_value,
        vbv_buffer_size_value,
    )


def _parse_h262_sequence_extension(
    data: bytes,
    header: _H262SequenceHeader,
) -> VideoProperties:
    if len(data) < 6:
        raise TruncatedData("H.262 sequence extension needs at least 6 bytes")
    if _read_bits(data, 0, 4) != 1:
        raise DecodeError("H.262 extension is not a sequence extension")
    profile_and_level = _read_bits(data, 4, 8)
    if profile_and_level & 0x80:
        raise DecodeError("H.262 escape profile/level indication is not supported")
    profile_code = (profile_and_level >> 4) & 0x7
    level_code = profile_and_level & 0xF
    profile = _H262_PROFILES.get(profile_code, f"Reserved ({profile_code})")
    level = _H262_LEVELS.get(level_code, f"Reserved ({level_code})")
    progressive = bool(_read_bits(data, 12, 1))
    chroma_code = _read_bits(data, 13, 2)
    if chroma_code not in _H262_CHROMA_FORMATS:
        raise DecodeError("reserved H.262 chroma_format 0")
    width = header.width | (_read_bits(data, 15, 2) << 12)
    height = header.height | (_read_bits(data, 17, 2) << 12)
    if _read_bits(data, 31, 1) != 1:
        raise DecodeError("H.262 sequence extension marker_bit must be one")
    frame_rate_extension_n = _read_bits(data, 41, 2)
    frame_rate_extension_d = _read_bits(data, 43, 5)
    bit_rate_value = header.bit_rate_value | (_read_bits(data, 19, 12) << 18)
    if bit_rate_value == 0:
        raise DecodeError("H.262 bit_rate must be non-zero")
    bit_rate = bit_rate_value * 400
    vbv_buffer_size_value = header.vbv_buffer_size_value | (
        _read_bits(data, 32, 8) << 10
    )
    vbv_buffer_size = vbv_buffer_size_value * 16 * 1024
    frame_rate = _H262_FRAME_RATES[header.frame_rate_code]
    frame_rate *= Fraction(frame_rate_extension_n + 1, frame_rate_extension_d + 1)
    display_aspect_ratio = _H262_DISPLAY_ASPECT_RATIOS.get(
        header.aspect_ratio_information,
        Fraction(width, height),
    )
    return VideoProperties(
        stream_type=0x02,
        codec="H.262/MPEG-2 Video",
        width=width,
        height=height,
        display_aspect_ratio=display_aspect_ratio,
        frame_rate=frame_rate,
        progressive=progressive,
        profile=profile,
        level=level,
        chroma_format=_H262_CHROMA_FORMATS[chroma_code],
        profile_code=profile_code,
        level_code=level_code,
        bit_depth_luma=8,
        bit_depth_chroma=8,
        frame_rate_extension_n=frame_rate_extension_n,
        frame_rate_extension_d=frame_rate_extension_d,
        bit_rate=bit_rate,
        vbv_buffer_size=vbv_buffer_size,
    )


def _start_codes(data: bytes | bytearray) -> tuple[tuple[int, int], ...]:
    starts: list[tuple[int, int]] = []
    cursor = 0
    while cursor + 3 <= len(data):
        if data[cursor : cursor + 3] == b"\x00\x00\x01":
            starts.append((cursor, 3))
            cursor += 3
        else:
            cursor += 1
    return tuple(starts)


class H262VideoPropertiesParser:
    """Incrementally inspect H.262 sequence headers and extensions."""

    def __init__(self, *, max_unit_size: int = 4 * 1024 * 1024) -> None:
        if isinstance(max_unit_size, bool) or not isinstance(max_unit_size, int):
            raise TypeError("max_unit_size must be an integer")
        if max_unit_size < 8:
            raise ValueError("max_unit_size must be at least 8")
        self.max_unit_size = max_unit_size
        self._buffer = bytearray()
        self._synchronized = False
        self._finished = False
        self._header: _H262SequenceHeader | None = None

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def _process_unit(self, unit: bytes) -> tuple[VideoProperties, ...]:
        if not unit:
            return ()
        if unit[0] == 0xB3:
            self._header = _parse_h262_sequence_header(unit[1:])
            return ()
        if unit[0] != 0xB5 or not unit[1:] or unit[1] >> 4 != 1:
            return ()
        if self._header is None:
            return ()
        return (_parse_h262_sequence_extension(unit[1:], self._header),)

    def feed(self, data: bytes | bytearray | memoryview) -> tuple[VideoProperties, ...]:
        """Consume arbitrary elementary-stream chunks and emit complete properties."""

        if self._finished:
            raise RuntimeError("cannot feed a finished video properties parser")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("video data must be bytes-like")
        self._buffer.extend(data)
        starts = _start_codes(self._buffer)
        if not self._synchronized:
            if not starts:
                if len(self._buffer) > 3:
                    del self._buffer[:-3]
                return ()
            if starts[0][0]:
                del self._buffer[: starts[0][0]]
            self._synchronized = True
            starts = _start_codes(self._buffer)
        if len(starts) < 2:
            if len(self._buffer) > self.max_unit_size + 4:
                raise DecodeError(
                    f"H.262 elementary-stream unit exceeds {self.max_unit_size} bytes"
                )
            return ()
        result: list[VideoProperties] = []
        for index, (start, prefix_length) in enumerate(starts[:-1]):
            end = starts[index + 1][0]
            if end - start - prefix_length > self.max_unit_size:
                raise DecodeError(
                    f"H.262 elementary-stream unit exceeds {self.max_unit_size} bytes"
                )
            result.extend(self._process_unit(bytes(self._buffer[start + prefix_length : end])))
        del self._buffer[: starts[-1][0]]
        return tuple(result)

    def finish(self) -> tuple[VideoProperties, ...]:
        """Process the final bounded start-code unit and finish the parser."""

        if self._finished:
            return ()
        self._finished = True
        if not self._synchronized or not self._buffer:
            self._buffer.clear()
            return ()
        starts = _start_codes(self._buffer)
        if not starts:
            self._buffer.clear()
            return ()
        start, prefix_length = starts[0]
        unit = bytes(self._buffer[start + prefix_length :])
        self._buffer.clear()
        if len(unit) > self.max_unit_size:
            raise DecodeError(
                f"H.262 elementary-stream unit exceeds {self.max_unit_size} bytes"
            )
        return self._process_unit(unit)

    def reset(self) -> None:
        """Discard partial bytes and sequence context at a stream boundary."""

        self._buffer.clear()
        self._synchronized = False
        self._finished = False
        self._header = None


_AVC_HIGH_PROFILES = frozenset(
    {100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135}
)
_AVC_CHROMA_FORMATS = {0: "monochrome", 1: "4:2:0", 2: "4:2:2", 3: "4:4:4"}
_AVC_SAMPLE_ASPECT_RATIOS = {
    1: Fraction(1, 1), 2: Fraction(12, 11), 3: Fraction(10, 11),
    4: Fraction(16, 11), 5: Fraction(40, 33), 6: Fraction(24, 11),
    7: Fraction(20, 11), 8: Fraction(32, 11), 9: Fraction(80, 33),
    10: Fraction(18, 11), 11: Fraction(15, 11), 12: Fraction(64, 33),
    13: Fraction(160, 99), 14: Fraction(4, 3), 15: Fraction(3, 2),
    16: Fraction(2, 1),
}


class _BitReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    def read(self, width: int) -> int:
        value = _read_bits(self.data, self.offset, width)
        self.offset += width
        return value

    def unsigned_exp_golomb(self) -> int:
        leading_zero_bits = 0
        while self.read(1) == 0:
            leading_zero_bits += 1
            if leading_zero_bits > 31:
                raise DecodeError("AVC Exp-Golomb value exceeds 32 bits")
        return (1 << leading_zero_bits) - 1 + self.read(leading_zero_bits)

    def signed_exp_golomb(self) -> int:
        code_num = self.unsigned_exp_golomb()
        magnitude = (code_num + 1) // 2
        return magnitude if code_num & 1 else -magnitude


def _unescape_avc_ebsp(data: bytes) -> bytes:
    result = bytearray()
    cursor = 0
    while cursor < len(data):
        if (
            cursor + 3 < len(data)
            and data[cursor : cursor + 3] == b"\x00\x00\x03"
            and data[cursor + 3] <= 3
        ):
            result.extend(b"\x00\x00")
            cursor += 3
        else:
            result.append(data[cursor])
            cursor += 1
    return bytes(result)


def _skip_avc_scaling_list(reader: _BitReader, size: int) -> None:
    last_scale = 8
    next_scale = 8
    for _ in range(size):
        if next_scale != 0:
            next_scale = (last_scale + reader.signed_exp_golomb() + 256) % 256
        if next_scale != 0:
            last_scale = next_scale


def _parse_avc_vui(
    reader: _BitReader, *, width: int, height: int
) -> tuple[Fraction | None, Fraction | None, bool | None]:
    display_aspect_ratio: Fraction | None = None
    if reader.read(1):
        aspect_ratio_idc = reader.read(8)
        if aspect_ratio_idc == 255:
            sar_width = reader.read(16)
            sar_height = reader.read(16)
            if sar_width and sar_height:
                display_aspect_ratio = Fraction(width * sar_width, height * sar_height)
        elif aspect_ratio_idc in _AVC_SAMPLE_ASPECT_RATIOS:
            display_aspect_ratio = Fraction(width, height) * _AVC_SAMPLE_ASPECT_RATIOS[
                aspect_ratio_idc
            ]
    if reader.read(1):
        reader.read(1)  # overscan_appropriate_flag
    if reader.read(1):
        reader.read(3)
        reader.read(1)
        if reader.read(1):
            reader.read(24)
    if reader.read(1):
        reader.unsigned_exp_golomb()
        reader.unsigned_exp_golomb()
    frame_rate: Fraction | None = None
    frame_rate_is_fixed: bool | None = None
    if reader.read(1):
        num_units_in_tick = reader.read(32)
        time_scale = reader.read(32)
        frame_rate_is_fixed = bool(reader.read(1))
        if num_units_in_tick:
            frame_rate = Fraction(time_scale, 2 * num_units_in_tick)
    return display_aspect_ratio, frame_rate, frame_rate_is_fixed


def _avc_profile_name(profile_idc: int, constraint_flags: int) -> str:
    if profile_idc == 66:
        return "Constrained Baseline" if constraint_flags & 0x40 else "Baseline"
    return {77: "Main", 100: "High"}.get(profile_idc, f"Profile {profile_idc}")


def _avc_level_name(profile_idc: int, level_idc: int, constraint_flags: int) -> str:
    if level_idc == 9 and profile_idc == 100:
        return "1b"
    if level_idc == 11 and profile_idc in {66, 77} and constraint_flags & 0x10:
        return "1b"
    return f"{level_idc // 10}.{level_idc % 10}"


def _parse_avc_sps(ebsp: bytes) -> VideoProperties:
    reader = _BitReader(_unescape_avc_ebsp(ebsp))
    profile_idc = reader.read(8)
    constraint_flags = reader.read(8)
    if constraint_flags & 0x3:
        raise DecodeError("AVC SPS reserved_zero_2bits must be zero")
    level_idc = reader.read(8)
    reader.unsigned_exp_golomb()
    chroma_format_idc = 1
    separate_colour_plane = False
    bit_depth_luma = 8
    bit_depth_chroma = 8
    if profile_idc in _AVC_HIGH_PROFILES:
        chroma_format_idc = reader.unsigned_exp_golomb()
        if chroma_format_idc > 3:
            raise DecodeError(f"reserved AVC chroma_format_idc {chroma_format_idc}")
        if chroma_format_idc == 3:
            separate_colour_plane = bool(reader.read(1))
        bit_depth_luma = reader.unsigned_exp_golomb() + 8
        bit_depth_chroma = reader.unsigned_exp_golomb() + 8
        reader.read(1)
        if reader.read(1):
            scaling_count = 8 if chroma_format_idc != 3 else 12
            for index in range(scaling_count):
                if reader.read(1):
                    _skip_avc_scaling_list(reader, 16 if index < 6 else 64)
    reader.unsigned_exp_golomb()
    pic_order_cnt_type = reader.unsigned_exp_golomb()
    if pic_order_cnt_type == 0:
        reader.unsigned_exp_golomb()
    elif pic_order_cnt_type == 1:
        reader.read(1)
        reader.signed_exp_golomb()
        reader.signed_exp_golomb()
        for _ in range(reader.unsigned_exp_golomb()):
            reader.signed_exp_golomb()
    elif pic_order_cnt_type > 2:
        raise DecodeError(f"reserved AVC pic_order_cnt_type {pic_order_cnt_type}")
    reader.unsigned_exp_golomb()
    reader.read(1)
    width_in_mbs_minus1 = reader.unsigned_exp_golomb()
    height_in_map_units_minus1 = reader.unsigned_exp_golomb()
    frame_mbs_only = bool(reader.read(1))
    if not frame_mbs_only:
        reader.read(1)
    reader.read(1)
    crop_left = crop_right = crop_top = crop_bottom = 0
    if reader.read(1):
        crop_left = reader.unsigned_exp_golomb()
        crop_right = reader.unsigned_exp_golomb()
        crop_top = reader.unsigned_exp_golomb()
        crop_bottom = reader.unsigned_exp_golomb()
    chroma_array_type = 0 if separate_colour_plane else chroma_format_idc
    sub_width = {0: 1, 1: 2, 2: 2, 3: 1}[chroma_array_type]
    sub_height = {0: 1, 1: 2, 2: 1, 3: 1}[chroma_array_type]
    coded_width = (width_in_mbs_minus1 + 1) * 16
    coded_height = (2 - int(frame_mbs_only)) * (height_in_map_units_minus1 + 1) * 16
    width = coded_width - sub_width * (crop_left + crop_right)
    height = coded_height
    height -= sub_height * (2 - int(frame_mbs_only)) * (crop_top + crop_bottom)
    if width <= 0 or height <= 0:
        raise DecodeError("AVC SPS cropping produces non-positive dimensions")
    display_aspect_ratio: Fraction | None = None
    frame_rate: Fraction | None = None
    frame_rate_is_fixed: bool | None = None
    if reader.read(1):
        display_aspect_ratio, frame_rate, frame_rate_is_fixed = _parse_avc_vui(
            reader, width=width, height=height
        )
    return VideoProperties(
        stream_type=0x1B,
        codec="H.264/AVC",
        width=width,
        height=height,
        display_aspect_ratio=display_aspect_ratio,
        frame_rate=frame_rate,
        progressive=frame_mbs_only,
        profile=_avc_profile_name(profile_idc, constraint_flags),
        level=_avc_level_name(profile_idc, level_idc, constraint_flags),
        chroma_format=_AVC_CHROMA_FORMATS[chroma_format_idc],
        profile_code=profile_idc,
        level_code=level_idc,
        frame_rate_is_fixed=frame_rate_is_fixed,
        bit_depth_luma=bit_depth_luma,
        bit_depth_chroma=bit_depth_chroma,
        coded_width=coded_width,
        coded_height=coded_height,
    )


class AVCVideoPropertiesParser:
    """Incrementally inspect Annex-B AVC/H.264 sequence parameter sets."""

    def __init__(self, *, max_unit_size: int = 4 * 1024 * 1024) -> None:
        if isinstance(max_unit_size, bool) or not isinstance(max_unit_size, int):
            raise TypeError("max_unit_size must be an integer")
        if max_unit_size < 4:
            raise ValueError("max_unit_size must be at least 4")
        self.max_unit_size = max_unit_size
        self._buffer = bytearray()
        self._synchronized = False
        self._finished = False

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def _process_unit(self, unit: bytes) -> tuple[VideoProperties, ...]:
        if not unit or unit[0] & 0x80 or unit[0] & 0x1F != 7:
            return ()
        return (_parse_avc_sps(unit[1:]),)

    def feed(self, data: bytes | bytearray | memoryview) -> tuple[VideoProperties, ...]:
        """Consume arbitrary elementary-stream chunks and emit decoded SPS records."""

        if self._finished:
            raise RuntimeError("cannot feed a finished video properties parser")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("video data must be bytes-like")
        self._buffer.extend(data)
        starts = _start_codes(self._buffer)
        if not self._synchronized:
            if not starts:
                if len(self._buffer) > 3:
                    del self._buffer[:-3]
                return ()
            if starts[0][0]:
                del self._buffer[: starts[0][0]]
            self._synchronized = True
            starts = _start_codes(self._buffer)
        if len(starts) < 2:
            if len(self._buffer) > self.max_unit_size + 4:
                raise DecodeError(f"AVC elementary-stream unit exceeds {self.max_unit_size} bytes")
            return ()
        result: list[VideoProperties] = []
        for index, (start, prefix_length) in enumerate(starts[:-1]):
            end = starts[index + 1][0]
            if end - start - prefix_length > self.max_unit_size:
                raise DecodeError(f"AVC elementary-stream unit exceeds {self.max_unit_size} bytes")
            result.extend(self._process_unit(bytes(self._buffer[start + prefix_length : end])))
        del self._buffer[: starts[-1][0]]
        return tuple(result)

    def finish(self) -> tuple[VideoProperties, ...]:
        """Process the final bounded NAL unit and finish the parser."""

        if self._finished:
            return ()
        self._finished = True
        if not self._synchronized or not self._buffer:
            self._buffer.clear()
            return ()
        starts = _start_codes(self._buffer)
        if not starts:
            self._buffer.clear()
            return ()
        start, prefix_length = starts[0]
        unit = bytes(self._buffer[start + prefix_length :])
        self._buffer.clear()
        if len(unit) > self.max_unit_size:
            raise DecodeError(f"AVC elementary-stream unit exceeds {self.max_unit_size} bytes")
        return self._process_unit(unit)

    def reset(self) -> None:
        """Discard partial bytes at a stream boundary."""

        self._buffer.clear()
        self._synchronized = False
        self._finished = False


def _parse_hevc_profile_tier_level(
    reader: _BitReader,
    max_sub_layers_minus1: int,
) -> tuple[int, int, bool | None]:
    reader.read(2)  # general_profile_space
    reader.read(1)  # general_tier_flag
    profile_idc = reader.read(5)
    reader.read(32)  # general_profile_compatibility_flags
    progressive_source = bool(reader.read(1))
    interlaced_source = bool(reader.read(1))
    reader.read(1)  # general_non_packed_constraint_flag
    reader.read(1)  # general_frame_only_constraint_flag
    reader.read(44)  # remaining general constraint indicator flags
    level_idc = reader.read(8)
    sub_layer_profile_present: list[bool] = []
    sub_layer_level_present: list[bool] = []
    for _ in range(max_sub_layers_minus1):
        sub_layer_profile_present.append(bool(reader.read(1)))
        sub_layer_level_present.append(bool(reader.read(1)))
    if max_sub_layers_minus1:
        for _ in range(max_sub_layers_minus1, 8):
            if reader.read(2) != 0:
                raise DecodeError("HEVC reserved_zero_2bits must be zero")
    for profile_present, level_present in zip(
        sub_layer_profile_present, sub_layer_level_present, strict=True
    ):
        if profile_present:
            reader.read(88)
        if level_present:
            reader.read(8)
    progressive: bool | None
    if progressive_source and not interlaced_source:
        progressive = True
    elif interlaced_source and not progressive_source:
        progressive = False
    else:
        progressive = None
    return profile_idc, level_idc, progressive


def _hevc_level_name(level_idc: int) -> str:
    whole, remainder = divmod(level_idc, 30)
    if remainder == 0:
        return str(whole)
    if remainder in {3, 6}:
        return f"{whole}.{remainder // 3}"
    return f"Level IDC {level_idc}"


def _skip_hevc_scaling_list(reader: _BitReader) -> None:
    for size_id in range(4):
        matrix_step = 3 if size_id == 3 else 1
        for _matrix_id in range(0, 6, matrix_step):
            if not reader.read(1):
                reader.unsigned_exp_golomb()  # scaling_list_pred_matrix_id_delta
                continue
            coefficient_count = min(64, 1 << (4 + (size_id << 1)))
            if size_id > 1:
                reader.signed_exp_golomb()  # scaling_list_dc_coef_minus8
            for _ in range(coefficient_count):
                reader.signed_exp_golomb()  # scaling_list_delta_coef


def _skip_hevc_short_term_reference_sets(
    reader: _BitReader,
    count: int,
) -> None:
    delta_poc_counts: list[int] = []
    for index in range(count):
        predicted = index > 0 and bool(reader.read(1))
        if predicted:
            reader.read(1)  # delta_rps_sign
            reader.unsigned_exp_golomb()  # abs_delta_rps_minus1
            retained = 0
            for _ in range(delta_poc_counts[index - 1] + 1):
                used = bool(reader.read(1))
                use_delta = used or bool(reader.read(1))
                retained += int(use_delta)
            delta_poc_counts.append(retained)
            continue
        negative = reader.unsigned_exp_golomb()
        positive = reader.unsigned_exp_golomb()
        if negative + positive > 64:
            raise DecodeError("HEVC short-term reference picture set exceeds 64 pictures")
        for _ in range(negative + positive):
            reader.unsigned_exp_golomb()  # delta_poc_s0/s1_minus1
            reader.read(1)  # used_by_curr_pic_s0/s1_flag
        delta_poc_counts.append(negative + positive)


def _parse_hevc_vui(
    reader: _BitReader,
    *,
    width: int,
    height: int,
) -> tuple[Fraction | None, Fraction | None]:
    display_aspect_ratio: Fraction | None = None
    if reader.read(1):
        aspect_ratio_idc = reader.read(8)
        sample_aspect_ratio: Fraction | None = None
        if aspect_ratio_idc == 255:
            sar_width = reader.read(16)
            sar_height = reader.read(16)
            if sar_width and sar_height:
                sample_aspect_ratio = Fraction(sar_width, sar_height)
        elif aspect_ratio_idc in _AVC_SAMPLE_ASPECT_RATIOS:
            sample_aspect_ratio = _AVC_SAMPLE_ASPECT_RATIOS[aspect_ratio_idc]
        if sample_aspect_ratio is not None:
            display_aspect_ratio = Fraction(width, height) * sample_aspect_ratio
    if reader.read(1):
        reader.read(1)  # overscan_appropriate_flag
    if reader.read(1):
        reader.read(3)  # video_format
        reader.read(1)  # video_full_range_flag
        if reader.read(1):
            reader.read(24)  # colour description
    if reader.read(1):
        reader.unsigned_exp_golomb()  # chroma_sample_loc_type_top_field
        reader.unsigned_exp_golomb()  # chroma_sample_loc_type_bottom_field
    reader.read(1)  # neutral_chroma_indication_flag
    reader.read(1)  # field_seq_flag
    reader.read(1)  # frame_field_info_present_flag
    if reader.read(1):
        for _ in range(4):
            reader.unsigned_exp_golomb()  # default display window offsets
    frame_rate: Fraction | None = None
    if reader.read(1):
        num_units_in_tick = reader.read(32)
        time_scale = reader.read(32)
        if not num_units_in_tick or not time_scale:
            raise DecodeError("HEVC VUI timing values must be non-zero")
        poc_proportional = bool(reader.read(1))
        ticks_per_poc = reader.unsigned_exp_golomb() + 1 if poc_proportional else 1
        frame_rate = Fraction(time_scale, num_units_in_tick * ticks_per_poc)
    return display_aspect_ratio, frame_rate


def _parse_hevc_sps(ebsp: bytes) -> VideoProperties:
    reader = _BitReader(_unescape_avc_ebsp(ebsp))
    reader.read(4)  # sps_video_parameter_set_id
    max_sub_layers_minus1 = reader.read(3)
    reader.read(1)  # sps_temporal_id_nesting_flag
    profile_idc, level_idc, progressive = _parse_hevc_profile_tier_level(
        reader, max_sub_layers_minus1
    )
    reader.unsigned_exp_golomb()  # sps_seq_parameter_set_id
    chroma_format_idc = reader.unsigned_exp_golomb()
    if chroma_format_idc > 3:
        raise DecodeError(f"reserved HEVC chroma_format_idc {chroma_format_idc}")
    separate_colour_plane = False
    if chroma_format_idc == 3:
        separate_colour_plane = bool(reader.read(1))
    coded_width = reader.unsigned_exp_golomb()
    coded_height = reader.unsigned_exp_golomb()
    crop_left = crop_right = crop_top = crop_bottom = 0
    if reader.read(1):
        crop_left = reader.unsigned_exp_golomb()
        crop_right = reader.unsigned_exp_golomb()
        crop_top = reader.unsigned_exp_golomb()
        crop_bottom = reader.unsigned_exp_golomb()
    chroma_array_type = 0 if separate_colour_plane else chroma_format_idc
    sub_width = {0: 1, 1: 2, 2: 2, 3: 1}[chroma_array_type]
    sub_height = {0: 1, 1: 2, 2: 1, 3: 1}[chroma_array_type]
    width = coded_width - sub_width * (crop_left + crop_right)
    height = coded_height - sub_height * (crop_top + crop_bottom)
    if width <= 0 or height <= 0:
        raise DecodeError("HEVC SPS conformance window produces non-positive dimensions")
    bit_depth_luma = reader.unsigned_exp_golomb() + 8
    bit_depth_chroma = reader.unsigned_exp_golomb() + 8
    log2_max_pic_order_cnt_lsb_minus4 = reader.unsigned_exp_golomb()
    if log2_max_pic_order_cnt_lsb_minus4 > 12:
        raise DecodeError("HEVC log2_max_pic_order_cnt_lsb_minus4 exceeds 12")
    ordering_info_present = bool(reader.read(1))
    ordering_start = 0 if ordering_info_present else max_sub_layers_minus1
    for _ in range(ordering_start, max_sub_layers_minus1 + 1):
        reader.unsigned_exp_golomb()  # sps_max_dec_pic_buffering_minus1
        reader.unsigned_exp_golomb()  # sps_max_num_reorder_pics
        reader.unsigned_exp_golomb()  # sps_max_latency_increase_plus1
    for _ in range(6):
        reader.unsigned_exp_golomb()  # coding/transform block and hierarchy sizes
    if reader.read(1) and reader.read(1):
        _skip_hevc_scaling_list(reader)
    reader.read(1)  # amp_enabled_flag
    reader.read(1)  # sample_adaptive_offset_enabled_flag
    if reader.read(1):
        reader.read(4)  # pcm_sample_bit_depth_luma_minus1
        reader.read(4)  # pcm_sample_bit_depth_chroma_minus1
        reader.unsigned_exp_golomb()  # log2_min_pcm_luma_coding_block_size_minus3
        reader.unsigned_exp_golomb()  # log2_diff_max_min_pcm_luma_coding_block_size
        reader.read(1)  # pcm_loop_filter_disabled_flag
    short_term_reference_sets = reader.unsigned_exp_golomb()
    if short_term_reference_sets > 64:
        raise DecodeError("HEVC num_short_term_ref_pic_sets exceeds 64")
    _skip_hevc_short_term_reference_sets(reader, short_term_reference_sets)
    if reader.read(1):
        long_term_reference_pictures = reader.unsigned_exp_golomb()
        if long_term_reference_pictures > 32:
            raise DecodeError("HEVC num_long_term_ref_pics_sps exceeds 32")
        poc_lsb_width = log2_max_pic_order_cnt_lsb_minus4 + 4
        for _ in range(long_term_reference_pictures):
            reader.read(poc_lsb_width)
            reader.read(1)  # used_by_curr_pic_lt_sps_flag
    reader.read(1)  # sps_temporal_mvp_enabled_flag
    reader.read(1)  # strong_intra_smoothing_enabled_flag
    display_aspect_ratio: Fraction | None = None
    frame_rate: Fraction | None = None
    if reader.read(1):
        display_aspect_ratio, frame_rate = _parse_hevc_vui(
            reader,
            width=width,
            height=height,
        )
    profile = {1: "Main", 2: "Main 10", 3: "Main Still Picture"}.get(
        profile_idc, f"Profile {profile_idc}"
    )
    chroma_format = _AVC_CHROMA_FORMATS[chroma_format_idc]
    return VideoProperties(
        stream_type=0x24,
        codec="H.265/HEVC",
        width=width,
        height=height,
        display_aspect_ratio=display_aspect_ratio,
        frame_rate=frame_rate,
        progressive=progressive,
        profile=profile,
        level=_hevc_level_name(level_idc),
        chroma_format=chroma_format,
        profile_code=profile_idc,
        level_code=level_idc,
        bit_depth_luma=bit_depth_luma,
        bit_depth_chroma=bit_depth_chroma,
        coded_width=coded_width,
        coded_height=coded_height,
    )


class HEVCVideoPropertiesParser:
    """Incrementally inspect Annex-B H.265/HEVC sequence parameter sets."""

    def __init__(self, *, max_unit_size: int = 4 * 1024 * 1024) -> None:
        if isinstance(max_unit_size, bool) or not isinstance(max_unit_size, int):
            raise TypeError("max_unit_size must be an integer")
        if max_unit_size < 5:
            raise ValueError("max_unit_size must be at least 5")
        self.max_unit_size = max_unit_size
        self._buffer = bytearray()
        self._synchronized = False
        self._finished = False

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def _process_unit(self, unit: bytes) -> tuple[VideoProperties, ...]:
        if len(unit) < 2 or unit[0] & 0x80 or (unit[0] >> 1) & 0x3F != 33:
            return ()
        if unit[1] & 0x7 == 0:
            raise DecodeError("HEVC nuh_temporal_id_plus1 must be non-zero")
        return (_parse_hevc_sps(unit[2:]),)

    def feed(self, data: bytes | bytearray | memoryview) -> tuple[VideoProperties, ...]:
        """Consume arbitrary elementary-stream chunks and emit decoded SPS records."""

        if self._finished:
            raise RuntimeError("cannot feed a finished video properties parser")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("video data must be bytes-like")
        self._buffer.extend(data)
        starts = _start_codes(self._buffer)
        if not self._synchronized:
            if not starts:
                if len(self._buffer) > 3:
                    del self._buffer[:-3]
                return ()
            if starts[0][0]:
                del self._buffer[: starts[0][0]]
            self._synchronized = True
            starts = _start_codes(self._buffer)
        if len(starts) < 2:
            if len(self._buffer) > self.max_unit_size + 4:
                raise DecodeError(f"HEVC elementary-stream unit exceeds {self.max_unit_size} bytes")
            return ()
        result: list[VideoProperties] = []
        for index, (start, prefix_length) in enumerate(starts[:-1]):
            end = starts[index + 1][0]
            if end - start - prefix_length > self.max_unit_size:
                raise DecodeError(f"HEVC elementary-stream unit exceeds {self.max_unit_size} bytes")
            result.extend(self._process_unit(bytes(self._buffer[start + prefix_length : end])))
        del self._buffer[: starts[-1][0]]
        return tuple(result)

    def finish(self) -> tuple[VideoProperties, ...]:
        """Process the final bounded NAL unit and finish the parser."""

        if self._finished:
            return ()
        self._finished = True
        if not self._synchronized or not self._buffer:
            self._buffer.clear()
            return ()
        starts = _start_codes(self._buffer)
        if not starts:
            self._buffer.clear()
            return ()
        start, prefix_length = starts[0]
        unit = bytes(self._buffer[start + prefix_length :])
        self._buffer.clear()
        if len(unit) > self.max_unit_size:
            raise DecodeError(f"HEVC elementary-stream unit exceeds {self.max_unit_size} bytes")
        return self._process_unit(unit)

    def reset(self) -> None:
        """Discard partial bytes at a stream boundary."""

        self._buffer.clear()
        self._synchronized = False
        self._finished = False
