from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from io import BytesIO
from uuid import UUID

import pytest

from stanag4609 import (
    FMVVerifier,
    MISMMSecurityContext,
    MISMMSPopulationStatus,
    ST0601ValidationContext,
    VerificationStatus,
    VideoTimestampVerificationSummary,
    verify_fmv_file,
)
from stanag4609.klv.ber import encode_ber_length
from stanag4609.klv.model import KLVPacket
from stanag4609.st0102 import (
    SecurityClassification,
    decode_security_local_set,
    encode_security_local_set,
)
from stanag4609.st0601 import (
    ActivePayloads,
    ActiveWavelengthList,
    AmendLocalSet,
    ControlCommand,
    ControlCommandVerificationList,
    FieldDecodingMode,
    IMAPFieldValue,
    MetadataSubstreamID,
    PayloadList,
    PayloadRecord,
    RawFieldValue,
    SegmentLocalSet,
    SpecialValue,
    WavelengthRecord,
    WavelengthsList,
    WaypointList,
    WaypointRecord,
    WeaponsStores,
    WeaponStatus,
    WeaponStore,
    decode_amend_local_set,
    decode_segment_local_set,
    decode_uas_local_set,
    encode_amend_local_set,
    encode_segment_local_set,
    encode_uas_local_set,
    update_uas_local_set,
)
from stanag4609.st0604 import encode_avc_timestamp_sei
from stanag4609.st0903 import (
    DetectionStatus,
    OntologyEntityResolution,
    OntologyLocalSet,
    VMTIValidationContext,
    VTargetData,
    encode_vmti_local_set,
)
from stanag4609.st1204 import IdentifierQuality, MIISCoreIdentifier
from stanag4609.st1602 import CompositeImagingLocalSet
from stanag4609.transport.demux import PESStreamEvent
from stanag4609.transport.metadata import (
    MetadataSTDDescriptor,
    asynchronous_klv_stream,
    synchronous_klv_stream,
)
from stanag4609.transport.mpegts import ProgramClockReference
from stanag4609.transport.mux import (
    TransportMuxer,
    build_pmt_section,
    encode_pcr_packet,
    encode_pes_packet,
)
from stanag4609.transport.psi import (
    Descriptor,
    ElementaryStreamInfo,
    mpeg2_crc32,
    parse_pmt,
)
from stanag4609.verifier import main, verify_fmv_stream


def test_video_timestamp_summary_retains_existing_positional_constructor() -> None:
    summary = VideoTimestampVerificationSummary(
        1,
        1,
        1,
        0,
        1_700_000_000_000_000,
        1_700_000_000_000_000,
        None,
        None,
        0,
        0,
        0,
    )

    assert summary.timestamped_access_units == 0
    assert summary.missing_access_units == 0


def _complete_uas() -> bytes:
    return encode_uas_local_set(
        {
            2: 1_700_000_000_000_000,
            3: "mission-1",
            5: 90.0,
            6: 0.0,
            7: 0.0,
            10: "platform-1",
            11: "EO",
            12: "Geodetic WGS84",
            13: 49.0,
            14: -123.0,
            15: 1_000.0,
            16: 20.0,
            17: 15.0,
            18: 0.0,
            19: -20.0,
            20: 0.0,
            21: 5_000.0,
            22: 10.0,
            23: 49.1,
            24: -123.1,
            25: 100.0,
            65: 19,
        }
    )


def _segment(identifier: int, values: dict[int, object]) -> SegmentLocalSet:
    encoded = encode_segment_local_set(
        {**values, 143: MetadataSubstreamID(identifier)}
    )
    return decode_segment_local_set(encoded)


def _amend(identifier: int, values: dict[int, object]) -> AmendLocalSet:
    encoded = encode_amend_local_set(
        {**values, 143: MetadataSubstreamID(identifier)}
    )
    return decode_amend_local_set(encoded)


def _complete_uas_values(*, without: set[int] = frozenset()) -> dict[int, object]:
    return {
        field.definition.tag: field.value
        for field in decode_uas_local_set(_complete_uas()).fields
        if field.definition.tag != 1 and field.definition.tag not in without
    }


def _layer_ii_frame() -> bytes:
    header = bytes.fromhex("FFFC8444")
    return header + bytes(384 - len(header))


def _transport(
    klv: bytes | None = None,
    *,
    second_video: bool = False,
    audio: bool = False,
    second_pcr_base: int = 9_000,
    second_video_pts: int = 9_000,
    repeat_tables: bool = False,
    audio_payload: bytes | None = None,
    include_audio_pes: bool = True,
    video_stream_type: int = 0x1B,
    video_payload: bytes = b"\x00\x00\x01\x09video-1",
    video_pts: int | None = 0,
    audio_pts: int | None = 0,
    metadata_pts: int = 9_000,
    metadata_input_leak_rate: int = 1_000,
    metadata_buffer_size: int = 200_000,
) -> bytes:
    video = ElementaryStreamInfo(video_stream_type, 0x101, ())
    metadata = synchronous_klv_stream(
        0x102,
        metadata_input_leak_rate=metadata_input_leak_rate,
        metadata_buffer_size=metadata_buffer_size,
    )
    streams = (video, metadata)
    if audio or audio_payload is not None:
        streams += (ElementaryStreamInfo(0x03, 0x103, ()),)
    muxer = TransportMuxer(
        transport_stream_id=1,
        program_number=1,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=streams,
    )
    output = bytearray(b"".join(muxer.program_tables()))
    output.extend(muxer.mux_pcr(ProgramClockReference(0, 0)))
    output.extend(
        b"".join(
            muxer.mux_pes(
                0x101,
                encode_pes_packet(
                    video_payload,
                    stream_id=0xE0,
                    pts=video_pts,
                ),
            )
        )
    )
    if (audio or audio_payload is not None) and include_audio_pes:
        payload = _layer_ii_frame() if audio_payload is None else audio_payload
        output.extend(
            b"".join(
                muxer.mux_pes(
                    0x103,
                    encode_pes_packet(payload, stream_id=0xC0, pts=audio_pts),
                )
            )
        )
    if second_video:
        output.extend(
            b"".join(
                muxer.mux_pes(
                    0x101,
                    encode_pes_packet(
                        b"\x00\x00\x01\x09video-2",
                        stream_id=0xE0,
                        pts=second_video_pts,
                    ),
                )
            )
        )
    output.extend(
        b"".join(
            muxer.mux_sync_klv(0x102, klv or _complete_uas(), pts=metadata_pts)
        )
    )
    output.extend(muxer.mux_pcr(ProgramClockReference(second_pcr_base, 0)))
    if repeat_tables:
        output.extend(b"".join(muxer.program_tables()))
    return bytes(output)


def _transport_sequence(*klv_packets: bytes) -> bytes:
    muxer = TransportMuxer(
        transport_stream_id=1,
        program_number=1,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=(
            ElementaryStreamInfo(0x1B, 0x101, ()),
            synchronous_klv_stream(
                0x102,
                metadata_input_leak_rate=1_000,
                metadata_buffer_size=200_000,
            ),
        ),
    )
    output = bytearray(b"".join(muxer.program_tables()))
    output.extend(muxer.mux_pcr(ProgramClockReference(0, 0)))
    output.extend(
        b"".join(
            muxer.mux_pes(
                0x101,
                encode_pes_packet(
                    b"\x00\x00\x01\x09video",
                    stream_id=0xE0,
                    pts=0,
                ),
            )
        )
    )
    for index, klv in enumerate(klv_packets):
        output.extend(
            b"".join(muxer.mux_sync_klv(0x102, klv, pts=9_000 + index * 3_000))
        )
    output.extend(muxer.mux_pcr(ProgramClockReference(9_000, 0)))
    return bytes(output)


def _asynchronous_transport(klv: bytes) -> bytes:
    muxer = TransportMuxer(
        transport_stream_id=1,
        program_number=1,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=(
            ElementaryStreamInfo(0x1B, 0x101, ()),
            asynchronous_klv_stream(0x102),
        ),
    )
    output = bytearray(b"".join(muxer.program_tables()))
    output.extend(muxer.mux_pcr(ProgramClockReference(0, 0)))
    output.extend(
        b"".join(
            muxer.mux_pes(
                0x101,
                encode_pes_packet(
                    b"\x00\x00\x01\x09video-1",
                    stream_id=0xE0,
                    pts=0,
                ),
            )
        )
    )
    output.extend(b"".join(muxer.mux_async_klv(0x102, klv)))
    output.extend(muxer.mux_pcr(ProgramClockReference(9_000, 0)))
    return bytes(output)


