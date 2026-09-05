"""Incremental FMV conformance and interoperability reporting."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import Any, BinaryIO

from stanag4609.audio.timing import AudioPESFrameParser, TimedCompressedAudioFrame
from stanag4609.errors import Stanag4609Error
from stanag4609.st0102 import (
    CountryCodingMethod,
    ObjectCountryCodingMethod,
    SecurityClassification,
)
from stanag4609.st0601 import (
    FIELD_DEFINITIONS,
    FieldDecodingMode,
    ST0601ValidationContext,
    UASLocalSet,
    misp_timestamp_to_utc,
    resolve_target_elevation,
)
from stanag4609.st0601_state import (
    ControlCommandIssue,
    ControlCommandState,
    PayloadTableIssue,
    PayloadTableState,
    WavelengthTableIssue,
    WavelengthTableState,
    WaypointListIssue,
    WaypointListState,
    WeaponsStoresState,
)
from stanag4609.st0604 import (
    SUPPORTED_ST0604_STREAM_TYPES,
    EmbeddedVideoTimestamp,
    TimestampedVideoAccessUnit,
    TimestampResolution,
    VideoTimestampedAccessUnitParser,
)
from stanag4609.st0902 import (
    MISMMSecurityContext,
    MISMMSPopulationStatus,
    MISMMSRequirementCoverage,
    MISMMSValidationIssue,
    MISMMSValidator,
)
from stanag4609.st0903 import OntologyResolver, VMTILocalSet
from stanag4609.st0903_state import VMTILifecycleIssue, VMTILifecycleState
from stanag4609.st1001 import find_st1001_audio_streams, validate_st1001_audio_profile
from stanag4609.st1402 import validate_st1402_metadata_program
from stanag4609.st1607_state import (
    MetadataTreeSnapshot,
    MetadataTreeState,
    ST1607PolicyIssue,
    validate_st1602_composite,
    validate_st1607_mismms,
    validate_st1607_security,
)
from stanag4609.transport.access_unit_timing import VideoAccessUnitPTSValidator
from stanag4609.transport.demux import (
    PATEvent,
    PESStreamEvent,
    PMTEvent,
    ProgramClockEvent,
    StreamKind,
    TransportDemuxer,
)
from stanag4609.transport.metadata import MetadataSTDDescriptor
from stanag4609.transport.metadata_stream import (
    KLVMetadataEvent,
    MetadataStreamDecoder,
    ST0601ContextProvider,
)
from stanag4609.transport.mpegts import TransportPacket, TransportStreamParser
from stanag4609.transport.pcr import PCRCadenceValidator
from stanag4609.transport.psi import (
    KLVCarriage,
    ProgramMapTable,
    PSISectionAssembler,
    find_klv_streams,
    mpeg2_crc32,
)
from stanag4609.transport.psi_timing import (
    PCRBracketedPSICadenceValidator,
    PSITimingIssue,
)
from stanag4609.transport.pts import PTSCadenceValidator
from stanag4609.transport.std import MetadataDelayIssue, MetadataDelayValidator
from stanag4609.transport.std_stream import (
    MetadataSTDStreamIssue,
    MetadataSTDStreamValidator,
)
from stanag4609.video import (
    AVCVideoPropertiesParser,
    H262VideoPropertiesParser,
    HEVCVideoPropertiesParser,
    MISPImageContext,
    VideoProperties,
)


class VerificationStatus(str, Enum):
    """Outcome of one verifier check."""

    PASS = "pass"
    WARNING = "warning"
    ERROR = "error"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class VerificationFinding:
    """One coalesced, actionable verification result."""

    status: VerificationStatus
    code: str
    message: str
    requirement: str | None = None
    program_number: int | None = None
    pid: int | None = None
    tags: tuple[int, ...] = ()
    count: int = 1
    first_offset: int | None = None
    last_offset: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation."""

        result: dict[str, Any] = {
            "status": self.status.value,
            "code": self.code,
            "message": self.message,
            "count": self.count,
        }
        for name, value in (
            ("requirement", self.requirement),
            ("program_number", self.program_number),
            ("pid", self.pid),
            ("first_offset", self.first_offset),
            ("last_offset", self.last_offset),
        ):
            if value is not None:
                result[name] = value
        if self.tags:
            result["tags"] = list(self.tags)
        return result


@dataclass(frozen=True, slots=True)
class AudioVerificationSummary:
    """Observed compressed-frame facts for one ST 1001 audio stream."""

    frames: int
    samples: int
    duration_seconds: float
    timestamped_frames: int
    sample_rates: tuple[int, ...]
    channel_counts: tuple[int, ...]
    unknown_channel_configurations: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "frames": self.frames,
            "samples": self.samples,
            "duration_seconds": self.duration_seconds,
            "timestamped_frames": self.timestamped_frames,
            "sample_rates": list(self.sample_rates),
            "channel_counts": list(self.channel_counts),
            "unknown_channel_configurations": self.unknown_channel_configurations,
        }


@dataclass(frozen=True, slots=True)
class VideoTimestampVerificationSummary:
    """Observed ST 0604 timestamp coverage for one compressed video stream."""

    access_units: int
    timestamps: int
    microsecond_timestamps: int
    nanosecond_timestamps: int
    first_microseconds: int | None
    last_microseconds: int | None
    first_nanoseconds: int | None
    last_nanoseconds: int | None
    unlocked_timestamps: int
    discontinuities: int
    parsing_errors: int
    timestamped_access_units: int = 0
    missing_access_units: int = 0
    duplicate_timestamp_access_units: int = 0
    unassociated_timestamps: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_units": self.access_units,
            "timestamps": self.timestamps,
            "timestamped_access_units": self.timestamped_access_units,
            "missing_access_units": self.missing_access_units,
            "duplicate_timestamp_access_units": self.duplicate_timestamp_access_units,
            "unassociated_timestamps": self.unassociated_timestamps,
            "microsecond_timestamps": self.microsecond_timestamps,
            "nanosecond_timestamps": self.nanosecond_timestamps,
            "first_microseconds": self.first_microseconds,
            "last_microseconds": self.last_microseconds,
            "first_nanoseconds": self.first_nanoseconds,
            "last_nanoseconds": self.last_nanoseconds,
            "unlocked_timestamps": self.unlocked_timestamps,
            "discontinuities": self.discontinuities,
            "parsing_errors": self.parsing_errors,
        }


@dataclass(frozen=True, slots=True)
class VideoPropertiesVerificationSummary:
    """Observed compressed-video sequence properties for one stream."""

    sequences: int
    latest: VideoProperties | None
    property_changes: int
    parsing_errors: int
    profile_level_violations: int = 0
    interlaced_sequences: int = 0
    ambiguous_scan_sequences: int = 0
    pixel_depth_violations: int = 0
    maximum_bit_depth: int | None = None
    level_picture_size_violations: int = 0
    level_picture_size_unverifiable: int = 0
    level_sample_rate_violations: int = 0
    level_sample_rate_unverifiable: int = 0
    h262_frame_rate_extension_violations: int = 0
    h262_bit_rate_violations: int = 0
    h262_vbv_buffer_violations: int = 0
    h262_chroma_format_violations: int = 0
    h262_constrained_parameters_violations: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequences": self.sequences,
            "latest": self.latest.to_dict() if self.latest is not None else None,
            "property_changes": self.property_changes,
            "parsing_errors": self.parsing_errors,
            "profile_level_violations": self.profile_level_violations,
            "interlaced_sequences": self.interlaced_sequences,
            "ambiguous_scan_sequences": self.ambiguous_scan_sequences,
            "pixel_depth_violations": self.pixel_depth_violations,
            "maximum_bit_depth": self.maximum_bit_depth,
            "level_picture_size_violations": self.level_picture_size_violations,
            "level_picture_size_unverifiable": self.level_picture_size_unverifiable,
            "level_sample_rate_violations": self.level_sample_rate_violations,
            "level_sample_rate_unverifiable": self.level_sample_rate_unverifiable,
            "h262_frame_rate_extension_violations": (
                self.h262_frame_rate_extension_violations
            ),
            "h262_bit_rate_violations": self.h262_bit_rate_violations,
            "h262_vbv_buffer_violations": self.h262_vbv_buffer_violations,
            "h262_chroma_format_violations": self.h262_chroma_format_violations,
            "h262_constrained_parameters_violations": (
                self.h262_constrained_parameters_violations
            ),
        }


@dataclass(frozen=True, slots=True)
class ST0601TagVerificationSummary:
    """Observed coverage and decode health for one ST 0601 local tag."""

    tag: int
    name: str | None
    packets_present: int
    occurrences: int
    zero_length_items: int
    decoding_issues: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "name": self.name,
            "packets_present": self.packets_present,
            "occurrences": self.occurrences,
            "zero_length_items": self.zero_length_items,
            "decoding_issues": self.decoding_issues,
        }


@dataclass(frozen=True, slots=True)
class ST0601StreamVerificationSummary:
    """Field inventory for one program/PID/metadata-service ST 0601 stream."""

    program_number: int
    pid: int
    metadata_service_id: int | None
    packets: int
    timestamped_packets: int
    invalid_or_missing_timestamp_packets: int
    first_timestamp: datetime | None
    last_timestamp: datetime | None
    first_misp_timestamp_microseconds: int | None
    last_misp_timestamp_microseconds: int | None
    utc_timestamped_packets: int
    utc_conversion_unavailable_packets: int
    first_utc_timestamp: datetime | None
    last_utc_timestamp: datetime | None
    timestamp_regressions: int
    duplicate_timestamps: int
    maximum_forward_gap_seconds: float | None
    mismms_coverage: tuple[MISMMSRequirementCoverage, ...] | None
    versions: tuple[int, ...]
    tags: tuple[ST0601TagVerificationSummary, ...]
    untracked_item_occurrences: int
    context_provided_packets: int = 0
    birth_timestamp_validated_packets: int = 0
    imap_precision_validated_items: int = 0
    vmti_context_validated_packets: int = 0
    ground_truth_validated_items: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "program_number": self.program_number,
            "pid": self.pid,
            "pid_hex": f"0x{self.pid:04X}",
            "metadata_service_id": self.metadata_service_id,
            "packets": self.packets,
            "timestamped_packets": self.timestamped_packets,
            "invalid_or_missing_timestamp_packets": self.invalid_or_missing_timestamp_packets,
            "timestamp_time_scale": "MISP",
            "first_timestamp": (
                None if self.first_timestamp is None else self.first_timestamp.isoformat()
            ),
            "last_timestamp": (
                None if self.last_timestamp is None else self.last_timestamp.isoformat()
            ),
            "first_misp_timestamp_microseconds": self.first_misp_timestamp_microseconds,
            "last_misp_timestamp_microseconds": self.last_misp_timestamp_microseconds,
            "utc_timestamped_packets": self.utc_timestamped_packets,
            "utc_conversion_unavailable_packets": self.utc_conversion_unavailable_packets,
            "first_utc_timestamp": (
                None
                if self.first_utc_timestamp is None
                else self.first_utc_timestamp.isoformat()
            ),
            "last_utc_timestamp": (
                None
                if self.last_utc_timestamp is None
                else self.last_utc_timestamp.isoformat()
            ),
            "timestamp_regressions": self.timestamp_regressions,
            "duplicate_timestamps": self.duplicate_timestamps,
            "maximum_forward_gap_seconds": self.maximum_forward_gap_seconds,
            "validation_context": {
                "packets_provided": self.context_provided_packets,
                "birth_timestamp_validated_packets": (
                    self.birth_timestamp_validated_packets
                ),
                "imap_precision_validated_items": self.imap_precision_validated_items,
                "vmti_context_validated_packets": self.vmti_context_validated_packets,
                "ground_truth_validated_items": self.ground_truth_validated_items,
            },
            "mismms_coverage": (
                None
                if self.mismms_coverage is None
                else [item.to_dict() for item in self.mismms_coverage]
            ),
            "versions": list(self.versions),
            "tags": {str(item.tag): item.to_dict() for item in self.tags},
            "untracked_item_occurrences": self.untracked_item_occurrences,
        }


