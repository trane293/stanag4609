"""MPEG-2 metadata access units and ST 1402 KLVA stream descriptors."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import IntEnum

from stanag4609.errors import DecodeError, LimitExceeded, TruncatedData
from stanag4609.transport.psi import Descriptor, ElementaryStreamInfo


class CellFragmentation(IntEnum):
    """ISO/IEC 13818-1 Metadata_AU_cell fragment indication."""

    MIDDLE = 0b00
    LAST = 0b01
    FIRST = 0b10
    COMPLETE = 0b11


@dataclass(frozen=True, slots=True)
class MetadataAUCell:
    """One metadata access-unit cell with its original bytes."""

    metadata_service_id: int
    sequence_number: int
    fragmentation: CellFragmentation
    decoder_config: bool
    random_access: bool
    data: bytes
    raw: bytes


@dataclass(frozen=True, slots=True)
class MetadataDescriptorHeader:
    """Identity and service fields at the front of an H.222.0 descriptor."""

    application_format: int
    application_format_identifier: bytes | None
    metadata_format: int
    metadata_format_identifier: bytes | None
    metadata_service_id: int
    decoder_config_flags: int
    dsm_cc: bool


def _validate_uint(value: int, maximum: int, *, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be an integer from 0 to {maximum}")


@dataclass(frozen=True, slots=True)
class MetadataSTDDescriptor:
    """Typed H.222.0 metadata System Target Decoder parameters.

    Leak-rate codes use units of 400 bits/s and the buffer-size code uses
    units of 1024 bytes. The raw codes remain available for lossless PMT work;
    physical-unit properties make their meaning explicit to applications.
    """

    input_leak_rate: int
    buffer_size: int
    output_leak_rate: int = 0

    def __post_init__(self) -> None:
        _validate_uint(self.input_leak_rate, 0x3FFFFF, name="input_leak_rate")
        _validate_uint(self.buffer_size, 0x3FFFFF, name="buffer_size")
        _validate_uint(self.output_leak_rate, 0x3FFFFF, name="output_leak_rate")

    @classmethod
    def from_physical(
        cls,
        *,
        input_bits_per_second: int,
        buffer_bytes: int,
        output_bits_per_second: int = 0,
    ) -> MetadataSTDDescriptor:
        """Construct from exact physical units without silently rounding."""

        _validate_uint(
            input_bits_per_second,
            0x3FFFFF * 400,
            name="input_bits_per_second",
        )
        _validate_uint(buffer_bytes, 0x3FFFFF * 1024, name="buffer_bytes")
        _validate_uint(
            output_bits_per_second,
            0x3FFFFF * 400,
            name="output_bits_per_second",
        )
        if input_bits_per_second % 400:
            raise ValueError("input_bits_per_second must be a multiple of 400")
        if buffer_bytes % 1024:
            raise ValueError("buffer_bytes must be a multiple of 1024")
        if output_bits_per_second % 400:
            raise ValueError("output_bits_per_second must be a multiple of 400")
        return cls(
            input_bits_per_second // 400,
            buffer_bytes // 1024,
            output_bits_per_second // 400,
        )

    @property
    def input_bits_per_second(self) -> int:
        return self.input_leak_rate * 400

    @property
    def buffer_bytes(self) -> int:
        return self.buffer_size * 1024

    @property
    def output_bits_per_second(self) -> int:
        return self.output_leak_rate * 400


def encode_metadata_au_cell(
    data: bytes,
    *,
    metadata_service_id: int = 0,
    sequence_number: int = 0,
    fragmentation: CellFragmentation = CellFragmentation.COMPLETE,
    decoder_config: bool = False,
    random_access: bool = False,
) -> bytes:
    """Encode the five-byte header and data of one Metadata_AU_cell."""

    if not data:
        raise ValueError("metadata AU cell data must not be empty")
    if len(data) > 0xFFFF:
        raise ValueError("metadata AU cell data exceeds 65535 bytes")
    _validate_uint(metadata_service_id, 0xFF, name="metadata_service_id")
    _validate_uint(sequence_number, 0xFF, name="sequence_number")
    if not isinstance(fragmentation, CellFragmentation):
        raise TypeError("fragmentation must be CellFragmentation")
    flags = (
        (int(fragmentation) << 6)
        | (int(decoder_config) << 5)
        | (int(random_access) << 4)
        | 0x0F
    )
    return (
        bytes((metadata_service_id, sequence_number, flags))
        + len(data).to_bytes(2, "big")
        + data
    )


def parse_metadata_au_cells(
    data: bytes,
    *,
    max_cells: int = 65_536,
) -> tuple[MetadataAUCell, ...]:
    """Parse a Metadata Access Unit Wrapper containing concatenated cells."""

    if max_cells < 1:
        raise ValueError("max_cells must be positive")
    cells: list[MetadataAUCell] = []
    cursor = 0
    previous_sequence: int | None = None
    while cursor < len(data):
        if len(cells) >= max_cells:
            raise LimitExceeded(f"metadata AU wrapper exceeds {max_cells} cells")
        if len(data) - cursor < 5:
            raise TruncatedData("metadata AU wrapper ends inside a five-byte cell header")
        start = cursor
        service_id = data[cursor]
        sequence = data[cursor + 1]
        flags = data[cursor + 2]
        if flags & 0x0F != 0x0F:
            raise DecodeError("metadata AU cell reserved bits are not all set")
        length = int.from_bytes(data[cursor + 3 : cursor + 5], "big")
        cursor += 5
        end = cursor + length
        if end > len(data):
            raise TruncatedData(
                f"metadata AU cell data overruns wrapper: declares {length} bytes, "
                f"only {len(data) - cursor} remain"
            )
        if previous_sequence is not None and sequence != (previous_sequence + 1) & 0xFF:
            raise DecodeError(
                f"metadata AU cell sequence is {sequence}, expected "
                f"{(previous_sequence + 1) & 0xFF}"
            )
        cells.append(
            MetadataAUCell(
                service_id,
                sequence,
                CellFragmentation(flags >> 6),
                bool(flags & 0x20),
                bool(flags & 0x10),
                data[cursor:end],
                data[start:end],
            )
        )
        previous_sequence = sequence
        cursor = end
    return tuple(cells)


def asynchronous_klv_stream(pid: int) -> ElementaryStreamInfo:
    """Return an ST 1402 asynchronous KLVA PMT stream declaration."""

    _validate_uint(pid, 0x1FFF, name="PID")
    return ElementaryStreamInfo(0x06, pid, (Descriptor(0x05, b"KLVA"),))


def decode_metadata_descriptor_header(
    descriptor: Descriptor,
) -> MetadataDescriptorHeader:
    """Decode the fixed and conditional identity prefix of a metadata descriptor.

    Decoder-configuration and private-data bodies follow this prefix and are
    intentionally left application-owned. The prefix is sufficient to bind
    Metadata AU cells to the services declared in a PMT.
    """

    if not isinstance(descriptor, Descriptor):
        raise TypeError("descriptor must be a Descriptor")
    if descriptor.tag != 0x26:
        raise DecodeError("metadata descriptor must use tag 0x26")
    data = descriptor.data
    cursor = 0

    def take(length: int, name: str) -> bytes:
        nonlocal cursor
        end = cursor + length
        if end > len(data):
            raise DecodeError(f"metadata descriptor is truncated in {name}")
        value = data[cursor:end]
        cursor = end
        return value

    application_format = int.from_bytes(take(2, "metadata_application_format"), "big")
    application_identifier = (
        take(4, "metadata_application_format_identifier")
        if application_format == 0xFFFF
        else None
    )
    metadata_format = take(1, "metadata_format")[0]
    format_identifier = (
        take(4, "metadata_format_identifier") if metadata_format == 0xFF else None
    )
    service_id = take(1, "metadata_service_id")[0]
    flags = take(1, "decoder configuration flags")[0]
    if flags & 0x0F != 0x0F:
        raise DecodeError("metadata descriptor reserved bits must be '1111'")
    return MetadataDescriptorHeader(
        application_format,
        application_identifier,
        metadata_format,
        format_identifier,
        service_id,
        flags >> 5,
        bool(flags & 0x10),
    )


def klva_metadata_service_ids(stream: ElementaryStreamInfo) -> frozenset[int]:
    """Return well-formed KLVA service IDs declared for one elementary stream."""

    if not isinstance(stream, ElementaryStreamInfo):
        raise TypeError("stream must be an ElementaryStreamInfo")
    services: set[int] = set()
    for descriptor in stream.descriptors:
        if descriptor.tag != 0x26:
            continue
        try:
            header = decode_metadata_descriptor_header(descriptor)
        except DecodeError:
            continue
        if header.metadata_format_identifier == b"KLVA":
            services.add(header.metadata_service_id)
    return frozenset(services)


def _std_value(value: int, *, name: str) -> bytes:
    _validate_uint(value, 0x3FFFFF, name=name)
    return ((0b11 << 22) | value).to_bytes(3, "big")


def encode_metadata_std_descriptor(value: MetadataSTDDescriptor) -> Descriptor:
    """Encode one H.222.0 ``metadata_std_descriptor`` (tag ``0x27``)."""

    if not isinstance(value, MetadataSTDDescriptor):
        raise TypeError("value must be a MetadataSTDDescriptor")
    return Descriptor(
        0x27,
        _std_value(value.input_leak_rate, name="input_leak_rate")
        + _std_value(value.buffer_size, name="buffer_size")
        + _std_value(value.output_leak_rate, name="output_leak_rate"),
    )


def decode_metadata_std_descriptor(descriptor: Descriptor) -> MetadataSTDDescriptor:
    """Decode and validate one H.222.0 ``metadata_std_descriptor``."""

    if not isinstance(descriptor, Descriptor):
        raise TypeError("descriptor must be a Descriptor")
    if descriptor.tag != 0x27:
        raise DecodeError("metadata STD descriptor must use tag 0x27")
    if len(descriptor.data) != 9:
        raise DecodeError("metadata STD descriptor must contain exactly nine bytes")
    values: list[int] = []
    for index in (0, 3, 6):
        encoded = int.from_bytes(descriptor.data[index : index + 3], "big")
        if encoded >> 22 != 0b11:
            raise DecodeError("metadata STD descriptor reserved bits must be '11'")
        values.append(encoded & 0x3FFFFF)
    return MetadataSTDDescriptor(*values)


def synchronous_klv_stream(
    pid: int,
    *,
    metadata_input_leak_rate: int | None = None,
    metadata_buffer_size: int | None = None,
    metadata_output_leak_rate: int = 0,
    metadata_service_id: int = 0,
    metadata_service_ids: Iterable[int] | None = None,
    metadata_std: MetadataSTDDescriptor | None = None,
) -> ElementaryStreamInfo:
    """Return an ST 1402 synchronous KLVA PMT stream declaration."""

    _validate_uint(pid, 0x1FFF, name="PID")
    _validate_uint(metadata_service_id, 0xFF, name="metadata_service_id")
    service_ids: tuple[int, ...]
    if metadata_service_ids is None:
        service_ids = (metadata_service_id,)
    else:
        if metadata_service_id != 0:
            raise ValueError(
                "supply either metadata_service_id or metadata_service_ids, not both"
            )
        try:
            service_ids = tuple(metadata_service_ids)
        except TypeError as error:
            raise TypeError("metadata_service_ids must be an iterable of integers") from error
        if not service_ids:
            raise ValueError("metadata_service_ids must not be empty")
        for service_id in service_ids:
            _validate_uint(service_id, 0xFF, name="metadata_service_id")
        if len(set(service_ids)) != len(service_ids):
            raise ValueError("metadata_service_ids must be unique")
    if metadata_std is not None:
        if not isinstance(metadata_std, MetadataSTDDescriptor):
            raise TypeError("metadata_std must be a MetadataSTDDescriptor or None")
        if (
            metadata_input_leak_rate is not None
            or metadata_buffer_size is not None
            or metadata_output_leak_rate != 0
        ):
            raise ValueError(
                "supply either metadata_std or individual metadata STD codes, not both"
            )
        std_value = metadata_std
    else:
        if metadata_input_leak_rate is None or metadata_buffer_size is None:
            raise ValueError(
                "metadata_input_leak_rate and metadata_buffer_size are required "
                "when metadata_std is omitted"
            )
        std_value = MetadataSTDDescriptor(
            metadata_input_leak_rate,
            metadata_buffer_size,
            metadata_output_leak_rate,
        )
    metadata_descriptors = tuple(
        Descriptor(
            0x26,
            b"\x01\x00\xffKLVA" + bytes((service_id, 0x0F)),
        )
        for service_id in service_ids
    )
    std_descriptor = encode_metadata_std_descriptor(std_value)
    return ElementaryStreamInfo(0x15, pid, (*metadata_descriptors, std_descriptor))
