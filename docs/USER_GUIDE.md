# User guide

This guide explains the parts of an FMV application that sit between raw
MPEG-TS packets and an operator-facing product. Start with the layer you are
building, then follow the links to its timing, failure, and deployment details.

## Transport and timing

Use the transport layer when you need to receive, demultiplex, transform, or
send MPEG-TS while preserving its program structure and media timing.

- [Live transform pipeline](LIVE_PIPELINE.md) covers KLV replacement,
  injection, program selection, signalling, and reconnects.
- [UDP transport](UDP_TRANSPORT.md) and [RTP transport](RTP_TRANSPORT.md) cover
  network packetization and receive behavior.
- [MISP and UTC timestamps](TIMESTAMPS.md) separates the MPEG PTS clock, MISP
  absolute time, and civil UTC.
- [Transport rate and PCR restamping](TRANSPORT_RATE.md) applies when an output
  must follow a physical constant-rate schedule.

## Metadata and media

Use these pages when decoding or presenting what is inside an FMV service:

- [Audio](AUDIO.md) reconstructs and optionally decodes ST 1001 audio frames.
- [Video properties](VIDEO.md) inspects common H.262, AVC, and HEVC sequence
  information without decoding pixels.
- [GeoJSON streams](GEOJSON.md) turns current ST 0601 and VMTI geometry into an
  incremental GIS boundary.
- The [API reference](api/index.md) covers KLV, ST 0601, ST 0902, ST 0903, range
  imagery, transformations, and transport types.

## Applications and integrations

- [Reference player](PLAYER.md) is the shortest route to synchronized video,
  telemetry, mapping, detections, and a timeline.
- [AI and VMTI sidecars](AI_SIDECARS.md) connects decoded frames to local or
  remote inference and standards-native detections.
- [Adapter guide](ADAPTERS.md) defines the boundary for model, codec, GIS,
  network, and application runtimes.
- [Web clients](WEB_CLIENTS.md) explains recorded and live browser timing.
- [FFmpeg and GStreamer](FFMPEG_GSTREAMER.md) describes where mature media
  runtimes fit around the pure-Python core.
- [Ecosystem interoperability](ECOSYSTEM.md) covers CSV, GeoJSON, native
  transport, and third-party application boundaries.

## Standards and operational boundaries

Read [common pitfalls](COMMON_PITFALLS.md) before joining timestamps, carrying
sparse metadata state, or projecting detections onto a map. The
[conformance matrix](CONFORMANCE.md) and [known limitations](LIMITATIONS.md)
state which requirements and deployment profiles are in scope. Use the
[glossary](GLOSSARY.md) for unfamiliar transport or MISB terms.
