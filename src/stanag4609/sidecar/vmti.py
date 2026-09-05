"""Convert model-neutral object detections into embedded ST 0903 VMTI."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from stanag4609.sidecar.model import Detection, FrameEnvelope, InferenceContext
from stanag4609.st0601 import (
    FieldDecodingMode,
    ST0601ValidationContext,
    UASLocalSet,
    encode_uas_local_set,
    update_uas_local_set,
    utc_to_misp_timestamp,
)
from stanag4609.st0903 import (
    AlgorithmLocalSet,
    OntologyLocalSet,
    VMTIValidationContext,
    VObjectLocalSet,
    VTargetData,
    encode_vmti_local_set,
)
from stanag4609.transport.processor import TimedKLVPacket
from stanag4609.transport.psi import KLVCarriage

_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _pixel_number(*, row: int, column: int, frame_width: int) -> int:
    """Return the one-based ST 0903 row-major pixel number."""

    return column + ((row - 1) * frame_width)


def _confidence_percent(confidence: float) -> int:
    return int(
        (Decimal(str(confidence)) * Decimal(100)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    )


def _target(
    frame: FrameEnvelope,
    detection: Detection,
    ontology_by_label: Mapping[str, int],
) -> VTargetData:
    box = detection.bounding_box
    if box.right > frame.width or box.bottom > frame.height:
        raise ValueError(
            f"target {detection.target_id} bounding box exceeds the {frame.width}x"
            f"{frame.height} frame"
        )

    top_row = box.top + 1
    left_column = box.left + 1
    bottom_row = box.bottom
    right_column = box.right
    centroid_row = ((box.top + box.bottom - 1) // 2) + 1
    centroid_column = ((box.left + box.right - 1) // 2) + 1
    values: dict[int, object] = {
        1: _pixel_number(row=centroid_row, column=centroid_column, frame_width=frame.width),
        2: _pixel_number(row=top_row, column=left_column, frame_width=frame.width),
        3: _pixel_number(row=bottom_row, column=right_column, frame_width=frame.width),
        5: _confidence_percent(detection.confidence),
        19: centroid_row,
        20: centroid_column,
        23: detection.status,
    }
    if detection.algorithm_id is not None:
        values[22] = detection.algorithm_id
    if detection.location is not None:
        values[17] = detection.location
    if detection.label is not None:
        try:
            ontology_id = ontology_by_label[detection.label]
        except KeyError as error:
            raise ValueError(
                f"target {detection.target_id} label {detection.label!r} has no ontology mapping"
            ) from error
        values[107] = (
            VObjectLocalSet(
                ontology_id=ontology_id,
                confidence=detection.confidence * 100,
                confidence_length=2,
            ),
        )
    return VTargetData(detection.target_id, values)


def encode_embedded_vmti(
    frame: FrameEnvelope,
    detections: Iterable[Detection],
    *,
    system_name: str | None = None,
    source_sensor: str | None = None,
    version: int = 6,
    algorithms: Iterable[AlgorithmLocalSet] = (),
    ontologies: Iterable[OntologyLocalSet] = (),
    ontology_by_label: Mapping[str, int] | None = None,
    leap_seconds: int | None = None,
    correction_offset: int = 0,
) -> bytes:
    """Encode common AI detections as an ST 0601 Item 74 VMTI value.

    Input boxes use the zero-based half-open convention common to inference
    libraries. Output pixel numbers and row/column pairs use ST 0903's one-based
    convention. ``ontology_by_label`` resolves convenient model labels to the
    packet's standards-native Ontology Series; each label becomes a VObject.
    ``FrameEnvelope.timestamp_microseconds`` is UTC/POSIX time. When present,
    ``leap_seconds`` is required to convert it to the MISP time scale used by
    ST 0903 and the enclosing ST 0601 Item 2.
    """

    if not isinstance(frame, FrameEnvelope):
        raise TypeError("frame must be FrameEnvelope")
    detection_items = tuple(detections)
    if any(not isinstance(item, Detection) for item in detection_items):
        raise TypeError("detections must contain only Detection values")
    if len({item.target_id for item in detection_items}) != len(detection_items):
        raise ValueError("detection target IDs must be unique within a frame")
    if ontology_by_label is None:
        ontology_by_label = {}
    if not isinstance(ontology_by_label, Mapping):
        raise TypeError("ontology_by_label must be a mapping")
    algorithm_items = tuple(algorithms)
    ontology_items = tuple(ontologies)

    values: dict[int, object] = {
        4: version,
        5: len(detection_items),
        8: frame.width,
        9: frame.height,
    }
    if frame.timestamp_microseconds is not None:
        if leap_seconds is None:
            raise ValueError(
                "leap_seconds is required to convert the frame UTC timestamp to MISP"
            )
        try:
            utc_timestamp = _UNIX_EPOCH + timedelta(
                microseconds=frame.timestamp_microseconds
            )
        except OverflowError as error:
            raise ValueError("frame UTC timestamp is outside the datetime range") from error
        values[2] = utc_to_misp_timestamp(
            utc_timestamp,
            leap_seconds=leap_seconds,
            correction_offset=correction_offset,
        )
    if system_name is not None:
        values[3] = system_name
    if source_sensor is not None:
        values[10] = source_sensor
    return encode_vmti_local_set(
        values,
        targets=tuple(
            _target(frame, detection, ontology_by_label) for detection in detection_items
        ),
        algorithms=algorithm_items,
        ontologies=ontology_items,
    )


class VMTIMetadataEmitter:
    """Turn one named inference result into a synchronous ST 0601/VMTI packet.

    When a correlated ST 0601 parent is available, unrelated fields and unknown
    wire items are preserved. Otherwise ``metadata_pid`` selects a predeclared
    synchronous KLVA stream and a minimal new ST 0601 packet is created.
    """

    __slots__ = (
        "algorithms",
        "leap_seconds",
        "metadata_pid",
        "metadata_service_id",
        "ontologies",
        "ontology_by_label",
        "output_stage",
        "random_access",
        "source_sensor",
        "system_name",
        "uas_version",
    )

    def __init__(
        self,
        output_stage: str,
        *,
        metadata_pid: int | None = None,
        metadata_service_id: int | None = None,
        leap_seconds: int | None = None,
        uas_version: int = 19,
        system_name: str | None = None,
        source_sensor: str | None = None,
        algorithms: Iterable[AlgorithmLocalSet] = (),
        ontologies: Iterable[OntologyLocalSet] = (),
        ontology_by_label: Mapping[str, int] | None = None,
        random_access: bool = False,
    ) -> None:
        if not isinstance(output_stage, str) or not output_stage:
            raise ValueError("output_stage must be a non-empty string")
        if metadata_pid is not None and (
            isinstance(metadata_pid, bool)
            or not isinstance(metadata_pid, int)
            or not 0 <= metadata_pid <= 0x1FFF
        ):
            raise ValueError("metadata_pid must be an integer from 0 to 8191 or None")
        if metadata_service_id is not None and (
            isinstance(metadata_service_id, bool)
            or not isinstance(metadata_service_id, int)
            or not 0 <= metadata_service_id <= 0xFF
        ):
            raise ValueError(
                "metadata_service_id must be an integer from 0 to 255 or None"
            )
        if leap_seconds is not None and (
            isinstance(leap_seconds, bool)
            or not isinstance(leap_seconds, int)
            or not -(2**31) <= leap_seconds <= 2**31 - 1
        ):
            raise ValueError("leap_seconds must be a signed 32-bit integer or None")
        if (
            isinstance(uas_version, bool)
            or not isinstance(uas_version, int)
            or not 0 <= uas_version <= 0xFF
        ):
            raise ValueError("uas_version must be an integer from 0 to 255")
        if not isinstance(random_access, bool):
            raise TypeError("random_access must be bool")
        if ontology_by_label is not None and not isinstance(ontology_by_label, Mapping):
            raise TypeError("ontology_by_label must be a mapping")
        self.output_stage = output_stage
        self.metadata_pid = metadata_pid
        self.metadata_service_id = metadata_service_id
        self.leap_seconds = leap_seconds
        self.uas_version = uas_version
        self.system_name = system_name
        self.source_sensor = source_sensor
        self.algorithms = tuple(algorithms)
        self.ontologies = tuple(ontologies)
        self.ontology_by_label = dict(ontology_by_label or {})
        self.random_access = random_access

    def __call__(self, context: InferenceContext) -> TimedKLVPacket:
        if not isinstance(context, InferenceContext):
            raise TypeError("context must be InferenceContext")
        output = context.result(self.output_stage)
        if output is None:
            raise ValueError(f"inference stage {self.output_stage!r} has no result")
        frame = context.frame
        if frame.timestamp_microseconds is None:
            raise ValueError("VMTI emission requires a frame UTC timestamp")
        try:
            utc_timestamp = _UNIX_EPOCH + timedelta(
                microseconds=frame.timestamp_microseconds
            )
        except OverflowError as error:
            raise ValueError("frame UTC timestamp is outside the datetime range") from error
        parents = tuple(
            packet
            for packet in frame.metadata
            if isinstance(packet.decoded, UASLocalSet)
            and packet.carriage is KLVCarriage.SYNCHRONOUS
            and (self.metadata_pid is None or packet.pid == self.metadata_pid)
            and (
                self.metadata_service_id is None
                or packet.metadata_service_id == self.metadata_service_id
            )
        )
        if len(parents) > 1:
            raise ValueError(
                "multiple correlated ST 0601 parents match; select metadata_pid and "
                "metadata_service_id explicitly"
            )
        parent = parents[0] if parents else None
        parent_leap_seconds = None if parent is None else parent.decoded.value(136)
        leap_seconds = (
            parent_leap_seconds if self.leap_seconds is None else self.leap_seconds
        )
        if not isinstance(leap_seconds, int) or isinstance(leap_seconds, bool):
            raise ValueError(
                "leap_seconds is required when no correlated ST 0601 Item 136 is available"
            )
        correction_offset = 0 if parent is None else parent.decoded.value(137, 0)
        if not isinstance(correction_offset, int) or isinstance(correction_offset, bool):
            raise ValueError("correlated ST 0601 Item 137 must be a typed integer")
        misp_microseconds = utc_to_misp_timestamp(
            utc_timestamp,
            leap_seconds=leap_seconds,
            correction_offset=correction_offset,
        )
        misp_timestamp = _UNIX_EPOCH + timedelta(microseconds=misp_microseconds)
        vmti = encode_embedded_vmti(
            frame,
            output.detections,
            system_name=self.system_name,
            source_sensor=self.source_sensor,
            algorithms=self.algorithms,
            ontologies=self.ontologies,
            ontology_by_label=self.ontology_by_label,
            leap_seconds=leap_seconds,
            correction_offset=correction_offset,
        )
        validation = ST0601ValidationContext(
            metadata_birth_timestamp=misp_timestamp,
            vmti_context=VMTIValidationContext(
                vmti_frame_timestamp=misp_timestamp,
                frame_width=frame.width,
                frame_height=frame.height,
            ),
        )
        if parents:
            assert parent is not None
            updates: dict[int, object] = {2: misp_microseconds, 74: vmti}
            if self.leap_seconds is not None and parent.decoded.local_set.getall(136):
                updates[136] = leap_seconds
            packet_bytes = update_uas_local_set(
                parent.decoded,
                updates,
                field_decoding=FieldDecodingMode.PRESERVE,
                context=validation,
            )
            pid = parent.pid
            service_id = parent.metadata_service_id
        else:
            if self.metadata_pid is None:
                raise ValueError(
                    "metadata_pid is required when no correlated ST 0601 parent is available"
                )
            packet_bytes = encode_uas_local_set(
                {
                    2: misp_microseconds,
                    65: self.uas_version,
                    74: vmti,
                    136: leap_seconds,
                },
                context=validation,
            )
            pid = self.metadata_pid
            service_id = 0 if self.metadata_service_id is None else self.metadata_service_id
        assert service_id is not None
        return TimedKLVPacket.from_bytes(
            packet_bytes,
            program_number=frame.program_number,
            pid=pid,
            carriage=KLVCarriage.SYNCHRONOUS,
            pts=frame.pts,
            metadata_service_id=service_id,
            random_access=self.random_access,
        )
