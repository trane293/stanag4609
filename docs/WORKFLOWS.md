# Choose a workflow

Start with the outcome you need. Each path below points to one runnable example
and the corresponding API guide.

## Inspect or validate an FMV file

Use `stanag4609-verify` to inventory programs, video, audio, KLV carriage, MISB
metadata, and timing. Text output works well at a terminal; JSON is intended for
automation; HTML produces a standalone report.

```console
stanag4609-verify mission.ts
stanag4609-verify mission.ts --format json > verification.json
```

Continue with the [inspection tutorial](tutorials/inspect_fmv.md) or the
[verifier guide](VERIFIER.md).

## Read metadata in Python

Use `scan_transport_file()` when you need a presentation-ready timeline. Use
`TransportDemuxer` and `MetadataStreamDecoder` when your application needs each
transport event as it arrives.

Set an application target ceiling when VMTI comes from an untrusted stream:

```python
from stanag4609 import MetadataStreamDecoder

decoder = MetadataStreamDecoder(max_vmti_targets=256)
```

The ceiling applies to both standalone ST 0903 and VMTI embedded in ST 0601
Item 74. A packet above the limit raises `LimitExceeded` before the decoder
allocates more than 256 targets, even when field decoding uses `PRESERVE`.

Start with the [quickstart](QUICKSTART.md), then read the [ST 0601 API](api/st0601.md)
and [transport API](api/transport.md).

## Change KLV in transit

Use `LiveTransportTransformer` to pass, drop, replace, or inject metadata in a
selected program. The transformer carries the selected video and audio streams
into its output transport.

Follow [Transform live KLV and fan it out](tutorials/live_fanout.md), then use
the [live pipeline guide](LIVE_PIPELINE.md) for timing, program changes, and
reconnect behavior.

## Create FMV from video and CSV

Use `stanag4609-mux-esri` when you have ordinary video or audio plus an
ArcGIS-style metadata CSV:

```console
stanag4609-mux-esri Raw_Video.mpeg Raw_Metadata.csv mission.ts
```

The [creation tutorial](tutorials/create_fmv.md) explains timestamp handling,
verification, playback, and export.

## Add AI detections

The sidecar API joins decoded frames to KLV timestamps, runs sequential or
parallel inference stages, and emits ST 0903 VMTI. Use the model-neutral
interfaces for your own function or service, or install an adapter for
Ultralytics, ONNX Runtime, or Triton.

Start with [Add AI detections as VMTI](tutorials/ai_to_vmti.md), then read the
[sidecar guide](AI_SIDECARS.md).

## Build a video and map application

Use `stanag4609-player` for a complete local viewer. For your own backend, call
the player asset or timeline APIs and bind every UI panel to the same media
clock.

The [dashboard tutorial](tutorials/web_dashboard.md) and [web client guide](WEB_CLIENTS.md)
cover recorded and live designs.

## Export metadata or send the stream elsewhere

Use `stanag4609-export-geojson` for a timed GIS side stream and
`stanag4609-export-esri` for CSV. Use the UDP and RTP APIs when the destination
expects MPEG-TS.

Continue with [GeoJSON streams](GEOJSON.md), [UDP transport](UDP_TRANSPORT.md),
[RTP transport](RTP_TRANSPORT.md), or [ecosystem interoperability](ECOSYSTEM.md).

## Work directly with a standard

Use the [API reference](api/index.md) to find typed codecs and validators. Check
the [conformance matrix](CONFORMANCE.md) before relying on a particular profile
or requirement.
