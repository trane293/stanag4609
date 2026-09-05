"""MISB ST 1303.2 Multi-Dimensional Array Pack codecs."""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from enum import Enum
from itertools import product
from typing import TypeAlias

from stanag4609.errors import DecodeError, LimitExceeded, NeedMoreData
from stanag4609.imap import IMAPB, IMAPSpecialValue
from stanag4609.klv.ber import (
    decode_ber_length,
    decode_ber_oid,
    encode_ber_length,
    encode_ber_oid,
)


class MDAPAlgorithm(Enum):
    """Array Processing Algorithm values from ST 1303 Table 3."""

    NATURAL = 1
    IMAP = 2
    BOOLEAN = 3
    UNSIGNED_INTEGER = 4
    RUN_LENGTH = 5


class MDAPElementType(Enum):
    """Invoking-document context for interpreting fixed-width elements."""

    RAW = "raw"
    IEEE = "ieee"
    SIGNED_INTEGER = "signed_integer"
    UNSIGNED_INTEGER = "unsigned_integer"
    BOOLEAN = "boolean"
    IMAP = "imap"


MDAPValue: TypeAlias = bytes | int | float | bool | IMAPSpecialValue


@dataclass(frozen=True, slots=True)
class MDAPPatch:
    """One ordered run-length patch; later patches overwrite earlier ones."""

    value: MDAPValue
    start: tuple[int, ...]
    shape: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class MDAP:
    """Decoded or producer-supplied ST 1303 array pack.

    ``elements`` is a flattened row-major tuple for every algorithm except
    run-length encoding. RLE stays compact in ``patches``; use
    :meth:`materialize` when a dense tuple is actually needed.
    """

    dimensions: tuple[int, ...]
    element_size: int
    algorithm: MDAPAlgorithm
    elements: tuple[MDAPValue, ...] = ()
    element_type: MDAPElementType = MDAPElementType.RAW
    imap_bounds: tuple[float, float] | None = None
    imap_parameter_size: int | None = None
    uint_bias: int | None = None
    rle_default: MDAPValue | None = None
    patches: tuple[MDAPPatch, ...] = ()
    raw: bytes | None = field(default=None, compare=False, repr=False)

    @property
    def ndim(self) -> int:
        return len(self.dimensions)

    @property
    def element_count(self) -> int:
        return math.prod(self.dimensions)

    def _indices(self, indices: tuple[int, ...]) -> None:
        if len(indices) != self.ndim:
            raise ValueError(f"MDAP requires {self.ndim} indices; observed {len(indices)}")
        for index, size in zip(indices, self.dimensions, strict=True):
            if isinstance(index, bool) or not isinstance(index, int):
                raise TypeError("MDAP index must be an integer")
            if not 0 <= index < size:
                raise IndexError(f"MDAP index {index} is outside [0, {size})")

    def element_at(self, *indices: int) -> MDAPValue:
        """Return one logical array element without materializing an RLE pack."""
        self._indices(indices)
        if self.algorithm is MDAPAlgorithm.RUN_LENGTH:
            for patch in reversed(self.patches):
                if all(
                    start <= index < start + length
                    for index, start, length in zip(
                        indices, patch.start, patch.shape, strict=True
                    )
                ):
                    return patch.value
            if self.rle_default is None:
                raise LookupError("MDAP run-length array has no default")
            return self.rle_default
        if not self.elements:
            raise LookupError("MDAP contains no elements")
        offset = 0
        for index, size in zip(indices, self.dimensions, strict=True):
            offset = offset * size + index
        return self.elements[offset]

    def materialize(self, *, max_elements: int = 1_000_000) -> tuple[MDAPValue, ...]:
        """Return a bounded dense row-major tuple, expanding RLE if necessary."""
        _validate_limit(max_elements, name="max_elements", minimum=0)
        if self.element_count > max_elements:
            raise LimitExceeded(
                f"MDAP materialization needs {self.element_count} elements; "
                f"configured maximum is {max_elements}"
            )
        if self.algorithm is not MDAPAlgorithm.RUN_LENGTH:
            return self.elements
        return tuple(self.element_at(*indices) for indices in product(*map(range, self.dimensions)))


_CONTEXTUAL_TYPES = frozenset(
    {
        MDAPElementType.RAW,
        MDAPElementType.IEEE,
        MDAPElementType.SIGNED_INTEGER,
        MDAPElementType.UNSIGNED_INTEGER,
    }
)


