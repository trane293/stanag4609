"""MISB ST 1202.3 generalized image-transformation Local Set codec."""

from __future__ import annotations

import math
import struct
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from stanag4609.errors import DecodeError
from stanag4609.klv.ber import encode_ber_length, encode_ber_oid
from stanag4609.klv.local_set import parse_local_set
from stanag4609.klv.model import LocalSet, LocalSetItem
from stanag4609.st1010 import SDCCFLP, decode_sdcc_flp, encode_sdcc_flp

GENERALIZED_TRANSFORMATION_KEY = bytes.fromhex(
    "06 0E 2B 34 02 0B 01 01 0E 01 03 05 05 00 00 00"
)
ST1202_SDCC_SOURCE_TAGS = tuple(range(1, 9))

_COEFFICIENT_NAMES = (
    "x Equation Numerator - x factor",
    "x Equation Numerator - y factor",
    "x Equation Numerator - Constant factor",
    "y Equation Numerator - x factor",
    "y Equation Numerator - y factor",
    "y Equation Numerator - Constant factor",
    "Denominator - x factor",
    "Denominator - y factor",
)


class TransformationType(IntEnum):
    """Transformation enumeration from ST 1202.3 Table 1."""

    NO_DEFINED_TRANSFORMATION = 0
    CHIPPING = 1
    CHILD_PARENT = 2
    DEFAULT_PIXEL_TO_IMAGE = 3
    OPTICAL = 4


@dataclass(frozen=True, slots=True)
class RawTransformationValue:
    """Opaque value for a future ST 1202 extension tag."""

    data: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes):
            raise TypeError("RawTransformationValue data must be bytes")


@dataclass(frozen=True, slots=True)
class TransformationField:
    """A decoded ST 1202 item with its original wire representation."""

    tag: int
    name: str
    value: Any
    raw: bytes
    item: LocalSetItem

    @property
    def width(self) -> int:
        return len(self.raw)


