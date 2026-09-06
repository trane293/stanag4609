# Build interoperable full-motion-video systems

`stanag4609` is an MIT-licensed, pure-Python toolkit for recorded and live
STANAG 4609 motion imagery, MISB KLV metadata, synchronized audio, and ST 0903
VMTI detections.

It is designed for systems that must inspect, validate, transform, enrich,
remultiplex, visualize, or fan out FMV without hiding timing or wire behavior.
The dependency-free core works on arbitrary stream chunks; codec and inference
runtimes stay behind optional adapters.

!!! info "Current maturity"

    The library is production-ready for the documented complete profiles while
    its public API remains pre-1.0. This is not a claim of universal STANAG
    certification or complete STANAG 4609/MISP, ST 0601.19, ST 0903.6, or
    ST 1001.1 conformance. Start with the
    [production-readiness audit](PRODUCTION_READINESS.md),
    [conformance matrix](CONFORMANCE.md), and [known limitations](LIMITATIONS.md).

## Standards support at a glance

“Complete” below means the edition's software-verifiable requirements are
source-bound, implemented, and exercised by automated tests. It does not claim
that software can prove a producer's physical measurements, security decisions,
or geospatial truth, and it is not third-party certification.

| Status | Standards and profiles |
| --- | --- |
| **Complete software-verifiable profile** | MISB ST 0102.12 Security; ST 0107.5 KLV baseline; ST 0806.4 RVT; ST 0902.8 Minimum Metadata; ST 1002.3 Range Imagery; ST 1010.3 SDCC-FLP; ST 1201.5 IMAP; ST 1204.3 MIIS Core IDs; ST 1206.1 SAR; ST 1303.2 MDAP; ST 1402.2 MPEG-TS metadata carriage; ST 1601.2 Geo-Registration; ST 1602.2 Composite Imaging; ST 1607.2 Segment/Amend |
| **Substantial, intentionally partial** | NATO STANAG 4609 Ed. 5 / MISP-2019.1 overall profile; MISB ST 0601.19 (every active root item is typed, but remaining producer-truth semantics prevent a whole-standard claim); ST 0604.6 embedded timestamps; ST 0804.4 RTP/RTCP transport; ST 0903.6 VMTI, tracks, masks, chips, and AI vocabulary; ST 1001.1 common audio; ST 1202.3 image transformations; SMPTE ST 336:2017 KLV subset; SMPTE RP 217:2001 TS branch; common H.222.0/H.262/AVC/HEVC profiles |
| **Future or separate profiles** | MISB ST 0801 / ST 1107 photogrammetry; native elementary-stream RTP and RTSP; complete MXF/SDI profiles; native GStreamer element; whole-MPTS rewriting; additional uncommon codecs |

See the [requirement-level conformance matrix](CONFORMANCE.md),
[edition and source inventory](STANDARDS.md), and
[known limitations](LIMITATIONS.md) for the exact boundary behind each label.

<div class="grid cards" markdown>

-   :material-clock-fast:{ .lg .middle } **Inspect an FMV stream**

    ---

    Install the package, parse KLV incrementally, and reconstruct sparse
    Report-on-Change metadata.

    [:octicons-arrow-right-24: Quickstart](QUICKSTART.md)

-   :material-swap-horizontal:{ .lg .middle } **Transform KLV in transit**

    ---

    Pass, drop, replace, or inject metadata while preserving video, audio, and
    other elementary streams.

    [:octicons-arrow-right-24: Live pipeline](LIVE_PIPELINE.md)

-   :material-brain:{ .lg .middle } **Bring your own AI**

    ---

    Correlate frames, compose sequential and parallel inference stages, and
    encode detections as standards-native VMTI.

    [:octicons-arrow-right-24: AI sidecars](AI_SIDECARS.md)

-   :material-monitor-dashboard:{ .lg .middle } **Build a viewer**

    ---

    Use the bundled player or consume the synchronized timeline and GeoJSON
    APIs in a custom web client.

    [:octicons-arrow-right-24: Web clients](WEB_CLIENTS.md)

-   :material-map-marker-path:{ .lg .middle } **Connect downstream tools**

    ---

    Export GeoJSON or ArcGIS-compatible CSV, or preserve the standards-native
    MPEG-TS and KLV streams for another FMV consumer.

    [:octicons-arrow-right-24: Ecosystem interoperability](ECOSYSTEM.md)

</div>

## Install for development

```console
git clone git@github.com:trane293/stanag4609.git
cd stanag4609
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,docs]'
```

The published core will have no required third-party Python dependencies.
Optional extras add documentation, codec, and model-runtime integrations only
when an application selects them.

## Choose a path

| Goal | Start here |
| --- | --- |
| Decode and validate KLV | [Quickstart](QUICKSTART.md) |
| Diagnose an FMV file | [Verify and debug FMV](VERIFIER.md) |
| Edit metadata without transcoding media | [Live transform pipeline](LIVE_PIPELINE.md) |
| Add detections from YOLO, ONNX, Triton, or custom code | [AI and VMTI sidecars](AI_SIDECARS.md) |
| Create a browser interface | [Web clients](WEB_CLIENTS.md) |
| Use FFmpeg or GStreamer around the core | [FFmpeg and GStreamer](FFMPEG_GSTREAMER.md) |
| Export parallel geospatial streams | [GeoJSON](GEOJSON.md) and [ecosystem interoperability](ECOSYSTEM.md) |
| Look up classes, functions, and signatures | [Python API reference](api/index.md) |
| Audit protocol coverage | [Conformance](CONFORMANCE.md) |

## Design promises

- Preserve unknown wire data so a partial decoder is not a destructive one.
- Keep live memory and latency bounded through explicit limits and backpressure.
- Treat timing, discontinuities, malformed input, and rollover as public behavior.
- Keep model runtimes, codecs, user interfaces, and network frameworks outside
  the dependency-free protocol core.
- Make every conformance claim traceable to a named standard edition and test.