def _validate_limit(value: int, *, name: str, minimum: int = 1) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < minimum:
        relation = "non-negative" if minimum == 0 else "positive"
        raise ValueError(f"{name} must be {relation}")


def _read_oid(data: bytes, offset: int, *, name: str) -> tuple[int, int]:
    try:
        value, used = decode_ber_oid(data, offset, max_octets=10)
    except NeedMoreData as error:
        raise DecodeError(f"ST 1303 {name} is truncated") from error
    return value, offset + used


def _validate_dimensions(
    dimensions: tuple[int, ...],
    *,
    max_dimensions: int,
    max_elements: int,
    error_type: type[Exception],
) -> int:
    if not isinstance(dimensions, tuple) or not dimensions:
        raise error_type("ST 1303 requires at least one dimension")
    if len(dimensions) > max_dimensions:
        raise LimitExceeded(
            f"ST 1303 dimensions {len(dimensions)} exceed configured maximum {max_dimensions}"
        )
    count = 1
    for size in dimensions:
        if isinstance(size, bool) or not isinstance(size, int):
            raise TypeError("ST 1303 dimension sizes must be integers")
        if size < 1:
            raise error_type("ST 1303 dimension sizes must be positive")
        if count > max_elements // size:
            raise LimitExceeded(
                f"ST 1303 array elements exceed configured maximum {max_elements}"
            )
        count *= size
    return count


def _decode_element(data: bytes, element_type: MDAPElementType) -> MDAPValue:
    if element_type is MDAPElementType.RAW:
        return data
    if element_type is MDAPElementType.IEEE:
        formats = {2: ">e", 4: ">f", 8: ">d"}
        try:
            return float(struct.unpack(formats[len(data)], data)[0])
        except KeyError as error:
            raise DecodeError("ST 1303 IEEE elements require 2, 4, or 8 bytes") from error
    if element_type is MDAPElementType.SIGNED_INTEGER:
        return int.from_bytes(data, "big", signed=True)
    if element_type is MDAPElementType.UNSIGNED_INTEGER:
        return int.from_bytes(data, "big")
    raise ValueError(f"{element_type.value} is not an invoking-document element type")


def _encode_element(value: MDAPValue, length: int, element_type: MDAPElementType) -> bytes:
    if element_type is MDAPElementType.RAW:
        if not isinstance(value, bytes):
            raise TypeError("ST 1303 raw element must be bytes")
        if len(value) != length:
            raise ValueError(f"ST 1303 raw element requires {length} bytes")
        return value
    if element_type is MDAPElementType.IEEE:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("ST 1303 IEEE element must be numeric")
        formats = {2: ">e", 4: ">f", 8: ">d"}
        try:
            format_code = formats[length]
        except KeyError as error:
            raise ValueError("ST 1303 IEEE elements require 2, 4, or 8 bytes") from error
        try:
            return struct.pack(format_code, value)
        except (OverflowError, struct.error) as error:
            raise ValueError("ST 1303 IEEE element is outside its representation") from error
    if element_type in {
        MDAPElementType.SIGNED_INTEGER,
        MDAPElementType.UNSIGNED_INTEGER,
    }:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("ST 1303 integer element must be an integer")
        signed = element_type is MDAPElementType.SIGNED_INTEGER
        try:
            return value.to_bytes(length, "big", signed=signed)
        except OverflowError as error:
            raise ValueError(f"ST 1303 integer element does not fit {length} bytes") from error
    raise ValueError(f"{element_type.value} is not an invoking-document element type")


def _validate_patch(
    patch: MDAPPatch,
    dimensions: tuple[int, ...],
    *,
    error_type: type[Exception],
) -> None:
    if not isinstance(patch, MDAPPatch):
        raise TypeError("ST 1303 RLE patches must be MDAPPatch instances")
    if len(patch.start) != len(dimensions) or len(patch.shape) != len(dimensions):
        raise error_type("ST 1303 RLE patch dimensionality must match the array")
    for start, length, size in zip(patch.start, patch.shape, dimensions, strict=True):
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (start, length)):
            raise TypeError("ST 1303 RLE patch coordinates and lengths must be integers")
        if length < 1:
            raise error_type("ST 1303 RLE patch lengths must be positive")
        if start < 0 or start + length > size:
            raise error_type("ST 1303 RLE patch lies outside the array")