@dataclass(frozen=True, slots=True)
class GeneralizedTransformation:
    """One embedded ST 1202 generalized projective transformation.

    Missing coefficient items have the standard-defined value zero. Consequently,
    the default object is an identity transform.
    """

    a: float = 0.0
    b: float = 0.0
    c: float = 0.0
    d: float = 0.0
    e: float = 0.0
    f: float = 0.0
    g: float = 0.0
    h: float = 0.0
    document_version: int = 3
    transformation_type: TransformationType = TransformationType.NO_DEFINED_TRANSFORMATION
    uncertainty: SDCCFLP | None = None
    extensions: Mapping[int, RawTransformationValue] = field(default_factory=dict)
    local_set: LocalSet | None = field(default=None, compare=False, repr=False)
    fields: tuple[TransformationField, ...] = field(default=(), compare=False, repr=False)

    def __post_init__(self) -> None:
        for name, value in zip("abcdefgh", self.coefficients, strict=True):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"coefficient {name} must be numeric")
            if not math.isfinite(float(value)):
                raise ValueError(f"coefficient {name} must be finite")
        if isinstance(self.document_version, bool) or not isinstance(self.document_version, int):
            raise TypeError("ST 1202 Document Version must be an integer")
        if self.document_version < 1:
            raise ValueError("ST 1202 Document Version must be positive")
        if not isinstance(self.transformation_type, TransformationType):
            raise TypeError("transformation_type must be a TransformationType")
        if self.uncertainty is not None and not isinstance(self.uncertainty, SDCCFLP):
            raise TypeError("uncertainty must be an SDCCFLP or None")
        if not isinstance(self.extensions, Mapping):
            raise TypeError("extensions must be a mapping")

    @property
    def coefficients(self) -> tuple[float, ...]:
        values = (self.a, self.b, self.c, self.d, self.e, self.f, self.g, self.h)
        return tuple(float(value) for value in values)

    @property
    def unknown_items(self) -> tuple[tuple[int, bytes], ...]:
        return tuple((tag, value.data) for tag, value in self.extensions.items())

    @classmethod
    def for_chipping(
        cls,
        *,
        scale_factor: float,
        center_line: float,
        center_sample: float,
        chip_height: float,
        chip_width: float,
        document_version: int = 3,
    ) -> GeneralizedTransformation:
        """Construct the Chipping Transformation from ST 1202 Equations 6-13."""
        scale = _positive_finite(scale_factor, name="scale_factor")
        line, sample = _finite_point(center_line, center_sample)
        height = _positive_finite(chip_height, name="chip_height")
        width = _positive_finite(chip_width, name="chip_width")
        inverse_scale = _finite_reciprocal(scale, name="scale_factor")
        coefficient = 1.0 - inverse_scale
        return cls(
            a=coefficient,
            c=line - inverse_scale * height / 2.0,
            e=coefficient,
            f=sample - inverse_scale * width / 2.0,
            document_version=document_version,
            transformation_type=TransformationType.CHIPPING,
        )

    @classmethod
    def for_digital_zoom(
        cls,
        *,
        scale_factor: float,
        image_height: float,
        image_width: float,
        document_version: int = 3,
    ) -> GeneralizedTransformation:
        """Construct the centered Digital Zoom from ST 1202 Equations 15-22."""
        scale = _positive_finite(scale_factor, name="scale_factor")
        height = _positive_finite(image_height, name="image_height")
        width = _positive_finite(image_width, name="image_width")
        inverse_scale = _finite_reciprocal(scale, name="scale_factor")
        coefficient = 1.0 - inverse_scale
        return cls(
            a=coefficient,
            c=height * coefficient / 2.0,
            e=coefficient,
            f=width * coefficient / 2.0,
            document_version=document_version,
            transformation_type=TransformationType.CHIPPING,
        )

    @classmethod
    def for_csm_pixel_to_image(
        cls,
        *,
        pixel_size_x: float,
        pixel_size_y: float,
        image_height: float,
        image_width: float,
        document_version: int = 3,
    ) -> GeneralizedTransformation:
        """Construct the CSM DPIT from ST 1202 Equations 26-33."""
        size_x = _positive_finite(pixel_size_x, name="pixel_size_x")
        size_y = _positive_finite(pixel_size_y, name="pixel_size_y")
        height = _positive_finite(image_height, name="image_height")
        width = _positive_finite(image_width, name="image_width")
        return cls(
            a=1.0,
            b=size_x,
            c=-size_x * width / 2.0,
            d=-size_y,
            e=1.0,
            f=size_y * height / 2.0,
            document_version=document_version,
            transformation_type=TransformationType.DEFAULT_PIXEL_TO_IMAGE,
        )

    def transform(self, x: float, y: float) -> tuple[float, float]:
        """Apply ST 1202 Equations 1 and 2."""
        x_value, y_value = _finite_point(x, y)
        a, b, c, d, e, f, g, h = self.coefficients
        denominator = g * x_value + h * y_value + 1.0
        if denominator == 0.0:
            raise ZeroDivisionError("ST 1202 transformation maps the point to infinity")
        return (
            ((1.0 - a) * x_value + b * y_value + c) / denominator,
            (d * x_value + (1.0 - e) * y_value + f) / denominator,
        )

    def inverse_transform(self, x: float, y: float) -> tuple[float, float]:
        """Apply the inverse projective transformation (Equations 3 and 4)."""
        x_value, y_value = _finite_point(x, y)
        a, b, c, d, e, f, g, h = self.coefficients
        m00, m01, m02 = 1.0 - a, b, c
        m10, m11, m12 = d, 1.0 - e, f
        determinant = (
            m00 * (m11 - m12 * h)
            - m01 * (m10 - m12 * g)
            + m02 * (m10 * h - m11 * g)
        )
        if determinant == 0.0:
            raise ValueError("ST 1202 transformation is singular")
        numerator_x = (
            (m11 - m12 * h) * x_value
            + (m02 * h - m01) * y_value
            + (m01 * m12 - m02 * m11)
        )
        numerator_y = (
            (m12 * g - m10) * x_value
            + (m00 - m02 * g) * y_value
            + (m02 * m10 - m00 * m12)
        )
        denominator = (
            (m10 * h - m11 * g) * x_value
            + (m01 * g - m00 * h) * y_value
            + (m00 * m11 - m01 * m10)
        )
        if denominator == 0.0:
            raise ZeroDivisionError("ST 1202 inverse transformation maps the point to infinity")
        return numerator_x / denominator, numerator_y / denominator


def apply_transformation_sequence(
    transformations: Iterable[GeneralizedTransformation],
    x: float,
    y: float,
    *,
    inverse: bool = False,
) -> tuple[float, float]:
    """Apply the ordered ST 1202 image transformation chain.

    Section 6.3 defines the image-to-ground order as Chipping,
    Child-Parent, Default Pixel-to-Image, then Optical. The inverse
    ground-to-image operation applies each inverse in reverse order.
    """
    if not isinstance(inverse, bool):
        raise TypeError("inverse must be a boolean")
    sequence = tuple(transformations)
    for transformation in sequence:
        if not isinstance(transformation, GeneralizedTransformation):
            raise TypeError(
                "transformations must contain GeneralizedTransformation values"
            )
        if (
            transformation.transformation_type
            is TransformationType.NO_DEFINED_TRANSFORMATION
        ):
            raise ValueError(
                "a transformation sequence requires a defined production type"
            )
    types = tuple(value.transformation_type for value in sequence)
    if len(set(types)) != len(types):
        raise ValueError("ST 1202 transformation types must be unique in a sequence")
    if types != tuple(sorted(types, key=int)):
        raise ValueError(
            "ST 1202 transformations must follow the Section 6.3 image-to-ground order"
        )

    point = _finite_point(x, y)
    ordered = reversed(sequence) if inverse else sequence
    for transformation in ordered:
        point = (
            transformation.inverse_transform(*point)
            if inverse
            else transformation.transform(*point)
        )
    return point