def _dual_carriage_transport(klv: bytes) -> bytes:
    muxer = TransportMuxer(
        transport_stream_id=1,
        program_number=1,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=(
            ElementaryStreamInfo(0x1B, 0x101, ()),
            synchronous_klv_stream(
                0x102,
                metadata_input_leak_rate=1_000,
                metadata_buffer_size=200_000,
            ),
            asynchronous_klv_stream(0x103),
        ),
    )
    output = bytearray(b"".join(muxer.program_tables()))
    output.extend(muxer.mux_pcr(ProgramClockReference(0, 0)))
    output.extend(
        b"".join(
            muxer.mux_pes(
                0x101,
                encode_pes_packet(
                    b"\x00\x00\x01\x09video-1", stream_id=0xE0, pts=0
                ),
            )
        )
    )
    output.extend(b"".join(muxer.mux_sync_klv(0x102, klv, pts=9_000)))
    output.extend(b"".join(muxer.mux_async_klv(0x103, klv)))
    output.extend(muxer.mux_pcr(ProgramClockReference(9_000, 0)))
    return bytes(output)


def _vmti_uas(
    status: DetectionStatus | None,
    *,
    timestamp: int,
    target_id: int = 7,
) -> bytes:
    target_values: dict[int, object] = {23: status} if status is not None else {5: 80}
    if status in {DetectionStatus.ACTIVE_MOVING, DetectionStatus.ACTIVE_STOPPED}:
        target_values[1] = 1
    vmti = encode_vmti_local_set(
        {2: timestamp, 4: 6, 8: 1},
        targets=(VTargetData(target_id, target_values),),
        standalone=False,
    )
    values = {
        field.definition.tag: field.value
        for field in decode_uas_local_set(_complete_uas()).fields
        if field.definition.tag != 1
    }
    values[2] = timestamp
    values[74] = vmti
    return encode_uas_local_set(values)


class _OntologyResolver:
    def __init__(self, result: OntologyEntityResolution | None) -> None:
        self.result = result

    def resolve_entity(
        self,
        ontology_iri: str,
        entity_iri: str,
    ) -> OntologyEntityResolution | None:
        return self.result


def _ontology_uas() -> tuple[bytes, OntologyLocalSet]:
    ontology = OntologyLocalSet(
        ontology_id=12,
        ontology_iri="https://example.org/fmv-objects.owl",
        entity_iri="https://example.org/fmv-objects.owl#Truck",
        label="truck",
    )
    vmti = encode_vmti_local_set({4: 6}, ontologies=(ontology,))
    values = {
        field.definition.tag: field.value
        for field in decode_uas_local_set(_complete_uas()).fields
        if field.definition.tag != 1
    }
    values[74] = vmti
    return encode_uas_local_set(values), ontology


def _command_uas(
    *,
    timestamp: int,
    commands: tuple[ControlCommand, ...] = (),
    acknowledgements: tuple[int, ...] = (),
) -> bytes:
    values = {
        field.definition.tag: field.value
        for field in decode_uas_local_set(_complete_uas()).fields
        if field.definition.tag != 1
    }
    values[2] = timestamp
    if commands:
        values[115] = commands
    if acknowledgements:
        values[116] = ControlCommandVerificationList(acknowledgements)
    return encode_uas_local_set(values)


def _stateful_uas(*, timestamp: int, values: dict[int, object]) -> bytes:
    fields = {
        field.definition.tag: field.value
        for field in decode_uas_local_set(_complete_uas()).fields
        if field.definition.tag != 1
    }
    fields[2] = timestamp
    fields.update(values)
    return encode_uas_local_set(fields)


def _verify(data: bytes) -> object:
    verifier = FMVVerifier(require_security=False, require_miis=False)
    for offset in range(0, len(data), 73):
        verifier.feed(data[offset : offset + 73])
    return verifier.finish(source="memory.ts")


def test_verifier_reports_stream_inventory_and_successful_checks() -> None:
    report = _verify(_transport())

    assert report.ok
    assert report.source == "memory.ts"
    assert report.transport_packets == 6
    assert report.programs == (1,)
    assert report.klv_packets == 1
    assert report.st0601_packets == 1
    assert report.st0903_packets == 0
    assert [(stream.pid, stream.kind, stream.pes_packets) for stream in report.streams] == [
        (0x101, "video", 1),
        (0x102, "klv", 1),
    ]
    passed = {
        finding.code
        for finding in report.findings
        if finding.status is VerificationStatus.PASS
    }
    assert {
        "transport.structure",
        "transport.continuity",
        "psi.pat",
        "psi.pmt",
        "fmv.video",
        "fmv.klv",
        "st1402.metadata",
        "st1402.pcr",
        "metadata.decode",
        "st0902.profile",
    } <= passed
    assert any(
        finding.code == "st1001.audio"
        and finding.status is VerificationStatus.NOT_APPLICABLE
        for finding in report.findings
    )
    assert any(
        finding.code == "st1402.metadata_delay"
        and finding.status is VerificationStatus.PASS
        for finding in report.findings
    )
    assert any(
        finding.code == "st1402.metadata_std"
        and finding.status is VerificationStatus.PASS
        for finding in report.findings
    )


def test_verifier_pmt_validation_cache_is_bounded_to_active_programs() -> None:
    verifier = FMVVerifier(require_security=False, require_miis=False)
    stream = ElementaryStreamInfo(0x1B, 0x101, ())

    for revision in range(128):
        pmt = parse_pmt(
            build_pmt_section(
                program_number=1,
                pcr_pid=0x101,
                streams=(stream,),
                descriptors=(Descriptor(0x80, revision.to_bytes(1, "big")),),
                version_number=revision % 32,
            )
        )
        verifier._observe_pmt(pmt)

    assert verifier._validated_pmts == {
        1: (pmt.version_number, pmt.raw),
    }


def test_verifier_accepts_security_metadata_in_one_carriage_mechanism() -> None:
    security = decode_security_local_set(
        encode_security_local_set(
            {
                1: SecurityClassification.UNCLASSIFIED,
                2: 14,
                3: "//USA",
                12: 14,
                13: "USA",
                22: 12,
            },
            standalone=False,
        ),
        standalone=False,
    )
    packet = encode_uas_local_set({**_complete_uas_values(), 48: security})

    report = _verify(_transport(packet))

    finding = next(
        item for item in report.findings if item.code == "misp.security.carriage"
    )
    assert finding.status is VerificationStatus.PASS
    assert finding.requirement == "MISP-2015.1-49"
    assert finding.program_number == 1
    assert "synchronous (PID 258 service 0)" in finding.message


def test_verifier_rejects_security_metadata_in_both_carriage_mechanisms() -> None:
    security = decode_security_local_set(
        encode_security_local_set(
            {
                1: SecurityClassification.UNCLASSIFIED,
                2: 14,
                3: "//USA",
                12: 14,
                13: "USA",
                22: 12,
            },
            standalone=False,
        ),
        standalone=False,
    )
    packet = encode_uas_local_set({**_complete_uas_values(), 48: security})

    report = _verify(_dual_carriage_transport(packet))

    finding = next(
        item
        for item in report.errors
        if item.code == "misp.security.multiple_carriage"
    )
    assert finding.requirement == "MISP-2015.1-49"
    assert finding.program_number == 1
    assert "asynchronous (PID 259)" in finding.message
    assert "synchronous (PID 258 service 0)" in finding.message