def _validate_decode_options(
    element_type: MDAPElementType,
    max_dimensions: int,
    max_elements: int,
    max_patches: int,
    max_element_size: int,
    max_pack_length: int,
) -> None:
    if not isinstance(element_type, MDAPElementType):
        raise TypeError("element_type must be an MDAPElementType")
    _validate_limit(max_dimensions, name="max_dimensions")
    _validate_limit(max_elements, name="max_elements")
    _validate_limit(max_patches, name="max_patches", minimum=0)
    _validate_limit(max_element_size, name="max_element_size")
    _validate_limit(max_pack_length, name="max_pack_length", minimum=0)


def decode_mdap(
    data: bytes,
    *,
    element_type: MDAPElementType = MDAPElementType.RAW,
    max_dimensions: int = 16,
    max_elements: int = 1_000_000,
    max_patches: int = 100_000,
    max_element_size: int = 1_048_576,
    max_pack_length: int = 64 * 1024 * 1024,
) -> MDAP:
    """Decode one complete, bounded ST 1303 Multi-Dimensional Array Pack."""
    if not isinstance(data, bytes):
        raise TypeError("MDAP data must be bytes")
    _validate_decode_options(
        element_type,
        max_dimensions,
        max_elements,
        max_patches,
        max_element_size,
        max_pack_length,
    )
    try:
        pack_length, length_used = decode_ber_length(data, max_value=max_pack_length)
    except NeedMoreData as error:
        raise DecodeError("ST 1303 BER length is truncated") from error
    if len(data) != length_used + pack_length:
        raise DecodeError(
            f"ST 1303 pack length declares {pack_length} bytes; "
            f"observed {len(data) - length_used}"
        )
    body = data[length_used:]
    ndim, offset = _read_oid(body, 0, name="NDim")
    if ndim < 1:
        raise DecodeError("ST 1303 NDim must be positive")
    if ndim > max_dimensions:
        raise LimitExceeded(
            f"ST 1303 dimensions {ndim} exceed configured maximum {max_dimensions}"
        )
    dimensions_list: list[int] = []
    for index in range(ndim):
        size, offset = _read_oid(body, offset, name=f"dimension {index + 1}")
        if size < 1:
            raise DecodeError("ST 1303 dimension sizes must be positive")
        dimensions_list.append(size)
    dimensions = tuple(dimensions_list)
    element_count = _validate_dimensions(
        dimensions,
        max_dimensions=max_dimensions,
        max_elements=max_elements,
        error_type=DecodeError,
    )
    element_size, offset = _read_oid(body, offset, name="EBytes")
    if element_size > max_element_size:
        raise LimitExceeded(
            f"ST 1303 EBytes {element_size} exceeds configured maximum {max_element_size}"
        )
    algorithm_value, offset = _read_oid(body, offset, name="APA")
    try:
        algorithm = MDAPAlgorithm(algorithm_value)
    except ValueError as error:
        raise DecodeError(f"ST 1303 APA value {algorithm_value} is reserved or unknown") from error
    payload = body[offset:]

    if algorithm is MDAPAlgorithm.NATURAL:
        if element_type not in _CONTEXTUAL_TYPES:
            raise ValueError("natural MDAP requires a contextual element type")
        expected = element_count * element_size
        if len(payload) != expected:
            raise DecodeError(
                f"ST 1303 Natural array has {len(payload)} bytes; expected {expected}"
            )
        elements = tuple(
            _decode_element(payload[index : index + element_size], element_type)
            for index in range(0, len(payload), element_size)
        ) if element_size else ()
        return MDAP(
            dimensions,
            element_size,
            algorithm,
            elements,
            element_type=element_type,
            raw=data,
        )

    if algorithm is MDAPAlgorithm.IMAP:
        if element_size < 1:
            raise DecodeError("ST 1303 IMAP EBytes must be positive")
        array_size = element_count * element_size
        parameter_length = len(payload) - array_size
        if parameter_length not in {8, 16}:
            raise DecodeError("ST 1303 IMAP APAS requires two 32-bit or two 64-bit values")
        parameter_size = parameter_length // 2
        format_code = ">f" if parameter_size == 4 else ">d"
        minimum = float(struct.unpack(format_code, payload[:parameter_size])[0])
        maximum = float(struct.unpack(format_code, payload[parameter_size:parameter_length])[0])
        if not math.isfinite(minimum) or not math.isfinite(maximum) or maximum <= minimum:
            raise DecodeError("ST 1303 IMAP bounds must be finite and increasing")
        mapping = IMAPB(minimum, maximum, element_size)
        elements = tuple(
            mapping.decode(payload[index : index + element_size])
            for index in range(parameter_length, len(payload), element_size)
        )
        return MDAP(
            dimensions,
            element_size,
            algorithm,
            elements,
            element_type=MDAPElementType.IMAP,
            imap_bounds=(minimum, maximum),
            imap_parameter_size=parameter_size,
            raw=data,
        )

    if algorithm is MDAPAlgorithm.BOOLEAN:
        if element_size != 1:
            raise DecodeError("ST 1303 Boolean APA requires EBytes 1")
        expected = (element_count + 7) // 8
        if len(payload) != expected:
            raise DecodeError(
                f"ST 1303 Boolean array has {len(payload)} bytes; expected {expected}"
            )
        padding = expected * 8 - element_count
        if padding and payload[-1] & ((1 << padding) - 1):
            raise DecodeError("ST 1303 Boolean array padding bits must be zero")
        elements = tuple(
            bool(payload[index // 8] & (1 << (7 - index % 8)))
            for index in range(element_count)
        )
        return MDAP(
            dimensions,
            element_size,
            algorithm,
            elements,
            element_type=MDAPElementType.BOOLEAN,
            raw=data,
        )

    if algorithm is MDAPAlgorithm.UNSIGNED_INTEGER:
        if element_size != 1:
            raise DecodeError("ST 1303 Unsigned Integer APA requires EBytes 1")
        bias, cursor = _read_oid(payload, 0, name="Unsigned Integer bias")
        values: list[int] = []
        for _ in range(element_count):
            value, cursor = _read_oid(payload, cursor, name="BER-OID element")
            values.append(value + bias)
        if cursor != len(payload):
            raise DecodeError("ST 1303 Unsigned Integer array has trailing bytes")
        if values and min(values) != bias:
            raise DecodeError("ST 1303 Unsigned Integer bias must equal the array minimum")
        return MDAP(
            dimensions,
            element_size,
            algorithm,
            tuple(values),
            element_type=MDAPElementType.UNSIGNED_INTEGER,
            uint_bias=bias,
            raw=data,
        )

    if element_size < 1:
        raise DecodeError("ST 1303 RLE requires a positive EBytes value for its default")
    if len(payload) < element_size:
        raise DecodeError("ST 1303 RLE default value is truncated")
    if element_type not in _CONTEXTUAL_TYPES:
        raise ValueError("run-length MDAP requires a contextual element type")
    default = _decode_element(payload[:element_size], element_type)
    cursor = element_size
    patches: list[MDAPPatch] = []
    while cursor < len(payload):
        if len(patches) >= max_patches:
            raise LimitExceeded(f"ST 1303 RLE patches exceed configured maximum {max_patches}")
        value_end = cursor + element_size
        if value_end > len(payload):
            raise DecodeError("ST 1303 RLE patch value is truncated")
        patch_value = _decode_element(payload[cursor:value_end], element_type)
        cursor = value_end
        start_values: list[int] = []
        shape_values: list[int] = []
        for index in range(ndim):
            coordinate, cursor = _read_oid(payload, cursor, name=f"RLE coordinate {index + 1}")
            start_values.append(coordinate)
        for index in range(ndim):
            length, cursor = _read_oid(payload, cursor, name=f"RLE length {index + 1}")
            shape_values.append(length)
        patch = MDAPPatch(patch_value, tuple(start_values), tuple(shape_values))
        _validate_patch(patch, dimensions, error_type=DecodeError)
        patches.append(patch)
    return MDAP(
        dimensions,
        element_size,
        algorithm,
        element_type=element_type,
        rle_default=default,
        patches=tuple(patches),
        raw=data,
    )


def _validate_model(
    pack: MDAP,
    *,
    max_dimensions: int,
    max_elements: int,
    max_patches: int,
    max_element_size: int,
) -> int:
    if not isinstance(pack, MDAP):
        raise TypeError("pack must be an MDAP")
    if not isinstance(pack.algorithm, MDAPAlgorithm):
        raise TypeError("MDAP algorithm must be an MDAPAlgorithm")
    if not isinstance(pack.element_type, MDAPElementType):
        raise TypeError("MDAP element_type must be an MDAPElementType")
    element_count = _validate_dimensions(
        pack.dimensions,
        max_dimensions=max_dimensions,
        max_elements=max_elements,
        error_type=ValueError,
    )
    if isinstance(pack.element_size, bool) or not isinstance(pack.element_size, int):
        raise TypeError("ST 1303 EBytes must be an integer")
    if pack.element_size < 0:
        raise ValueError("ST 1303 EBytes must be non-negative")
    if pack.element_size > max_element_size:
        raise LimitExceeded(
            f"ST 1303 EBytes {pack.element_size} exceeds configured maximum {max_element_size}"
        )
    if len(pack.patches) > max_patches:
        raise LimitExceeded(f"ST 1303 RLE patches exceed configured maximum {max_patches}")
    return element_count


def encode_mdap(
    pack: MDAP,
    *,
    max_dimensions: int = 16,
    max_elements: int = 1_000_000,
    max_patches: int = 100_000,
    max_element_size: int = 1_048_576,
    max_pack_length: int = 64 * 1024 * 1024,
) -> bytes:
    """Encode one canonical, bounded ST 1303 Multi-Dimensional Array Pack."""
    _validate_decode_options(
        MDAPElementType.RAW,
        max_dimensions,
        max_elements,
        max_patches,
        max_element_size,
        max_pack_length,
    )
    element_count = _validate_model(
        pack,
        max_dimensions=max_dimensions,
        max_elements=max_elements,
        max_patches=max_patches,
        max_element_size=max_element_size,
    )
    if pack.algorithm is not MDAPAlgorithm.IMAP and (
        pack.imap_bounds is not None or pack.imap_parameter_size is not None
    ):
        raise ValueError("ST 1303 IMAP parameters require the IMAP APA")
    if pack.algorithm is not MDAPAlgorithm.UNSIGNED_INTEGER and pack.uint_bias is not None:
        raise ValueError("ST 1303 unsigned bias requires the Unsigned Integer APA")
    if pack.algorithm is not MDAPAlgorithm.RUN_LENGTH and (
        pack.rle_default is not None or pack.patches
    ):
        raise ValueError("ST 1303 RLE parameters require the Run-Length APA")
    body = bytearray(encode_ber_oid(pack.ndim))
    for dimension in pack.dimensions:
        body.extend(encode_ber_oid(dimension))
    body.extend(encode_ber_oid(pack.element_size))
    body.extend(encode_ber_oid(pack.algorithm.value))

    if pack.algorithm is MDAPAlgorithm.NATURAL:
        if pack.element_type not in _CONTEXTUAL_TYPES:
            raise ValueError("natural MDAP requires a contextual element type")
        if pack.element_size == 0:
            if pack.elements:
                raise ValueError("ST 1303 empty Natural array cannot contain elements")
        elif len(pack.elements) != element_count:
            raise ValueError(f"ST 1303 Natural array requires {element_count} elements")
        else:
            for value in pack.elements:
                body.extend(_encode_element(value, pack.element_size, pack.element_type))
    elif pack.algorithm is MDAPAlgorithm.IMAP:
        if pack.element_type is not MDAPElementType.IMAP:
            raise ValueError("ST 1303 IMAP APA requires the IMAP element type")
        if pack.element_size < 1:
            raise ValueError("ST 1303 IMAP EBytes must be positive")
        if len(pack.elements) != element_count:
            raise ValueError(f"ST 1303 IMAP array requires {element_count} elements")
        if not isinstance(pack.imap_bounds, tuple) or len(pack.imap_bounds) != 2:
            raise ValueError("ST 1303 IMAP requires two bounds")
        if pack.imap_parameter_size not in {4, 8}:
            raise ValueError("ST 1303 IMAP parameter size must be 4 or 8 bytes")
        minimum, maximum = pack.imap_bounds
        if (
            isinstance(minimum, bool)
            or isinstance(maximum, bool)
            or not isinstance(minimum, (int, float))
            or not isinstance(maximum, (int, float))
        ):
            raise ValueError("ST 1303 IMAP bounds must be finite and increasing")
        try:
            numeric_minimum = float(minimum)
            numeric_maximum = float(maximum)
        except OverflowError as error:
            raise ValueError("ST 1303 IMAP bounds must be finite and increasing") from error
        if (
            not math.isfinite(numeric_minimum)
            or not math.isfinite(numeric_maximum)
            or numeric_maximum <= numeric_minimum
        ):
            raise ValueError("ST 1303 IMAP bounds must be finite and increasing")
        format_code = ">f" if pack.imap_parameter_size == 4 else ">d"
        try:
            encoded_minimum = struct.pack(format_code, numeric_minimum)
            encoded_maximum = struct.pack(format_code, numeric_maximum)
        except (OverflowError, struct.error) as error:
            raise ValueError("ST 1303 IMAP bounds do not fit their IEEE representation") from error
        wire_minimum = float(struct.unpack(format_code, encoded_minimum)[0])
        wire_maximum = float(struct.unpack(format_code, encoded_maximum)[0])
        if not math.isfinite(wire_minimum) or not math.isfinite(wire_maximum) or (
            wire_maximum <= wire_minimum
        ):
            raise ValueError("ST 1303 encoded IMAP bounds must be finite and increasing")
        body.extend(encoded_minimum)
        body.extend(encoded_maximum)
        mapping = IMAPB(wire_minimum, wire_maximum, pack.element_size)
        for value in pack.elements:
            if isinstance(value, (bool, bytes)):
                raise TypeError("ST 1303 IMAP element must be numeric or an IMAPSpecialValue")
            mapped_value = value
            if isinstance(value, (int, float)):
                if numeric_minimum <= value < wire_minimum:
                    mapped_value = wire_minimum
                elif wire_maximum < value <= numeric_maximum:
                    mapped_value = wire_maximum
            body.extend(mapping.encode(mapped_value))
    elif pack.algorithm is MDAPAlgorithm.BOOLEAN:
        if pack.element_type is not MDAPElementType.BOOLEAN:
            raise ValueError("ST 1303 Boolean APA requires the Boolean element type")
        if pack.element_size != 1:
            raise ValueError("ST 1303 Boolean APA requires EBytes 1")
        if len(pack.elements) != element_count:
            raise ValueError(f"ST 1303 Boolean array requires {element_count} elements")
        encoded = bytearray((element_count + 7) // 8)
        for index, value in enumerate(pack.elements):
            if not isinstance(value, bool):
                raise TypeError("ST 1303 Boolean array elements must be boolean")
            if value:
                encoded[index // 8] |= 1 << (7 - index % 8)
        body.extend(encoded)
    elif pack.algorithm is MDAPAlgorithm.UNSIGNED_INTEGER:
        if pack.element_type is not MDAPElementType.UNSIGNED_INTEGER:
            raise ValueError("ST 1303 Unsigned Integer APA requires the unsigned element type")
        if pack.element_size != 1:
            raise ValueError("ST 1303 Unsigned Integer APA requires EBytes 1")
        if isinstance(pack.uint_bias, bool) or not isinstance(pack.uint_bias, int):
            raise TypeError("ST 1303 Unsigned Integer bias must be an integer")
        if pack.uint_bias < 0:
            raise ValueError("ST 1303 Unsigned Integer bias must be non-negative")
        if len(pack.elements) != element_count:
            raise ValueError(f"ST 1303 Unsigned Integer array requires {element_count} elements")
        integer_values: list[int] = []
        for value in pack.elements:
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError("ST 1303 Unsigned Integer element must be an integer")
            integer_values.append(value)
        if integer_values and min(integer_values) != pack.uint_bias:
            raise ValueError("ST 1303 Unsigned Integer bias must equal the array minimum")
        body.extend(encode_ber_oid(pack.uint_bias))
        for value in integer_values:
            if value < pack.uint_bias:
                raise ValueError("ST 1303 Unsigned Integer element is below its bias")
            body.extend(encode_ber_oid(value - pack.uint_bias))
    else:
        if pack.element_size < 1:
            raise ValueError("ST 1303 RLE requires a positive EBytes value")
        if pack.element_type not in _CONTEXTUAL_TYPES:
            raise ValueError("run-length MDAP requires a contextual element type")
        if pack.rle_default is None:
            raise ValueError("ST 1303 RLE requires a default value")
        if pack.elements:
            raise ValueError("ST 1303 RLE uses patches rather than dense elements")
        body.extend(_encode_element(pack.rle_default, pack.element_size, pack.element_type))
        for patch in pack.patches:
            _validate_patch(patch, pack.dimensions, error_type=ValueError)
            body.extend(_encode_element(patch.value, pack.element_size, pack.element_type))
            for coordinate in patch.start:
                body.extend(encode_ber_oid(coordinate))
            for length in patch.shape:
                body.extend(encode_ber_oid(length))

    if len(body) > max_pack_length:
        raise LimitExceeded(
            f"ST 1303 pack length {len(body)} exceeds configured maximum {max_pack_length}"
        )
    return encode_ber_length(len(body)) + body