def _finite_point(x: float, y: float) -> tuple[float, float]:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise TypeError("x must be numeric")
    if isinstance(y, bool) or not isinstance(y, (int, float)):
        raise TypeError("y must be numeric")
    result = float(x), float(y)
    if not all(math.isfinite(value) for value in result):
        raise ValueError("coordinates must be finite")
    return result


def _positive_finite(value: float, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise ValueError(f"{name} must be positive and finite")
    return result


def _finite_reciprocal(value: float, *, name: str) -> float:
    result = 1.0 / value
    if not math.isfinite(result):
        raise ValueError(f"{name} is too small to represent its reciprocal")
    return result


def _decode_float(data: bytes) -> float:
    try:
        format_code = {2: ">e", 4: ">f", 8: ">d"}[len(data)]
    except KeyError as error:
        raise DecodeError("ST 1202 float values must contain 2, 4, or 8 bytes") from error
    value = float(struct.unpack(format_code, data)[0])
    if not math.isfinite(value):
        raise DecodeError("ST 1202 float values must be finite")
    return value


def _decode_uint(data: bytes, *, name: str) -> int:
    if not data:
        raise DecodeError(f"ST 1202 {name} is empty")
    if len(data) > 1 and data[0] == 0:
        raise DecodeError(f"ST 1202 {name} must use a minimal unsigned integer")
    return int.from_bytes(data, "big")


def _minimal_uint(value: int, *, name: str) -> bytes:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"ST 1202 {name} must be an integer")
    if value < 0:
        raise ValueError(f"ST 1202 {name} must be non-negative")
    return value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")


def decode_generalized_transformation(data: bytes) -> GeneralizedTransformation:
    """Decode an embedded ST 1202.3 Local Set value."""
    if not isinstance(data, bytes):
        raise TypeError("ST 1202 data must be bytes")
    local_set = parse_local_set(data)
    seen: set[int] = set()
    decoded_fields: list[TransformationField] = []
    coefficient_values = [0.0] * 8
    document_version: int | None = None
    transformation_type = TransformationType.NO_DEFINED_TRANSFORMATION
    uncertainty: SDCCFLP | None = None
    extensions: dict[int, RawTransformationValue] = {}
    for index, item in enumerate(local_set.items):
        if item.tag in seen:
            raise DecodeError(f"duplicate ST 1202 tag {item.tag}")
        seen.add(item.tag)
        if 1 <= item.tag <= 8:
            value: Any = _decode_float(item.value)
            coefficient_values[item.tag - 1] = value
            name = _COEFFICIENT_NAMES[item.tag - 1]
        elif item.tag == 9:
            uncertainty = decode_sdcc_flp(item.value, require_mode=2)
            if uncertainty.matrix_size > 8:
                raise DecodeError("ST 1202 SDCC matrix exceeds its eight-item Source List")
            preceding = local_set.items[max(0, index - uncertainty.matrix_size) : index]
            source_tags = tuple(source.tag for source in preceding)
            if len(source_tags) != uncertainty.matrix_size or any(
                tag not in ST1202_SDCC_SOURCE_TAGS for tag in source_tags
            ):
                raise DecodeError("ST 1202 SDCC must immediately follow its Refined Source List")
            uncertainty = SDCCFLP(
                uncertainty.matrix_size,
                uncertainty.parse_control,
                uncertainty.standard_deviations,
                uncertainty.correlation_coefficients,
                uncertainty.standard_deviation_imap_bounds,
                source_tags,
            )
            value = uncertainty
            name = "Standard Deviation and Correlation Coefficients"
        elif item.tag == 10:
            document_version = _decode_uint(item.value, name="Document Version")
            if document_version < 1:
                raise DecodeError("ST 1202 Document Version must be positive")
            value = document_version
            name = "Document Version"
        elif item.tag == 11:
            raw_enumeration = _decode_uint(item.value, name="Transformation Enumeration")
            try:
                transformation_type = TransformationType(raw_enumeration)
            except ValueError as error:
                message = f"unknown ST 1202 transformation enumeration {raw_enumeration}"
                raise DecodeError(message) from error
            value = transformation_type
            name = "Transformation Enumeration"
        else:
            raw = RawTransformationValue(item.value)
            extensions[item.tag] = raw
            value = raw
            name = f"Unknown Tag {item.tag}"
        decoded_fields.append(TransformationField(item.tag, name, value, item.value, item))
    if document_version is None:
        raise DecodeError("ST 1202 Document Version item 10 is required")
    return GeneralizedTransformation(
        a=coefficient_values[0],
        b=coefficient_values[1],
        c=coefficient_values[2],
        d=coefficient_values[3],
        e=coefficient_values[4],
        f=coefficient_values[5],
        g=coefficient_values[6],
        h=coefficient_values[7],
        document_version=document_version,
        transformation_type=transformation_type,
        uncertainty=uncertainty,
        extensions=extensions,
        local_set=local_set,
        fields=tuple(decoded_fields),
    )


