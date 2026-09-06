# Remaining work and continuation guide

This page records the work that remains after the `0.1.0` initial release.
It separates verified capability from future scope so another developer or agent
can resume without reconstructing project history.

The [objective completion audit](COMPLETION_AUDIT.md) maps the original release
goal to current evidence and explains why the overall goal remains open.

## Proven release baseline

- The full unit suite exceeds the configured 90% branch-coverage gate.
- Public integration tests cover H.264 with asynchronous KLVA, H.264/AAC with
  synchronous KLVA, and browser asset preparation with geospatial samples.
- An independently published 21-file negative-conformance corpus is pinned;
  20 members prove fault-specific verifier diagnostics after per-member hash
  authentication. The sole unasserted PCR/PTS-drift member stays inside the
  applicable published timing bound and is retained as an explicit non-claim.
- The ArcGIS raw-video/CSV workflow generates MPEG-2 video, Layer II audio, and
  866 synchronous ST 0601 packets; its structural verifier report is clean and
  every packet appears in the player timeline.
- Distribution builds, console entry points, strict documentation, and fresh
  wheel/source installs are release gates rather than manual assumptions.

See [public fixtures](PUBLIC_FIXTURES.md) for reproducible identities and the
important negative-conformance expectations.

## Highest-priority protocol work

1. Complete MISP-2019.1 software-verifiable image-profile checks. Embedded
   ST 0604 timestamps are now parsed and count-audited for H.262/AVC/HEVC;
   every declared video stream is checked against the three approved Class 1
   codec families while non-profile stream types remain losslessly preservable;
   H.262/AVC/HEVC dimensions, display/timing fields, scan signalling, and codec
   exact non-reserved profile/level signalling and the Class 1
   eight-bit-per-band limit are now inspected
   across every observed sequence property set, so an early profile, scan, or
   bit-depth violation cannot be hidden by a later sequence. H.262 enforces its
   Main-profile 4:2:0 and constrained-flag signalling, Main/High Level
   sampling-density, padded-luminance-rate, declared bit-rate, and VBV-buffer
   limits, plus the zero frame-rate-extension rule for defined profiles; AVC
   and HEVC enforce
   Annex A coded-picture dimensions and available
   sequence-signalled sample throughput.
   Timestamp messages are now associated with
   recognized compressed access units using H.262 picture, AVC prefix-SEI, and
   HEVC prefix/suffix-SEI placement; broader whole-bitstream codec certification
   remains. All 86 active and 34 deprecated requirement identifiers now have
   checksum-bound exact inventory and human-readable disposition; the verifier
   also enforces MISP-2015.1-49 single-mechanism Security Metadata carriage.
   Producer-supplied source aspect ratio, source/conversion scan history, and
   analog/digital provenance are now checked directly for
   MISP-2015.1-01/-02/-05/-06 instead of being inferred from encoded display
   properties.
2. Close the remaining contextual and cross-item ST 0601.19 conformance gaps,
   especially producer-supplied time-of-birth/precision facts. The child
   standards share a checksum-bound complete ST 0107.5 baseline for KLV,
   value encoding, UTF-8, and Report-on-Change semantics. The child
   standards embedded by Items 48, 73, 94, 95, 97, 98, 99, and 102 now have
   typed bridges;
   Item 48 has checksum-bound exact ST 0102.12 accounting plus complete
   Universal/Local Set conversion and policy-context enforcement for its
   software-verifiable profile;
   Item 73 has checksum-bound exact ST 0806.4 requirement accounting and
   producer-supplied time-of-birth validation;
   Item 94 has checksum-bound exact ST 1204.3 requirement accounting plus
   standalone KLV, checked text, XML, multi-sensor, and window-derivation
   support for its complete software-verifiable profile;
   Item 95 additionally has checksum-bound exact ST 1206.1 requirement
   accounting and effective-PRF/RCS exploitation helpers;
   Item 98 additionally has parallel Amend/MSID Report-on-Change resolution,
   and rejects Item 9/10 uncertainty arrays that lack their dimensionally
   compatible Item 4/5/8 source geometry, with checksum-bound exact ST 1601.2
   requirement accounting for its complete software-verifiable profile;
   Item 99 has cross-branch policy validation and checksum-bound exact ST 1602.2
   requirement accounting for its complete software-verifiable profile;
   Item 97 includes checksum-bound exact ST 1002.3 requirement accounting,
   caller-dimension SPRM center defaults, plane subtraction, and reconstruction
   for its complete software-verifiable profile.
   Every active root item is typed, and
   verifier summaries quantify which packets received birth-time,
   IMAP-precision, VMTI frame, and exact/tolerance-aware field ground-truth
   context, including every occurrence of multi-use root tags through
   order-independent matching; retain lossless unknown-item behavior for future
   extensions. The
   shared ST 1201.5 IMAP dependency now has checksum-bound exact requirement
   accounting and complete official-vector coverage. The shared ST 1303.2
   multidimensional-array dependency likewise has checksum-bound exact
   requirement accounting and complete software-verifiable algorithm coverage.
