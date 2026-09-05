# Ecosystem interoperability

FMV applications rarely have one output. A useful pipeline may preserve the
standards-native transport for another exploitation tool, publish geometry to
a map, and send inference events to an analytics service at the same time.

## Supported exchange surfaces

| Surface | Direction | Status | Typical use |
| --- | --- | --- | --- |
| MPEG-2 TS with KLVA | Read, transform, write | Implemented | Standards-native FMV consumers |
| ArcGIS FMV Multiplexer CSV | Import and export | Implemented | Parallel metadata interchange and authoring |
| Line-delimited GeoJSON | Export/stream | Implemented | Maps, event buses, GIS and OSINT applications |
| Synchronized timeline JSON | Application API | Implemented | Web and desktop viewers |
| ST 0903 VMTI | Decode and encode | Implemented slice | Detection/track exchange inside KLV |
| GStreamer native element | — | Planned | Media graphs and device pipelines |

The status describes the repository implementation, not certification by any
third-party product vendor.

## Fan out without coupling sinks

A live application should give every destination a bounded queue and an
explicit overload policy. Archival transport may require backpressure; a map
display may prefer latest-only delivery; an inference audit sink may fail the
mission pipeline rather than lose an event. One slow HTTP or GIS client must
not create unbounded memory use for every other sink.

Preserve the identity needed to reconcile streams later:

- program number and elementary-stream PID;
- MPEG PTS and, where available, MISB Precision Time Stamp;
- metadata sequence/discontinuity information;
- stable VMTI target and algorithm identity;
- a mission/source identifier selected by the application.

## GIS and web maps

The GeoJSON sequence exposes sensor, frame-center, target, and footprint
features. Generic GIS, OSINT, and defence-oriented situational-awareness tools
can ingest that stream directly when they support GeoJSON, or through a small
application adapter when their schema differs. Browser applications can use
the same features with common map renderers.

Product-specific integrations must document and test the exact accepted file
or streaming schema, coordinate reference system, timestamp interpretation,
and version. Until that evidence exists, this project describes such tools as
consumers of the generic interchange boundary rather than claiming a certified
native connector.

## ArcGIS-oriented workflows

Create standards-native transport from video plus a metadata table:

```console
stanag4609-mux-esri raw_video.mpeg metadata.csv mission.ts
```

Export reconstructed metadata as a parallel table for downstream processing:

```console
stanag4609-export-esri mission.ts metadata.csv
```

The import/export schema is the ArcGIS FMV Multiplexer CSV shape. See the CLI
guide for overwrite and PID controls.

## Integration acceptance criteria

Every named third-party connector added to this documentation must include:

1. supported product/runtime versions and an authoritative format reference;
2. an executable sample using public or synthetic data;
3. timing, coordinate, security-marking, and unknown-field behavior;
4. bounded retry/backpressure and failure semantics for live use;
5. an integration test or a reproducible manual verification recipe;
6. a clear statement of what is and is not vendor-certified.
