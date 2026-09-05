"""Build browser-friendly media-relative timelines from STANAG 4609 metadata."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from enum import Enum, IntEnum, IntFlag
from fractions import Fraction
from pathlib import Path
from typing import Any, cast

from stanag4609.geojson import snapshot_geojson_features
from stanag4609.st0601 import (
    DecodedField,
    FieldDecodingMode,
    LaserPRFCode,
    UASLocalSet,
    WeaponFired,
    WeaponLoad,
    effective_uas_fields,
    misp_timestamp_to_utc,
    resolve_target_elevation,
)
from stanag4609.st0601_state import ReportOnChangeSnapshot, ReportOnChangeState
from stanag4609.st0903 import (
    DetectionStatus,
    VMaskLocalSet,
    VMTILocalSet,
    VObjectLocalSet,
    resolve_vtarget_location,
)
from stanag4609.transport.demux import PESStreamEvent, StreamKind, TransportDemuxer
from stanag4609.transport.metadata_stream import KLVMetadataEvent, MetadataStreamDecoder
from stanag4609.transport.timing import PTS_CLOCK_RATE, unwrap_pts

_PANEL_TAGS = frozenset(
    {
        2,
        3,
        4,
        5,
        6,
        7,
        10,
        11,
        12,
        13,
        14,
        15,
        16,
        17,
        18,
        19,
        20,
        21,
        22,
        23,
        24,
        25,
        26,
        27,
        28,
        29,
        30,
        31,
        32,
        33,
        34,
        47,
        40,
        41,
        42,
        38,
        50,
        52,
        60,
        61,
        62,
        63,
        65,
        69,
        74,
        75,
        76,
        77,
        78,
        82,
        83,
        84,
        85,
        86,
        87,
        88,
        89,
        90,
        91,
        92,
        93,
        96,
        103,
        104,
        105,
        124,
        125,
        126,
        136,
        137,
    }
)


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Fraction):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(cast(Any, value)))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return str(value)


def _humanize_identifier(value: str) -> str:
    return value.replace("_", " ").title()


def _semantic_entry(value: Any) -> dict[str, Any]:
    """Project a decoded value without discarding its exact machine representation."""

    entry: dict[str, Any] = {"value": _json_value(value)}
    if isinstance(value, IntFlag):
        names = [
            member.name.lower()
            for member in type(value)
            if member.name is not None and member in value
        ]
        label = " + ".join(_humanize_identifier(name) for name in names) or "No Flags"
        entry["display"] = f"{label} (0x{int(value):02X})"
        entry["flags"] = names
    elif isinstance(value, IntEnum):
        entry["display"] = f"{_humanize_identifier(value.name)} ({int(value)})"
    elif isinstance(value, WeaponLoad):
        entry["display"] = (
            f"Station {value.station_number}, substation {value.substation_number}, "
            f"type {value.weapon_type}, variant {value.weapon_variant} "
            f"(0x{int(value):04X})"
        )
        entry["components"] = {
            "station_number": value.station_number,
            "substation_number": value.substation_number,
            "weapon_type": value.weapon_type,
            "weapon_variant": value.weapon_variant,
        }
    elif isinstance(value, WeaponFired):
        entry["display"] = (
            f"Station {value.station_number}, substation {value.substation_number} "
            f"(0x{int(value):02X})"
        )
        entry["components"] = {
            "station_number": value.station_number,
            "substation_number": value.substation_number,
        }
    elif isinstance(value, LaserPRFCode):
        entry["display"] = str(int(value))
    return entry


@dataclass(frozen=True, slots=True)
class MetadataSample:
    time_seconds: float
    pts: int | None
    program_number: int
    pid: int
    fields: Mapping[str, Any]
    geospatial: tuple[Mapping[str, Any], ...] = ()
    detections: tuple[OverlayDetection, ...] = ()
    issues: tuple[str, ...] = ()

    def to_json(self, *, indent: int | None = None) -> str:
        """Serialize this sample with the same lossless projection as its timeline."""
        separators = None if indent else (",", ":")
        return json.dumps(_json_value(self), indent=indent, separators=separators)


@dataclass(frozen=True, slots=True)
class OverlayMaskRun:
    """One compact 1-based row-major VMTI mask run for browser rendering."""

    start_pixel: int
    run_length: int


@dataclass(frozen=True, slots=True)
class OverlayDetection:
    """One normalized VMTI target suitable for a video overlay."""

    target_id: int
    status: str | None
    confidence: int | None
    label: str | None
    algorithm_id: int | None
    algorithm_name: str | None
    left: float | None
    top: float | None
    right: float | None
    bottom: float | None
    center_x: float | None
    center_y: float | None
    contour: tuple[tuple[float, float], ...] = ()
    mask_runs: tuple[OverlayMaskRun, ...] = ()
    mask_width: int | None = None
    mask_height: int | None = None
    latitude: float | None = None
    longitude: float | None = None
    hae: float | None = None
    location_source: str | None = None
    ground_polygon: tuple[tuple[float, float], ...] = ()
    ground_polygon_source: str | None = None


@dataclass(frozen=True, slots=True)
class MetadataTimeline:
    video_start_pts: int | None
    samples: tuple[MetadataSample, ...]
    media_start_pts: int | None = None

    def to_json(self, *, indent: int | None = None) -> str:
        separators = None if indent else (",", ":")
        return json.dumps(_json_value(self), indent=indent, separators=separators)


def _chunks(path: Path, chunk_size: int) -> Iterator[bytes]:
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            yield chunk


def _fields(
    decoded_fields: Iterable[DecodedField],
    *,
    frame_center_latitude: object | None = None,
    frame_center_longitude: object | None = None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    effective = effective_uas_fields(decoded_fields)
    by_tag = {field.definition.tag: field for field in effective}
    target_elevation = resolve_target_elevation(effective)
    for field in effective:
        if field.definition.tag not in _PANEL_TAGS:
            continue
        if field.definition.tag == 74 and isinstance(field.value, VMTILocalSet):
            target_locations = []
            for target in field.value.targets:
                location = resolve_vtarget_location(
                    target,
                    frame_center_latitude=frame_center_latitude,
                    frame_center_longitude=frame_center_longitude,
                )
                target_locations.append(
                    {
                        "target_id": target.target_id,
                        "latitude": None if location is None else location.latitude,
                        "longitude": None if location is None else location.longitude,
                        "hae": None if location is None else location.hae,
                        "source": None if location is None else location.source,
                    }
                )
            value: Any = {
                "version": field.value.value(4),
                "targets_reported": len(field.value.targets),
                "system_name": field.value.value(3),
                "source_sensor": field.value.value(10),
                "targets": target_locations,
            }
        else:
            value = field.value
        entry = _semantic_entry(value)
        if field.definition.tag == 2 and len(field.raw) == 8:
            entry["time_scale"] = "MISP"
            entry["microseconds_since_epoch"] = int.from_bytes(field.raw, "big")
        if field.definition.units is not None:
            entry["units"] = field.definition.units
        if field.definition.tag == 42 and target_elevation is not None:
            entry["vertical_datum"] = (
                "unknown"
                if target_elevation.datum is None
                else target_elevation.datum.value
            )
            entry["datum_basis_tags"] = list(target_elevation.basis_tags)
        fields[field.definition.name] = entry
    timestamp = by_tag.get(2)
    leap_seconds = by_tag.get(136)
    correction_offset = by_tag.get(137)
    if (
        timestamp is not None
        and leap_seconds is not None
        and isinstance(leap_seconds.value, int)
        and not isinstance(leap_seconds.value, bool)
    ):
        correction = correction_offset.value if correction_offset is not None else 0
        if isinstance(correction, int) and not isinstance(correction, bool):
            utc = misp_timestamp_to_utc(
                timestamp.value,
                leap_seconds=leap_seconds.value,
                correction_offset=correction,
            )
            fields["UTC Timestamp"] = {
                "value": utc.isoformat(),
                "time_scale": "UTC",
                "derived_from_tags": [2, 136, *([137] if correction_offset else [])],
            }
    return fields


def _pixel(number: int, *, width: int, height: int) -> tuple[int, int] | None:
    if not 1 <= number <= width * height:
        return None
    index = number - 1
    return index % width, index // width


def _coordinate(
    value: object,
    *,
    minimum: float,
    maximum: float,
) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Fraction)):
        return None
    result = float(value)
    return result if math.isfinite(result) and minimum <= result <= maximum else None


def _frame_corners(
    snapshot: ReportOnChangeSnapshot,
) -> tuple[tuple[float, float], ...] | None:
    """Return top-left through bottom-left WGS-84 frame corners."""

    full: list[tuple[float, float]] = []
    for latitude_tag, longitude_tag in ((82, 83), (84, 85), (86, 87), (88, 89)):
        latitude = _coordinate(snapshot.value(latitude_tag), minimum=-90, maximum=90)
        longitude = _coordinate(snapshot.value(longitude_tag), minimum=-180, maximum=180)
        if latitude is None or longitude is None:
            full = []
            break
        full.append((longitude, latitude))
    if len(full) == 4:
        return tuple(full)

    center_latitude = _coordinate(snapshot.value(23), minimum=-90, maximum=90)
    center_longitude = _coordinate(snapshot.value(24), minimum=-180, maximum=180)
    if center_latitude is None or center_longitude is None:
        return None
    offsets: list[tuple[float, float]] = []
    for latitude_tag, longitude_tag in ((26, 27), (28, 29), (30, 31), (32, 33)):
        latitude_offset = _coordinate(snapshot.value(latitude_tag), minimum=-0.075, maximum=0.075)
        longitude_offset = _coordinate(snapshot.value(longitude_tag), minimum=-0.075, maximum=0.075)
        if latitude_offset is None or longitude_offset is None:
            return None
        latitude = center_latitude + latitude_offset
        if not -90 <= latitude <= 90:
            return None
        longitude = (center_longitude + longitude_offset + 180) % 360 - 180
        offsets.append((longitude, latitude))
    return tuple(offsets)


def _validate_frame_corners(
    corners: tuple[tuple[float, float], ...] | None,
) -> tuple[tuple[float, float], ...] | None:
    if corners is None:
        return None
    if not isinstance(corners, tuple) or len(corners) != 4:
        raise ValueError("frame_corners must contain four longitude/latitude pairs")
    result: list[tuple[float, float]] = []
    for point in corners:
        if not isinstance(point, tuple) or len(point) != 2:
            raise ValueError("frame_corners must contain four longitude/latitude pairs")
        longitude = _coordinate(point[0], minimum=-180, maximum=180)
        latitude = _coordinate(point[1], minimum=-90, maximum=90)
        if longitude is None or latitude is None:
            raise ValueError("frame_corners must contain four longitude/latitude pairs")
        result.append((longitude, latitude))
    return tuple(result)


def _project_frame_point(
    corners: tuple[tuple[float, float], ...],
    x: float,
    y: float,
) -> tuple[float, float]:
    reference = corners[0][0]
    longitudes: list[float] = []
    for longitude, _latitude in corners:
        while longitude - reference > 180:
            longitude -= 360
        while longitude - reference < -180:
            longitude += 360
        longitudes.append(longitude)
    top_longitude = longitudes[0] * (1 - x) + longitudes[1] * x
    bottom_longitude = longitudes[3] * (1 - x) + longitudes[2] * x
    longitude = top_longitude * (1 - y) + bottom_longitude * y
    top_latitude = corners[0][1] * (1 - x) + corners[1][1] * x
    bottom_latitude = corners[3][1] * (1 - x) + corners[2][1] * x
    latitude = top_latitude * (1 - y) + bottom_latitude * y
    return ((longitude + 180) % 360 - 180, latitude)


def extract_overlay_detections(
    vmti: VMTILocalSet,
    *,
    frame_center_latitude: object | None = None,
    frame_center_longitude: object | None = None,
    frame_corners: tuple[tuple[float, float], ...] | None = None,
) -> tuple[OverlayDetection, ...]:
    """Convert typed ST 0903 targets to normalized browser-overlay geometry."""

    if not isinstance(vmti, VMTILocalSet):
        raise TypeError("vmti must be VMTILocalSet")
    validated_corners = _validate_frame_corners(frame_corners)
    width = vmti.value(8)
    height = vmti.value(9)
    if not isinstance(width, int) or not isinstance(height, int) or width < 1 or height < 1:
        return ()
    ontology_labels = {
        ontology.ontology_id: ontology.label or ontology.entity_iri
        for ontology in vmti.ontologies
    }
    algorithm_names = {
        algorithm.algorithm_id: algorithm.name for algorithm in vmti.algorithms
    }
    detections: list[OverlayDetection] = []
    for target in vmti.targets:
        left = top = right = bottom = None
        top_left = target.value(2)
        bottom_right = target.value(3)
        if isinstance(top_left, int) and isinstance(bottom_right, int):
            first = _pixel(top_left, width=width, height=height)
            last = _pixel(bottom_right, width=width, height=height)
            if (
                first is not None
                and last is not None
                and first[0] <= last[0]
                and first[1] <= last[1]
            ):
                left = first[0] / width
                top = first[1] / height
                right = (last[0] + 1) / width
                bottom = (last[1] + 1) / height

        contour: tuple[tuple[float, float], ...] = ()
        mask_runs: tuple[OverlayMaskRun, ...] = ()
        mask_width = mask_height = None
        mask = target.value(101)
        if isinstance(mask, VMaskLocalSet):
            points = tuple(
                _pixel(pixel, width=width, height=height)
                for pixel in mask.pixel_contour
            )
            if points and all(point is not None for point in points):
                contour = tuple(
                    ((point[0] + 0.5) / width, (point[1] + 0.5) / height)
                    for point in points
                    if point is not None
                )
            mask_runs = tuple(
                OverlayMaskRun(run.start_pixel, run.run_length)
                for run in mask.bit_mask_series
            )
            mask_width = width
            mask_height = height

        center_x = center_y = None
        row = target.value(19)
        column = target.value(20)
        if (
            isinstance(row, int)
            and isinstance(column, int)
            and 1 <= row <= height
            and 1 <= column <= width
        ):
            center_x = (column - 0.5) / width
            center_y = (row - 0.5) / height
        else:
            centroid = target.value(1)
            point = (
                _pixel(centroid, width=width, height=height)
                if isinstance(centroid, int)
                else None
            )
            if point is not None:
                center_x = (point[0] + 0.5) / width
                center_y = (point[1] + 0.5) / height
            elif None not in {left, top, right, bottom}:
                assert left is not None and top is not None
                assert right is not None and bottom is not None
                center_x = (left + right) / 2
                center_y = (top + bottom) / 2
            elif contour:
                center_x = (
                    min(point[0] for point in contour)
                    + max(point[0] for point in contour)
                ) / 2
                center_y = (
                    min(point[1] for point in contour)
                    + max(point[1] for point in contour)
                ) / 2

        label: str | None = None
        objects = target.value(107, ())
        if isinstance(objects, tuple):
            first_object = next(
                (item for item in objects if isinstance(item, VObjectLocalSet)),
                None,
            )
            if first_object is not None:
                label = ontology_labels.get(first_object.ontology_id)
        status_value = target.value(23)
        status = status_value.name.lower() if isinstance(status_value, DetectionStatus) else None
        confidence_value = target.value(5)
        confidence = confidence_value if isinstance(confidence_value, int) else None
        algorithm_value = target.value(22)
        algorithm_id = algorithm_value if isinstance(algorithm_value, int) else None
        target_location = resolve_vtarget_location(
            target,
            frame_center_latitude=frame_center_latitude,
            frame_center_longitude=frame_center_longitude,
        )
        ground_polygon: tuple[tuple[float, float], ...] = ()
        if validated_corners is not None and None not in {left, top, right, bottom}:
            assert left is not None and top is not None
            assert right is not None and bottom is not None
            ground_polygon = tuple(
                _project_frame_point(validated_corners, x, y)
                for x, y in (
                    (left, top),
                    (right, top),
                    (right, bottom),
                    (left, bottom),
                    (left, top),
                )
            )
        detections.append(
            OverlayDetection(
                target.target_id,
                status,
                confidence,
                label,
                algorithm_id,
                algorithm_names.get(algorithm_id) if algorithm_id is not None else None,
                left,
                top,
                right,
                bottom,
                center_x,
                center_y,
                contour,
                mask_runs,
                mask_width,
                mask_height,
                None if target_location is None else target_location.latitude,
                None if target_location is None else target_location.longitude,
                None if target_location is None else target_location.hae,
                None if target_location is None else target_location.source,
                ground_polygon,
                "frame_footprint_bilinear" if ground_polygon else None,
            )
        )
    return tuple(detections)


def scan_transport_timeline(chunks: Iterable[bytes]) -> MetadataTimeline:
    """Scan MPEG-2 TS chunks into a media-relative ST 0601 metadata timeline."""
    demuxer = TransportDemuxer()
    decoder = MetadataStreamDecoder(
        field_decoding=FieldDecodingMode.PRESERVE,
        validate_sequence=False,
    )
    first_video_pts: int | None = None
    first_media_pts_by_stream: dict[tuple[int, int], int] = {}
    metadata: list[KLVMetadataEvent] = []

    def consume(events: Iterable[object]) -> None:
        nonlocal first_video_pts
        for event in events:
            if not isinstance(event, PESStreamEvent):
                continue
            if event.kind in {StreamKind.VIDEO, StreamKind.AUDIO} and event.pes.pts is not None:
                first_media_pts_by_stream.setdefault(
                    (event.program_number, event.pid), event.pes.pts
                )
            if (
                event.kind is StreamKind.VIDEO
                and event.pes.pts is not None
                and first_video_pts is None
            ):
                first_video_pts = event.pes.pts
            elif event.kind is StreamKind.KLV:
                metadata.extend(decoder.feed(event))

    for chunk in chunks:
        consume(demuxer.feed(chunk))
    consume(demuxer.finish())
    decoder.finish()

    media_reference = first_video_pts
    if media_reference is None and first_media_pts_by_stream:
        media_reference = next(iter(first_media_pts_by_stream.values()))
    media_start_pts = (
        min(
            unwrap_pts(pts, reference=media_reference)
            for pts in first_media_pts_by_stream.values()
        )
        if media_reference is not None
        else None
    )

    first_timestamp: datetime | None = None
    for event in metadata:
        if isinstance(event.decoded, UASLocalSet):
            timestamp = event.decoded.value(2)
            if isinstance(timestamp, datetime):
                first_timestamp = timestamp
                break

    samples: list[MetadataSample] = []
    receiver_states: dict[tuple[int, int], ReportOnChangeState] = {}
    for event in metadata:
        if not isinstance(event.decoded, UASLocalSet):
            continue
        if event.pts is not None and media_start_pts is not None:
            unwrapped = unwrap_pts(event.pts, reference=media_start_pts)
            seconds = (unwrapped - media_start_pts) / PTS_CLOCK_RATE
        else:
            timestamp = event.decoded.value(2)
            seconds = (
                (timestamp - first_timestamp).total_seconds()
                if isinstance(timestamp, datetime) and first_timestamp is not None
                else 0.0
            )
        receiver_state = receiver_states.setdefault(
            (event.program_number, event.pid), ReportOnChangeState()
        )
        snapshot = receiver_state.observe(event.decoded)
        vmti_value = snapshot.value(74)
        frame_corners = _frame_corners(snapshot)
        samples.append(
            MetadataSample(
                max(0.0, seconds),
                event.pts,
                event.program_number,
                event.pid,
                _fields(
                    snapshot.fields,
                    frame_center_latitude=snapshot.value(23),
                    frame_center_longitude=snapshot.value(24),
                ),
                snapshot_geojson_features(snapshot),
                (
                    extract_overlay_detections(
                        vmti_value,
                        frame_center_latitude=snapshot.value(23),
                        frame_center_longitude=snapshot.value(24),
                        frame_corners=frame_corners,
                    )
                    if isinstance(vmti_value, VMTILocalSet)
                    else ()
                ),
                tuple(issue.message for issue in snapshot.issues),
            )
        )
    samples.sort(key=lambda sample: sample.time_seconds)
    return MetadataTimeline(first_video_pts, tuple(samples), media_start_pts)


def scan_transport_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> MetadataTimeline:
    """Scan one MPEG-2 transport-stream file without loading it all into memory."""
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size < 188:
        raise ValueError("chunk_size must be an integer of at least 188 bytes")
    source = Path(path)
    return scan_transport_timeline(_chunks(source, chunk_size))
