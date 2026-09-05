"""Stream reconstructed ST 0601 geospatial metadata as GeoJSON sequences."""

from __future__ import annotations

import argparse
import json
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from stanag4609.st0601 import (
    DecodedField,
    FieldDecodingMode,
    SpecialValue,
    ST0601Semantic,
    UASLocalSet,
    resolve_preferred_uas_field,
    resolve_target_elevation,
)
from stanag4609.st0601_state import ReportOnChangeSnapshot, ReportOnChangeState
from stanag4609.st0903 import (
    DetectionStatus,
    VMTILocalSet,
    resolve_vtarget_location,
)
from stanag4609.transport.demux import PESStreamEvent, StreamKind, TransportDemuxer
from stanag4609.transport.metadata_stream import KLVMetadataEvent, MetadataStreamDecoder


@dataclass(frozen=True, slots=True)
class GeoJsonExportResult:
    """Summary of one completed line-delimited GeoJSON export."""

    destination: Path
    records_written: int
    features_written: int
    program_numbers: frozenset[int]
    metadata_pids: frozenset[int]


def _number(value: Any, *, minimum: float, maximum: float) -> float | None:
    if (
        value is None
        or isinstance(value, (bool, SpecialValue))
        or not isinstance(value, (int, float, Fraction))
    ):
        return None
    result = float(value)
    return result if minimum <= result <= maximum else None


def _longitude(value: float) -> float:
    """Wrap a longitude to the GeoJSON range."""

    return (value + 180.0) % 360.0 - 180.0


def _point_geometry(
    snapshot: ReportOnChangeSnapshot,
    *,
    latitude_tag: int,
    longitude_tag: int,
    altitude_field: DecodedField | None = None,
) -> dict[str, Any] | None:
    latitude = _number(snapshot.value(latitude_tag), minimum=-90, maximum=90)
    longitude = _number(snapshot.value(longitude_tag), minimum=-180, maximum=180)
    if latitude is None or longitude is None:
        return None
    coordinates = [longitude, latitude]
    if altitude_field is not None:
        altitude = _number(altitude_field.value, minimum=-900, maximum=40_000)
        if altitude is not None:
            coordinates.append(altitude)
    return {"type": "Point", "coordinates": coordinates}


def _full_corners(snapshot: ReportOnChangeSnapshot) -> list[list[float]] | None:
    points: list[list[float]] = []
    for latitude_tag, longitude_tag in ((82, 83), (84, 85), (86, 87), (88, 89)):
        latitude = _number(snapshot.value(latitude_tag), minimum=-90, maximum=90)
        longitude = _number(snapshot.value(longitude_tag), minimum=-180, maximum=180)
        if latitude is None or longitude is None:
            return None
        points.append([longitude, latitude])
    return points


def _offset_corners(snapshot: ReportOnChangeSnapshot) -> list[list[float]] | None:
    center_latitude = _number(snapshot.value(23), minimum=-90, maximum=90)
    center_longitude = _number(snapshot.value(24), minimum=-180, maximum=180)
    if center_latitude is None or center_longitude is None:
        return None
    points: list[list[float]] = []
    for latitude_tag, longitude_tag in ((26, 27), (28, 29), (30, 31), (32, 33)):
        latitude_offset = _number(snapshot.value(latitude_tag), minimum=-0.075, maximum=0.075)
        longitude_offset = _number(
            snapshot.value(longitude_tag), minimum=-0.075, maximum=0.075
        )
        if latitude_offset is None or longitude_offset is None:
            return None
        latitude = center_latitude + latitude_offset
        if not -90 <= latitude <= 90:
            return None
        points.append([center_longitude + longitude_offset, latitude])
    return points


def _clip_longitude(
    points: list[list[float]], *, boundary: float, keep_less: bool
) -> list[list[float]]:
    """Clip a polygon against one vertical longitude boundary."""

    clipped: list[list[float]] = []
    previous = points[-1]
    previous_inside = previous[0] <= boundary if keep_less else previous[0] >= boundary
    for current in points:
        current_inside = current[0] <= boundary if keep_less else current[0] >= boundary
        if current_inside != previous_inside:
            distance = current[0] - previous[0]
            latitude = previous[1] + (current[1] - previous[1]) * (
                (boundary - previous[0]) / distance
            )
            clipped.append([boundary, latitude])
        if current_inside:
            clipped.append(current)
        previous = current
        previous_inside = current_inside
    return clipped


def _ring(points: list[list[float]]) -> list[list[float]]:
    """Close a GeoJSON exterior ring and enforce counterclockwise winding."""

    area = sum(
        point[0] * points[(index + 1) % len(points)][1]
        - points[(index + 1) % len(points)][0] * point[1]
        for index, point in enumerate(points)
    )
    ordered = points if area > 0 else list(reversed(points))
    return [*ordered, ordered[0]]