def test_verifier_reports_embedded_st0604_timestamp_coverage() -> None:
    first = 1_700_000_000_000_000
    conforming_sps = bytes.fromhex(
        "000000016742c028d9005005bb0110000003001000000303c0f183248000"
    )
    payload = b"".join(
        (
            conforming_sps,
            encode_avc_timestamp_sei(first),
            b"\x00\x00\x01\x65\x80frame-1",
            encode_avc_timestamp_sei(first + 33_333),
            b"\x00\x00\x01\x41\x80frame-2",
            b"\x00\x00\x01\x0b",
        )
    )

    report = _verify(_transport(video_payload=payload))
    video = next(stream for stream in report.streams if stream.kind == "video")

    assert video.video_timestamps is not None
    assert video.video_timestamps.access_units == 2
    assert video.video_timestamps.timestamps == 2
    assert video.video_timestamps.timestamped_access_units == 2
    assert video.video_timestamps.missing_access_units == 0
    assert video.video_timestamps.duplicate_timestamp_access_units == 0
    assert video.video_timestamps.unassociated_timestamps == 0
    assert video.video_timestamps.microsecond_timestamps == 2
    assert video.video_timestamps.first_microseconds == first
    assert video.video_timestamps.last_microseconds == first + 33_333
    assert video.video_properties is not None
    assert video.video_properties.sequences == 1
    assert video.video_properties.latest is not None
    assert video.video_properties.latest.width == 1280
    assert video.video_properties.latest.height == 720
    assert video.video_properties.latest.misp_profile_level is True
    assert video.to_dict()["video_timestamps"]["timestamps"] == 2
    assert video.to_dict()["video_properties"]["latest"]["profile"] == (
        "Constrained Baseline"
    )
    assert "ST0604=2/2 access units" in report.format_text()
    assert "1280x720 Constrained Baseline Level 4.0 progressive" in report.format_text()
    assert any(
        finding.code == "st0604.timestamp.syntax"
        and finding.status is VerificationStatus.PASS
        for finding in report.findings
    )
    assert any(
        finding.code == "misp.video.profile_level"
        and finding.status is VerificationStatus.PASS
        for finding in report.findings
    )


def test_verifier_rejects_avc_profile_outside_adopted_misp_range() -> None:
    public_fixture_sps = bytes.fromhex(
        "00000001674200298c680780227e5ffc00040004400000fa40003a9825"
        "0000000000000000"
    )
    report = _verify(_transport(video_payload=public_fixture_sps + b"\x00\x00\x01\x09"))

    issue = next(
        finding
        for finding in report.errors
        if finding.code == "misp.video.profile_level"
    )
    assert issue.requirement == "MISP-2018.2-114"
    assert "Baseline Level 4.1" in issue.message


def test_verifier_accepts_hevc_main10_profile_in_adopted_misp_range() -> None:
    main10_sps = bytes.fromhex(
        "0000000142010102200000030090000003000003003fa005020169365959a493"
        "2bc05a02000007d20000ea6010"
    )
    report = _verify(
        _transport(
            video_stream_type=0x24,
            video_payload=main10_sps + b"\x00\x00\x01\x44\x01",
        )
    )
    video = next(stream for stream in report.streams if stream.kind == "video")

    assert video.video_properties is not None
    assert video.video_properties.latest is not None
    assert video.video_properties.latest.profile == "Main 10"
    assert video.video_properties.latest.display_aspect_ratio == Fraction(16, 9)
    assert video.video_properties.latest.frame_rate == Fraction(30_000, 1_001)
    finding = next(
        item for item in report.findings if item.code == "misp.video.profile_level"
    )
    assert finding.status is VerificationStatus.PASS
    assert finding.requirement == "MISP-2018.2-113"


def test_verifier_fails_when_recognized_video_frames_lack_st0604_timestamps() -> None:
    payload = b"".join(
        (
            encode_avc_timestamp_sei(1_700_000_000_000_000),
            b"\x00\x00\x01\x65\x80frame-1",
            b"\x00\x00\x01\x41\x80frame-2",
            b"\x00\x00\x01\x0b",
        )
    )

    report = _verify(_transport(video_payload=payload))
    video = next(stream for stream in report.streams if stream.kind == "video")
    issue = next(
        finding
        for finding in report.errors
        if finding.code == "st0604.timestamp.missing"
    )

    assert issue.requirement == "MISP-2018.1-104 / MISB ST 0604.6"
    assert issue.program_number == 1
    assert issue.pid == 0x101
    assert "1 of 2 recognized video access unit(s)" in issue.message
    assert video.video_timestamps.timestamped_access_units == 1
    assert video.video_timestamps.missing_access_units == 1


def test_verifier_reports_duplicate_and_unassociated_st0604_timestamps() -> None:
    payload = b"".join(
        (
            encode_avc_timestamp_sei(1_700_000_000_000_000),
            encode_avc_timestamp_sei(1_700_000_000_000_001),
            b"\x00\x00\x01\x65\x80frame-1",
            encode_avc_timestamp_sei(1_700_000_000_000_002),
            b"\x00\x00\x01\x0b",
        )
    )

    report = _verify(_transport(video_payload=payload))
    video = next(stream for stream in report.streams if stream.kind == "video")

    assert video.video_timestamps is not None
    assert video.video_timestamps.access_units == 1
    assert video.video_timestamps.timestamps == 3
    assert video.video_timestamps.timestamped_access_units == 1
    assert video.video_timestamps.duplicate_timestamp_access_units == 1
    assert video.video_timestamps.unassociated_timestamps == 1
    assert {finding.code for finding in report.errors} >= {
        "st0604.timestamp.duplicate",
        "st0604.timestamp.unassociated",
    }


def test_verifier_reports_provable_synchronous_metadata_delay_violation() -> None:
    report = _verify(_transport(metadata_pts=99_001))

    issue = next(
        finding
        for finding in report.errors
        if finding.code == "st1402.metadata_delay.excessive_delay"
    )
    assert issue.requirement == "ST 1402.2 ST 1402-12"
    assert issue.program_number == 1
    assert issue.pid == 0x102
    assert "minimum decoder delay" in issue.message
    assert any(
        finding.code == "st1402.metadata_std.excessive_delay"
        for finding in report.errors
    )


def test_verifier_reports_exact_metadata_std_buffer_overflow() -> None:
    unknown_key = bytes.fromhex("060E2B34020B01010E0103017F000000")
    large_klv = unknown_key + encode_ber_length(1_100) + b"x" * 1_100
    report = _verify(
        _transport(
            large_klv,
            metadata_buffer_size=1,
            metadata_input_leak_rate=4_000,
        )
    )

    issue = next(
        finding
        for finding in report.errors
        if finding.code == "st1402.metadata_std.main_buffer_overflow"
    )
    assert issue.requirement == "ITU-T H.222.0 §2.12.10"
    assert issue.pid == 0x102


def test_verifier_marks_trailing_metadata_std_window_unverifiable() -> None:
    report = _verify(_transport()[:-188])

    assert any(
        finding.code == "st1402.metadata_std.unverifiable"
        for finding in report.warnings
    )
    assert not any(finding.code == "st1402.metadata_std" for finding in report.passes)


def test_verifier_audits_configured_asynchronous_metadata_std() -> None:
    unknown_key = bytes.fromhex("060E2B34020B01010E0103017F000000")
    large_klv = unknown_key + encode_ber_length(1_100) + b"x" * 1_100
    descriptor = MetadataSTDDescriptor.from_physical(
        input_bits_per_second=1_600_000,
        buffer_bytes=1_024,
        output_bits_per_second=400,
    )

    report = verify_fmv_stream(
        BytesIO(_asynchronous_transport(large_klv)),
        require_security=False,
        require_miis=False,
        asynchronous_std_descriptors={(1, 0x102): descriptor},
    )

    issue = next(
        finding
        for finding in report.errors
        if finding.code == "st1402.metadata_std.main_buffer_overflow"
    )
    assert issue.requirement == "ITU-T H.222.0 §2.12.10"
    assert issue.program_number == 1
    assert issue.pid == 0x102


def test_verifier_reports_complete_asynchronous_metadata_std_coverage() -> None:
    descriptor = MetadataSTDDescriptor.from_physical(
        input_bits_per_second=1_600_000,
        buffer_bytes=16 * 1_024,
        output_bits_per_second=800_000,
    )

    report = verify_fmv_stream(
        BytesIO(_asynchronous_transport(_complete_uas())),
        require_security=False,
        require_miis=False,
        asynchronous_std_descriptors={(1, 0x102): descriptor},
    )

    assert any(finding.code == "st1402.metadata_std" for finding in report.passes)
    assert not any(
        finding.code == "st1402.metadata_std.unverifiable"
        for finding in report.warnings
    )


