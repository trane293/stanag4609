# Build Full Motion Video applications in Python

`stanag4609` reads, writes, verifies, transforms, and displays MPEG transport
streams with MISB KLV metadata. The core is pure Python and has no required
third-party dependencies.

## Install and run the demo

```console
python -m pip install stanag4609
stanag4609-player --demo day
```

The second command creates a short H.264 FMV stream with audio and ST 0601
telemetry, then opens it in the reference player. You can inspect the video,
sensor coordinates, platform attitude, and map from the same timeline.

[:octicons-arrow-right-24: Follow the quickstart](QUICKSTART.md)

## Choose what to build

<div class="grid cards" markdown>

-   :material-file-search:{ .lg .middle } **Inspect an FMV stream**

    ---

    Inventory its programs and media, decode metadata, and turn format or
    conformance problems into structured findings.

    [:octicons-arrow-right-24: Verify and debug FMV](VERIFIER.md)

-   :material-swap-horizontal:{ .lg .middle } **Transform a live stream**

    ---

    Pass, drop, replace, or inject KLV while carrying the selected video and
    audio streams forward.

    [:octicons-arrow-right-24: Live transform pipeline](LIVE_PIPELINE.md)

-   :material-brain:{ .lg .middle } **Add AI detections**

    ---

    Run local or remote models, correlate results with video frames, and encode
    detections as ST 0903 VMTI.

    [:octicons-arrow-right-24: AI and VMTI sidecars](AI_SIDECARS.md)

-   :material-monitor-dashboard:{ .lg .middle } **Build a viewer**

    ---

    Drive video overlays, a map, telemetry, an activity feed, and a detection
    timeline from one media clock.

    [:octicons-arrow-right-24: Web clients](WEB_CLIENTS.md)

-   :material-map-marker-path:{ .lg .middle } **Connect another system**

    ---

    Export GeoJSON or CSV, send MPEG-TS over UDP or RTP, or feed another FMV
    application.

    [:octicons-arrow-right-24: Interoperability](ECOSYSTEM.md)

-   :material-code-braces:{ .lg .middle } **Use the Python API**

    ---

    Find the public classes and functions for KLV, metadata, transport, video,
    audio, verification, playback, and sidecars.

    [:octicons-arrow-right-24: API reference](api/index.md)

</div>

The [workflow chooser](WORKFLOWS.md) maps common applications to the relevant
commands, APIs, and tutorials.

## Standards support at a glance

| Status | Standards and profiles |
| --- | --- |
| **Complete software-verifiable profile** | MISB ST 0102.12 Security; ST 0107.5 KLV baseline; ST 0806.4 RVT; ST 0902.8 Minimum Metadata; ST 1002.3 Range Imagery; ST 1010.3 SDCC-FLP; ST 1201.5 IMAP; ST 1204.3 MIIS Core IDs; ST 1206.1 SAR; ST 1303.2 MDAP; ST 1402.2 MPEG-TS metadata carriage; ST 1601.2 Geo-Registration; ST 1602.2 Composite Imaging; ST 1607.2 Segment/Amend |
| **Substantial, intentionally partial** | NATO STANAG 4609 Ed. 5 and MISP-2019.1; MISB ST 0601.19; ST 0603.5 MISP time; ST 0604.6 video timestamps; ST 0804.4 RTP/RTCP; ST 0903.6 VMTI; ST 1001.1 audio; ST 1202.3 image transformations; SMPTE ST 336 KLV; SMPTE RP 217 asynchronous carriage; common H.222.0, H.262, AVC, and HEVC profiles |
| **Future or separate profiles** | MISB ST 0801 / ST 1107 photogrammetry; elementary-stream RTP and RTSP; complete MXF and SDI profiles; a native GStreamer element; whole-MPTS rewriting; uncommon codecs |

The label “complete” applies to checks software can make from the supplied data
and context. The [conformance matrix](CONFORMANCE.md) gives the requirement-level
scope, while [known limitations](LIMITATIONS.md) covers deployment and
producer-owned boundaries. The Python API remains pre-1.0.

## Learn the system

- Start with the [examples](EXAMPLES.md) for complete applications.
- Read [how the modules fit together](ARCHITECTURE.md) before designing a live
  pipeline.
- Keep [common FMV pitfalls](COMMON_PITFALLS.md) nearby when working with time,
  sparse metadata, or geospatial detections.
- Use the [glossary](GLOSSARY.md) when a transport or MISB term is unfamiliar.