@dataclass(frozen=True, slots=True)
class StreamVerificationSummary:
    """Observed and declared facts for one elementary stream."""

    program_number: int
    pid: int
    kind: str
    stream_type: int
    carriage: str | None
    codec: str | None
    transport_packets: int
    pes_packets: int
    payload_bytes: int
    timestamped_pes_packets: int
    first_pts: int | None
    last_pts: int | None
    audio: AudioVerificationSummary | None = None
    video_timestamps: VideoTimestampVerificationSummary | None = None
    video_properties: VideoPropertiesVerificationSummary | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation."""

        result: dict[str, Any] = {
            "program_number": self.program_number,
            "pid": self.pid,
            "pid_hex": f"0x{self.pid:04X}",
            "kind": self.kind,
            "stream_type": self.stream_type,
            "stream_type_hex": f"0x{self.stream_type:02X}",
            "carriage": self.carriage,
            "codec": self.codec,
            "transport_packets": self.transport_packets,
            "pes_packets": self.pes_packets,
            "payload_bytes": self.payload_bytes,
            "timestamped_pes_packets": self.timestamped_pes_packets,
            "first_pts": self.first_pts,
            "last_pts": self.last_pts,
        }
        if self.audio is not None:
            result["audio"] = self.audio.to_dict()
        if self.video_timestamps is not None:
            result["video_timestamps"] = self.video_timestamps.to_dict()
        if self.video_properties is not None:
            result["video_properties"] = self.video_properties.to_dict()
        return result


@dataclass(frozen=True, slots=True)
class FMVVerificationReport:
    """Immutable verification report for a finite FMV input."""

    source: str | None
    bytes_read: int
    transport_packets: int
    programs: tuple[int, ...]
    streams: tuple[StreamVerificationSummary, ...]
    klv_packets: int
    st0601_packets: int
    st0903_packets: int
    st0903_target_observations: int
    st0903_unique_targets: int
    unknown_klv_packets: int
    st0601_streams: tuple[ST0601StreamVerificationSummary, ...]
    findings: tuple[VerificationFinding, ...]

    @property
    def errors(self) -> tuple[VerificationFinding, ...]:
        return tuple(item for item in self.findings if item.status is VerificationStatus.ERROR)

    @property
    def warnings(self) -> tuple[VerificationFinding, ...]:
        return tuple(
            item for item in self.findings if item.status is VerificationStatus.WARNING
        )

    @property
    def passes(self) -> tuple[VerificationFinding, ...]:
        return tuple(item for item in self.findings if item.status is VerificationStatus.PASS)

    @property
    def not_applicable(self) -> tuple[VerificationFinding, ...]:
        return tuple(
            item
            for item in self.findings
            if item.status is VerificationStatus.NOT_APPLICABLE
        )

    @property
    def ok(self) -> bool:
        """Whether no conformance or structural error was observed."""

        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        """Return a stable JSON-compatible report."""

        return {
            "schema_version": 1,
            "result": "pass" if self.ok else "fail",
            "source": self.source,
            "summary": {
                "errors": len(self.errors),
                "warnings": len(self.warnings),
                "passed": len(self.passes),
                "not_applicable": len(self.not_applicable),
            },
            "input": {
                "bytes": self.bytes_read,
                "transport_packets": self.transport_packets,
                "programs": list(self.programs),
            },
            "metadata": {
                "klv_packets": self.klv_packets,
                "st0601_packets": self.st0601_packets,
                "st0903_packets": self.st0903_packets,
                "st0903_target_observations": self.st0903_target_observations,
                "st0903_unique_targets": self.st0903_unique_targets,
                "unknown_klv_packets": self.unknown_klv_packets,
            },
            "streams": [stream.to_dict() for stream in self.streams],
            "st0601_streams": [stream.to_dict() for stream in self.st0601_streams],
            "findings": [finding.to_dict() for finding in self.findings],
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        """Serialize the report without losing exact integer timestamps."""

        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    def to_html(self, *, title: str = "FMV verification report") -> str:
        """Render a portable, dependency-free HTML diagnostic report."""

        from stanag4609.verification_html import render_verification_html

        return render_verification_html(self, title=title)

    def format_text(self) -> str:
        """Render a compact terminal-friendly report without ANSI escapes."""

        lines = [
            "FMV verification report",
            f"Source: {self.source or '<stream>'}",
            f"Result: {'PASS' if self.ok else 'FAIL'}",
            (
                f"Input: {self.bytes_read} bytes, {self.transport_packets} TS packets, "
                f"{len(self.programs)} program(s)"
            ),
            (
                f"Metadata: {self.klv_packets} KLV packet(s), "
                f"{self.st0601_packets} ST 0601, {self.st0903_packets} ST 0903, "
                f"{self.unknown_klv_packets} unknown"
            ),
            (
                f"VMTI targets: {self.st0903_target_observations} observation(s), "
                f"{self.st0903_unique_targets} stream-scoped unique ID(s)"
            ),
            "",
            "Streams:",
        ]
        if not self.streams:
            lines.append("  (none discovered)")
        for stream in self.streams:
            detail = f"stream_type=0x{stream.stream_type:02X}, PES={stream.pes_packets}"
            if stream.carriage is not None:
                detail += f", {stream.carriage}"
            if stream.codec is not None:
                detail += f", {stream.codec}"
            if stream.audio is not None:
                detail += f", frames={stream.audio.frames}"
                if len(stream.audio.sample_rates) == 1:
                    detail += f", {stream.audio.sample_rates[0]} Hz"
                if len(stream.audio.channel_counts) == 1:
                    detail += f", {stream.audio.channel_counts[0]} channel(s)"
            if stream.video_timestamps is not None:
                detail += (
                    f", ST0604={stream.video_timestamps.timestamped_access_units}/"
                    f"{stream.video_timestamps.access_units} access units"
                )
            if stream.video_properties is not None and stream.video_properties.latest:
                properties = stream.video_properties.latest
                scan = (
                    "unknown-scan"
                    if properties.progressive is None
                    else "progressive"
                    if properties.progressive
                    else "interlaced"
                )
                detail += (
                    f", {properties.width}x{properties.height} {properties.profile} "
                    f"Level {properties.level} {scan}"
                )
            lines.append(
                f"  program {stream.program_number} PID 0x{stream.pid:04X} "
                f"{stream.kind}: {detail}"
            )
        lines.extend(("", "ST 0601 services:"))
        if not self.st0601_streams:
            lines.append("  (none decoded)")
        for metadata in self.st0601_streams:
            known = sum(item.name is not None for item in metadata.tags)
            unknown = len(metadata.tags) - known
            service = (
                "asynchronous"
                if metadata.metadata_service_id is None
                else f"service {metadata.metadata_service_id}"
            )
            lines.append(
                f"  program {metadata.program_number} PID 0x{metadata.pid:04X} {service}: "
                f"packets={metadata.packets}, tags={known} known/{unknown} unknown, "
                f"versions={list(metadata.versions)}"
            )
            lines.append(
                "    MISP Item 2: "
                f"first={metadata.first_misp_timestamp_microseconds}, "
                f"last={metadata.last_misp_timestamp_microseconds}; "
                f"UTC converted={metadata.utc_timestamped_packets}, "
                f"unavailable={metadata.utc_conversion_unavailable_packets}"
            )
            lines.append(
                "    ST 0601 external context: "
                f"{metadata.context_provided_packets}/{metadata.packets} packet(s), "
                f"birth={metadata.birth_timestamp_validated_packets}, "
                f"IMAP items={metadata.imap_precision_validated_items}, "
                f"VMTI={metadata.vmti_context_validated_packets}, "
                f"ground truth items={metadata.ground_truth_validated_items}"
            )
            if metadata.mismms_coverage is not None:
                statuses = {
                    status: sum(
                        item.status is status for item in metadata.mismms_coverage
                    )
                    for status in MISMMSPopulationStatus
                }
                lines.append(
                    "    ST 0902 population: "
                    f"{statuses[MISMMSPopulationStatus.CURRENT]} current, "
                    f"{statuses[MISMMSPopulationStatus.MISSING]} missing, "
                    f"{statuses[MISMMSPopulationStatus.OVERDUE]} overdue"
                )
        lines.extend(("", "Checks:"))
        labels = {
            VerificationStatus.PASS: "PASS",
            VerificationStatus.WARNING: "WARN",
            VerificationStatus.ERROR: "ERROR",
            VerificationStatus.NOT_APPLICABLE: "N/A",
        }
        for finding in self.findings:
            context: list[str] = []
            if finding.program_number is not None:
                context.append(f"program {finding.program_number}")
            if finding.pid is not None:
                context.append(f"PID 0x{finding.pid:04X}")
            if finding.requirement is not None:
                context.append(finding.requirement)
            suffix = f" ({', '.join(context)})" if context else ""
            repeated = f" [x{finding.count}]" if finding.count > 1 else ""
            lines.append(
                f"  [{labels[finding.status]}] {finding.code}{suffix}: "
                f"{finding.message}{repeated}"
            )
        lines.append(
            f"\nSummary: {len(self.errors)} error(s), {len(self.warnings)} warning(s), "
            f"{len(self.passes)} passed, {len(self.not_applicable)} not applicable"
        )
        return "\n".join(lines)


@dataclass(slots=True)
class _StreamStats:
    program_number: int
    pid: int
    kind: str
    stream_type: int
    carriage: str | None
    codec: str | None
    pes_packets: int = 0
    payload_bytes: int = 0
    timestamped_pes_packets: int = 0
    first_pts: int | None = None
    last_pts: int | None = None


@dataclass(slots=True)
class _AudioStats:
    parser: AudioPESFrameParser = field(default_factory=AudioPESFrameParser)
    frames: int = 0
    samples: int = 0
    duration_seconds: Fraction = Fraction(0)
    timestamped_frames: int = 0
    sample_rates: set[int] = field(default_factory=set)
    channel_counts: set[int] = field(default_factory=set)
    unknown_channel_configurations: int = 0
    last_offset: int | None = None
    needs_initial_pts: bool = True

    def observe(self, frames: Sequence[TimedCompressedAudioFrame]) -> None:
        for timed in frames:
            frame_value = timed.frame
            self.frames += 1
            self.samples += frame_value.sample_count
            self.duration_seconds += frame_value.duration_seconds
            self.sample_rates.add(frame_value.sample_rate)
            if frame_value.channel_count is None:
                self.unknown_channel_configurations += 1
            else:
                self.channel_counts.add(frame_value.channel_count)
            if timed.presentation_ticks is not None:
                self.timestamped_frames += 1

    def summary(self) -> AudioVerificationSummary:
        return AudioVerificationSummary(
            self.frames,
            self.samples,
            float(self.duration_seconds),
            self.timestamped_frames,
            tuple(sorted(self.sample_rates)),
            tuple(sorted(self.channel_counts)),
            self.unknown_channel_configurations,
        )


@dataclass(slots=True)
class _VideoTimestampStats:
    stream_type: int
    parser: VideoTimestampedAccessUnitParser = field(init=False)
    closed_access_units: int = 0
    timestamps: int = 0
    timestamped_access_units: int = 0
    duplicate_timestamp_access_units: int = 0
    unassociated_timestamps: int = 0
    microsecond_timestamps: int = 0
    nanosecond_timestamps: int = 0
    first_microseconds: int | None = None
    last_microseconds: int | None = None
    first_nanoseconds: int | None = None
    last_nanoseconds: int | None = None
    unlocked_timestamps: int = 0
    discontinuities: int = 0
    parsing_errors: int = 0

    def __post_init__(self) -> None:
        self.parser = VideoTimestampedAccessUnitParser(self.stream_type)

    @property
    def access_units(self) -> int:
        return self.closed_access_units + self.parser.access_units

    def _observe_timestamps(self, timestamps: Sequence[EmbeddedVideoTimestamp]) -> None:
        for timestamp in timestamps:
            self.timestamps += 1
            if timestamp.resolution is TimestampResolution.MICROSECONDS:
                self.microsecond_timestamps += 1
                if self.first_microseconds is None:
                    self.first_microseconds = timestamp.value
                self.last_microseconds = timestamp.value
            else:
                self.nanosecond_timestamps += 1
                if self.first_nanoseconds is None:
                    self.first_nanoseconds = timestamp.value
                self.last_nanoseconds = timestamp.value
            if not timestamp.time_status.locked:
                self.unlocked_timestamps += 1
            if timestamp.time_status.discontinuity:
                self.discontinuities += 1

    def observe(self, access_units: Sequence[TimestampedVideoAccessUnit]) -> None:
        for access_unit in access_units:
            if access_unit.timestamps:
                self.timestamped_access_units += 1
            if len(access_unit.timestamps) > 1:
                self.duplicate_timestamp_access_units += 1
            self._observe_timestamps(access_unit.timestamps)

    def finish_parser(self) -> None:
        self.observe(self.parser.finish())
        unassociated = self.parser.unassociated_timestamps
        self.unassociated_timestamps += len(unassociated)
        self._observe_timestamps(unassociated)

    def reset_parser(self) -> None:
        try:
            self.finish_parser()
        except Stanag4609Error:
            self.parsing_errors += 1
        self.closed_access_units += self.parser.access_units
        self.parser = VideoTimestampedAccessUnitParser(self.stream_type)

    def summary(self) -> VideoTimestampVerificationSummary:
        return VideoTimestampVerificationSummary(
            access_units=self.access_units,
            timestamps=self.timestamps,
            microsecond_timestamps=self.microsecond_timestamps,
            nanosecond_timestamps=self.nanosecond_timestamps,
            first_microseconds=self.first_microseconds,
            last_microseconds=self.last_microseconds,
            first_nanoseconds=self.first_nanoseconds,
            last_nanoseconds=self.last_nanoseconds,
            unlocked_timestamps=self.unlocked_timestamps,
            discontinuities=self.discontinuities,
            parsing_errors=self.parsing_errors,
            timestamped_access_units=self.timestamped_access_units,
            missing_access_units=self.access_units - self.timestamped_access_units,
            duplicate_timestamp_access_units=self.duplicate_timestamp_access_units,
            unassociated_timestamps=self.unassociated_timestamps,
        )


@dataclass(slots=True)
class _VideoPropertiesStats:
    stream_type: int
    parser: (
        H262VideoPropertiesParser | AVCVideoPropertiesParser | HEVCVideoPropertiesParser
    ) = field(init=False)
    sequences: int = 0
    latest: VideoProperties | None = None
    property_changes: int = 0
    parsing_errors: int = 0
    profile_level_violations: int = 0
    interlaced_sequences: int = 0
    ambiguous_scan_sequences: int = 0
    pixel_depth_violations: int = 0
    maximum_bit_depth: int | None = None
    level_picture_size_violations: int = 0
    level_picture_size_unverifiable: int = 0
    level_sample_rate_violations: int = 0
    level_sample_rate_unverifiable: int = 0
    h262_frame_rate_extension_violations: int = 0
    h262_bit_rate_violations: int = 0
    h262_vbv_buffer_violations: int = 0
    h262_chroma_format_violations: int = 0
    h262_constrained_parameters_violations: int = 0

    def __post_init__(self) -> None:
        self.parser = self._new_parser()

    def _new_parser(
        self,
    ) -> H262VideoPropertiesParser | AVCVideoPropertiesParser | HEVCVideoPropertiesParser:
        if self.stream_type == 0x02:
            return H262VideoPropertiesParser()
        if self.stream_type == 0x1B:
            return AVCVideoPropertiesParser()
        return HEVCVideoPropertiesParser()

    def observe(self, records: Sequence[VideoProperties]) -> None:
        for properties in records:
            self.sequences += 1
            if not properties.misp_profile_level:
                self.profile_level_violations += 1
            if properties.progressive is False:
                self.interlaced_sequences += 1
            elif properties.progressive is None:
                self.ambiguous_scan_sequences += 1
            if any(
                depth is not None and depth > 8
                for depth in (properties.bit_depth_luma, properties.bit_depth_chroma)
            ):
                self.pixel_depth_violations += 1
            known_depths = tuple(
                depth
                for depth in (properties.bit_depth_luma, properties.bit_depth_chroma)
                if depth is not None
            )
            if known_depths:
                sequence_maximum = max(known_depths)
                self.maximum_bit_depth = max(
                    sequence_maximum,
                    self.maximum_bit_depth or sequence_maximum,
                )
            picture_size = properties.level_picture_size_conforms
            if picture_size is False:
                self.level_picture_size_violations += 1
            elif picture_size is None:
                self.level_picture_size_unverifiable += 1
            sample_rate = properties.level_sample_rate_conforms
            if sample_rate is False:
                self.level_sample_rate_violations += 1
            elif sample_rate is None:
                self.level_sample_rate_unverifiable += 1
            if properties.h262_frame_rate_extension_conforms is False:
                self.h262_frame_rate_extension_violations += 1
            if properties.h262_level_bit_rate_conforms is False:
                self.h262_bit_rate_violations += 1
            if properties.h262_level_vbv_buffer_conforms is False:
                self.h262_vbv_buffer_violations += 1
            if properties.h262_main_profile_chroma_conforms is False:
                self.h262_chroma_format_violations += 1
            if properties.h262_constrained_parameters_conforms is False:
                self.h262_constrained_parameters_violations += 1
            if self.latest is not None and properties != self.latest:
                self.property_changes += 1
            self.latest = properties

    def reset_parser(self) -> None:
        self.parser = self._new_parser()

    def summary(self) -> VideoPropertiesVerificationSummary:
        return VideoPropertiesVerificationSummary(
            self.sequences,
            self.latest,
            self.property_changes,
            self.parsing_errors,
            self.profile_level_violations,
            self.interlaced_sequences,
            self.ambiguous_scan_sequences,
            self.pixel_depth_violations,
            self.maximum_bit_depth,
            self.level_picture_size_violations,
            self.level_picture_size_unverifiable,
            self.level_sample_rate_violations,
            self.level_sample_rate_unverifiable,
            self.h262_frame_rate_extension_violations,
            self.h262_bit_rate_violations,
            self.h262_vbv_buffer_violations,
            self.h262_chroma_format_violations,
            self.h262_constrained_parameters_violations,
        )


@dataclass(slots=True)
class _ST0601TagStats:
    name: str | None
    packets_present: int = 0
    occurrences: int = 0
    zero_length_items: int = 0
    decoding_issues: int = 0


@dataclass(slots=True)
class _ST0601Stats:
    max_tags: int
    packets: int = 0
    timestamped_packets: int = 0
    invalid_or_missing_timestamp_packets: int = 0
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None
    first_misp_timestamp_microseconds: int | None = None
    last_misp_timestamp_microseconds: int | None = None
    utc_timestamped_packets: int = 0
    utc_conversion_unavailable_packets: int = 0
    first_utc_timestamp: datetime | None = None
    last_utc_timestamp: datetime | None = None
    previous_timestamp: datetime | None = None
    timestamp_regressions: int = 0
    duplicate_timestamps: int = 0
    maximum_forward_gap_seconds: float | None = None
    context_provided_packets: int = 0
    birth_timestamp_validated_packets: int = 0
    imap_precision_validated_items: int = 0
    vmti_context_validated_packets: int = 0
    ground_truth_validated_items: int = 0
    versions: set[int] = field(default_factory=set)
    tags: dict[int, _ST0601TagStats] = field(default_factory=dict)
    untracked_item_occurrences: int = 0
    _time_adjustments: dict[int, tuple[int, datetime]] = field(default_factory=dict)

    def observe(
        self,
        uas: UASLocalSet,
        context: ST0601ValidationContext | None = None,
    ) -> None:
        self.packets += 1
        if context is not None:
            self.context_provided_packets += 1
            if context.metadata_birth_timestamp is not None:
                self.birth_timestamp_validated_packets += 1
            present_tags = {item.tag for item in uas.local_set.items}
            self.imap_precision_validated_items += len(
                present_tags.intersection(context.imap_system_precisions)
            )
            if context.vmti_context is not None and isinstance(
                uas.value(74), VMTILocalSet
            ):
                self.vmti_context_validated_packets += 1
            self.ground_truth_validated_items += len(
                present_tags.intersection(context.field_expectations)
            )
        timestamp = uas.value(2)
        if isinstance(timestamp, datetime):
            self.timestamped_packets += 1
            monotonic = self.last_timestamp is None or timestamp >= self.last_timestamp
            if self.first_timestamp is None or timestamp < self.first_timestamp:
                self.first_timestamp = timestamp
            if self.last_timestamp is None or timestamp > self.last_timestamp:
                self.last_timestamp = timestamp
            if self.previous_timestamp is not None:
                gap_seconds = (timestamp - self.previous_timestamp).total_seconds()
                if gap_seconds < 0:
                    self.timestamp_regressions += 1
                elif gap_seconds == 0:
                    self.duplicate_timestamps += 1
                elif (
                    self.maximum_forward_gap_seconds is None
                    or gap_seconds > self.maximum_forward_gap_seconds
                ):
                    self.maximum_forward_gap_seconds = gap_seconds
            self.previous_timestamp = timestamp

            raw_timestamp = uas.misp_timestamp_microseconds
            if raw_timestamp is not None:
                if (
                    self.first_misp_timestamp_microseconds is None
                    or raw_timestamp < self.first_misp_timestamp_microseconds
                ):
                    self.first_misp_timestamp_microseconds = raw_timestamp
                if (
                    self.last_misp_timestamp_microseconds is None
                    or raw_timestamp > self.last_misp_timestamp_microseconds
                ):
                    self.last_misp_timestamp_microseconds = raw_timestamp
                self._observe_utc_timestamp(uas, timestamp, raw_timestamp, monotonic)
        else:
            self.invalid_or_missing_timestamp_packets += 1
        version = uas.value(65)
        if isinstance(version, int) and not isinstance(version, bool):
            self.versions.add(version)

        items_by_tag: dict[int, list[bytes]] = {}
        for item in uas.local_set.items:
            items_by_tag.setdefault(item.tag, []).append(item.value)
        issues_by_tag: dict[int, int] = {}
        for issue in uas.issues:
            issues_by_tag[issue.tag] = issues_by_tag.get(issue.tag, 0) + 1
        for tag, values in items_by_tag.items():
            tag_stats = self.tags.get(tag)
            if tag_stats is None:
                if len(self.tags) >= self.max_tags:
                    self.untracked_item_occurrences += len(values)
                    continue
                definition = FIELD_DEFINITIONS.get(tag)
                tag_stats = _ST0601TagStats(
                    None if definition is None else definition.name
                )
                self.tags[tag] = tag_stats
            tag_stats.packets_present += 1
            tag_stats.occurrences += len(values)
            tag_stats.zero_length_items += sum(not value for value in values)
            tag_stats.decoding_issues += issues_by_tag.get(tag, 0)

    def _observe_utc_timestamp(
        self,
        uas: UASLocalSet,
        timestamp: datetime,
        raw_timestamp: int,
        monotonic: bool,
    ) -> None:
        if not monotonic:
            self.utc_conversion_unavailable_packets += 1
            return

        for tag, (_, observed_at) in tuple(self._time_adjustments.items()):
            if timestamp - observed_at > timedelta(seconds=30):
                del self._time_adjustments[tag]

        for tag in (136, 137):
            raw_items = uas.local_set.getall(tag)
            if any(not item.value for item in raw_items):
                self._time_adjustments.pop(tag, None)
                continue
            value = uas.value(tag)
            if isinstance(value, int) and not isinstance(value, bool):
                self._time_adjustments[tag] = (value, timestamp)

        leap_state = self._time_adjustments.get(136)
        if leap_state is None:
            self.utc_conversion_unavailable_packets += 1
            return
        correction_state = self._time_adjustments.get(137)
        utc_timestamp = misp_timestamp_to_utc(
            raw_timestamp,
            leap_seconds=leap_state[0],
            correction_offset=0 if correction_state is None else correction_state[0],
        )
        self.utc_timestamped_packets += 1
        if self.first_utc_timestamp is None or utc_timestamp < self.first_utc_timestamp:
            self.first_utc_timestamp = utc_timestamp
        if self.last_utc_timestamp is None or utc_timestamp > self.last_utc_timestamp:
            self.last_utc_timestamp = utc_timestamp

    def summary(
        self,
        key: tuple[int, int, int | None],
        mismms_coverage: tuple[MISMMSRequirementCoverage, ...] | None,
    ) -> ST0601StreamVerificationSummary:
        return ST0601StreamVerificationSummary(
            program_number=key[0],
            pid=key[1],
            metadata_service_id=key[2],
            packets=self.packets,
            timestamped_packets=self.timestamped_packets,
            invalid_or_missing_timestamp_packets=self.invalid_or_missing_timestamp_packets,
            first_timestamp=self.first_timestamp,
            last_timestamp=self.last_timestamp,
            first_misp_timestamp_microseconds=self.first_misp_timestamp_microseconds,
            last_misp_timestamp_microseconds=self.last_misp_timestamp_microseconds,
            utc_timestamped_packets=self.utc_timestamped_packets,
            utc_conversion_unavailable_packets=self.utc_conversion_unavailable_packets,
            first_utc_timestamp=self.first_utc_timestamp,
            last_utc_timestamp=self.last_utc_timestamp,
            timestamp_regressions=self.timestamp_regressions,
            duplicate_timestamps=self.duplicate_timestamps,
            maximum_forward_gap_seconds=self.maximum_forward_gap_seconds,
            context_provided_packets=self.context_provided_packets,
            birth_timestamp_validated_packets=self.birth_timestamp_validated_packets,
            imap_precision_validated_items=self.imap_precision_validated_items,
            vmti_context_validated_packets=self.vmti_context_validated_packets,
            ground_truth_validated_items=self.ground_truth_validated_items,
            mismms_coverage=mismms_coverage,
            versions=tuple(sorted(self.versions)),
            tags=tuple(
                ST0601TagVerificationSummary(
                    tag,
                    stats.name,
                    stats.packets_present,
                    stats.occurrences,
                    stats.zero_length_items,
                    stats.decoding_issues,
                )
                for tag, stats in sorted(self.tags.items())
            ),
            untracked_item_occurrences=self.untracked_item_occurrences,
        )


@dataclass(frozen=True, slots=True)
class _ContinuityState:
    counter: int
    raw: bytes


class FMVVerifier:
    """Incrementally inspect an FMV transport and produce a finite report.

    The verifier never needs to retain the complete input. Unique findings are
    bounded and repeated findings are coalesced with an occurrence count.
    """

    def __init__(
        self,
        *,
        require_security: bool = True,
        require_miis: bool = True,
        require_audio: bool = False,
        validate_mismms: bool = True,
        security_context: MISMMSecurityContext | None = None,
        ontology_resolver: OntologyResolver | None = None,
        st0601_context_provider: ST0601ContextProvider | None = None,
        image_context: MISPImageContext | None = None,
        asynchronous_std_descriptors: Mapping[
            tuple[int, int], MetadataSTDDescriptor
        ] | None = None,
        max_findings: int = 10_000,
        max_st0601_tags_per_stream: int = 4_096,
    ) -> None:
        policies = (require_security, require_miis, require_audio, validate_mismms)
        if not all(isinstance(value, bool) for value in policies):
            raise TypeError("verifier policy options must be booleans")
        if isinstance(max_findings, bool) or not isinstance(max_findings, int) or max_findings < 1:
            raise ValueError("max_findings must be a positive integer")
        if (
            isinstance(max_st0601_tags_per_stream, bool)
            or not isinstance(max_st0601_tags_per_stream, int)
            or max_st0601_tags_per_stream < 1
        ):
            raise ValueError("max_st0601_tags_per_stream must be a positive integer")
        if security_context is None:
            security_context = MISMMSecurityContext()
        if not isinstance(security_context, MISMMSecurityContext):
            raise TypeError("security_context must be a MISMMSecurityContext")
        if ontology_resolver is not None and not isinstance(
            ontology_resolver, OntologyResolver
        ):
            raise TypeError("ontology_resolver must implement resolve_entity or be None")
        if st0601_context_provider is not None and not callable(
            st0601_context_provider
        ):
            raise TypeError("st0601_context_provider must be callable or None")
        if image_context is not None and not isinstance(image_context, MISPImageContext):
            raise TypeError("image_context must be a MISPImageContext or None")
        if not require_security and security_context.has_policy:
            raise ValueError("security_context cannot require fields when require_security=False")
        if not validate_mismms and security_context.has_policy:
            raise ValueError("security_context policy requires validate_mismms=True")

        self._require_security = require_security
        self._require_miis = require_miis
        self._require_audio = require_audio
        self._validate_mismms = validate_mismms
        self._security_context = security_context
        self._ontology_resolver = ontology_resolver
        self._st0601_context_provider = st0601_context_provider
        self._image_context = image_context
        self._max_findings = max_findings
        self._max_st0601_tags_per_stream = max_st0601_tags_per_stream
        self._packet_parser = TransportStreamParser()
        self._demuxer = TransportDemuxer()
        self._pcr = PCRCadenceValidator()
        self._psi_timing = PCRBracketedPSICadenceValidator()
        self._sdt_assembler = PSISectionAssembler(pid=0x11)
        self._pts = PTSCadenceValidator()
        self._first_video_pts = VideoAccessUnitPTSValidator()
        self._metadata_delay = MetadataDelayValidator()
        self._metadata_std = MetadataSTDStreamValidator(
            asynchronous_descriptors=asynchronous_std_descriptors
        )
        self._metadata_decoders: dict[int, MetadataStreamDecoder] = {}
        self._mismms: dict[tuple[int, int, int | None], MISMMSValidator] = {}
        self._metadata_trees: dict[
            tuple[int, int, int | None], MetadataTreeState
        ] = {}
        self._metadata_tree_snapshots: dict[
            tuple[int, int, int | None], MetadataTreeSnapshot
        ] = {}
        self._control_commands: dict[
            tuple[int, int, int | None], ControlCommandState
        ] = {}
        self._wavelength_states: dict[
            tuple[int, int, int | None], WavelengthTableState
        ] = {}
        self._payload_states: dict[
            tuple[int, int, int | None], PayloadTableState
        ] = {}
        self._weapons_states: dict[
            tuple[int, int, int | None], WeaponsStoresState
        ] = {}
        self._waypoint_states: dict[
            tuple[int, int, int | None], WaypointListState
        ] = {}
        self._vmti_lifecycles: dict[
            tuple[int, int, int | None], VMTILifecycleState
        ] = {}
        self._st0601_inventory: dict[tuple[int, int, int | None], _ST0601Stats] = {}
        self._security_carriages: dict[
            int, dict[KLVCarriage, tuple[int, int | None]]
        ] = {}
        self._audio: dict[tuple[int, int], _AudioStats] = {}
        self._audio_failed: set[tuple[int, int]] = set()
        self._video_timestamps: dict[tuple[int, int], _VideoTimestampStats] = {}
        self._video_properties: dict[tuple[int, int], _VideoPropertiesStats] = {}
        self._findings: dict[tuple[object, ...], VerificationFinding] = {}
        self._continuity: dict[int, _ContinuityState] = {}
        self._transport_pid_packets: dict[int, int] = {}
        self._streams: dict[tuple[int, int], _StreamStats] = {}
        self._pmts: dict[int, ProgramMapTable] = {}
        self._validated_pmts: dict[int, tuple[int, bytes]] = {}
        self._pat_count = 0
        self._pmt_count = 0
        self._bytes_read = 0
        self._transport_packets = 0
        self._scrambled_packets = 0
        self._klv_packets = 0
        self._st0601_packets = 0
        self._st0903_packets = 0
        self._st0903_target_observations = 0
        self._unknown_klv_packets = 0
        self._demux_failed = False
        self._packet_scan_failed = False
        self._finished = False
        self._suppressed_findings = 0
        self._retained_error_count = 0

    def feed(self, data: bytes | bytearray | memoryview) -> None:
        """Consume an arbitrary binary chunk."""

        if self._finished:
            raise RuntimeError("FMV verifier is already finished")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("data must be bytes-like")
        self._bytes_read += len(data)
        if not self._packet_scan_failed:
            try:
                for packet in self._packet_parser.feed(data):
                    self._observe_transport_packet(packet)
            except Stanag4609Error as exc:
                self._add(VerificationStatus.ERROR, "transport.structure", str(exc))
                self._packet_scan_failed = True
        if not self._demux_failed:
            try:
                self._consume_events(self._demuxer.feed(data))
            except Stanag4609Error as exc:
                self._add(
                    VerificationStatus.ERROR,
                    "transport.decode",
                    str(exc),
                )
                self._demux_failed = True

    def finish(self, *, source: str | None = None) -> FMVVerificationReport:
        """Finalize the finite input and return its report."""

        if self._finished:
            raise RuntimeError("FMV verifier is already finished")
        self._finished = True
        if not self._packet_scan_failed:
            try:
                for packet in self._packet_parser.finish():
                    self._observe_transport_packet(packet)
            except Stanag4609Error as exc:
                self._add(VerificationStatus.ERROR, "transport.structure", str(exc))
            try:
                self._sdt_assembler.finish()
            except Stanag4609Error as exc:
                self._add(
                    VerificationStatus.ERROR,
                    "psi.structure",
                    f"DVB service-information structure is invalid: {exc}",
                    requirement="ETSI EN 300 468",
                    pid=0x11,
                )
        if not self._demux_failed:
            try:
                self._consume_events(self._demuxer.finish())
            except Stanag4609Error as exc:
                self._add(VerificationStatus.ERROR, "transport.decode", str(exc))
        for pts_issue in self._first_video_pts.finish():
            self._add(
                VerificationStatus.ERROR,
                f"st1402.pts.{pts_issue.code}",
                pts_issue.message,
                requirement=pts_issue.requirement,
                program_number=pts_issue.program_number,
                pid=pts_issue.pid,
                offset=pts_issue.source_offset,
            )
        for key, video in self._video_timestamps.items():
            try:
                video.finish_parser()
            except Stanag4609Error as exc:
                video.parsing_errors += 1
                self._add(
                    VerificationStatus.ERROR,
                    "st0604.timestamp.decode",
                    str(exc),
                    requirement="MISB ST 0604.6",
                    program_number=key[0],
                    pid=key[1],
                )
        for key, property_stats in self._video_properties.items():
            try:
                property_stats.observe(property_stats.parser.finish())
            except Stanag4609Error as exc:
                property_stats.parsing_errors += 1
                self._add(
                    VerificationStatus.ERROR,
                    "video.properties.decode",
                    str(exc),
                    requirement="ITU-T H.262 / H.264 sequence syntax",
                    program_number=key[0],
                    pid=key[1],
                )
        for pid, decoder in self._metadata_decoders.items():
            try:
                decoder.finish()
            except Stanag4609Error as exc:
                self._add(
                    VerificationStatus.ERROR,
                    "metadata.decode",
                    str(exc),
                    pid=pid,
                )
        self._metadata_delay.finish()
        for metadata_std_issue in self._metadata_std.finish():
            self._add_metadata_std(metadata_std_issue)
        if self._metadata_delay.indeterminate_pes:
            self._add(
                VerificationStatus.WARNING,
                "st1402.metadata_delay.indeterminate",
                f"{self._metadata_delay.indeterminate_pes} synchronous metadata PES "
                "packet(s) straddled the one-second delay boundary between PCR samples",
                requirement="ST 1402.2 ST 1402-12",
            )
        if self._metadata_delay.unverifiable_pes:
            self._add(
                VerificationStatus.WARNING,
                "st1402.metadata_delay.unverifiable",
                f"{self._metadata_delay.unverifiable_pes} synchronous metadata PES "
                "packet(s) lacked a complete PCR arrival-time bracket",
                requirement="ST 1402.2 ST 1402-12",
            )
        if self._metadata_std.unverifiable_pes:
            self._add(
                VerificationStatus.WARNING,
                "st1402.metadata_std.unverifiable",
                f"{self._metadata_std.unverifiable_pes} metadata PES "
                "packet(s) could not be included in exact T-STD occupancy simulation",
                requirement="ITU-T H.222.0 §§2.4.2.2-2.4.2.6, 2.12.10",
            )
        if self._metadata_std.compliant:
            self._add(
                VerificationStatus.PASS,
                "st1402.metadata_std",
                "every configured metadata PES was included in exact PCR-derived "
                "transport and main-buffer simulation without a violation",
                requirement="ITU-T H.222.0 §§2.4.2.2-2.4.2.6, 2.12.10",
            )
        for key, audio in self._audio.items():
            if key in self._audio_failed:
                continue
            try:
                audio.observe(audio.parser.finish())
            except Stanag4609Error as exc:
                self._audio_failed.add(key)
                self._add_audio_error(
                    str(exc),
                    program_number=key[0],
                    pid=key[1],
                    offset=audio.last_offset,
                )
        for profile_key, validator in self._mismms.items():
            program_number, pid, _service_id = profile_key
            tree_snapshot = self._metadata_tree_snapshots.get(profile_key)
            if tree_snapshot is not None and tree_snapshot.branches:
                continue
            for issue in validator.finish():
                self._add_mismms(issue, program_number=program_number, pid=pid)
        for (program_number, pid, _service_id), inventory in self._st0601_inventory.items():
            if inventory.untracked_item_occurrences:
                self._add(
                    VerificationStatus.WARNING,
                    "st0601.inventory.truncated",
                    f"{inventory.untracked_item_occurrences} item occurrence(s) were not "
                    "retained after the per-stream tag inventory limit was reached",
                    program_number=program_number,
                    pid=pid,
                )
            if inventory.timestamp_regressions:
                self._add(
                    VerificationStatus.WARNING,
                    "st0601.timestamp.regression",
                    f"metadata timestamps regressed {inventory.timestamp_regressions} time(s) "
                    "in transport order",
                    requirement="ST 0601 Precision Time Stamp chronology diagnostic",
                    program_number=program_number,
                    pid=pid,
                )
        self._add_summary_checks()
        if self._suppressed_findings:
            self._add_unbounded(
                VerificationStatus.WARNING,
                "report.truncated",
                f"suppressed {self._suppressed_findings} unique finding(s) after reaching "
                f"the configured limit of {self._max_findings}",
            )

        audio_summaries = {key: value.summary() for key, value in self._audio.items()}
        mismms_coverage = {
            key: profile_validator.coverage()
            for key, profile_validator in self._mismms.items()
            if not self._metadata_tree_snapshots.get(key)
            or not self._metadata_tree_snapshots[key].branches
        }
        st0601_summaries = tuple(
            inventory.summary(key, mismms_coverage.get(key))
            for key, inventory in sorted(
                self._st0601_inventory.items(),
                key=lambda item: (
                    item[0][0],
                    item[0][1],
                    -1 if item[0][2] is None else item[0][2],
                ),
            )
        )
        streams = tuple(
            StreamVerificationSummary(
                stats.program_number,
                stats.pid,
                stats.kind,
                stats.stream_type,
                stats.carriage,
                stats.codec,
                self._transport_pid_packets.get(stats.pid, 0),
                stats.pes_packets,
                stats.payload_bytes,
                stats.timestamped_pes_packets,
                stats.first_pts,
                stats.last_pts,
                audio_summaries.get((stats.program_number, stats.pid)),
                (
                    self._video_timestamps[(stats.program_number, stats.pid)].summary()
                    if (stats.program_number, stats.pid) in self._video_timestamps
                    else None
                ),
                (
                    self._video_properties[(stats.program_number, stats.pid)].summary()
                    if (stats.program_number, stats.pid) in self._video_properties
                    else None
                ),
            )
            for stats in sorted(
                self._streams.values(), key=lambda item: (item.program_number, item.pid)
            )
        )
        return FMVVerificationReport(
            source,
            self._bytes_read,
            self._transport_packets,
            tuple(sorted(self._pmts)),
            streams,
            self._klv_packets,
            self._st0601_packets,
            self._st0903_packets,
            self._st0903_target_observations,
            sum(
                len(state.snapshot().targets)
                for state in self._vmti_lifecycles.values()
            ),
            self._unknown_klv_packets,
            st0601_summaries,
            tuple(self._findings.values()),
        )

    def _observe_transport_packet(self, packet: TransportPacket) -> None:
        self._transport_packets += 1
        self._transport_pid_packets[packet.pid] = (
            self._transport_pid_packets.get(packet.pid, 0) + 1
        )
        if packet.scrambling_control:
            self._scrambled_packets += 1
        if packet.discontinuity_indicator:
            self._continuity.pop(packet.pid, None)
            if packet.pid == 0x11:
                self._sdt_assembler = PSISectionAssembler(pid=0x11)
        if packet.pid == 0x1FFF or not packet.has_payload or not packet.payload:
            return
        previous = self._continuity.get(packet.pid)
        if previous is None:
            self._continuity[packet.pid] = _ContinuityState(
                packet.continuity_counter, packet.raw
            )
        elif packet.continuity_counter == previous.counter and packet.raw == previous.raw:
            self._add(
                VerificationStatus.WARNING,
                "transport.duplicate",
                "exact duplicate MPEG-TS payload packet observed",
                pid=packet.pid,
                offset=packet.offset,
            )
            return
        else:
            expected = (previous.counter + 1) & 0x0F
            if packet.continuity_counter != expected:
                self._add(
                    VerificationStatus.ERROR,
                    "transport.continuity",
                    (
                        f"payload continuity counter is {packet.continuity_counter}, "
                        f"expected {expected}"
                    ),
                    requirement="ISO/IEC 13818-1 continuity_counter",
                    pid=packet.pid,
                    offset=packet.offset,
                )
                if packet.pid == 0x11:
                    self._sdt_assembler = PSISectionAssembler(pid=0x11)
            self._continuity[packet.pid] = _ContinuityState(
                packet.continuity_counter, packet.raw
            )
        if packet.pid == 0x11:
            self._observe_service_information(packet)

    def _observe_service_information(self, packet: TransportPacket) -> None:
        try:
            sections = self._sdt_assembler.feed(packet)
        except Stanag4609Error as exc:
            self._add(
                VerificationStatus.ERROR,
                "psi.structure",
                f"DVB service-information structure is invalid: {exc}",
                requirement="ETSI EN 300 468",
                pid=packet.pid,
                offset=packet.offset,
            )
            self._sdt_assembler = PSISectionAssembler(pid=0x11)
            return
        for section in sections:
            if section[0] not in {0x42, 0x46}:
                continue
            if mpeg2_crc32(section) != 0:
                self._add(
                    VerificationStatus.ERROR,
                    "psi.crc",
                    "DVB SDT MPEG-2 CRC-32 mismatch",
                    requirement="ETSI EN 300 468 SDT CRC_32",
                    pid=packet.pid,
                    offset=packet.offset,
                )

    def _consume_events(self, events: Sequence[object]) -> None:
        for event in events:
            if isinstance(event, PATEvent):
                self._consume_psi_timing(event)
                if event.table.current_next_indicator:
                    self._pat_count += 1
            elif isinstance(event, PMTEvent):
                self._consume_psi_timing(event)
                if event.table.current_next_indicator:
                    self._observe_pmt(event.table)
            elif isinstance(event, ProgramClockEvent):
                self._consume_psi_timing(event)
                if event.discontinuity:
                    self._pts.reset(program_number=event.program_number)
                    self._first_video_pts.reset(program_number=event.program_number)
                for metadata_delay_issue in self._metadata_delay.observe_clock(event):
                    self._add_metadata_delay(metadata_delay_issue)
                for metadata_std_issue in self._metadata_std.observe_clock(event):
                    self._add_metadata_std(metadata_std_issue)
                for pcr_issue in self._pcr.observe(event):
                    self._add(
                        VerificationStatus.ERROR,
                        f"st1402.pcr.{pcr_issue.code}",
                        pcr_issue.message,
                        requirement=pcr_issue.requirement,
                        program_number=pcr_issue.program_number,
                        pid=pcr_issue.pid,
                        offset=pcr_issue.current_source_offset,
                    )
            elif isinstance(event, PESStreamEvent):
                self._observe_pes(event)

    def _consume_psi_timing(
        self, event: PATEvent | PMTEvent | ProgramClockEvent
    ) -> None:
        for issue in self._psi_timing.observe(event):
            self._add_psi_timing(issue)

    def _add_psi_timing(self, issue: PSITimingIssue) -> None:
        self._add(
            VerificationStatus.ERROR,
            f"st1402.psi.{issue.table.lower()}.{issue.code}",
            issue.message,
            requirement=issue.requirement,
            program_number=issue.program_number,
            offset=issue.current_source_offset,
        )

    def _observe_pmt(self, table: ProgramMapTable) -> None:
        self._pmt_count += 1
        previous_table = self._pmts.get(table.program_number)
        if previous_table is not None and previous_table.raw != table.raw:
            self._pts.reset(program_number=table.program_number)
            self._first_video_pts.reset(program_number=table.program_number)
            self._metadata_delay.reset(program_number=table.program_number)
            for key, video in self._video_timestamps.items():
                if key[0] == table.program_number:
                    video.reset_parser()
            for key, property_stats in self._video_properties.items():
                if key[0] == table.program_number:
                    property_stats.reset_parser()
        self._pmts[table.program_number] = table
        for metadata_std_issue in self._metadata_std.observe_pmt(table):
            self._add_metadata_std(metadata_std_issue)
        klv_by_pid = dict(find_klv_streams(table))
        for stream in table.streams:
            kind = self._declared_kind(stream.stream_type, stream.elementary_pid, klv_by_pid)
            codec: str | None = None
            carriage = klv_by_pid.get(stream.elementary_pid)
            audio = next(
                (
                    candidate
                    for candidate in find_st1001_audio_streams(table)
                    if candidate.pid == stream.elementary_pid
                ),
                None,
            )
            if audio is not None:
                codec = audio.codec.value
            self._streams.setdefault(
                (table.program_number, stream.elementary_pid),
                _StreamStats(
                    table.program_number,
                    stream.elementary_pid,
                    kind,
                    stream.stream_type,
                    carriage.value if carriage is not None else None,
                    codec,
                ),
            )

        identity = (table.version_number, table.raw)
        if self._validated_pmts.get(table.program_number) == identity:
            return
        self._validated_pmts[table.program_number] = identity
        for metadata_issue in validate_st1402_metadata_program(table):
            self._add(
                VerificationStatus.ERROR,
                f"st1402.metadata.{metadata_issue.code}",
                metadata_issue.message,
                requirement=metadata_issue.requirement,
                program_number=metadata_issue.program_number,
                pid=metadata_issue.elementary_pid,
            )
        for audio_issue in validate_st1001_audio_profile(
            table, require_audio=self._require_audio
        ):
            self._add(
                VerificationStatus.ERROR,
                f"st1001.{audio_issue.code.lower()}",
                audio_issue.message,
                requirement=audio_issue.requirement,
                program_number=table.program_number,
                pid=audio_issue.elementary_pid,
            )

    @staticmethod
    def _declared_kind(
        stream_type: int,
        pid: int,
        klv_by_pid: dict[int, KLVCarriage],
    ) -> str:
        if pid in klv_by_pid:
            return StreamKind.KLV.value
        if stream_type in {0x01, 0x02, 0x10, 0x1B, 0x20, 0x21, 0x24, 0x42}:
            return StreamKind.VIDEO.value
        if stream_type in {0x03, 0x04, 0x0F, 0x11, 0x1C, 0x2D}:
            return StreamKind.AUDIO.value
        return StreamKind.DATA.value

    def _observe_pes(self, event: PESStreamEvent) -> None:
        for metadata_delay_issue in self._metadata_delay.observe_pes(event):
            self._add_metadata_delay(metadata_delay_issue)
        for metadata_std_issue in self._metadata_std.observe_pes(event):
            self._add_metadata_std(metadata_std_issue)
        for pts_issue in self._pts.observe(event):
            self._add(
                VerificationStatus.ERROR,
                f"st1402.pts.{pts_issue.code}",
                pts_issue.message,
                requirement=pts_issue.requirement,
                program_number=pts_issue.program_number,
                pid=pts_issue.pid,
                offset=pts_issue.current_source_offset,
            )
        for first_pts_issue in self._first_video_pts.observe(event):
            self._add(
                VerificationStatus.ERROR,
                f"st1402.pts.{first_pts_issue.code}",
                first_pts_issue.message,
                requirement=first_pts_issue.requirement,
                program_number=first_pts_issue.program_number,
                pid=first_pts_issue.pid,
                offset=first_pts_issue.source_offset,
            )
        key = (event.program_number, event.pid)
        stats = self._streams.get(key)
        if stats is None:
            stats = _StreamStats(
                event.program_number,
                event.pid,
                event.kind.value,
                event.stream.stream_type,
                event.klv_carriage.value if event.klv_carriage is not None else None,
                event.audio_codec.value if event.audio_codec is not None else None,
            )
            self._streams[key] = stats
        stats.pes_packets += 1
        stats.payload_bytes += len(event.pes.payload)
        if event.pes.pts is not None:
            stats.timestamped_pes_packets += 1
            if stats.first_pts is None:
                stats.first_pts = event.pes.pts
            stats.last_pts = event.pes.pts
        if event.kind is StreamKind.AUDIO:
            self._observe_audio(event)
            return
        if (
            event.kind is StreamKind.VIDEO
            and event.stream.stream_type in SUPPORTED_ST0604_STREAM_TYPES
        ):
            self._observe_video_timestamp(event)
            if event.stream.stream_type in {0x02, 0x1B, 0x24}:
                self._observe_video_properties(event)
            return
        if event.kind is not StreamKind.KLV:
            return
        decoder = self._metadata_decoders.setdefault(
            event.pid,
            MetadataStreamDecoder(
                field_decoding=FieldDecodingMode.PRESERVE,
                ontology_resolver=self._ontology_resolver,
                st0601_context_provider=self._st0601_context_provider,
            ),
        )
        try:
            metadata_events = decoder.feed(event)
        except Stanag4609Error as exc:
            self._add(
                VerificationStatus.ERROR,
                "metadata.decode",
                str(exc),
                program_number=event.program_number,
                pid=event.pid,
                offset=event.pes.offset,
            )
            self._metadata_decoders[event.pid] = MetadataStreamDecoder(
                field_decoding=FieldDecodingMode.PRESERVE,
                ontology_resolver=self._ontology_resolver,
                st0601_context_provider=self._st0601_context_provider,
            )
            return
        for metadata in metadata_events:
            self._observe_metadata(metadata)

    def _observe_video_timestamp(self, event: PESStreamEvent) -> None:
        key = (event.program_number, event.pid)
        video = self._video_timestamps.get(key)
        if video is None or video.stream_type != event.stream.stream_type:
            video = _VideoTimestampStats(event.stream.stream_type)
            self._video_timestamps[key] = video
        if any(packet.discontinuity_indicator for packet in event.pes.transport_packets):
            video.reset_parser()
        try:
            video.observe(video.parser.feed(event.pes.payload))
        except Stanag4609Error as exc:
            video.parsing_errors += 1
            self._add(
                VerificationStatus.ERROR,
                "st0604.timestamp.decode",
                str(exc),
                requirement="MISB ST 0604.6",
                program_number=event.program_number,
                pid=event.pid,
                offset=event.pes.offset,
            )
            video.reset_parser()

    def _observe_video_properties(self, event: PESStreamEvent) -> None:
        key = (event.program_number, event.pid)
        video = self._video_properties.get(key)
        if video is None or video.stream_type != event.stream.stream_type:
            video = _VideoPropertiesStats(event.stream.stream_type)
            self._video_properties[key] = video
        if any(packet.discontinuity_indicator for packet in event.pes.transport_packets):
            video.reset_parser()
        try:
            video.observe(video.parser.feed(event.pes.payload))
        except Stanag4609Error as exc:
            video.parsing_errors += 1
            self._add(
                VerificationStatus.ERROR,
                "video.properties.decode",
                str(exc),
                requirement="ITU-T H.262 / H.264 sequence syntax",
                program_number=event.program_number,
                pid=event.pid,
                offset=event.pes.offset,
            )
            video.reset_parser()

    def _add_metadata_delay(self, issue: MetadataDelayIssue) -> None:
        self._add(
            VerificationStatus.ERROR,
            f"st1402.metadata_delay.{issue.code}",
            issue.message,
            requirement=issue.requirement,
            program_number=issue.program_number,
            pid=issue.pid,
            offset=issue.source_offset,
        )

    def _add_metadata_std(self, issue: MetadataSTDStreamIssue) -> None:
        self._add(
            VerificationStatus.ERROR,
            f"st1402.metadata_std.{issue.code}",
            issue.message,
            requirement=issue.requirement,
            program_number=issue.program_number,
            pid=issue.pid,
            offset=issue.source_offset,
        )

    def _observe_audio(self, event: PESStreamEvent) -> None:
        if event.audio_codec is None:
            return
        key = (event.program_number, event.pid)
        if key in self._audio_failed:
            return
        audio = self._audio.setdefault(key, _AudioStats())
        audio.last_offset = event.pes.offset
        if any(packet.discontinuity_indicator for packet in event.pes.transport_packets):
            audio.needs_initial_pts = True
        try:
            frames = audio.parser.feed(event)
            if frames and audio.needs_initial_pts:
                if frames[0].presentation_ticks is None:
                    self._add(
                        VerificationStatus.ERROR,
                        "st1402.pts.first_access_unit",
                        (
                            f"program {event.program_number} PID {event.pid} first audio "
                            "access unit begins in a PES packet without PTS"
                        ),
                        requirement="ITU-T H.222.0 (10/2014) §2.7.5",
                        program_number=event.program_number,
                        pid=event.pid,
                        offset=event.pes.offset,
                    )
                audio.needs_initial_pts = False
            audio.observe(frames)
        except (Stanag4609Error, ValueError) as exc:
            self._audio_failed.add(key)
            self._add_audio_error(
                str(exc),
                program_number=event.program_number,
                pid=event.pid,
                offset=event.pes.offset,
            )

    def _add_audio_error(
        self,
        message: str,
        *,
        program_number: int,
        pid: int,
        offset: int | None,
    ) -> None:
        self._add(
            VerificationStatus.ERROR,
            "st1001.audio.frame",
            message,
            requirement="MISB ST 1001.1 / H.222.0 audio access units",
            program_number=program_number,
            pid=pid,
            offset=offset,
        )

    def _observe_metadata(self, event: KLVMetadataEvent) -> None:
        self._klv_packets += 1
        decoded = event.decoded
        if isinstance(decoded, UASLocalSet):
            self._st0601_packets += 1
            key = (event.program_number, event.pid, event.metadata_service_id)
            if any(item.value for item in decoded.local_set.getall(48)):
                program_carriages = self._security_carriages.setdefault(
                    event.program_number, {}
                )
                program_carriages.setdefault(
                    event.carriage, (event.pid, event.metadata_service_id)
                )
            inventory = self._st0601_inventory.setdefault(
                key, _ST0601Stats(self._max_st0601_tags_per_stream)
            )
            inventory.observe(decoded, event.validation_context)
            self._observe_st0601_distributed_states(decoded, event, key=key)
            if decoded.getall(115) or decoded.getall(116) or key in self._control_commands:
                self._observe_control_commands(decoded, event, key=key)
            vmti = decoded.value(74)
            if isinstance(vmti, VMTILocalSet):
                self._observe_vmti(vmti, event)
            for issue in decoded.issues:
                self._add(
                    VerificationStatus.ERROR,
                    "st0601.field",
                    issue.message,
                    requirement=issue.name,
                    program_number=event.program_number,
                    pid=event.pid,
                    tags=(issue.tag,),
                    offset=event.source.pes.offset,
                )
            tree_snapshot = self._observe_st1607_tree(decoded, event, key=key)
            if tree_snapshot is not None:
                views = [
                    (path, tree_snapshot.effective_fields(path))
                    for path in tree_snapshot.branches
                ]
                views.append(((), tree_snapshot.root.fields))
                for path, fields in views:
                    target_elevation = resolve_target_elevation(fields)
                    if target_elevation is None or target_elevation.datum is not None:
                        continue
                    scope = "root" if not path else f"metadata substream {path}"
                    self._add(
                        VerificationStatus.WARNING,
                        "st0601.target_elevation.datum_unknown",
                        f"{scope} Item 42 has no receiver-current Item 25 or 78 "
                        "to identify its MSL/HAE vertical datum",
                        requirement="MISB ST 0601.19 §8.42.1",
                        program_number=event.program_number,
                        pid=event.pid,
                        tags=(25, 42, 78),
                        offset=event.source.pes.offset,
                    )
            if self._validate_mismms:
                validator = self._mismms.setdefault(
                    key,
                    MISMMSValidator(
                        require_security=self._require_security,
                        require_miis=self._require_miis,
                        security_context=self._security_context,
                        field_decoding=FieldDecodingMode.PRESERVE,
                    ),
                )
                try:
                    root_issues = validator.observe(decoded)
                except Stanag4609Error as exc:
                    self._add(
                        VerificationStatus.ERROR,
                        "st0902.packet",
                        str(exc),
                        requirement="ST 0902.8 minimum metadata profile",
                        program_number=event.program_number,
                        pid=event.pid,
                        offset=event.source.pes.offset,
                    )
                else:
                    if tree_snapshot is not None and tree_snapshot.branches:
                        for policy_issue in validate_st1607_mismms(
                            tree_snapshot,
                            require_security=self._require_security,
                            require_miis=self._require_miis,
                            security_context=self._security_context,
                        ):
                            self._add_st1607_policy(
                                policy_issue,
                                program_number=event.program_number,
                                pid=event.pid,
                                offset=event.source.pes.offset,
                            )
                    else:
                        for profile_issue in root_issues:
                            self._add_mismms(
                                profile_issue,
                                program_number=event.program_number,
                                pid=event.pid,
                                offset=event.source.pes.offset,
                            )
        elif isinstance(decoded, VMTILocalSet):
            self._observe_vmti(decoded, event)
        else:
            self._unknown_klv_packets += 1
            self._add(
                VerificationStatus.WARNING,
                "metadata.unknown_key",
                f"no typed decoder is registered for Universal Key {event.packet.key.hex()}",
                program_number=event.program_number,
                pid=event.pid,
                offset=event.source.pes.offset,
            )

    def _observe_st0601_distributed_states(
        self,
        uas: UASLocalSet,
        event: KLVMetadataEvent,
        *,
        key: tuple[int, int, int | None],
    ) -> None:
        offset = event.source.pes.offset
        if uas.getall(121) or uas.getall(128) or key in self._wavelength_states:
            wavelength_state = self._wavelength_states.setdefault(
                key, WavelengthTableState()
            )
            try:
                wavelength_snapshot = wavelength_state.observe(uas)
            except Stanag4609Error as exc:
                self._add_st0601_state_error(
                    "wavelength",
                    exc,
                    requirement="MISB ST 0601.19 §§8.121, 8.128",
                    tags=(121, 128),
                    event=event,
                )
            else:
                for wavelength_issue in wavelength_snapshot.issues:
                    self._add_wavelength_issue(
                        wavelength_issue, event=event, offset=offset
                    )

        if uas.getall(138) or uas.getall(139) or key in self._payload_states:
            payload_state = self._payload_states.setdefault(key, PayloadTableState())
            try:
                payload_snapshot = payload_state.observe(uas)
            except Stanag4609Error as exc:
                self._add_st0601_state_error(
                    "payload",
                    exc,
                    requirement="MISB ST 0601.19 §§8.138-8.139",
                    tags=(138, 139),
                    event=event,
                )
            else:
                for payload_issue in payload_snapshot.issues:
                    self._add_payload_issue(payload_issue, event=event, offset=offset)

        if uas.getall(140) or key in self._weapons_states:
            weapons_state = self._weapons_states.setdefault(key, WeaponsStoresState())
            try:
                weapons_state.observe(uas)
            except Stanag4609Error as exc:
                self._add_st0601_state_error(
                    "weapons",
                    exc,
                    requirement="MISB ST 0601.19 §8.140",
                    tags=(140,),
                    event=event,
                )

        if uas.getall(141) or key in self._waypoint_states:
            waypoint_state = self._waypoint_states.setdefault(key, WaypointListState())
            try:
                waypoint_snapshot = waypoint_state.observe(uas)
            except Stanag4609Error as exc:
                self._add_st0601_state_error(
                    "waypoint",
                    exc,
                    requirement="MISB ST 0601.19 §8.141",
                    tags=(141,),
                    event=event,
                )
            else:
                for waypoint_issue in waypoint_snapshot.issues:
                    self._add_waypoint_issue(waypoint_issue, event=event, offset=offset)

    def _observe_st1607_tree(
        self,
        uas: UASLocalSet,
        event: KLVMetadataEvent,
        *,
        key: tuple[int, int, int | None],
    ) -> MetadataTreeSnapshot | None:
        tree = self._metadata_trees.setdefault(key, MetadataTreeState())
        previous = self._metadata_tree_snapshots.get(key)
        try:
            snapshot = tree.observe(uas)
        except Stanag4609Error as exc:
            if uas.getall(100) or uas.getall(101) or (
                previous is not None and previous.branches
            ):
                self._add(
                    VerificationStatus.ERROR,
                    "st1607.state",
                    str(exc),
                    requirement="MISB ST 1607.2 receiver state",
                    program_number=event.program_number,
                    pid=event.pid,
                    offset=event.source.pes.offset,
                )
            return None
        self._metadata_tree_snapshots[key] = snapshot
        if snapshot.branches:
            for policy_issue in validate_st1602_composite(snapshot):
                self._add_hierarchy_policy(
                    policy_issue,
                    standard="st1602",
                    program_number=event.program_number,
                    pid=event.pid,
                    offset=event.source.pes.offset,
                )
            for policy_issue in validate_st1607_security(snapshot):
                self._add_st1607_policy(
                    policy_issue,
                    program_number=event.program_number,
                    pid=event.pid,
                    offset=event.source.pes.offset,
                )
        return snapshot

    def _add_st0601_state_error(
        self,
        area: str,
        error: Stanag4609Error,
        *,
        requirement: str,
        tags: tuple[int, ...],
        event: KLVMetadataEvent,
    ) -> None:
        self._add(
            VerificationStatus.ERROR,
            f"st0601.{area}.state",
            str(error),
            requirement=requirement,
            program_number=event.program_number,
            pid=event.pid,
            tags=tags,
            offset=event.source.pes.offset,
        )

    def _add_wavelength_issue(
        self,
        issue: WavelengthTableIssue,
        *,
        event: KLVMetadataEvent,
        offset: int | None,
    ) -> None:
        tags = {
            "duplicate_custom_name": (128,),
            "reserved_active_id": (121,),
            "undefined_active_id": (121, 128),
        }.get(issue.code, (121, 128))
        identifiers = ", ".join(map(str, issue.wavelength_ids))
        self._add(
            VerificationStatus.ERROR,
            f"st0601.wavelength.{issue.code}",
            f"{issue.message} (Wavelength IDs: {identifiers})",
            requirement="MISB ST 0601.19 §§8.121, 8.128",
            program_number=event.program_number,
            pid=event.pid,
            tags=tags,
            offset=offset,
        )

    def _add_payload_issue(
        self,
        issue: PayloadTableIssue,
        *,
        event: KLVMetadataEvent,
        offset: int | None,
    ) -> None:
        tags = (138,) if issue.code == "payload_count_changed" else (138, 139)
        identifiers = (
            f" (Payload IDs: {', '.join(map(str, issue.payload_ids))})"
            if issue.payload_ids
            else ""
        )
        self._add(
            VerificationStatus.ERROR,
            f"st0601.payload.{issue.code}",
            issue.message + identifiers,
            requirement="MISB ST 0601.19 §§8.138-8.139",
            program_number=event.program_number,
            pid=event.pid,
            tags=tags,
            offset=offset,
        )

    def _add_waypoint_issue(
        self,
        issue: WaypointListIssue,
        *,
        event: KLVMetadataEvent,
        offset: int | None,
    ) -> None:
        identifiers = ", ".join(map(str, issue.waypoint_ids))
        self._add(
            VerificationStatus.ERROR,
            f"st0601.waypoint.{issue.code}",
            f"{issue.message} (Waypoint IDs: {identifiers})",
            requirement="MISB ST 0601.19 §8.141 / ST 0601.17-40",
            program_number=event.program_number,
            pid=event.pid,
            tags=(141,),
            offset=offset,
        )

    def _observe_control_commands(
        self,
        uas: UASLocalSet,
        event: KLVMetadataEvent,
        *,
        key: tuple[int, int, int | None],
    ) -> None:
        state = self._control_commands.setdefault(key, ControlCommandState())
        try:
            snapshot = state.observe(uas)
        except Stanag4609Error as exc:
            self._add(
                VerificationStatus.ERROR,
                "st0601.command.state",
                str(exc),
                requirement="MISB ST 0601.19 §8.115-8.116",
                program_number=event.program_number,
                pid=event.pid,
                tags=(115, 116),
                offset=event.source.pes.offset,
            )
            return
        for issue in snapshot.issues:
            self._add_control_command_issue(
                issue,
                program_number=event.program_number,
                pid=event.pid,
                offset=event.source.pes.offset,
            )

    def _add_control_command_issue(
        self,
        issue: ControlCommandIssue,
        *,
        program_number: int,
        pid: int,
        offset: int | None = None,
    ) -> None:
        acknowledgement_issue = issue.code in {
            "duplicate_acknowledgement",
            "unknown_acknowledgement",
        }
        identifiers = (
            f" (Command IDs: {', '.join(map(str, issue.command_ids))})"
            if issue.command_ids
            else ""
        )
        self._add(
            VerificationStatus.ERROR,
            f"st0601.command.{issue.code}",
            issue.message + identifiers,
            requirement="MISB ST 0601.19 §8.115-8.116",
            program_number=program_number,
            pid=pid,
            tags=(116,) if acknowledgement_issue else (115,),
            offset=offset,
        )

    def _observe_vmti(self, vmti: VMTILocalSet, event: KLVMetadataEvent) -> None:
        self._st0903_packets += 1
        self._st0903_target_observations += len(vmti.targets)
        key = (event.program_number, event.pid, event.metadata_service_id)
        state = self._vmti_lifecycles.setdefault(key, VMTILifecycleState())
        try:
            snapshot = state.observe(vmti)
        except Stanag4609Error as exc:
            self._add(
                VerificationStatus.ERROR,
                "st0903.lifecycle.state",
                str(exc),
                requirement="MISB ST 0903.6 §7.2",
                program_number=event.program_number,
                pid=event.pid,
                offset=event.source.pes.offset,
            )
            return
        for issue in snapshot.issues:
            self._add_vmti_lifecycle(
                issue,
                program_number=event.program_number,
                pid=event.pid,
                offset=event.source.pes.offset,
            )

    def _add_vmti_lifecycle(
        self,
        issue: VMTILifecycleIssue,
        *,
        program_number: int,
        pid: int,
        offset: int | None = None,
    ) -> None:
        status = (
            VerificationStatus.WARNING
            if issue.code == "missing_detection_status"
            else VerificationStatus.ERROR
        )
        requirement = (
            "ST 0903.6-129"
            if issue.code == "retired_target_id_reused"
            else "MISB ST 0903.6 §7.2"
        )
        self._add(
            status,
            f"st0903.lifecycle.{issue.code}",
            issue.message,
            requirement=requirement,
            program_number=program_number,
            pid=pid,
            tags=() if issue.code == "retired_target_id_reused" else (23,),
            offset=offset,
        )

    def _add_mismms(
        self,
        issue: MISMMSValidationIssue,
        *,
        program_number: int,
        pid: int,
        offset: int | None = None,
    ) -> None:
        self._add(
            VerificationStatus.ERROR,
            f"st0902.{issue.code}",
            issue.message,
            requirement=issue.requirement,
            program_number=program_number,
            pid=pid,
            tags=issue.tags,
            offset=offset,
        )

    def _add_st1607_policy(
        self,
        issue: ST1607PolicyIssue,
        *,
        program_number: int,
        pid: int,
        offset: int | None = None,
    ) -> None:
        self._add_hierarchy_policy(
            issue,
            standard="st1607",
            program_number=program_number,
            pid=pid,
            offset=offset,
        )

    def _add_hierarchy_policy(
        self,
        issue: ST1607PolicyIssue,
        *,
        standard: str,
        program_number: int,
        pid: int,
        offset: int | None = None,
    ) -> None:
        path = "root" if not issue.path else "/".join(
            str(identifier.universal_id or identifier.local_id)
            for identifier in issue.path
        )
        self._add(
            VerificationStatus.ERROR,
            f"{standard}.{issue.code}",
            f"metadata substream {path}: {issue.message}",
            requirement=issue.requirement,
            program_number=program_number,
            pid=pid,
            tags=issue.tags,
            offset=offset,
        )

    def _add_misp_image_context_checks(self) -> None:
        context = self._image_context
        if context is None:
            return
        if context.source_aspect_ratio is not None:
            ratio = context.source_aspect_ratio
            conforms = Fraction(1, 4) <= ratio <= Fraction(4, 1)
            self._add(
                VerificationStatus.PASS if conforms else VerificationStatus.ERROR,
                "misp.image.source_aspect_ratio",
                f"producer-supplied source aspect ratio {float(ratio):g} "
                + (
                    "is within the inclusive [0.25, 4.0] range"
                    if conforms
                    else "is outside the inclusive [0.25, 4.0] range"
                ),
                requirement="MISP-2015.1-01",
            )
        if context.source_progressive is not None:
            self._add(
                (
                    VerificationStatus.PASS
                    if context.source_progressive
                    else VerificationStatus.ERROR
                ),
                "misp.image.source_progressive",
                "producer-supplied imager source is progressive-scan"
                if context.source_progressive
                else "producer-supplied imager source is interlaced-scan",
                requirement="MISP-2015.1-02",
            )
        if context.conversion_progressive:
            interlaced = tuple(
                index
                for index, progressive in enumerate(
                    context.conversion_progressive, start=1
                )
                if not progressive
            )
            conforms = not interlaced
            detail = (
                f"all {len(context.conversion_progressive)} producer-supplied conversion "
                "stage(s) are progressive-scan"
                if conforms
                else "producer-supplied conversion stage "
                + ", ".join(str(index) for index in interlaced)
                + " is interlaced-scan"
            )
            self._add(
                VerificationStatus.PASS if conforms else VerificationStatus.ERROR,
                "misp.image.conversion_progressive",
                detail,
                requirement="MISP-2015.1-02",
            )
        if context.source_digital is False:
            self._add(
                VerificationStatus.PASS,
                "misp.image.legacy_digitization",
                "producer-supplied analog source reaches the verifier as digital FMV",
                requirement="MISP-2015.1-05",
            )
        elif context.source_digital is True:
            analog_stages = tuple(
                index
                for index, digital in enumerate(context.conversion_digital, start=1)
                if not digital
            )
            conforms = not analog_stages
            detail = (
                "producer-supplied digital source and conversion history remain digital"
                if conforms
                else "producer-supplied conversion stage "
                + ", ".join(str(index) for index in analog_stages)
                + " converts digital source imagery to analog form"
            )
            self._add(
                VerificationStatus.PASS if conforms else VerificationStatus.ERROR,
                "misp.image.digital_continuity",
                detail,
                requirement="MISP-2015.1-06",
            )

    def _add_summary_checks(self) -> None:
        if not self._has_error("transport.structure", "transport.decode"):
            self._add(
                VerificationStatus.PASS,
                "transport.structure",
                "all complete MPEG-TS packets and assembled structures decoded",
            )
        if not self._has_error("transport.continuity"):
            self._add(
                VerificationStatus.PASS,
                "transport.continuity",
                "payload continuity counters contain no unexplained gaps",
            )
        for program_number, carriage_endpoints in sorted(
            self._security_carriages.items()
        ):
            descriptions = []
            for carriage, endpoint in sorted(
                carriage_endpoints.items(), key=lambda item: item[0].value
            ):
                pid, service_id = endpoint
                endpoint_text = f"PID {pid}" + (
                    "" if service_id is None else f" service {service_id}"
                )
                descriptions.append(f"{carriage.value} ({endpoint_text})")
            if len(carriage_endpoints) == 1:
                self._add(
                    VerificationStatus.PASS,
                    "misp.security.carriage",
                    "security metadata uses one MPEG-TS carriage mechanism: "
                    + descriptions[0],
                    requirement="MISP-2015.1-49",
                    program_number=program_number,
                )
            else:
                self._add(
                    VerificationStatus.ERROR,
                    "misp.security.multiple_carriage",
                    "security metadata uses more than one MPEG-TS carriage mechanism: "
                    + "; ".join(descriptions),
                    requirement="MISP-2015.1-49",
                    program_number=program_number,
                )
        self._presence_check(
            self._pat_count > 0,
            code="psi.pat",
            present_message="a current CRC-valid PAT was decoded",
            missing="no current CRC-valid PAT was decoded",
            requirement="ISO/IEC 13818-1 PAT",
        )
        self._presence_check(
            self._pmt_count > 0,
            code="psi.pmt",
            present_message="at least one current CRC-valid PMT was decoded",
            missing="no current CRC-valid PMT was decoded",
            requirement="ISO/IEC 13818-1 PMT",
        )
        self._add_misp_image_context_checks()
        video_streams = tuple(item for item in self._streams.values() if item.kind == "video")
        self._presence_check(
            bool(video_streams) and any(item.pes_packets for item in video_streams),
            code="fmv.video",
            present_message="a declared motion-imagery stream contains PES data",
            missing=(
                "motion-imagery stream is declared but contains no PES data"
                if video_streams
                else "no recognized motion-imagery elementary stream was discovered"
            ),
        )
        approved_video_codecs = {
            0x02: "H.262/MPEG-2 Video",
            0x1B: "H.264/AVC",
            0x24: "H.265/HEVC",
        }
        for stream in sorted(video_streams, key=lambda item: (item.program_number, item.pid)):
            codec = approved_video_codecs.get(stream.stream_type)
            self._add(
                VerificationStatus.PASS if codec is not None else VerificationStatus.ERROR,
                "misp.video.codec",
                (
                    f"declared {codec} video is approved for MISP Class 1"
                    if codec is not None
                    else f"video stream_type 0x{stream.stream_type:02X} is outside the "
                    "MISP Class 1 approved codecs H.262, H.264/AVC, and H.265/HEVC"
                ),
                requirement="MISP-2019.1 §3.6.3.1",
                program_number=stream.program_number,
                pid=stream.pid,
            )
        for key, video in sorted(self._video_timestamps.items()):
            if video.access_units == 0:
                self._add(
                    VerificationStatus.WARNING,
                    "st0604.timestamp.coverage_unverifiable",
                    "no supported video access-unit start was recognized, so per-frame "
                    "embedded timestamp coverage cannot be measured",
                    requirement="MISP-2018.1-104 / MISB ST 0604.6",
                    program_number=key[0],
                    pid=key[1],
                )
            elif video.access_units - video.timestamped_access_units:
                missing_access_units = video.access_units - video.timestamped_access_units
                self._add(
                    VerificationStatus.ERROR,
                    "st0604.timestamp.missing",
                    f"associated embedded ST 0604 timestamps with "
                    f"{video.timestamped_access_units} of {video.access_units} recognized "
                    f"video access unit(s); {missing_access_units} access unit(s) are missing "
                    "a timestamp",
                    requirement="MISP-2018.1-104 / MISB ST 0604.6",
                    program_number=key[0],
                    pid=key[1],
                )
            if video.duplicate_timestamp_access_units:
                self._add(
                    VerificationStatus.ERROR,
                    "st0604.timestamp.duplicate",
                    f"{video.duplicate_timestamp_access_units} recognized video access unit(s) "
                    "contain more than one embedded ST 0604 timestamp",
                    requirement="MISP-2018.1-104 / MISB ST 0604.6",
                    program_number=key[0],
                    pid=key[1],
                )
            if video.unassociated_timestamps:
                self._add(
                    VerificationStatus.ERROR,
                    "st0604.timestamp.unassociated",
                    f"{video.unassociated_timestamps} embedded ST 0604 timestamp(s) cannot "
                    "be associated with a recognized video access unit",
                    requirement="MISB ST 0604.6",
                    program_number=key[0],
                    pid=key[1],
                )
            if (
                video.access_units
                and video.access_units == video.timestamped_access_units
                and not video.duplicate_timestamp_access_units
                and not video.unassociated_timestamps
                and not video.parsing_errors
            ):
                self._add(
                    VerificationStatus.PASS,
                    "st0604.timestamp.syntax",
                    f"associated exactly one valid embedded ST 0604 timestamp with each of "
                    f"{video.access_units} recognized video access unit(s)",
                    requirement="MISP-2018.1-104 / MISB ST 0604.6",
                    program_number=key[0],
                    pid=key[1],
                )
            if video.unlocked_timestamps:
                self._add(
                    VerificationStatus.WARNING,
                    "st0604.timestamp.lock_unknown",
                    f"{video.unlocked_timestamps} embedded timestamp(s) report that "
                    "clock lock is unknown",
                    requirement="MISB ST 0603.5 §7.4 Time Status",
                    program_number=key[0],
                    pid=key[1],
                )
        for key, property_stats in sorted(self._video_properties.items()):
            properties = property_stats.latest
            if properties is None:
                self._add(
                    VerificationStatus.WARNING,
                    "video.properties.unavailable",
                    "no complete sequence property set was observed; the capture may "
                    "begin after the most recent sequence header",
                    requirement="MISP-2015.1-02 / MISP-2018.2-114/-115",
                    program_number=key[0],
                    pid=key[1],
                )
                continue
            self._add(
                VerificationStatus.PASS,
                "video.properties.syntax",
                f"decoded {properties.codec} {properties.width}x{properties.height} "
                f"{properties.profile} Level {properties.level} sequence properties",
                requirement="ITU-T H.262 / H.264 sequence syntax",
                program_number=key[0],
                pid=key[1],
            )
            if property_stats.interlaced_sequences:
                self._add(
                    VerificationStatus.ERROR,
                    "misp.video.progressive",
                    f"{property_stats.interlaced_sequences} of "
                    f"{property_stats.sequences} observed sequence property sets permit "
                    "interlaced coded pictures",
                    requirement="MISP-2015.1-02",
                    program_number=key[0],
                    pid=key[1],
                )
            elif property_stats.ambiguous_scan_sequences:
                self._add(
                    VerificationStatus.WARNING,
                    "misp.video.progressive_unverifiable",
                    f"{property_stats.ambiguous_scan_sequences} of "
                    f"{property_stats.sequences} observed sequence property sets do not "
                    "declare an unambiguous progressive or interlaced source characteristic",
                    requirement="MISP-2015.1-02",
                    program_number=key[0],
                    pid=key[1],
                )
            else:
                self._add(
                    VerificationStatus.PASS,
                    "misp.video.progressive",
                    f"all {property_stats.sequences} observed sequence property set(s) "
                    "declare progressive-scan coding",
                    requirement="MISP-2015.1-02",
                    program_number=key[0],
                    pid=key[1],
                )
            self._add(
                VerificationStatus.PASS
                if not property_stats.profile_level_violations
                else VerificationStatus.ERROR,
                "misp.video.profile_level",
                (
                    f"all {property_stats.sequences} observed sequence property set(s) "
                    f"are within the selected MISP codec profile; latest is "
                    f"{properties.profile} Level {properties.level}"
                    if not property_stats.profile_level_violations
                    else f"{property_stats.profile_level_violations} of "
                    f"{property_stats.sequences} observed sequence property sets are "
                    "outside the selected MISP codec profile; latest is "
                    f"{properties.profile} Level {properties.level}"
                ),
                requirement={
                    0x02: "MISP-2018.2-115",
                    0x1B: "MISP-2018.2-114",
                    0x24: "MISP-2018.2-113",
                }[properties.stream_type],
                program_number=key[0],
                pid=key[1],
            )
            if properties.stream_type == 0x02:
                extension_violations = (
                    property_stats.h262_frame_rate_extension_violations
                )
                self._add(
                    VerificationStatus.PASS
                    if not extension_violations
                    else VerificationStatus.ERROR,
                    "video.h262.frame_rate_extension",
                    (
                        f"all {property_stats.sequences} observed sequence property "
                        "set(s) use zero H.262 frame-rate extensions"
                        if not extension_violations
                        else f"{extension_violations} of {property_stats.sequences} "
                        "observed sequence property sets use a non-zero H.262 "
                        "frame-rate extension"
                    ),
                    requirement=(
                        "ITU-T H.262 (02/2000) Table E.3 / MISP-2018.2-115"
                    ),
                    program_number=key[0],
                    pid=key[1],
                )
                bit_rate_violations = property_stats.h262_bit_rate_violations
                self._add(
                    VerificationStatus.PASS
                    if not bit_rate_violations
                    else VerificationStatus.ERROR,
                    "video.h262.bit_rate",
                    (
                        f"all {property_stats.sequences} observed sequence property "
                        "set(s) declare a bit rate within their H.262 level limit"
                        if not bit_rate_violations
                        else f"{bit_rate_violations} of {property_stats.sequences} "
                        "observed sequence property sets declare a bit rate above "
                        "their H.262 level limit"
                    ),
                    requirement=(
                        "ITU-T H.262 (02/2000) Table 8-13 / MISP-2018.2-115"
                    ),
                    program_number=key[0],
                    pid=key[1],
                )
                vbv_violations = property_stats.h262_vbv_buffer_violations
                self._add(
                    VerificationStatus.PASS
                    if not vbv_violations
                    else VerificationStatus.ERROR,
                    "video.h262.vbv_buffer_size",
                    (
                        f"all {property_stats.sequences} observed sequence property "
                        "set(s) declare a VBV buffer within their H.262 level limit"
                        if not vbv_violations
                        else f"{vbv_violations} of {property_stats.sequences} "
                        "observed sequence property sets declare a VBV buffer above "
                        "their H.262 level limit"
                    ),
                    requirement=(
                        "ITU-T H.262 (02/2000) Table 8-14 / MISP-2018.2-115"
                    ),
                    program_number=key[0],
                    pid=key[1],
                )
                chroma_violations = property_stats.h262_chroma_format_violations
                self._add(
                    VerificationStatus.PASS
                    if not chroma_violations
                    else VerificationStatus.ERROR,
                    "video.h262.chroma_format",
                    (
                        f"all {property_stats.sequences} observed sequence property "
                        "set(s) use 4:2:0 chroma for H.262 Main Profile"
                        if not chroma_violations
                        else f"{chroma_violations} of {property_stats.sequences} "
                        "observed sequence property sets use chroma outside the "
                        "H.262 Main Profile constraint"
                    ),
                    requirement=(
                        "ITU-T H.262 (02/2000) Table 8-5 / MISP-2018.2-115"
                    ),
                    program_number=key[0],
                    pid=key[1],
                )
                constrained_violations = (
                    property_stats.h262_constrained_parameters_violations
                )
                self._add(
                    VerificationStatus.PASS
                    if not constrained_violations
                    else VerificationStatus.ERROR,
                    "video.h262.constrained_parameters",
                    (
                        f"all {property_stats.sequences} observed sequence property "
                        "set(s) clear the legacy constrained-parameters flag"
                        if not constrained_violations
                        else f"{constrained_violations} of {property_stats.sequences} "
                        "observed sequence property sets assert the legacy MPEG-1 "
                        "constrained-parameters flag"
                    ),
                    requirement=(
                        "ITU-T H.262 (02/2000) Table E.2 / MISP-2018.2-115"
                    ),
                    program_number=key[0],
                    pid=key[1],
                )
            if properties.stream_type in {0x02, 0x1B, 0x24}:
                level_requirement = {
                    0x02: (
                        "ITU-T H.262 (02/2000) Tables 8-11/-12 / "
                        "MISP-2018.2-115"
                    ),
                    0x1B: "ITU-T H.264 (04/2017) Annex A / MISP-2018.2-114",
                    0x24: "ITU-T H.265 (02/2018) Annex A / MISP-2018.2-113",
                }[properties.stream_type]
                picture_violations = property_stats.level_picture_size_violations
                picture_unverifiable = property_stats.level_picture_size_unverifiable
                if picture_violations:
                    picture_status = VerificationStatus.ERROR
                    picture_message = (
                        f"{picture_violations} of {property_stats.sequences} observed "
                        "sequence property sets exceed the coded-picture dimensions "
                        "permitted by their signalled level"
                    )
                elif picture_unverifiable:
                    picture_status = VerificationStatus.WARNING
                    picture_message = (
                        f"coded-picture level limits could not be evaluated for "
                        f"{picture_unverifiable} of {property_stats.sequences} observed "
                        "sequence property sets"
                    )
                else:
                    picture_status = VerificationStatus.PASS
                    picture_message = (
                        f"all {property_stats.sequences} observed sequence property set(s) "
                        "fit the coded-picture dimensions permitted by their signalled level"
                    )
                self._add(
                    picture_status,
                    "video.level.picture_size",
                    picture_message,
                    requirement=level_requirement,
                    program_number=key[0],
                    pid=key[1],
                )
                rate_violations = property_stats.level_sample_rate_violations
                rate_unverifiable = property_stats.level_sample_rate_unverifiable
                if rate_violations:
                    rate_status = VerificationStatus.ERROR
                    rate_message = (
                        f"{rate_violations} of {property_stats.sequences} observed sequence "
                        "property sets exceed the coded-sample rate permitted by their "
                        "signalled level"
                    )
                elif rate_unverifiable:
                    rate_status = VerificationStatus.WARNING
                    rate_message = (
                        f"coded-sample-rate level limits could not be evaluated for "
                        f"{rate_unverifiable} of {property_stats.sequences} observed "
                        "sequence property sets because timing or a valid level was absent"
                    )
                else:
                    rate_status = VerificationStatus.PASS
                    rate_message = (
                        f"all {property_stats.sequences} observed sequence property set(s) "
                        "fit the coded-sample rate permitted by their signalled level"
                    )
                self._add(
                    rate_status,
                    "video.level.sample_rate",
                    rate_message,
                    requirement=level_requirement,
                    program_number=key[0],
                    pid=key[1],
                )
            maximum_bit_depth = property_stats.maximum_bit_depth
            if maximum_bit_depth is None:
                self._add(
                    VerificationStatus.WARNING,
                    "misp.video.pixel_value_range_unverifiable",
                    "coded luma/chroma bit depth is not available from the observed "
                    "sequence property sets",
                    requirement="MISP-2019.1 §3.6.2",
                    program_number=key[0],
                    pid=key[1],
                )
            else:
                conforms = not property_stats.pixel_depth_violations
                self._add(
                    VerificationStatus.PASS if conforms else VerificationStatus.ERROR,
                    "misp.video.pixel_value_range",
                    (
                        f"all {property_stats.sequences} observed sequence property set(s) "
                        f"use at most 8 bits per band (maximum {maximum_bit_depth}-bit)"
                        if conforms
                        else f"{property_stats.pixel_depth_violations} of "
                        f"{property_stats.sequences} observed sequence property sets "
                        "exceed the Class 1 maximum of 8 bits per band "
                        f"(maximum {maximum_bit_depth}-bit)"
                    ),
                    requirement="MISP-2019.1 §3.6.2",
                    program_number=key[0],
                    pid=key[1],
                )
        klv_streams = tuple(item for item in self._streams.values() if item.kind == "klv")
        self._presence_check(
            bool(klv_streams) and any(item.pes_packets for item in klv_streams),
            code="fmv.klv",
            present_message="a KLVA metadata stream contains PES data",
            missing=(
                "KLVA metadata is declared but contains no PES data"
                if klv_streams
                else "no correctly declared KLVA metadata elementary stream was discovered"
            ),
            requirement="MISB ST 1402 metadata carriage",
        )
        if klv_streams and not self._has_error_prefix("st1402.metadata."):
            self._add(
                VerificationStatus.PASS,
                "st1402.metadata",
                "KLVA program-map declarations satisfy the implemented ST 1402 checks",
                requirement="MISB ST 1402.2",
            )
        for program_number in sorted(self._pmts):
            if program_number not in self._pcr.programs:
                self._add(
                    VerificationStatus.ERROR,
                    "st1402.pcr.missing",
                    "no PCR was observed for the program's declared clock PID",
                    requirement="MISB ST 1402.2 §7.2",
                    program_number=program_number,
                    pid=self._pmts[program_number].pcr_pid,
                )
        if self._pmts and not self._has_error_prefix("st1402.pcr."):
            self._add(
                VerificationStatus.PASS,
                "st1402.pcr",
                "every program has PCR observations with no detected cadence violation",
                requirement="MISB ST 1402.2 §7.2",
            )
        if (
            self._metadata_delay.observed_pes
            and self._metadata_delay.compliant_pes
            == self._metadata_delay.observed_pes
        ):
            self._add(
                VerificationStatus.PASS,
                "st1402.metadata_delay",
                "every synchronous metadata PES has a PCR-bracketed delay range "
                "within zero and one second",
                requirement="ST 1402.2 ST 1402-12",
            )
        if (
            any(count >= 2 for count in self._pts.observation_counts.values())
            and not self._has_error_prefix("st1402.pts.")
        ):
            self._add(
                VerificationStatus.PASS,
                "st1402.pts",
                "all observed successive PTS pairs satisfy the 0.7-second limit",
                requirement="MISB ST 1402.2 §7.3",
            )
        if self._klv_packets and not self._has_error("metadata.decode"):
            self._add(
                VerificationStatus.PASS,
                "metadata.decode",
                "all completed KLV packets decoded without a carriage error",
            )
        if self._control_commands and not self._has_error_prefix("st0601.command."):
            self._add(
                VerificationStatus.PASS,
                "st0601.command.lifecycle",
                "Control Command IDs, repeats, issue times, and acknowledgements "
                "contain no detected lifecycle violation",
                requirement="MISB ST 0601.19 §8.115-8.116",
            )
        distributed_states = (
            (
                "wavelength",
                self._wavelength_states,
                "current wavelength definitions and active references",
                "MISB ST 0601.19 §§8.121, 8.128",
            ),
            (
                "payload",
                self._payload_states,
                "distributed payload definitions and active references",
                "MISB ST 0601.19 §§8.138-8.139",
            ),
            (
                "weapons",
                self._weapons_states,
                "distributed weapon-store records",
                "MISB ST 0601.19 §8.140",
            ),
            (
                "waypoint",
                self._waypoint_states,
                "distributed waypoint records and historical ordering",
                "MISB ST 0601.19 §8.141 / ST 0601.17-40",
            ),
        )
        for area, states, description, requirement in distributed_states:
            if states and not self._has_error_prefix(f"st0601.{area}."):
                self._add(
                    VerificationStatus.PASS,
                    f"st0601.{area}.lifecycle",
                    f"{description} contain no detected receiver-state violation",
                    requirement=requirement,
                )
        if not self._validate_mismms:
            self._add(
                VerificationStatus.NOT_APPLICABLE,
                "st0902.profile",
                "ST 0902 MISMMS profile validation was disabled by policy",
                requirement="MISB ST 0902.8",
            )
        elif self._st0601_packets:
            if not self._has_error_prefix("st0902.") and not self._has_error_prefix(
                "st1607.mismms_"
            ):
                self._add(
                    VerificationStatus.PASS,
                    "st0902.profile",
                    "ST 0601 observations satisfy the selected ST 0902 minimum profile",
                    requirement="MISB ST 0902.8",
                )
        else:
            self._add(
                VerificationStatus.NOT_APPLICABLE,
                "st0902.profile",
                "no ST 0601 Local Set was available for ST 0902 profile validation",
                requirement="MISB ST 0902.8",
            )
        if self._st0903_packets:
            self._add(
                VerificationStatus.PASS,
                "st0903.vmti",
                "standalone or embedded ST 0903 VMTI Local Sets decoded successfully",
                requirement="MISB ST 0903.6",
            )
            if not any(
                finding.code.startswith("st0903.lifecycle.")
                for finding in self._findings.values()
            ):
                self._add(
                    VerificationStatus.PASS,
                    "st0903.lifecycle",
                    "VMTI target identifiers contain no detected lifecycle violation",
                    requirement="MISB ST 0903.6 §7.2 / ST 0903.6-129",
                )
        else:
            self._add(
                VerificationStatus.NOT_APPLICABLE,
                "st0903.vmti",
                "no standalone or embedded ST 0903 VMTI Local Set was observed",
                requirement="MISB ST 0903.6",
            )
        audio_streams = tuple(item for item in self._streams.values() if item.kind == "audio")
        for stream in audio_streams:
            key = (stream.program_number, stream.pid)
            audio = self._audio.get(key)
            if stream.pes_packets == 0 or (
                key not in self._audio_failed
                and audio is not None
                and audio.frames == 0
            ):
                self._add(
                    VerificationStatus.ERROR,
                    "st1001.audio.empty",
                    "declared audio stream contains no complete compressed audio frame",
                    requirement="MISB ST 1001.1 / H.222.0 audio access units",
                    program_number=stream.program_number,
                    pid=stream.pid,
                )
            elif key not in self._audio_failed and audio is not None:
                self._add(
                    VerificationStatus.PASS,
                    "st1001.audio.frames",
                    f"decoded {audio.frames} complete compressed audio frame(s)",
                    requirement="MISB ST 1001.1",
                    program_number=stream.program_number,
                    pid=stream.pid,
                )
        if not audio_streams and not self._require_audio:
            self._add(
                VerificationStatus.NOT_APPLICABLE,
                "st1001.audio",
                "the program does not contain audio; ST 1001 does not require it",
                requirement="MISB ST 1001.1",
            )
        elif not self._has_error_prefix("st1001."):
            self._add(
                VerificationStatus.PASS,
                "st1001.audio",
                "declared audio stream types satisfy the implemented ST 1001 profile checks",
                requirement="MISB ST 1001.1",
            )
        if self._scrambled_packets:
            self._add(
                VerificationStatus.WARNING,
                "transport.scrambled",
                f"{self._scrambled_packets} scrambled transport packet(s) could not be "
                "fully inspected",
            )

    def _presence_check(
        self,
        condition: bool,
        *,
        code: str,
        present_message: str,
        missing: str,
        requirement: str | None = None,
    ) -> None:
        self._add(
            VerificationStatus.PASS if condition else VerificationStatus.ERROR,
            code,
            present_message if condition else missing,
            requirement=requirement,
        )

    def _has_error(self, *codes: str) -> bool:
        return any(
            finding.status is VerificationStatus.ERROR and finding.code in codes
            for finding in self._findings.values()
        )

    def _has_error_prefix(self, prefix: str) -> bool:
        return any(
            finding.status is VerificationStatus.ERROR and finding.code.startswith(prefix)
            for finding in self._findings.values()
        )

    def _add(
        self,
        status: VerificationStatus,
        code: str,
        message: str,
        *,
        requirement: str | None = None,
        program_number: int | None = None,
        pid: int | None = None,
        tags: tuple[int, ...] = (),
        offset: int | None = None,
    ) -> None:
        key = (status, code, message, requirement, program_number, pid, tags)
        prior = self._findings.get(key)
        if prior is not None:
            self._findings[key] = replace(
                prior,
                count=prior.count + 1,
                last_offset=offset if offset is not None else prior.last_offset,
            )
            return
        if len(self._findings) >= self._max_findings:
            if status is VerificationStatus.ERROR:
                if self._retained_error_count == 0:
                    victim = next(iter(self._findings))
                    del self._findings[victim]
                    self._suppressed_findings += 1
                else:
                    self._suppressed_findings += 1
                    return
            else:
                self._suppressed_findings += 1
                return
        self._findings[key] = VerificationFinding(
            status,
            code,
            message,
            requirement,
            program_number,
            pid,
            tags,
            first_offset=offset,
            last_offset=offset,
        )
        if status is VerificationStatus.ERROR:
            self._retained_error_count += 1

    def _add_unbounded(
        self, status: VerificationStatus, code: str, message: str
    ) -> None:
        key = (status, code, message, None, None, None, ())
        self._findings[key] = VerificationFinding(status, code, message)


def verify_fmv_stream(
    stream: BinaryIO,
    *,
    source: str | None = None,
    chunk_size: int = 1024 * 1024,
    require_security: bool = True,
    require_miis: bool = True,
    require_audio: bool = False,
    validate_mismms: bool = True,
    security_context: MISMMSecurityContext | None = None,
    ontology_resolver: OntologyResolver | None = None,
    st0601_context_provider: ST0601ContextProvider | None = None,
    image_context: MISPImageContext | None = None,
    asynchronous_std_descriptors: Mapping[
        tuple[int, int], MetadataSTDDescriptor
    ] | None = None,
    max_findings: int = 10_000,
    max_st0601_tags_per_stream: int = 4_096,
) -> FMVVerificationReport:
    """Verify a finite binary stream without loading it completely into memory."""

    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size < 188:
        raise ValueError("chunk_size must be an integer of at least 188 bytes")
    if not hasattr(stream, "read"):
        raise TypeError("stream must provide read()")
    verifier = FMVVerifier(
        require_security=require_security,
        require_miis=require_miis,
        require_audio=require_audio,
        validate_mismms=validate_mismms,
        security_context=security_context,
        ontology_resolver=ontology_resolver,
        st0601_context_provider=st0601_context_provider,
        image_context=image_context,
        asynchronous_std_descriptors=asynchronous_std_descriptors,
        max_findings=max_findings,
        max_st0601_tags_per_stream=max_st0601_tags_per_stream,
    )
    while chunk := stream.read(chunk_size):
        verifier.feed(chunk)
    return verifier.finish(source=source)


def verify_fmv_file(
    source: str | Path,
    *,
    chunk_size: int = 1024 * 1024,
    require_security: bool = True,
    require_miis: bool = True,
    require_audio: bool = False,
    validate_mismms: bool = True,
    security_context: MISMMSecurityContext | None = None,
    ontology_resolver: OntologyResolver | None = None,
    st0601_context_provider: ST0601ContextProvider | None = None,
    image_context: MISPImageContext | None = None,
    asynchronous_std_descriptors: Mapping[
        tuple[int, int], MetadataSTDDescriptor
    ] | None = None,
    max_findings: int = 10_000,
    max_st0601_tags_per_stream: int = 4_096,
) -> FMVVerificationReport:
    """Verify one MPEG-2 transport file and return an actionable report."""

    path = Path(source)
    with path.open("rb") as stream:
        return verify_fmv_stream(
            stream,
            source=str(path),
            chunk_size=chunk_size,
            require_security=require_security,
            require_miis=require_miis,
            require_audio=require_audio,
            validate_mismms=validate_mismms,
            security_context=security_context,
            ontology_resolver=ontology_resolver,
            st0601_context_provider=st0601_context_provider,
            image_context=image_context,
            asynchronous_std_descriptors=asynchronous_std_descriptors,
            max_findings=max_findings,
            max_st0601_tags_per_stream=max_st0601_tags_per_stream,
        )


def _parse_aspect_ratio(value: str) -> Fraction:
    try:
        if value.count(":") == 1:
            numerator, denominator = value.split(":")
            ratio = Fraction(int(numerator), int(denominator))
        elif ":" not in value:
            ratio = Fraction(value)
        else:
            raise ValueError
    except (ValueError, ZeroDivisionError) as error:
        raise argparse.ArgumentTypeError(
            "aspect ratio must be NUMBER or WIDTH:HEIGHT"
        ) from error
    if ratio <= 0:
        raise argparse.ArgumentTypeError("aspect ratio must be positive")
    return ratio


def main(argv: Sequence[str] | None = None) -> int:
    """Run the FMV verifier CLI."""

    parser = argparse.ArgumentParser(
        description="Verify STANAG 4609/MISB FMV and report passes and failures"
    )
    parser.add_argument("source", type=Path, help="input MPEG-2 transport stream")
    parser.add_argument(
        "--format",
        choices=("text", "json", "html"),
        default="text",
        help="report format (default: text)",
    )
    parser.add_argument(
        "--profile",
        choices=("mismms", "structural"),
        default="mismms",
        help="validation policy: ST 0902 MISMMS or structural FMV only",
    )
    parser.add_argument(
        "--no-require-security",
        action="store_true",
        help="do not require the ST 0902 Security Local Set profile item",
    )
    parser.add_argument(
        "--no-require-miis",
        action="store_true",
        help="do not require the ST 0902 MIIS Core Identifier profile item",
    )
    parser.add_argument(
        "--require-audio",
        action="store_true",
        help="apply an application policy requiring ST 1001 audio",
    )
    parser.add_argument(
        "--source-aspect-ratio",
        type=_parse_aspect_ratio,
        metavar="WIDTH:HEIGHT",
        help="producer-known imager aspect ratio for MISP-2015.1-01",
    )
    parser.add_argument(
        "--source-scan",
        choices=("progressive", "interlaced"),
        help="producer-known imager scan mode for MISP-2015.1-02",
    )
    parser.add_argument(
        "--conversion-scan",
        action="append",
        choices=("progressive", "interlaced"),
        default=[],
        help="scan mode of each conversion/transcode stage, in order; repeatable",
    )
    parser.add_argument(
        "--source-form",
        choices=("analog", "digital"),
        help="producer-known source signal form for MISP-2015.1-05/-06",
    )
    parser.add_argument(
        "--conversion-form",
        action="append",
        choices=("analog", "digital"),
        default=[],
        help="signal form of each conversion stage, in order; repeatable",
    )
    parser.add_argument(
        "--security-classification",
        choices=tuple(
            classification.name.lower().replace("_", "-")
            for classification in SecurityClassification
        ),
        help="require the ST 0102 classification to match this value",
    )
    parser.add_argument(
        "--classifying-country",
        help="require this ST 0102 classifying country code (without //)",
    )
    parser.add_argument(
        "--country-coding-method",
        choices=tuple(method.name.lower().replace("_", "-") for method in CountryCodingMethod),
        help="require this ST 0102 classifying/releasing country-code vocabulary",
    )
    parser.add_argument(
        "--security-sci-shi",
        help="require this exact ST 0102 SCI/SHI marking",
    )
    parser.add_argument(
        "--security-caveats",
        help="require this exact ST 0102 caveats value",
    )
    parser.add_argument(
        "--require-release-country",
        action="append",
        default=[],
        metavar="CODE",
        help="require a country in ST 0102 releasing instructions; repeatable",
    )
    parser.add_argument(
        "--require-object-country",
        action="append",
        default=[],
        metavar="CODE",
        help="require a country in ST 0102 object country codes; repeatable",
    )
    parser.add_argument(
        "--object-country-coding-method",
        choices=tuple(
            method.name.lower().replace("_", "-")
            for method in ObjectCountryCodingMethod
        ),
        help="require this ST 0102 object-country code vocabulary",
    )
    parser.add_argument(
        "--minimum-security-metadata-version",
        type=int,
        metavar="VERSION",
        help="require at least this ST 0102 Security Metadata version",
    )
    parser.add_argument("--chunk-size", type=int, default=1024 * 1024)
    parser.add_argument("--max-findings", type=int, default=10_000)
    parser.add_argument("--max-st0601-tags-per-stream", type=int, default=4_096)
    args = parser.parse_args(argv)
    try:
        image_context = (
            MISPImageContext(
                source_aspect_ratio=args.source_aspect_ratio,
                source_progressive=(
                    None if args.source_scan is None else args.source_scan == "progressive"
                ),
                conversion_progressive=tuple(
                    scan == "progressive" for scan in args.conversion_scan
                ),
                source_digital=(
                    None if args.source_form is None else args.source_form == "digital"
                ),
                conversion_digital=tuple(
                    form == "digital" for form in args.conversion_form
                ),
            )
            if args.source_aspect_ratio is not None
            or args.source_scan is not None
            or args.conversion_scan
            or args.source_form is not None
            or args.conversion_form
            else None
        )
        security_context = MISMMSecurityContext(
            expected_classification=(
                None
                if args.security_classification is None
                else SecurityClassification[
                    args.security_classification.upper().replace("-", "_")
                ]
            ),
            expected_country_coding_method=(
                None
                if args.country_coding_method is None
                else CountryCodingMethod[
                    args.country_coding_method.upper().replace("-", "_")
                ]
            ),
            expected_classifying_country=args.classifying_country,
            expected_sci_shi=args.security_sci_shi,
            expected_caveats=args.security_caveats,
            required_releasing_countries=frozenset(args.require_release_country),
            expected_object_country_coding_method=(
                None
                if args.object_country_coding_method is None
                else ObjectCountryCodingMethod[
                    args.object_country_coding_method.upper().replace("-", "_")
                ]
            ),
            required_object_countries=frozenset(args.require_object_country),
            minimum_security_metadata_version=args.minimum_security_metadata_version,
        )
        report = verify_fmv_file(
            args.source,
            chunk_size=args.chunk_size,
            require_security=not args.no_require_security,
            require_miis=not args.no_require_miis,
            require_audio=args.require_audio,
            validate_mismms=args.profile == "mismms",
            security_context=security_context,
            image_context=image_context,
            max_findings=args.max_findings,
            max_st0601_tags_per_stream=args.max_st0601_tags_per_stream,
        )
    except (OSError, ValueError) as exc:
        print(f"stanag4609-verify: cannot read or verify {args.source}: {exc}", file=sys.stderr)
        return 2
    if args.format == "html":
        rendered = report.to_html()
    elif args.format == "json":
        rendered = report.to_json()
    else:
        rendered = report.format_text()
    print(rendered)
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