def test_verifier_applies_event_aware_st0601_validation_context() -> None:
    expected_timestamp = 1_700_000_000_000_000
    observed: list[tuple[int, int, bytes]] = []

    def matching_context(
        event: PESStreamEvent, packet: KLVPacket
    ) -> ST0601ValidationContext:
        observed.append((event.program_number, event.pid, bytes(packet)))
        return ST0601ValidationContext(
            metadata_birth_timestamp=expected_timestamp
        )

    valid = verify_fmv_stream(
        BytesIO(_transport()),
        require_security=False,
        require_miis=False,
        st0601_context_provider=matching_context,
    )

    assert valid.ok
    assert observed == [(1, 0x102, _complete_uas())]
    context_summary = valid.st0601_streams[0]
    assert context_summary.context_provided_packets == 1
    assert context_summary.birth_timestamp_validated_packets == 1
    assert context_summary.imap_precision_validated_items == 0
    assert context_summary.vmti_context_validated_packets == 0
    serialized_context = valid.to_dict()["st0601_streams"][0]["validation_context"]
    assert serialized_context == {
        "packets_provided": 1,
        "birth_timestamp_validated_packets": 1,
        "imap_precision_validated_items": 0,
        "vmti_context_validated_packets": 0,
    }
    assert "external context: 1/1 packet(s)" in valid.format_text()

    def mismatching_context(
        _event: PESStreamEvent, _packet: KLVPacket
    ) -> ST0601ValidationContext:
        return ST0601ValidationContext(
            metadata_birth_timestamp=expected_timestamp + 1
        )

    invalid = verify_fmv_stream(
        BytesIO(_transport()),
        require_security=False,
        require_miis=False,
        st0601_context_provider=mismatching_context,
    )

    issue = next(
        finding
        for finding in invalid.errors
        if finding.code == "metadata.decode"
    )
    assert "time of birth" in issue.message
    assert issue.program_number == 1
    assert issue.pid == 0x102


def test_verifier_reports_imap_and_vmti_context_assurance() -> None:
    timestamp = 1_700_000_000_000_000
    klv = update_uas_local_set(
        _vmti_uas(DetectionStatus.ACTIVE_MOVING, timestamp=timestamp),
        {104: IMAPFieldValue(1_000.0, 3)},
    )
    context = ST0601ValidationContext(
        metadata_birth_timestamp=timestamp,
        imap_system_precisions={104: 0.5, 105: 0.5},
        vmti_context=VMTIValidationContext(),
    )

    report = verify_fmv_stream(
        BytesIO(_transport(klv)),
        validate_mismms=False,
        st0601_context_provider=lambda _event, _packet: context,
    )

    assert report.ok
    stream = report.st0601_streams[0]
    assert stream.context_provided_packets == 1
    assert stream.birth_timestamp_validated_packets == 1
    assert stream.imap_precision_validated_items == 1
    assert stream.vmti_context_validated_packets == 1
    html = report.to_html()
    assert "External context" in html
    assert "IMAP precision validated" in html
    assert "VMTI context validated" in html


def test_verifier_marks_unconfigured_asynchronous_metadata_std_unverifiable() -> None:
    report = verify_fmv_stream(
        BytesIO(_asynchronous_transport(_complete_uas())),
        require_security=False,
        require_miis=False,
    )

    issue = next(
        finding
        for finding in report.warnings
        if finding.code == "st1402.metadata_std.unverifiable"
    )
    assert "1 metadata PES" in issue.message
    assert not any(finding.code == "st1402.metadata_std" for finding in report.passes)


def test_verifier_reports_proven_synchronous_metadata_delay_compliance() -> None:
    report = _verify(_transport(metadata_pts=81_000))

    assert any(
        finding.code == "st1402.metadata_delay"
        and finding.status is VerificationStatus.PASS
        for finding in report.findings
    )
    assert not any(
        finding.code.startswith("st1402.metadata_delay.")
        for finding in (*report.errors, *report.warnings)
    )


def test_verifier_applies_optional_vmti_ontology_resolver() -> None:
    uas, ontology = _ontology_uas()
    accepted = OntologyEntityResolution(
        ontology_iri=ontology.ontology_iri,
        entity_iri=ontology.entity_iri,
        is_owl_ontology=True,
        rdfs_labels=frozenset({"truck"}),
    )

    verifier = FMVVerifier(
        require_security=False,
        require_miis=False,
        ontology_resolver=_OntologyResolver(accepted),
    )
    verifier.feed(_transport(uas))
    valid = verifier.finish()
    assert valid.st0903_packets == 1
    assert not any(finding.code == "st0601.field" for finding in valid.errors)

    verifier = FMVVerifier(
        require_security=False,
        require_miis=False,
        ontology_resolver=_OntologyResolver(None),
    )
    verifier.feed(_transport(uas))
    invalid = verifier.finish()
    issue = next(finding for finding in invalid.errors if finding.code == "st0601.field")
    assert issue.tags == (74,)
    assert "does not contain entityIRI" in issue.message


def test_verifier_reports_missing_st0902_items_without_throwing() -> None:
    encoded_transport = _transport(
        encode_uas_local_set({2: 1_700_000_000_000_000, 65: 19})
    )
    report = _verify(encoded_transport)

    assert encoded_transport
    assert not report.ok
    missing = [finding for finding in report.errors if finding.code == "st0902.missing"]
    assert missing
    assert any(finding.requirement == "mission_id" for finding in missing)
    assert all(finding.count == 1 for finding in missing)


def test_verifier_reports_distributed_sensor_altitude_exclusive_or() -> None:
    first = _complete_uas_values()
    first[2] = 1_700_000_000_000_000
    first[75] = 1_000.0
    second = {
        2: first[2] + 1_000_000,
        65: 19,
        104: IMAPFieldValue(1_001.0, 3),
    }

    report = _verify(
        _transport_sequence(
            encode_uas_local_set(first),
            encode_uas_local_set(second),
        )
    )

    issue = next(
        finding
        for finding in report.errors
        if finding.code == "st0902.exclusive_or"
    )
    assert issue.requirement == "sensor_altitude"
    assert issue.tags == (75, 104)
    assert issue.program_number == 1
    assert issue.pid == 0x102


def test_verifier_accepts_mismms_completed_by_terminal_segment_union() -> None:
    values = _complete_uas_values(without={13})
    values[100] = _segment(7, {13: 49.0})

    report = _verify(_transport(encode_uas_local_set(values)))

    assert not any(
        finding.code.startswith(("st0902.", "st1607."))
        for finding in report.errors
    )
    assert any(finding.code == "st0902.profile" for finding in report.passes)


def test_verifier_reports_incomplete_terminal_segment_union() -> None:
    values = _complete_uas_values(without={13})
    values[100] = _segment(7, {})

    report = _verify(_transport(encode_uas_local_set(values)))

    issue = next(
        finding
        for finding in report.errors
        if finding.code == "st1607.mismms_missing"
    )
    assert issue.requirement == "ST 1607-06"
    assert issue.tags == (13,)
    assert "7" in issue.message


def test_verifier_requires_amended_stream_root_to_satisfy_mismms() -> None:
    values = _complete_uas_values(without={13})
    values[101] = _amend(9, {13: 49.0})

    report = _verify(_transport(encode_uas_local_set(values)))

    issue = next(
        finding
        for finding in report.errors
        if finding.code == "st1607.mismms_missing"
    )
    assert issue.requirement == "ST 1607-05"
    assert issue.tags == (13,)
    assert "root" in issue.message


def test_structural_verifier_reports_invalid_segment_security_override() -> None:
    values = _complete_uas_values()
    incomplete_country_security = decode_security_local_set(
        bytes.fromhex("0C 01 0D"),
        standalone=False,
        require_required=False,
    )
    values[100] = _segment(7, {48: incomplete_country_security})
    verifier = FMVVerifier(
        require_security=False,
        require_miis=False,
        validate_mismms=False,
    )
    verifier.feed(_transport(encode_uas_local_set(values)))

    report = verifier.finish()

    issue = next(
        finding
        for finding in report.errors
        if finding.code == "st1607.incomplete_child_country_security"
    )
    assert issue.requirement == "ST 1607-04"
    assert issue.tags == (13,)
    assert "7" in issue.message