3. Expand ST 0903.6 beyond the implemented VMTI/VTarget/VTracker/VMask/VChip,
   ontology, algorithm, and geospatial slices with independent real-stream
   vectors. All 65 active and 78 deprecated identifiers now have
   checksum-bound exact accounting; remaining limitations are explicit
   contextual, producer-owned, or interoperability-evidence boundaries.
4. Broaden independently reviewed ST 0902.8 policy profiles beyond the now
   implemented caller-supplied classification, country, handling,
   country-code-vocabulary, and minimum-version checks; policy authorship and
   authoritative dated code-list selection remain external.
5. Broaden ST 1001.1-labelled audio conformance fixtures while keeping codec
   scope focused on common Layer II and AAC profiles.
6. Add a redistributable real ST 0903/VMTI FMV fixture. Current VMTI coverage is
   synthetic and standards-vector driven; it is not yet independently sourced
   recorded-media interoperability evidence. The public ImpleoTV negative
   corpus and OpenSensorHub sample stream were inspected and contain no VMTI;
   OpenSensorHub's upstream VMTI test permits zero targets, so neither closes
   this requirement.

## Transport and live deployment

- ST 1402.2 now has checksum-bound exact accounting for all 22 active and four
  deprecated numbered requirements, including strict descriptor-loop placement
  and unique synchronous service declarations. Continue the broader ISO Systems
  audit through the deliberately scoped H.222.0 trace rather than treating the
  ST 1402 profile as whole-standard MPEG certification.
- Extend the ST 0804.4 MPEG-2 TS-over-RTP core only where deployments need it.
  Count-bounded sequence reordering plus strict RTCP Sender Report read/write
  and exact RTP/NTP synchronization are implemented. SR/RR-first compound
  validation, typed SDES CNAME, and packetizer sender counters/emission are
  implemented too. Adaptive cadence, participant/collision/BYE state, native
  elementary-stream RTP, and RTSP remain separate profiles, not implied by
  multiplexed-TS support.
- Extend the implemented bounded MPTS-to-selected-SPTS transform with a
  separate whole-multiplex rewriting API if deployments require unrelated
  programs to remain in the same output transport.
- Extend the measured low-latency fragmented-MP4/MSE reference gateway from
  complete 148–371-second real-FMV runs to multi-hour paced input, repeated
  reconnect epochs, and concurrent viewers. The first-party JSON benchmark now
  proves bounded late-join histories and 8.44–17.68× finite-file headroom on
  three pinned fixtures; prior-epoch SSE cursors reset into retained metadata
  immediately, while the Chromium-proven client rebuilds its MediaSource after
  prior-epoch media cursors fail without long-polling.
  Sub-second targets, multi-viewer fan-out, adaptive
  bitrate, and WebRTC/HLS remain production deployment profiles.
- Extend the published live-player performance and memory method to sustained
  UDP, file demux, mux, verifier, and sidecar workloads.
- The verifier PMT-validation cache is now proven bounded to one active
  identity per program across 128 changing revisions; extend the same
  measurement discipline to end-to-end throughput and resident memory.
- Add failure-injection tests for prolonged loss, reorder, jitter, and output
  backpressure. Explicit reconnect boundaries now discard and report every
  partial TS/PSI/PES/KLV structure before rediscovery.

## Developer experience and assurance

- Add external-link checking and broader executable documentation snippets to
  CI. Generated API targets, the shipped player JavaScript syntax, strict site
  build, and a deterministic Chromium interaction job for playback, seeking,
  synchronized static/SSE metadata, reconnects, diagnostics, overlays, and map
  rendering are already gated.
- Seek authoritative profile evidence before assigning any failure to the sole
  unasserted ImpleoTV PCR/PTS-drift member. Its observed H.264 timing remains
  within ST 1402 §7.4, so a corpus filename is not a conformance requirement.
- Test optional Ultralytics, ONNX Runtime, Triton, and GStreamer examples in
  dedicated dependency jobs. PyAV now has a dedicated audio/video adapter job,
  all-ST-1001-codec decode coverage, and real AAC FMV acceptance coverage.
- Add verified, version-specific interoperability results for Esri and other FMV
  consumers instead of predicting compatibility.
- Expand the first real UI/CLI tutorial captures with upgrade guides, benchmark
  methodology, and production security/deployment guidance before a stable
  `1.0` claim.

## How to resume

1. Read [development method](DEVELOPMENT.md), [conformance](CONFORMANCE.md), and
   [standards provenance](STANDARDS.md).
2. Pick one traced requirement or integration outcome and write its failing test
   first.
3. Keep core dependencies empty; adapters own optional runtimes.
4. Add the implementation, requirement-trace evidence, limitation change, and
   runnable example in the same small conventional commit.
5. Run the full release gates in [releasing](RELEASING.md) before a tag.

Do not mark the overall library “fully STANAG 4609 conformant” until every
applicable normative requirement has an auditable trace and independent fixture
coverage. Pre-1.0 releases are useful integration milestones, not certification.