def _polygon_geometry(points: list[list[float]]) -> dict[str, Any]:
    """Build a correctly wound polygon, splitting it at the antimeridian."""

    unwrapped = [[_longitude(points[0][0]), points[0][1]]]
    for longitude, latitude in points[1:]:
        longitude = _longitude(longitude)
        previous = unwrapped[-1][0]
        while longitude - previous > 180:
            longitude -= 360
        while longitude - previous < -180:
            longitude += 360
        unwrapped.append([longitude, latitude])

    minimum = min(point[0] for point in unwrapped)
    maximum = max(point[0] for point in unwrapped)
    if maximum > 180:
        west = _clip_longitude(unwrapped, boundary=180, keep_less=True)
        east = [
            [longitude - 360, latitude]
            for longitude, latitude in _clip_longitude(
                unwrapped, boundary=180, keep_less=False
            )
        ]
        return {"type": "MultiPolygon", "coordinates": [[_ring(west)], [_ring(east)]]}
    if minimum < -180:
        west = [
            [longitude + 360, latitude]
            for longitude, latitude in _clip_longitude(
                unwrapped, boundary=-180, keep_less=True
            )
        ]
        east = _clip_longitude(unwrapped, boundary=-180, keep_less=False)
        return {"type": "MultiPolygon", "coordinates": [[_ring(west)], [_ring(east)]]}
    return {"type": "Polygon", "coordinates": [_ring(unwrapped)]}


def _feature(
    role: str,
    geometry: dict[str, Any],
    *,
    corner_source: str | None = None,
    altitude_tag: int | None = None,
    vertical_datum: str | None = None,
) -> dict[str, Any]:
    properties: dict[str, Any] = {"role": role}
    if corner_source is not None:
        properties["corner_source"] = corner_source
    if altitude_tag is not None:
        properties["altitude_tag"] = altitude_tag
    if vertical_datum is not None:
        properties["vertical_datum"] = vertical_datum
    return {"type": "Feature", "geometry": geometry, "properties": properties}


def snapshot_geojson_features(
    snapshot: ReportOnChangeSnapshot,
) -> tuple[dict[str, Any], ...]:
    """Project one reconstructed ST 0601 snapshot into GeoJSON features."""

    if not isinstance(snapshot, ReportOnChangeSnapshot):
        raise TypeError("snapshot must be ReportOnChangeSnapshot")
    features: list[dict[str, Any]] = []
    sensor_height = resolve_preferred_uas_field(
        snapshot.fields, ST0601Semantic.SENSOR_HEIGHT
    )
    frame_height = resolve_preferred_uas_field(
        snapshot.fields, ST0601Semantic.FRAME_CENTER_HEIGHT
    )
    target_elevation = resolve_target_elevation(snapshot.fields)
    altitudes: dict[str, tuple[DecodedField | None, str | None]] = {
        "sensor": (
            None if sensor_height is None else sensor_height.field,
            None
            if sensor_height is None
            else ("hae" if sensor_height.tag in {75, 104} else "msl"),
        ),
        "frame_center": (
            None if frame_height is None else frame_height.field,
            None if frame_height is None else ("hae" if frame_height.tag == 78 else "msl"),
        ),
        "target": (
            None if target_elevation is None else target_elevation.field,
            (
                None
                if target_elevation is None
                else (
                    "unknown"
                    if target_elevation.datum is None
                    else target_elevation.datum.value
                )
            ),
        ),
    }
    for role, latitude_tag, longitude_tag in (
        ("sensor", 13, 14),
        ("frame_center", 23, 24),
        ("target", 40, 41),
    ):
        altitude_field, vertical_datum = altitudes[role]
        geometry = _point_geometry(
            snapshot,
            latitude_tag=latitude_tag,
            longitude_tag=longitude_tag,
            altitude_field=altitude_field,
        )
        if geometry is not None:
            features.append(
                _feature(
                    role,
                    geometry,
                    altitude_tag=(
                        None if altitude_field is None else altitude_field.definition.tag
                    ),
                    vertical_datum=vertical_datum,
                )
            )

    corners = _full_corners(snapshot)
    corner_source = "full"
    if corners is None:
        corners = _offset_corners(snapshot)
        corner_source = "offset"
    if corners is not None:
        features.append(
            _feature(
                "frame_footprint",
                _polygon_geometry(corners),
                corner_source=corner_source,
            )
        )

    vmti = snapshot.value(74)
    if isinstance(vmti, VMTILocalSet):
        frame_center_latitude = snapshot.value(23)
        frame_center_longitude = snapshot.value(24)
        for target in vmti.targets:
            location = resolve_vtarget_location(
                target,
                frame_center_latitude=frame_center_latitude,
                frame_center_longitude=frame_center_longitude,
            )
            if location is None:
                continue
            coordinates = [location.longitude, location.latitude]
            if location.hae is not None:
                coordinates.append(location.hae)
            status = target.value(23)
            properties: dict[str, Any] = {
                "role": "vmti_target",
                "target_id": target.target_id,
                "location_source": location.source,
            }
            if isinstance(status, DetectionStatus):
                properties["status"] = status.name.lower()
            confidence = target.value(5)
            if isinstance(confidence, int):
                properties["confidence"] = confidence
            if location.hae is not None:
                properties["vertical_datum"] = "hae"
            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": coordinates},
                    "properties": properties,
                }
            )
    return tuple(features)


