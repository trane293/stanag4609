# GeoJSON metadata streams

`stanag4609-export-geojson` turns an MPEG-2 Transport Stream into a
line-delimited sequence of GeoJSON `FeatureCollection` objects. One line is
written for every decoded ST 0601 packet, so consumers can process a live feed
without waiting for the recording to finish or loading a mission-sized
`FeatureCollection` into memory.

```console
stanag4609-export-geojson input.ts metadata.geojsonl
```

Each record includes the source Item 2 coordinate labelled with
`timestamp_time_scale: "MISP"`, program number, metadata PID, 90 kHz PTS,
floating-point PTS seconds, and any lossless-decoding diagnostics. When the
current Report-on-Change state contains Item 136, `utc_timestamp` contains the
derived civil time after applying optional Item 137. The exporter does not
invent a leap-second offset when the stream omits it.
Its features use GeoJSON longitude/latitude coordinate order and may contain:

- `sensor`, from Items 13-14 and the preferred available height in the
  `104 > 75 > 15` chain;
- `frame_center`, from Items 23-24 and preferred HAE Item 78 or MSL Item 25;
- `target`, from Items 40-42;
- `vmti_target`, from an embedded ST 0903 VTarget absolute Location Item 17,
  or from Items 10-12 resolved against ST 0601 frame-center Items 23-24; and
- `frame_footprint`, from full corner Items 82-89, or from the frame center and
  offset corner Items 26-33 when the full coordinates are unavailable.

Full corners take precedence over offsets. Footprints crossing the
antimeridian are split into a correctly wound `MultiPolygon` so common map
clients do not draw a shape around the long side of the globe. A point or footprint is omitted if its required fields are
missing, carry a MISB special value, or are outside their geographic domain;
the exporter never substitutes a fabricated zero coordinate.
Absolute VTarget locations take precedence over parent-relative offsets. Each
VMTI target feature includes its target ID, location source, optional detection
status/confidence, and HAE datum when available.

When a point contains altitude, its properties include the selected
`altitude_tag` and, when the stream establishes it, `vertical_datum` as `hae`
or `msl`. Target Item 42 follows the datum established by the current preferred
frame-center height, as required by ST 0601.19 Section 8.42. This avoids
silently treating ellipsoid and mean-sea-level heights as interchangeable.
When Item 42 is present without a receiver-current Item 25 or 78, its numeric
value remains in the coordinate but `vertical_datum` is explicitly `unknown`.

For an application-owned sink, use the streaming iterator directly:

```python
from stanag4609 import iter_geojson_feature_collections

for collection in iter_geojson_feature_collections(network_chunks):
    publish_to_map(json.dumps(collection))
```

Applications operating on decoded objects can use the same resolver without
GeoJSON:

```python
from stanag4609 import resolve_target_elevation, resolve_vtarget_location

target_elevation = resolve_target_elevation(snapshot.fields)
if target_elevation is not None:
    print(target_elevation.value, target_elevation.datum)

location = resolve_vtarget_location(
    target,
    frame_center_latitude=uas.value(23),
    frame_center_longitude=uas.value(24),
)
```

The iterator maintains an independent bounded ST 0601 Report-on-Change state
per program and metadata PID. Sparse packets therefore retain still-current
geometry, while expired or explicitly cleared items disappear. The file API
`export_geojson_sequence()` writes atomically and refuses to replace an
existing destination unless `overwrite=True` (or `--force` on the CLI).

This is a GeoJSON text sequence for streaming and fan-out, not one monolithic
GeoJSON document. Most streaming databases, message buses, and web backends
can consume it one JSON object per line. Applications that require a single
FeatureCollection can combine the records according to their own retention
and sampling policy.