def _item(tag: int, value: bytes) -> bytes:
    return encode_ber_oid(tag) + encode_ber_length(len(value)) + value


def encode_generalized_transformation(
    value: GeneralizedTransformation,
    *,
    float_width: int = 8,
    preserve: bool = False,
) -> bytes:
    """Encode an embedded ST 1202.3 Local Set value.

    ``preserve=True`` reproduces a decoded Local Set byte-for-byte. Canonical
    encoding omits zero coefficients unless uncertainty names them as sources.
    """
    if not isinstance(value, GeneralizedTransformation):
        raise TypeError("value must be a GeneralizedTransformation")
    if isinstance(float_width, bool) or not isinstance(float_width, int):
        raise TypeError("float_width must be an integer")
    if float_width not in {2, 4, 8}:
        raise ValueError("float_width must be 2, 4, or 8")
    if not isinstance(preserve, bool):
        raise TypeError("preserve must be a boolean")
    if preserve and value.local_set is not None:
        return value.local_set.raw

    source_tags: tuple[int, ...] = ()
    if value.uncertainty is not None:
        if value.uncertainty.parse_control.mode != 2:
            raise ValueError("ST 1202 uncertainty requires ST 1010 Parse Control Mode 2")
        if value.uncertainty.matrix_size > 8:
            raise ValueError("ST 1202 SDCC matrix exceeds its eight-item Source List")
        source_tags = value.uncertainty.source_tags
        if not source_tags:
            if value.uncertainty.matrix_size == 8:
                source_tags = ST1202_SDCC_SOURCE_TAGS
            else:
                raise ValueError("partial ST 1202 uncertainty requires explicit source_tags")
        if len(source_tags) != value.uncertainty.matrix_size:
            raise ValueError("ST 1202 source_tags count must equal SDCC matrix size")
        if len(set(source_tags)) != len(source_tags) or any(
            tag not in ST1202_SDCC_SOURCE_TAGS for tag in source_tags
        ):
            raise ValueError("ST 1202 source_tags must be unique coefficient tags 1 through 8")

    format_code = {2: ">e", 4: ">f", 8: ">d"}[float_width]
    coefficients = value.coefficients
    encoded: list[bytes] = []
    non_source_tags = tuple(
        tag
        for tag in ST1202_SDCC_SOURCE_TAGS
        if tag not in source_tags and coefficients[tag - 1] != 0.0
    )
    # ST 1010 requires the ordered Refined Source List to be immediately before
    # its SDCC-FLP, so unrelated non-zero coefficients must precede that list.
    for tag in (*non_source_tags, *source_tags):
        try:
            raw = struct.pack(format_code, coefficients[tag - 1])
        except (OverflowError, struct.error) as error:
            raise ValueError(f"ST 1202 coefficient {tag} cannot fit float_width") from error
        if not math.isfinite(float(struct.unpack(format_code, raw)[0])):
            raise ValueError(f"ST 1202 coefficient {tag} cannot fit float_width")
        encoded.append(_item(tag, raw))
    if value.uncertainty is not None:
        encoded.append(_item(9, encode_sdcc_flp(value.uncertainty)))
    encoded.append(_item(10, _minimal_uint(value.document_version, name="Document Version")))
    if value.transformation_type is not TransformationType.NO_DEFINED_TRANSFORMATION:
        encoded.append(
            _item(
                11,
                _minimal_uint(int(value.transformation_type), name="Transformation Enumeration"),
            )
        )
    for tag in sorted(value.extensions):
        if isinstance(tag, bool) or not isinstance(tag, int):
            raise TypeError("ST 1202 extension tags must be integers")
        if tag < 1 or tag <= 11:
            raise ValueError("ST 1202 extension tags must be greater than 11")
        extension = value.extensions[tag]
        if not isinstance(extension, RawTransformationValue):
            raise TypeError(f"ST 1202 extension tag {tag} requires RawTransformationValue")
        encoded.append(_item(tag, extension.data))
    result = b"".join(encoded)
    decode_generalized_transformation(result)
    return result