def test_structural_verifier_reports_duplicate_composite_z_order() -> None:
    values = _complete_uas_values()
    composite = CompositeImagingLocalSet(2, 480, 640, 0, 0, 3)
    values[100] = (
        _segment(7, {99: composite}),
        _segment(8, {99: composite}),
    )

    report = _verify(_transport(encode_uas_local_set(values)))

    issues = [
        finding
        for finding in report.errors
        if finding.code == "st1602.duplicate_composite_z_order"
    ]
    assert len(issues) == 2
    assert all(issue.requirement == "ST 1602-04" for issue in issues)
    assert all(issue.tags == (99,) for issue in issues)
    assert {"7", "8"} == {
        issue.message.split("metadata substream ", 1)[1].split(":", 1)[0]
        for issue in issues
    }


def test_structural_verifier_requires_miis_in_every_composite_sensor() -> None:
    values = _complete_uas_values()
    values[100] = (
        _segment(7, {99: CompositeImagingLocalSet(2, 480, 640, 0, 0, 1)}),
        _segment(
            8,
            {
                94: MIISCoreIdentifier(
                    1,
                    sensor_quality=IdentifierQuality.PHYSICAL,
                    sensor_id=UUID("00000000-0000-4000-8000-000000000008"),
                ),
                99: CompositeImagingLocalSet(2, 480, 640, 0, 0, 2),
            },
        ),
    )

    report = _verify(_transport(encode_uas_local_set(values)))

    issue = next(
        finding
        for finding in report.errors
        if finding.code == "st1602.missing_composite_sensor_miis"
    )
    assert issue.requirement == "ST 1602.1-10"
    assert issue.tags == (94,)
    assert "metadata substream 7" in issue.message


def test_verifier_reports_nested_security_population_paths() -> None:
    encoded_transport = _transport(
        encode_uas_local_set({2: 1_700_000_000_000_000, 65: 19})
    )
    verifier = FMVVerifier(require_miis=False)
    verifier.feed(encoded_transport)
    report = verifier.finish()

    coverage = {
        item.requirement: item for item in report.st0601_streams[0].mismms_coverage or ()
    }
    security = coverage["security_classification"]
    assert security.status is MISMMSPopulationStatus.MISSING
    assert security.parent_tag == 48
    assert security.tag_paths == ((48, 1),)
    serialized = report.to_dict()["st0601_streams"][0]["mismms_coverage"]
    assert any(
        item["requirement"] == "security_classification"
        and item["tag_paths"] == [[48, 1]]
        for item in serialized
    )


def test_verifier_reports_caller_supplied_security_policy_mismatch() -> None:
    security = decode_security_local_set(
        encode_security_local_set(
            {
                1: SecurityClassification.UNCLASSIFIED,
                2: 14,
                3: "//USA",
                6: "USA",
                12: 14,
                13: "USA",
                22: 12,
            },
            standalone=False,
        ),
        standalone=False,
    )
    packet = encode_uas_local_set({**_complete_uas_values(), 48: security})
    verifier = FMVVerifier(
        require_miis=False,
        security_context=MISMMSecurityContext(
            expected_classification=SecurityClassification.SECRET,
            required_releasing_countries=frozenset({"USA", "CAN"}),
        ),
    )
    verifier.feed(_transport(packet))

    report = verifier.finish()
    policy = {
        finding.requirement: finding
        for finding in report.errors
        if finding.code == "st0902.security_policy"
    }

    assert set(policy) == {
        "security_classification",
        "security_releasing_instructions",
    }
    assert policy["security_classification"].tags == (1,)
    assert policy["security_releasing_instructions"].tags == (6,)


def test_verifier_cli_accepts_explicit_security_policy(tmp_path, capsys) -> None:
    security = decode_security_local_set(
        encode_security_local_set(
            {
                1: SecurityClassification.UNCLASSIFIED,
                2: 14,
                3: "//USA",
                6: "USA",
                12: 14,
                13: "USA",
                22: 12,
            },
            standalone=False,
        ),
        standalone=False,
    )
    source = tmp_path / "security-policy.ts"
    source.write_bytes(
        _transport(encode_uas_local_set({**_complete_uas_values(), 48: security}))
    )

    exit_code = main(
        [
            str(source),
            "--format",
            "json",
            "--no-require-miis",
            "--security-classification",
            "secret",
            "--require-release-country",
            "CAN",
        ]
    )

    assert exit_code == 1
    findings = json.loads(capsys.readouterr().out)["findings"]
    assert {
        finding["requirement"]
        for finding in findings
        if finding["code"] == "st0902.security_policy"
    } == {"security_classification", "security_releasing_instructions"}


def test_verifier_inventory_explains_st0601_field_coverage() -> None:
    start = 1_700_000_000_000_000
    first = update_uas_local_set(
        encode_uas_local_set({2: start + 1_000_000, 3: "mission-1", 65: 18}),
        {150: RawFieldValue(b"extension")},
    )
    second = encode_uas_local_set(
        {
            2: start,
            3: SpecialValue.UNKNOWN,
            65: 19,
        }
    )
    malformed = update_uas_local_set(
        first,
        {2: start + 2_000_000, 5: RawFieldValue(b"\x00")},
        field_decoding=FieldDecodingMode.PRESERVE,
    )

    report = _verify(_transport_sequence(first, second, malformed))

    assert len(report.st0601_streams) == 1
    stream = report.st0601_streams[0]
    assert stream.program_number == 1
    assert stream.pid == 0x102
    assert stream.metadata_service_id == 0
    assert stream.packets == 3
    assert stream.timestamped_packets == 3
    assert stream.invalid_or_missing_timestamp_packets == 0
    assert stream.first_timestamp == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
    assert stream.last_timestamp == datetime(2023, 11, 14, 22, 13, 22, tzinfo=timezone.utc)
    assert stream.timestamp_regressions == 1
    assert stream.duplicate_timestamps == 0
    assert stream.maximum_forward_gap_seconds == 2.0
    coverage = {item.requirement: item for item in stream.mismms_coverage or ()}
    assert coverage["precision_timestamp"].status is MISMMSPopulationStatus.CURRENT
    assert coverage["mission_id"].status is MISMMSPopulationStatus.CURRENT
    assert coverage["platform_heading"].status is MISMMSPopulationStatus.MISSING
    assert stream.versions == (18, 19)
    tags = {item.tag: item for item in stream.tags}
    assert tags[3].name == "Mission ID"
    assert tags[3].packets_present == 3
    assert tags[3].occurrences == 3
    assert tags[3].zero_length_items == 1
    assert tags[5].decoding_issues == 1
    assert tags[150].name is None
    assert tags[150].packets_present == 2
    assert stream.untracked_item_occurrences == 0
    payload = report.to_dict()["st0601_streams"][0]
    assert payload["tags"]["3"]["zero_length_items"] == 1
    assert payload["timestamp_regressions"] == 1
    assert any(
        item["requirement"] == "platform_heading" and item["status"] == "missing"
        for item in payload["mismms_coverage"]
    )
    text = report.format_text()
    assert "ST 0601 services:" in text
    assert "ST 0902 population:" in text
    assert any(
        finding.code == "st0601.timestamp.regression" for finding in report.warnings
    )


def test_verifier_labels_misp_time_and_tracks_derived_utc_with_roc_expiry() -> None:
    utc_start = datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
    raw_start = 1_700_000_029_000_000
    first = encode_uas_local_set(
        {2: raw_start, 65: 19, 136: 29, 137: 125_000}
    )
    second = encode_uas_local_set({2: raw_start + 1_000_000, 65: 19})
    expired = encode_uas_local_set({2: raw_start + 32_000_000, 65: 19})

    report = _verify(_transport_sequence(first, second, expired))
    stream = report.st0601_streams[0]

    assert stream.first_misp_timestamp_microseconds == raw_start
    assert stream.last_misp_timestamp_microseconds == raw_start + 32_000_000
    assert stream.utc_timestamped_packets == 2
    assert stream.utc_conversion_unavailable_packets == 1
    assert stream.first_utc_timestamp == utc_start + timedelta(microseconds=125_000)
    assert stream.last_utc_timestamp == utc_start + timedelta(
        seconds=1, microseconds=125_000
    )
    payload = stream.to_dict()
    assert payload["timestamp_time_scale"] == "MISP"
    assert payload["first_misp_timestamp_microseconds"] == raw_start
    assert payload["first_utc_timestamp"] == "2023-11-14T22:13:20.125000+00:00"
    assert "MISP Item 2" in report.format_text()
    html = report.to_html()
    assert "First MISP coordinate" in html
    assert "First UTC timestamp" in html


