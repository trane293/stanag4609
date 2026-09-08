# stanag4609

`stanag4609` is an MIT-licensed Python toolkit for Full Motion Video (FMV). It
reads, writes, verifies, transforms, and displays MPEG transport streams with
MISB KLV metadata.

Use it to build ingest services, metadata processors, AI enrichment pipelines,
GIS exports, and synchronized video applications without starting from raw
MPEG-TS packets or hand-written KLV codecs.

## Try it

Install the package from PyPI. Python 3.10 or newer is required.

```console
python -m pip install stanag4609
```

If FFmpeg is installed, this command generates a small FMV stream and opens the
reference player:

```console
stanag4609-player --demo day
```

The demo includes H.264 video, an audio channel, ST 0601 telemetry, and a map.
It is generated locally and does not download sample footage.

![Reference player with synchronized video, telemetry, and map](https://stanag4609.readthedocs.io/en/latest/assets/screenshots/reference-player.jpg)

## What you can build

- Inspect an FMV stream and explain malformed transport, KLV, metadata, timing,
  and media declarations.
- Decode and update ST 0601 metadata while preserving fields your application
  does not understand.
- Demultiplex, process, and remultiplex live MPEG-TS without transcoding the
  video or audio.
- Convert model detections to ST 0903 VMTI metadata and keep them synchronized
  with video frames.
- Export timed sensor, frame, target, and footprint geometry as GeoJSON or an
  ArcGIS-compatible CSV sidecar.
- Serve a local video, map, telemetry, activity-feed, and detection-timeline UI.
- Carry MPEG-TS over UDP or RTP and describe RTP sessions with SDP.

The core package has no required third-party Python dependencies. FFmpeg, PyAV,
Ultralytics, ONNX Runtime, and Triton support are optional integrations.

## Read a stream from Python

Generate two short sample streams:

```console
stanag4609-demo-samples demo --duration 4
```

Then inspect the synchronized timeline:

```python
from stanag4609.player import scan_transport_file

timeline = scan_transport_file("demo/stanag4609-day-demo.ts")
sample = timeline.samples[0]

print(f"metadata samples: {len(timeline.samples)}")
print(sample.fields["Sensor Latitude"]["value"])
print(sample.fields["Sensor Longitude"]["value"])
```

The [quickstart](https://stanag4609.readthedocs.io/en/latest/QUICKSTART/) walks
through the generated stream, the player, the verifier, and the Python API.

## Choose a workflow

| I want to… | Start here |
| --- | --- |
| Inspect or troubleshoot an FMV file | [Verifier guide](https://stanag4609.readthedocs.io/en/latest/VERIFIER/) |
| Decode or update MISB metadata | [ST 0601 API](https://stanag4609.readthedocs.io/en/latest/api/st0601/) |
| Process KLV in a live stream | [Live transform pipeline](https://stanag4609.readthedocs.io/en/latest/LIVE_PIPELINE/) |
| Add AI detections | [AI and VMTI sidecars](https://stanag4609.readthedocs.io/en/latest/AI_SIDECARS/) |
| Build a browser application | [Web clients](https://stanag4609.readthedocs.io/en/latest/WEB_CLIENTS/) |
| Export metadata to another system | [Ecosystem interoperability](https://stanag4609.readthedocs.io/en/latest/ECOSYSTEM/) |

See the [workflow chooser](https://stanag4609.readthedocs.io/en/latest/WORKFLOWS/)
for end-to-end paths and the [example gallery](https://stanag4609.readthedocs.io/en/latest/EXAMPLES/)
for runnable applications.

## Standards support at a glance

| Status | Standards and profiles |
| --- | --- |
| **Complete software-verifiable profile** | MISB ST 0102.12 Security; ST 0107.5 KLV baseline; ST 0806.4 RVT; ST 0902.8 Minimum Metadata; ST 1002.3 Range Imagery; ST 1010.3 SDCC-FLP; ST 1201.5 IMAP; ST 1204.3 MIIS Core IDs; ST 1206.1 SAR; ST 1303.2 MDAP; ST 1402.2 MPEG-TS metadata carriage; ST 1601.2 Geo-Registration; ST 1602.2 Composite Imaging; ST 1607.2 Segment/Amend |
| **Substantial, intentionally partial** | NATO STANAG 4609 Ed. 5 and MISP-2019.1; MISB ST 0601.19; ST 0603.5 MISP time; ST 0604.6 video timestamps; ST 0804.4 RTP/RTCP; ST 0903.6 VMTI; ST 1001.1 audio; ST 1202.3 image transformations; SMPTE ST 336 KLV; SMPTE RP 217 asynchronous carriage; common H.222.0, H.262, AVC, and HEVC profiles |
| **Future or separate profiles** | MISB ST 0801 / ST 1107 photogrammetry; elementary-stream RTP and RTSP; complete MXF and SDI profiles; a native GStreamer element; whole-MPTS rewriting; uncommon codecs |

“Complete” covers requirements that software can determine from the supplied
data and context. Sensor accuracy, security policy, and other producer-owned
facts remain outside that label. See the
[conformance matrix](https://stanag4609.readthedocs.io/en/latest/CONFORMANCE/)
for requirement-level scope.

## Design principles

- Parse files, sockets, and pipes incrementally.
- Put limits on retained input, queues, histories, and reordering.
- Preserve original bytes and unknown KLV fields during lossless updates.
- Make timestamp, discontinuity, truncation, and backpressure behavior visible.
- Keep codec and inference runtimes behind optional adapters.
- Tie standards claims to a named edition and executable evidence.

## Documentation

The documentation includes a [user guide](https://stanag4609.readthedocs.io/),
[tutorials](https://stanag4609.readthedocs.io/en/latest/EXAMPLES/), a generated
[API reference](https://stanag4609.readthedocs.io/en/latest/api/), and detailed
[standards coverage](https://stanag4609.readthedocs.io/en/latest/STANDARDS/).

The public API is still pre-1.0. Review the
[API stability policy](https://stanag4609.readthedocs.io/en/latest/API_STABILITY/)
before depending on less common extension points.

## Contributing

Development is specification-led and test-driven. See
[CONTRIBUTING.md](https://github.com/trane293/stanag4609/blob/main/CONTRIBUTING.md)
for environment setup, standards provenance, tests, and pull-request guidance.

## License

[MIT](https://github.com/trane293/stanag4609/blob/main/LICENSE)
