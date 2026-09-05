"""MISB ST 1402.2 MPEG-TS metadata program-map conformance checks."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from stanag4609.errors import DecodeError
from stanag4609.transport.metadata import (
    decode_metadata_descriptor_header,
    decode_metadata_std_descriptor,
)
from stanag4609.transport.psi import (
    Descriptor,
    ElementaryStreamInfo,
    KLVCarriage,
    ProgramMapTable,
    klv_carriage,
)


@dataclass(frozen=True, slots=True)
class ST1402ValidationIssue:
    """One metadata-carriage issue in an MPEG-TS program map."""

    code: str
    requirement: str
    message: str
    program_number: int
    elementary_pid: int


_MOTION_IMAGERY_STREAM_TYPES = frozenset(
    {0x01, 0x02, 0x10, 0x1B, 0x20, 0x21, 0x24, 0x42}
)


def _has_registration(descriptors: tuple[Descriptor, ...], identifier: bytes) -> bool:
    return any(
        descriptor.tag == 0x05 and descriptor.data[:4] == identifier
        for descriptor in descriptors
    )


def _valid_metadata_std_descriptor(descriptor: Descriptor) -> bool:
    try:
        decode_metadata_std_descriptor(descriptor)
    except DecodeError:
        return False
    return True


def _issue(
    pmt: ProgramMapTable,
    stream: ElementaryStreamInfo,
    code: str,
    requirement: str,
    message: str,
) -> ST1402ValidationIssue:
    return ST1402ValidationIssue(
        code,
        requirement,
        message,
        pmt.program_number,
        stream.elementary_pid,
    )


def _validate_sync(
    pmt: ProgramMapTable,
    stream: ElementaryStreamInfo,
) -> list[ST1402ValidationIssue]:
    issues: list[ST1402ValidationIssue] = []
    if stream.stream_type != 0x15:
        issues.append(
            _issue(
                pmt,
                stream,
                "stream_type",
                "ST 1402-06 (deprecated)",
                "synchronous metadata must use MPEG-TS stream_type 0x15",
            )
        )
    metadata_descriptors = tuple(
        descriptor for descriptor in stream.descriptors if descriptor.tag == 0x26
    )
    metadata_headers = []
    malformed_metadata_descriptors = 0
    for descriptor in metadata_descriptors:
        try:
            metadata_headers.append(decode_metadata_descriptor_header(descriptor))
        except DecodeError:
            malformed_metadata_descriptors += 1
    klva_descriptors = tuple(
        header
        for header in metadata_headers
        if header.metadata_format_identifier == b"KLVA"
    )
    if not metadata_descriptors and any(
        descriptor.tag == 0x26 for descriptor in pmt.descriptors
    ):
        issues.append(
            _issue(
                pmt,
                stream,
                "metadata_descriptor_location",
                "ST 1402-16",
                "metadata_descriptor is in the PMT program loop instead of the "
                "metadata elementary-stream descriptor loop",
            )
        )
    if not metadata_descriptors:
        issues.append(
            _issue(
                pmt,
                stream,
                "metadata_descriptor",
                "ST 1402-15",
                "synchronous metadata requires at least one metadata_descriptor",
            )
        )
    if malformed_metadata_descriptors:
        issues.append(
            _issue(
                pmt,
                stream,
                "metadata_descriptor_format",
                "ST 1402-15 / ISO/IEC 13818-1",
                "metadata_descriptor has a malformed identity or service prefix",
            )
        )
    if not klva_descriptors:
        issues.append(
            _issue(
                pmt,
                stream,
                "metadata_format_identifier",
                "ST 1402.1-26",
                "synchronous KLV metadata requires metadata_format_identifier 'KLVA'",
            )
        )

    std_descriptors = tuple(
        descriptor for descriptor in stream.descriptors if descriptor.tag == 0x27
    )
    if len(std_descriptors) != 1:
        issues.append(
            _issue(
                pmt,
                stream,
                "metadata_std_descriptor_count",
                "ST 1402-17",
                "synchronous metadata requires exactly one metadata_std_descriptor",
            )
        )
    if any(not _valid_metadata_std_descriptor(item) for item in std_descriptors):
        issues.append(
            _issue(
                pmt,
                stream,
                "metadata_std_descriptor_format",
                "ST 1402-17 / ISO/IEC 13818-1",
                "metadata_std_descriptor must contain three valid 22-bit STD values",
            )
        )
    return issues


def _validate_async(
    pmt: ProgramMapTable,
    stream: ElementaryStreamInfo,
) -> list[ST1402ValidationIssue]:
    issues: list[ST1402ValidationIssue] = []
    if stream.stream_type != 0x06:
        issues.append(
            _issue(
                pmt,
                stream,
                "stream_type",
                "ST 1402-23 (deprecated)",
                "asynchronous metadata must use MPEG-TS stream_type 0x06",
            )
        )
    registered = _has_registration(stream.descriptors, b"KLVA")
    if not registered:
        issues.extend(
            (
                _issue(
                    pmt,
                    stream,
                    "registration_descriptor",
                    "ST 1402-25",
                    "asynchronous metadata requires a registration_descriptor in its "
                    "elementary-stream descriptor loop",
                ),
                _issue(
                    pmt,
                    stream,
                    "format_identifier",
                    "ST 1402-03",
                    "asynchronous KLV metadata requires format_identifier 'KLVA'",
                ),
            )
        )
    return issues


def validate_st1402_metadata_program(
    pmt: ProgramMapTable,
    *,
    expected_carriage: Mapping[int, KLVCarriage] | None = None,
) -> tuple[ST1402ValidationIssue, ...]:
    """Validate ST 1402 metadata declarations within one program map.

    When ``expected_carriage`` is omitted, correctly registered asynchronous
    streams and every synchronous ``0x15`` candidate are discovered. Supply an
    explicit PID-to-carriage mapping to diagnose a stream whose identifying
    descriptor or stream type is itself missing or wrong.
    """

    if not isinstance(pmt, ProgramMapTable):
        raise TypeError("pmt must be a ProgramMapTable")
    if expected_carriage is not None and not isinstance(expected_carriage, Mapping):
        raise TypeError("expected_carriage must be a mapping")

    streams_by_pid = {stream.elementary_pid: stream for stream in pmt.streams}
    if expected_carriage is None:
        declarations: dict[int, KLVCarriage] = {}
        for stream in pmt.streams:
            carriage = klv_carriage(stream)
            if carriage is not None:
                declarations[stream.elementary_pid] = carriage
            elif stream.stream_type == 0x15:
                declarations[stream.elementary_pid] = KLVCarriage.SYNCHRONOUS
    else:
        declarations = dict(expected_carriage)
        for pid, carriage in declarations.items():
            if isinstance(pid, bool) or not isinstance(pid, int) or not 0 <= pid <= 0x1FFF:
                raise ValueError("metadata PID must be an integer that fits in 13 bits")
            if not isinstance(carriage, KLVCarriage):
                raise TypeError("expected carriage values must be KLVCarriage members")

    issues: list[ST1402ValidationIssue] = []
    has_motion_imagery = any(
        stream.stream_type in _MOTION_IMAGERY_STREAM_TYPES for stream in pmt.streams
    )
    for pid in sorted(declarations):
        carriage = declarations[pid]
        metadata_stream = streams_by_pid.get(pid)
        if metadata_stream is None:
            issues.append(
                ST1402ValidationIssue(
                    "missing_stream",
                    "ST 1402-14" if carriage is KLVCarriage.SYNCHRONOUS else "ST 1402-24",
                    f"program map does not declare expected metadata PID {pid}",
                    pmt.program_number,
                    pid,
                )
            )
            continue
        if not has_motion_imagery:
            issues.append(
                _issue(
                    pmt,
                    metadata_stream,
                    "motion_imagery_program",
                    "ST 1402-14"
                    if carriage is KLVCarriage.SYNCHRONOUS
                    else "ST 1402-24",
                    "metadata elementary stream is not in a program containing a "
                    "recognized motion-imagery elementary stream",
                )
            )
        issues.extend(
            _validate_sync(pmt, metadata_stream)
            if carriage is KLVCarriage.SYNCHRONOUS
            else _validate_async(pmt, metadata_stream)
        )
    return tuple(issues)
