# Known limitations

- NATO STANAG 4609 Edition 5, its adopted MISP-2019.1 profile, MISP-2023.2,
  SMPTE ST 336:2017, and SMPTE RP 217:2001 are present and checksum-catalogued.
  The STANAG/MISP-2019.1 copies came from a secondary public archive; their
  document identities are corroborated against NATO and U.S. ASSIST records.
  No whole-document conformance claim is made until their normative clauses are
  fully traced to executable evidence.
- Typed ST 0601 coverage includes every active root item and the structured
  packs named in the requirement trace. Coded icing, generic flags, lens and
  operational modes, legacy weapon nibbles, positioning sources, platform and
  sensor modes are semantic integer-compatible types; Item 62 enforces the
  exact three/four-digit Laser PRF profile. The remaining whole-standard
  semantic boundary is listed explicitly under "Remaining ST 0601.8-14 closure
  scope" in the requirement trace. Implemented receiver state includes
  Report-on-Change, distributed wavelength, payload, weapon-store, waypoint,
  and control-command data, plus Segment/Amend trees.
- Remaining ST 0601 producer-owned truth and measurement-quality semantics,
  additional cross-frame ST 0903 geometric semantics, external provisioning
  of asynchronous metadata STD parameters, dependency-free audio sample
  decoding, and whole-multiplex multi-program rewriting are pending.
  Generic incremental
  MPEG-TS framing, PSI discovery, PES reconstruction, ST 1201 IMAP mapping, and
  Layer II/AAC-LC compressed audio frame reconstruction,
  optional PyAV decoding for every ST 1001 codec,
  the ST 0903.6 top-level/VTarget decoder, absolute geospatial DLPs/Series,
  VTracker, VMask, and VChip structures,
  Algorithm/Ontology/VObject/VFeature label structures, core pixel-detection
  encoder, optional caller-owned OWL/entity/label semantic resolution, and a
  bounded timed-KLV processor contract are implemented.
  Embedded VChip payloads are checked for their declared JPEG or PNG signature;
  full image decoding and externally referenced IRI content are application concerns.
- The live transformer is pull-driven and emits one explicitly selected program
  from a bounded single- or multi-program input. It accepts versioned PAT moves
  of that program to a new PMT PID and versioned PMT updates, including KLVA PID additions,
  removals, and carriage changes, while preserving retained PID continuity and
  unchanged synchronous metadata sequence state. An affected KLVA transition
  is rejected when an asynchronous KLV item or synchronous access unit is
  partial, so buffered metadata is never discarded ambiguously. PMT route
  changes are validated transactionally: a rejected table leaves the full old
  topology active, while an incomplete PES may cross a compatible update only
  when its PID and stream definition are unchanged. Switching the selected
  program number and retaining unrelated programs remain out of scope for this
  SPTS output API.
  It preserves media PES bytes, their source TS payload/adaptation layout,
  PCR/OPCR, and PMT descriptors. The separate `TransportRateShaper` provides
  explicit constant-rate slots, bounded null fill, and retained-PCR restamping
  when the application supplies the actual output bitrate and clock anchor;
  the transformer deliberately does not invent those deployment facts.
  Receiver-side PCR interval validation is implemented, but physical PCR
  accuracy/jitter measurement is not. A
  KLVA PID can be added while accepting the first PMT, with a version bump and
  collision validation.
- The first-party sidecar package defines frame/detection envelopes, nested
  sequential and bounded-parallel inference graphs, and common detection-to-VMTI
  conversion and synchronous PTS/KLV correlation. The optional PyAV frame
  source decodes common FFmpeg-supported video into model-neutral frames and
  rescales native timestamps to the transport PTS clock; hardware selection,
  asynchronous-metadata association policy, and model lifecycle management
  remain application-owned. Bounded per-stage runtime counters and latency
  totals are
  available, but histogram/export backends remain application-owned. The
  bounded generic JSON-over-HTTP,
  ONNX Runtime, and Triton adapters intentionally retain application-owned
  schemas; ONNX models require explicit model-specific tensor hooks. An
  optional Ultralytics adapter normalizes detection boxes, labels, confidence,
  and track IDs, but does not own model loading or tracking policy. A bounded
  decoded-frame queue provides explicit blocking and drop policies but does not
  itself schedule a decoder.
- The reference player offers a complete FFmpeg H.264/AAC transcode for
  seekable recordings and a bounded, incremental fragmented-MP4/MSE mode for
  live TS input. The live reference forces one-second keyframes, applies source
  backpressure through the FFmpeg pipe, retains a configurable late-join
  window, and supports one or more polling clients; it is not an adaptive-
  bitrate, authenticated, horizontally scaled, or sub-second WebRTC service.
  An attributed OpenStreetMap baselayer has an offline-grid fallback. ST 0601
  points, frame footprints, resolved absolute or parent-offset VMTI locations,
  and footprint-interpolated VMTI bounding boxes appear on the map; the
  interpolation is not terrain-aware geolocation. Pixel bounding boxes,
  centroids, contours, and run masks are rendered over recorded video, but
  VMTI pixel masks are not projected onto the map. Incremental recorded mode
  retains 512 detailed samples plus a sparse 2,048-bin full-mission detection
  overview; the server still retains the decoded source timeline in memory.
- The GeoJSON sequence exporter covers ST 0601 sensor, frame-center, target,
  footprint geometry, and resolved ST 0903 absolute/parent-offset target points.
  It does not emit footprint-interpolated VMTI boxes, project VMTI masks, emit
  track-history geometries, or manage a long-lived spatial store.
- The supplied `Raw_Video.mpeg` is an MPEG Program Stream with MPEG-2 video and
  MP2 audio but no embedded KLV. Its companion 866-row `Raw_Metadata.csv` is
  an end-to-end producer fixture, not an independent known-good STANAG 4609
  conformance stream. Their generated transport passes the implemented
  structural verifier profile; that does not make the source pair a normative
  conformance vector.
- The package is alpha and makes no unqualified standards-conformance claim.
- MISP source aspect ratio and scan history are not inferable from an encoded
  display header. `MISPImageContext` and the corresponding verifier CLI options
  validate these facts when the producer supplies them; omission remains an
  explicit evidence boundary rather than an assumed pass.