def test_verifier_keeps_time_adjustment_at_boundary_and_honors_zli() -> None:
    raw_start = 1_700_000_029_000_000
    first = encode_uas_local_set({2: raw_start, 65: 19, 136: 29})
    boundary = encode_uas_local_set({2: raw_start + 30_000_000, 65: 19})
    cleared = encode_uas_local_set(
        {2: raw_start + 31_000_000, 65: 19, 136: SpecialValue.UNKNOWN}
    )

    stream = _verify(_transport_sequence(first, boundary, cleared)).st0601_streams[0]

    assert stream.utc_timestamped_packets == 2
    assert stream.utc_conversion_unavailable_packets == 1
    assert stream.first_utc_timestamp == datetime(
        2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc
    )
    assert stream.last_utc_timestamp == datetime(
        2023, 11, 14, 22, 13, 50, tzinfo=timezone.utc
    )


def test_verifier_does_not_apply_future_time_state_to_regressed_packets() -> None:
    raw_start = 1_700_000_029_000_000
    stream = _verify(
        _transport_sequence(
            encode_uas_local_set({2: raw_start, 65: 19, 136: 29}),
            encode_uas_local_set({2: raw_start - 2_000_000, 65: 19}),
            encode_uas_local_set({2: raw_start - 1_000_000, 65: 19}),
            encode_uas_local_set({2: raw_start + 1_000_000, 65: 19}),
        )
    ).st0601_streams[0]

    assert stream.timestamp_regressions == 1
    assert stream.utc_timestamped_packets == 2
    assert stream.utc_conversion_unavailable_packets == 2


def test_verifier_detects_transport_continuity_gaps() -> None:
    damaged = bytearray(_transport(second_video=True))
    video_packet_offsets = [
        offset
        for offset in range(0, len(damaged), 188)
        if ((damaged[offset + 1] & 0x1F) << 8) | damaged[offset + 2] == 0x101
        and damaged[offset + 1] & 0x40
    ]
    assert len(video_packet_offsets) == 2
    second = video_packet_offsets[1]
    damaged[second + 3] = (damaged[second + 3] & 0xF0) | 0x07

    report = _verify(bytes(damaged))

    issue = next(finding for finding in report.errors if finding.code == "transport.continuity")
    assert issue.pid == 0x101
    assert issue.first_offset == second
    assert "expected" in issue.message


def test_adaptation_only_discontinuity_reanchors_payload_continuity() -> None:
    transport = bytearray(_transport(second_video=True))
    video_packet_offsets = [
        offset
        for offset in range(0, len(transport), 188)
        if ((transport[offset + 1] & 0x1F) << 8) | transport[offset + 2] == 0x101
        and transport[offset + 1] & 0x40
    ]
    second = video_packet_offsets[1]
    transport[second + 3] = (transport[second + 3] & 0xF0) | 0x07
    discontinuity = encode_pcr_packet(
        pid=0x101,
        pcr=ProgramClockReference(4_500, 0),
        continuity_counter=0,
        discontinuity=True,
    )
    transport[second:second] = discontinuity

    report = _verify(bytes(transport))

    assert not any(finding.code == "transport.continuity" for finding in report.errors)


def test_program_clock_discontinuity_reanchors_all_program_pts_streams() -> None:
    transport = bytearray(
        _transport(second_video=True, second_video_pts=100_000)
    )
    video_packet_offsets = [
        offset
        for offset in range(0, len(transport), 188)
        if ((transport[offset + 1] & 0x1F) << 8) | transport[offset + 2] == 0x101
        and transport[offset + 1] & 0x40
    ]
    discontinuity = encode_pcr_packet(
        pid=0x101,
        pcr=ProgramClockReference(4_500, 0),
        continuity_counter=0,
        discontinuity=True,
    )
    transport[video_packet_offsets[1] : video_packet_offsets[1]] = discontinuity

    report = _verify(bytes(transport))

    assert not any(finding.code == "st1402.pts.interval" for finding in report.errors)


def test_verifier_reports_corrupt_dvb_service_description_crc() -> None:
    body = bytes.fromhex("0001 C10000 0001 FF")
    header = bytes((0x42, 0xF0, len(body) + 4))
    without_crc = header + body
    section = bytearray(without_crc + mpeg2_crc32(without_crc).to_bytes(4, "big"))
    section[-1] ^= 0x01
    payload = b"\x00" + section
    adaptation_length = 183 - len(payload)
    sdt_packet = (
        bytes((0x47, 0x40, 0x11, 0x30, adaptation_length, 0x00))
        + b"\xff" * (adaptation_length - 1)
        + payload
    )
    transport = _transport()
    report = _verify(transport[:376] + sdt_packet + transport[376:])

    finding = next(item for item in report.errors if item.code == "psi.crc")
    assert finding.pid == 0x11
    assert finding.first_offset == 376
    assert finding.requirement == "ETSI EN 300 468 SDT CRC_32"
    assert "SDT MPEG-2 CRC-32 mismatch" in finding.message


def test_verifier_integrates_pcr_bracketed_pat_and_pmt_blackout_proof() -> None:
    muxer = TransportMuxer(
        transport_stream_id=1,
        program_number=1,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=(ElementaryStreamInfo(0x1B, 0x101, ()),),
    )
    transport = bytearray(b"".join(muxer.program_tables()))
    transport.extend(muxer.mux_pcr(ProgramClockReference(0, 0)))
    transport.extend(b"".join(muxer.program_tables()))
    transport.extend(muxer.mux_pcr(ProgramClockReference(9_000, 0)))
    transport.extend(muxer.mux_pcr(ProgramClockReference(31_500, 0)))
    transport.extend(b"".join(muxer.program_tables()))
    transport.extend(muxer.mux_pcr(ProgramClockReference(36_000, 0)))

    report = _verify(bytes(transport))
    findings = {item.code: item for item in report.errors}

    assert findings["st1402.psi.pat.interval"].requirement == (
        "MISB ST 1402.2 ST 1402-02"
    )
    assert "at least 250.000000 milliseconds" in findings[
        "st1402.psi.pat.interval"
    ].message
    assert "at least 250.000000 milliseconds" in findings[
        "st1402.psi.pmt.interval"
    ].message


def test_verifier_returns_actionable_report_for_non_transport_input() -> None:
    report = _verify(b"not-an-mpeg-transport")

    assert not report.ok
    assert report.transport_packets == 0
    assert {finding.code for finding in report.errors} >= {
        "transport.structure",
        "psi.pat",
        "psi.pmt",
    }
    payload = report.to_dict()
    assert payload["result"] == "fail"
    assert json.loads(report.to_json())["summary"]["errors"] == len(report.errors)
    assert "Result: FAIL" in report.format_text()
    html = report.to_html(title='Unsafe <report> & "title"')
    assert "<!doctype html>" in html
    assert "Unsafe &lt;report&gt; &amp; &quot;title&quot;" in html
    assert "Source: memory.ts" in html
    assert 'class="result error">FAIL' in html


def test_file_api_and_cli_emit_machine_readable_report(tmp_path, capsys) -> None:
    source = tmp_path / "mission.ts"
    source.write_bytes(_transport())

    report = verify_fmv_file(source, require_security=False, require_miis=False)
    assert report.ok
    assert report.source == str(source)

    exit_code = main(
        [
            str(source),
            "--format",
            "json",
            "--no-require-security",
            "--no-require-miis",
        ]
    )
    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["result"] == "pass"
    assert output["streams"][0]["kind"] == "video"

    exit_code = main(
        [
            str(source),
            "--format",
            "html",
            "--no-require-security",
            "--no-require-miis",
        ]
    )
    assert exit_code == 0
    html = capsys.readouterr().out
    assert "<!doctype html>" in html
    assert str(source) in html
    assert 'class="result pass">PASS' in html
    assert "ST 0902 minimum-item population at stream end" in html

    exit_code = main([str(source), "--format", "json", "--profile", "structural"])
    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    profile = next(item for item in output["findings"] if item["code"] == "st0902.profile")
    assert profile["status"] == "not_applicable"