def _feature_collection(
    snapshot: ReportOnChangeSnapshot,
    event: KLVMetadataEvent,
) -> dict[str, Any]:
    features = snapshot_geojson_features(snapshot)

    pts = event.pts
    properties = {
        "timestamp": snapshot.timestamp.isoformat(),
        "timestamp_time_scale": "MISP",
        "program_number": event.program_number,
        "metadata_pid": event.pid,
        "pts": pts,
        "pts_seconds": None if pts is None else pts / 90_000,
        "issues": [issue.message for issue in snapshot.issues],
    }
    if isinstance(snapshot.value(136), int) and not isinstance(snapshot.value(136), bool):
        properties["utc_timestamp"] = snapshot.utc_timestamp().isoformat()
    return {
        "type": "FeatureCollection",
        "properties": properties,
        "features": list(features),
    }


class _GeoJsonDecoder:
    __slots__ = ("_decoder", "_demuxer", "_states", "metadata_pids", "program_numbers")

    def __init__(self) -> None:
        self._demuxer = TransportDemuxer()
        self._decoder = MetadataStreamDecoder(
            field_decoding=FieldDecodingMode.PRESERVE,
            validate_sequence=False,
        )
        self._states: dict[tuple[int, int], ReportOnChangeState] = {}
        self.program_numbers: set[int] = set()
        self.metadata_pids: set[int] = set()

    def feed(self, data: bytes | bytearray | memoryview) -> list[dict[str, Any]]:
        return self._consume(self._demuxer.feed(data))

    def finish(self) -> list[dict[str, Any]]:
        collections = self._consume(self._demuxer.finish())
        self._decoder.finish()
        return collections

    def _consume(self, events: Iterable[object]) -> list[dict[str, Any]]:
        collections: list[dict[str, Any]] = []
        for event in events:
            if not isinstance(event, PESStreamEvent) or event.kind is not StreamKind.KLV:
                continue
            for metadata in self._decoder.feed(event):
                if not isinstance(metadata.decoded, UASLocalSet):
                    continue
                self.program_numbers.add(metadata.program_number)
                self.metadata_pids.add(metadata.pid)
                state = self._states.setdefault(
                    (metadata.program_number, metadata.pid),
                    ReportOnChangeState(),
                )
                collections.append(_feature_collection(state.observe(metadata.decoded), metadata))
        return collections


def iter_geojson_feature_collections(
    chunks: Iterable[bytes],
) -> Iterator[dict[str, Any]]:
    """Yield one reconstructed GeoJSON FeatureCollection per ST 0601 packet."""

    decoder = _GeoJsonDecoder()
    for chunk in chunks:
        yield from decoder.feed(chunk)
    yield from decoder.finish()


def export_geojson_sequence(
    source_transport: str | Path,
    destination: str | Path,
    *,
    chunk_size: int = 1024 * 1024,
    overwrite: bool = False,
) -> GeoJsonExportResult:
    """Atomically export ST 0601 geometry as line-delimited GeoJSON."""

    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size < 188:
        raise ValueError("chunk_size must be an integer of at least 188 bytes")
    source_path = Path(source_transport)
    destination_path = Path(destination)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if source_path.resolve() == destination_path.resolve():
        raise ValueError("source and destination must be different files")
    if destination_path.exists() and not overwrite:
        raise FileExistsError(destination_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    decoder = _GeoJsonDecoder()
    temporary_path: Path | None = None
    records_written = 0
    features_written = 0
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{destination_path.name}.",
            suffix=".tmp",
            dir=destination_path.parent,
            delete=False,
        ) as output:
            temporary_path = Path(output.name)
            with source_path.open("rb") as source:
                while chunk := source.read(chunk_size):
                    for collection in decoder.feed(chunk):
                        output.write(json.dumps(collection, separators=(",", ":")) + "\n")
                        records_written += 1
                        features_written += len(collection["features"])
            for collection in decoder.finish():
                output.write(json.dumps(collection, separators=(",", ":")) + "\n")
                records_written += 1
                features_written += len(collection["features"])
        assert temporary_path is not None
        temporary_path.replace(destination_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return GeoJsonExportResult(
        destination_path,
        records_written,
        features_written,
        frozenset(decoder.program_numbers),
        frozenset(decoder.metadata_pids),
    )


def main(argv: list[str] | None = None) -> int:
    """Run the ST 0601 to line-delimited GeoJSON export CLI."""

    parser = argparse.ArgumentParser(
        description="Export ST 0601 geospatial metadata from MPEG-TS to GeoJSON lines"
    )
    parser.add_argument("transport", type=Path, help="input STANAG-style MPEG-2 TS")
    parser.add_argument("destination", type=Path, help="output line-delimited GeoJSON")
    parser.add_argument("--force", action="store_true", help="replace an existing destination")
    args = parser.parse_args(argv)
    result = export_geojson_sequence(
        args.transport,
        args.destination,
        overwrite=args.force,
    )
    print(
        f"wrote {result.records_written} metadata records and {result.features_written} "
        f"features to {result.destination} from PID(s) "
        f"{', '.join(hex(pid) for pid in sorted(result.metadata_pids)) or 'none'}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
