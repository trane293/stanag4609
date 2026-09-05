# Objective completion audit

This is the evidence ledger for the project's release objective. It is stricter
than the feature list: a capability is marked proven only when the repository
contains a concrete implementation boundary and executable evidence matching
the breadth of the claim.

Last audited: 5 September 2026, release version `0.1.0`.

## Current evidence

| Objective requirement | Authoritative evidence | Assessment |
| --- | --- | --- |
| Fully open source under the MIT License | `LICENSE`; PEP 621 `license = "MIT"`; OSI classifier; public source repository | Proven |
| Pure-Python library core | Empty runtime `dependencies`; universal `py3-none-any` wheel; Python 3.10–3.14 CI | Proven for the core; FFmpeg, PyAV, and AI runtimes are explicit optional boundaries |
| Read recorded and arbitrarily chunked live MPEG-2 TS | `TransportDemuxer`, incremental TS/PSI/PES/KLV parsers, bounded RTP reorder/receiver path, UDP receivers, real-fixture tests, randomized chunk tests | Proven for implemented H.222.0/ST 1402 structures and ST 0804 multiplexed-TS RTP carriage |
| Write and modify STANAG 4609 transport | `TransportMuxer`, `LiveTransportTransformer`, CSV/KLV muxer, byte-preserving media tests, generated real-media acceptance | Proven for selected-program SPTS output; whole-MPTS rewriting remains outside this API |
| Read, validate, write, and losslessly update ST 0601.19 | Complete active-item registry, strict/preserve decoders, canonical encoder, lossless updater, Report-on-Change state, and [requirement trace](requirements/ST0601.19.md) | Every active item and active requirement identifier has structural or contextual evidence; the whole-standard claim remains conditional on producer facts and separately invoked child standards |
| Enforce ST 0902.8 MISMMS | Packet/current-state validators, inclusive 30-second cadence, nested Security cadence and policy contexts, ST 1607 hierarchy checks, verifier integration, and [requirement trace](requirements/ST0902.8.md) | Proven for all active requirements when caller-owned security policy and applicability facts are supplied |
| Preserve common audio channels | Per-PID demux and timing, Layer II/AAC frame reconstruction, byte-preserving transform tests, all-codec PyAV CI, real AAC FMV-to-PCM acceptance | Proven for the three ST 1001 codecs; mixing, output-device selection, and resampling are application policy |
| Verify and explain malformed FMV | Incremental `FMVVerifier`, terminal/JSON/HTML CLI reports, embedded ST 0604 timestamp coverage, real deployed-stream defects, 20 hash-pinned fault-specific cases from an independent vendor corpus, stable report-schema tests | Proven for the checks listed in the [verifier guide](VERIFIER.md); the sole unasserted corpus member is within the applicable published timing bound, and this is not a certification authority |
| Play supplied, public, and live FMV with synchronized metadata | `stanag4609-player`, recorded timeline/range mode, bounded fragmented-MP4/MSE live gateway, media-origin PTS alignment, numbered SSE, plus real-Chromium recorded/live playback, seek, synchronization, canvas, and telemetry tests | Proven for recorded fixtures and incremental browser delivery; production multi-viewer/WebRTC deployment remains application architecture |
| Show actual requested telemetry | Real Day/Night FMV player acceptance requires MISP timestamp, sensor latitude/longitude/altitude, platform attitude, sensor attitude, frame center, target coordinates, and sensor/frame/target map features on every sample; Item 42 exposes its receiver-current MSL/HAE/unknown datum | Proven against two checksum-pinned independent recordings plus normative datum-state tests |
| Show VMTI AI detections | Typed ST 0903 VMTI codec, checksum-bound exact accounting for all 65 active and 78 deprecated ST 0903.6 identifiers, real YOLO-derived demo artifact, synthetic standards-vector overlay/geolocation tests; negative inspection of the public ImpleoTV corpus and OpenSensorHub sample | Implemented; neither inspected source contains VMTI, so an independently sourced recorded VMTI FMV fixture is still missing |
| Decode video into AI sidecars | Optional `PyAVFrameSource`, exact native-time-base to transport-PTS conversion, model-neutral BGR/native frames, and real YOLO tutorial integration | Proven at the adapter boundary; FFmpeg codec and hardware behavior remains in the optional backend |
| Publish installable packages and documentation | Production PyPI `0.1.0`, owner-gated release workflow, wheel/sdist install checks, strict MkDocs/Read the Docs configuration | Proven for the initial release process |

## Why the overall objective remains open

The repository has strong alpha evidence, but it does not yet justify an
unqualified “complete STANAG 4609 implementation” or stable `1.0` claim:

1. NATO STANAG 4609 and the adopted MISP contain broader system and deployment
   clauses than the implemented receiver/writer library profile. All 86 active
   and 34 deprecated MISP-2019.1 identifiers now have checksum-bound exact
   accounting and a human-readable applicability disposition. Multiplexed
   MPEG-2 TS over RTP is traced through MISP-2019.1-76 and
   ST 0804.4-18, and embedded video time through MISP-2018.1-104/ST 0604.6;
   H.262/AVC/HEVC core sequence and VUI properties are inspected, producer-known
   source aspect ratio, source/conversion scan facts, and analog/digital
   provenance can be checked explicitly, ST 0604
   timestamps are associated with recognized compressed access units, and RTCP
   Sender Report codec/clock mapping is implemented. Native elementary-stream
   RTP, a complete RTCP session engine, RTSP, remaining image coding, and file
   profiles remain.
2. Some ST 0601 correctness depends on facts unavailable in the bytes—metadata
   time of birth, producer IMAP precision, sensor truth, and security policy.
   The library accepts, enforces, and reports those contexts, including exact
   or tolerance-aware ST 0601 field expectations, but cannot invent them.
3. Real recorded VMTI interoperability remains synthetic/derived rather than
   independently sourced. The ImpleoTV public negative corpus and OpenSensorHub
   recording were byte-inspected and contain no VMTI. Third-party Esri
   interoperability is tested at file/CSV boundaries rather than claimed from
   a vendor certification suite.
4. The reference player now proves bounded incremental fragmented-MP4/MSE and
   SSE delivery to a browser. Sub-second glass-to-glass targets, adaptive
   bitrate, authenticated multi-viewer fan-out, and production WebRTC/HLS are
   deployment concerns beyond this localhost reference gateway.
5. The live gateway now has reproducible JSON measurements across all three
   pinned 148–371-second FMV recordings plus 100,000-event/10,000-fragment
   retention stress tests. Multi-hour paced runs, repeated reconnect epochs,
   concurrent viewers, and prolonged loss/reorder/backpressure campaigns remain
   necessary before a production stability claim.

The detailed next actions live in the [continuation roadmap](ROADMAP.md). This
audit must be updated whenever a release changes a proof or closes a limitation.