def test_verifier_reports_bad_klv_unknown_keys_and_embedded_vmti() -> None:
    corrupt = bytearray(_complete_uas())
    corrupt[-1] ^= 0xFF
    bad = _verify(_transport(bytes(corrupt)))
    assert any(finding.code == "metadata.decode" for finding in bad.errors)

    unknown_key = bytes.fromhex("060E2B34020B01010E0103017F000000")
    unknown = _verify(_transport(unknown_key + encode_ber_length(1) + b"x"))
    assert unknown.ok
    assert unknown.unknown_klv_packets == 1
    assert any(finding.code == "metadata.unknown_key" for finding in unknown.warnings)

    vmti_uas = encode_uas_local_set(
        {
            2: 1_700_000_000_000_000,
            3: "mission-1",
            5: 90.0,
            6: 0.0,
            7: 0.0,
            10: "platform-1",
            11: "EO",
            12: "Geodetic WGS84",
            13: 49.0,
            14: -123.0,
            15: 1_000.0,
            16: 20.0,
            17: 15.0,
            18: 0.0,
            19: -20.0,
            20: 0.0,
            21: 5_000.0,
            22: 10.0,
            23: 49.1,
            24: -123.1,
            25: 100.0,
            65: 19,
            74: bytes.fromhex("040106060100"),
        }
    )
    embedded = _verify(_transport(vmti_uas))
    assert embedded.st0903_packets == 1
    assert any(finding.code == "st0903.vmti" for finding in embedded.passes)


def test_verifier_reports_vmti_lifecycle_and_target_inventory() -> None:
    start = 1_700_000_000_000_000
    report = _verify(
        _transport_sequence(
            _vmti_uas(DetectionStatus.ACTIVE_MOVING, timestamp=start),
            _vmti_uas(DetectionStatus.DROPPED, timestamp=start + 1_000_000),
            _vmti_uas(DetectionStatus.ACTIVE_MOVING, timestamp=start + 2_000_000),
        )
    )

    assert report.st0903_packets == 3
    assert report.st0903_target_observations == 3
    assert report.st0903_unique_targets == 1
    assert report.to_dict()["metadata"]["st0903_unique_targets"] == 1
    assert "VMTI targets: 3 observation(s)" in report.format_text()
    issue = next(
        finding
        for finding in report.errors
        if finding.code == "st0903.lifecycle.retired_target_id_reused"
    )
    assert issue.requirement == "ST 0903.6-129"
    assert "target 7" in issue.message


def test_verifier_warns_when_vmti_detection_status_is_missing() -> None:
    report = _verify(
        _transport_sequence(
            _vmti_uas(None, timestamp=1_700_000_000_000_000),
        )
    )

    issue = next(
        finding
        for finding in report.warnings
        if finding.code == "st0903.lifecycle.missing_detection_status"
    )
    assert issue.requirement == "MISB ST 0903.6 §7.2"
    assert report.ok


def test_verifier_accepts_valid_control_command_lifecycle() -> None:
    start = 1_700_000_000_000_000
    report = _verify(
        _transport_sequence(
            _command_uas(
                timestamp=start,
                commands=(ControlCommand(5, "Fly"),),
            ),
            _command_uas(
                timestamp=start + 1_000_000,
                commands=(
                    ControlCommand(
                        5,
                        "Fly",
                        datetime.fromtimestamp(start / 1_000_000, timezone.utc),
                    ),
                ),
            ),
            _command_uas(
                timestamp=start + 2_000_000,
                acknowledgements=(5,),
            ),
        )
    )

    assert report.ok
    assert any(
        finding.code == "st0601.command.lifecycle" for finding in report.passes
    )


def test_verifier_reports_control_command_lifecycle_violations() -> None:
    start = 1_700_000_000_000_000
    report = _verify(
        _transport_sequence(
            _command_uas(
                timestamp=start,
                commands=(ControlCommand(5, "Fly"),),
            ),
            _command_uas(
                timestamp=start + 1_000_000,
                commands=(ControlCommand(5, "Land"), ControlCommand(4, "Orbit")),
                acknowledgements=(7,),
            ),
            _command_uas(
                timestamp=start + 2_000_000,
                acknowledgements=(5,),
            ),
            _command_uas(
                timestamp=start + 3_000_000,
                commands=(ControlCommand(5, "Fly"),),
            ),
        )
    )

    issues = {
        finding.code: finding
        for finding in report.errors
        if finding.code.startswith("st0601.command.")
    }
    assert set(issues) == {
        "st0601.command.command_changed",
        "st0601.command.non_increasing_command_id",
        "st0601.command.unknown_acknowledgement",
        "st0601.command.command_after_acknowledgement",
    }
    assert issues["st0601.command.command_changed"].tags == (115,)
    assert issues["st0601.command.unknown_acknowledgement"].tags == (116,)
    assert issues["st0601.command.command_after_acknowledgement"].tags == (115,)
    assert all(
        issue.requirement == "MISB ST 0601.19 §8.115-8.116"
        for issue in issues.values()
    )


def test_verifier_accepts_valid_distributed_st0601_state() -> None:
    start = 1_700_000_000_000_000
    report = _verify(
        _transport_sequence(
            _stateful_uas(
                timestamp=start,
                values={
                    121: ActiveWavelengthList((21,)),
                    128: WavelengthsList(
                        (WavelengthRecord(21, 1_000.0, 2_000.0, "IR"),)
                    ),
                    138: PayloadList(1, (PayloadRecord(0, 0, "EO"),)),
                    139: ActivePayloads(frozenset({0})),
                    140: WeaponsStores(
                        (
                            WeaponStore(
                                1,
                                1,
                                1,
                                1,
                                WeaponStatus(3),
                                "Hellfire",
                            ),
                        )
                    ),
                    141: WaypointList((WaypointRecord(1, 0),)),
                },
            )
        )
    )

    assert report.ok
    passes = {finding.code for finding in report.passes}
    assert {
        "st0601.wavelength.lifecycle",
        "st0601.payload.lifecycle",
        "st0601.weapons.lifecycle",
        "st0601.waypoint.lifecycle",
    } <= passes


def test_verifier_reports_distributed_st0601_state_violations() -> None:
    start = 1_700_000_000_000_000
    report = _verify(
        _transport_sequence(
            _stateful_uas(
                timestamp=start,
                values={
                    121: ActiveWavelengthList((7,)),
                    128: WavelengthsList(
                        (WavelengthRecord(21, 1_000.0, 2_000.0, "DUPLICATE"),)
                    ),
                    138: PayloadList(2, (PayloadRecord(0, 0, "EO"),)),
                    139: ActivePayloads(frozenset({1})),
                    141: WaypointList((WaypointRecord(1, -5),)),
                },
            ),
            _stateful_uas(
                timestamp=start + 1_000_000,
                values={
                    128: WavelengthsList(
                        (WavelengthRecord(22, 2_000.0, 3_000.0, "DUPLICATE"),)
                    ),
                    138: PayloadList(1, (PayloadRecord(0, 0, "EO"),)),
                    141: WaypointList((WaypointRecord(2, -2),)),
                },
            ),
        )
    )

    issues = {
        finding.code: finding
        for finding in report.errors
        if finding.code.startswith(
            ("st0601.wavelength.", "st0601.payload.", "st0601.waypoint.")
        )
    }
    assert set(issues) == {
        "st0601.wavelength.reserved_active_id",
        "st0601.wavelength.duplicate_custom_name",
        "st0601.payload.undefined_active_payload",
        "st0601.payload.payload_count_changed",
        "st0601.waypoint.historical_order_not_decreasing",
    }
    assert issues["st0601.wavelength.reserved_active_id"].tags == (121,)
    assert issues["st0601.wavelength.duplicate_custom_name"].tags == (128,)
    assert issues["st0601.payload.undefined_active_payload"].tags == (138, 139)
    assert issues["st0601.payload.payload_count_changed"].tags == (138,)
    assert issues["st0601.waypoint.historical_order_not_decreasing"].tags == (141,)


def test_verifier_reports_missing_pts_on_first_video_and_audio_access_units() -> None:
    video = _verify(_transport(video_pts=None))
    video_issue = next(
        finding
        for finding in video.errors
        if finding.code == "st1402.pts.first_access_unit"
    )
    assert video_issue.requirement == "ITU-T H.222.0 (10/2014) §2.7.5"
    assert video_issue.pid == 0x101
    assert "first video access unit" in video_issue.message

    audio = _verify(_transport(audio=True, audio_pts=None))
    audio_issue = next(
        finding
        for finding in audio.errors
        if finding.code == "st1402.pts.first_access_unit" and finding.pid == 0x103
    )
    assert audio_issue.requirement == "ITU-T H.222.0 (10/2014) §2.7.5"
    assert "first audio access unit" in audio_issue.message

    misaligned = _verify(_transport(video_payload=b"not-an-access-unit"))
    alignment_issue = next(
        finding
        for finding in misaligned.errors
        if finding.code == "st1402.pts.pts_without_access_unit"
    )
    assert alignment_issue.requirement == "ITU-T H.222.0 (10/2014) §2.7.5"
    assert alignment_issue.pid == 0x101


def test_verifier_reports_pcr_gap_audio_policy_and_formats_stream_details() -> None:
    gap = _verify(_transport(second_pcr_base=9_001))
    assert any(finding.code == "st1402.pcr.interval" for finding in gap.errors)

    pts_gap = _verify(_transport(second_video=True, second_video_pts=63_001))
    pts_issue = next(
        finding for finding in pts_gap.errors if finding.code == "st1402.pts.interval"
    )
    assert pts_issue.requirement == "ST 1402.2 §7.3"
    assert pts_issue.pid == 0x101

    valid_pts = _verify(_transport(second_video=True, second_video_pts=63_000))
    assert any(finding.code == "st1402.pts" for finding in valid_pts.passes)

    audio = _verify(_transport(audio=True, repeat_tables=True))
    assert audio.ok
    audio_stream = next(stream for stream in audio.streams if stream.kind == "audio")
    assert audio_stream.codec == "mpeg-1-layer-ii"
    assert audio_stream.audio is not None
    assert audio_stream.audio.frames == 1
    assert audio_stream.audio.samples == 1_152
    assert audio_stream.audio.duration_seconds == pytest.approx(0.024)
    assert audio_stream.audio.sample_rates == (48_000,)
    assert audio_stream.audio.channel_counts == (2,)
    assert audio_stream.to_dict()["audio"]["frames"] == 1
    text = audio.format_text()
    assert "PID 0x0103 audio" in text
    assert "mpeg-1-layer-ii" in text
    assert "frames=1" in text

    verifier = FMVVerifier(
        require_security=False,
        require_miis=False,
        require_audio=True,
    )
    verifier.feed(_transport())
    required = verifier.finish()
    assert any(finding.code == "st1001.st1001_audio_required" for finding in required.errors)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"not-an-audio-frame", "sync"),
        (bytes.fromhex("FFFC8444") + bytes(10), "incomplete"),
    ],
)
def test_verifier_reports_malformed_or_truncated_audio_frames(
    payload: bytes, message: str
) -> None:
    report = _verify(_transport(audio_payload=payload))

    issue = next(
        finding for finding in report.errors if finding.code == "st1001.audio.frame"
    )
    assert message in issue.message
    assert issue.requirement == "MISB ST 1001.1 / H.222.0 audio access units"
    assert issue.pid == 0x103


def test_verifier_reports_declared_audio_without_pes_payload() -> None:
    report = _verify(_transport(audio=True, include_audio_pes=False))

    issue = next(
        finding for finding in report.errors if finding.code == "st1001.audio.empty"
    )
    assert issue.pid == 0x103
    assert "no complete" in issue.message


def test_verifier_reports_duplicate_scrambled_and_transport_error_packets() -> None:
    transport = _transport()
    duplicate = transport[:188] + transport[:188] + transport[188:]
    duplicate_report = _verify(duplicate)
    warning = next(
        finding for finding in duplicate_report.warnings if finding.code == "transport.duplicate"
    )
    assert warning.pid == 0
    assert warning.first_offset == 188

    scrambled_null = bytes.fromhex("471FFF90") + bytes(184)
    scrambled = _verify(transport + scrambled_null)
    assert any(finding.code == "transport.scrambled" for finding in scrambled.warnings)

    damaged = bytearray(transport)
    video_offset = next(
        offset
        for offset in range(0, len(damaged), 188)
        if ((damaged[offset + 1] & 0x1F) << 8) | damaged[offset + 2] == 0x101
        and damaged[offset + 1] & 0x40
    )
    damaged[video_offset + 1] |= 0x80
    error_report = _verify(bytes(damaged))
    assert any(finding.code == "transport.decode" for finding in error_report.errors)


def test_verifier_bounds_findings_and_validates_incremental_api() -> None:
    with pytest.raises(TypeError, match="booleans"):
        FMVVerifier(require_audio=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="booleans"):
        FMVVerifier(validate_mismms=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_findings"):
        FMVVerifier(max_findings=0)
    with pytest.raises(ValueError, match="max_st0601_tags_per_stream"):
        FMVVerifier(max_st0601_tags_per_stream=0)
    with pytest.raises(TypeError, match="security_context"):
        FMVVerifier(security_context=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ontology_resolver"):
        FMVVerifier(ontology_resolver=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="st0601_context_provider"):
        FMVVerifier(st0601_context_provider=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="security_context"):
        FMVVerifier(
            require_security=False,
            security_context=MISMMSecurityContext(sci_shi=True),
        )
    with pytest.raises(ValueError, match="validate_mismms"):
        FMVVerifier(
            validate_mismms=False,
            security_context=MISMMSecurityContext(
                expected_classification=SecurityClassification.SECRET
            ),
        )

    verifier = FMVVerifier(max_findings=1)
    with pytest.raises(TypeError, match="bytes-like"):
        verifier.feed("not bytes")  # type: ignore[arg-type]
    verifier.feed(b"not-a-transport")
    report = verifier.finish()
    assert not report.ok
    assert any(finding.code == "report.truncated" for finding in report.findings)
    with pytest.raises(RuntimeError, match="already finished"):
        verifier.feed(b"")
    with pytest.raises(RuntimeError, match="already finished"):
        verifier.finish()


def test_verifier_bounds_st0601_inventory() -> None:
    verifier = FMVVerifier(
        require_security=False,
        require_miis=False,
        max_st0601_tags_per_stream=1,
    )
    verifier.feed(_transport())
    report = verifier.finish()

    assert len(report.st0601_streams[0].tags) == 1
    assert report.st0601_streams[0].untracked_item_occurrences > 0
    assert any(
        finding.code == "st0601.inventory.truncated" for finding in report.warnings
    )


def test_verifier_can_apply_structural_policy_without_mismms_profile() -> None:
    source = _transport(encode_uas_local_set({2: 1_700_000_000_000_000, 65: 19}))

    report = verify_fmv_stream(BytesIO(source), validate_mismms=False)

    assert report.ok
    assert not any(finding.code.startswith("st0902.") for finding in report.errors)
    profile = next(finding for finding in report.findings if finding.code == "st0902.profile")
    assert profile.status is VerificationStatus.NOT_APPLICABLE
    assert "disabled" in profile.message
    assert report.st0601_streams[0].mismms_coverage is None


def test_stream_wrapper_and_cli_validate_operational_inputs(tmp_path, capsys) -> None:
    with pytest.raises(ValueError, match="chunk_size"):
        verify_fmv_stream(BytesIO(b""), chunk_size=187)
    with pytest.raises(TypeError, match=r"read\(\)"):
        verify_fmv_stream(object())  # type: ignore[arg-type]

    failed_source = tmp_path / "broken.ts"
    failed_source.write_bytes(b"broken")
    assert main([str(failed_source), "--format", "text"]) == 1
    assert "Result: FAIL" in capsys.readouterr().out

    missing_source = tmp_path / "missing.ts"
    assert main([str(missing_source)]) == 2
    assert "cannot read or verify" in capsys.readouterr().err


def test_cli_uses_distinct_exit_status_for_invalid_input(tmp_path, capsys) -> None:
    source = tmp_path / "broken.ts"
    source.write_bytes(b"broken")

    assert main([str(source), "--format", "json"]) == 1
    assert json.loads(capsys.readouterr().out)["result"] == "fail"
    assert main([str(tmp_path / "missing.ts")]) == 2
    assert "cannot read" in capsys.readouterr().err
