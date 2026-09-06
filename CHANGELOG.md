# Changelog

All notable changes are documented here. Releases use Semantic Versioning;
the pre-1.0 minor version and package classifier communicate API maturity.

## Unreleased

- Add a module-level architecture guide with end-to-end transport, live
  transformation, AI sidecar, player, verification, timing, and backpressure
  diagrams plus entry-point guidance.
- Add a dependency-installed Ultralytics/PyTorch CI gate that executes real CPU
  YOLO prediction and persistent ByteTrack through the first-party adapter.

## 0.3.0 - 2026-09-05

- Add recorded, simulated-live, and synthetic day/thermal player demos;
  VMTI class/confidence/overlay controls and persistent-ID trails; and native
  Ultralytics `track` mode with ByteTrack or BoT-SORT configuration.
- Add an operator-allowlisted, token-protected UDP MPEG-TS output control to the
  player for paced recorded replay or live teeing into VLC and compatible FMV
  receivers.

## 0.2.0 - 2026-09-05

- Publish matching full, partial, and future standards-support matrices on the
  README and documentation landing pages, with explicit claim boundaries and
  links to requirement-level evidence.

- Add `stanag4609-soak-live`, a versioned JSON stability harness that replays
  identified FMV at a controlled average media rate across isolated
  gateway/FFmpeg process epochs, preserves runtime-failure evidence, and
  reports pacing lag, bounded histories, throughput counts, and memory
  high-water marks; publish a two-epoch Esri Truck result.
- Recover live metadata immediately when an SSE reconnect presents an event ID
  from a prior server epoch, reject future media-fragment cursors without a
  pointless long poll, and make the browser automatically build a fresh
  MediaSource and rejoin with capped exponential backoff.
- Prove live broadcast isolation with eight concurrent HTTP viewers consuming
  identical numbered media and metadata during publication, plus three
  consecutive Chromium media-epoch rejoins.
- Extend ST 0601 producer-ground-truth validation to all occurrences of
  multi-use root tags with immutable, order-independent, tolerance-aware
  matching and occurrence-accurate verifier assurance counts.
- Bind all 22 active and four deprecated ST 1402.2 requirements to the
  checksum-pinned publication, reject misplaced metadata/registration
  descriptors even when another valid declaration exists, and reject duplicate
  synchronous metadata-service identifiers.
- Bind all ten active and eleven deprecated ST 1303.2 requirements to the
  checksum-pinned publication and support the EBytes-zero empty-array signal
  across every defined APA while rejecting contradictory parameters or data.
- Bind all seven active and eleven deprecated ST 1201.5 requirements to the
  checksum-pinned publication; cover every official Appendix A vector, signed
  quiet NaNs, strict byte-length inputs, and parent-unspecified overflow
  resolution while retaining lossless special-value decoding.
- Add an attributed OpenStreetMap/offline-grid mission map, ST 0601
  footprint-projected VMTI boxes, exact-versus-interpolated activity locations,
  a bounded grouped activity feed, and a pixel-binned interactive detection
  timeline with hover summaries and synchronized seeking to the reference
  player.
- Add a sparse server-side full-mission detection summary with bounded
  heavy-hitter label memory and exact totals, keep incremental browser detail
  at 512 samples, and make paused scrubbing fetch the effective metadata sample.
- Bind all 40 active and five deprecated ST 1204.3 requirements to the
  checksum-pinned publication; add standalone KLV, checked text, bounded XML,
  lowest-quality multi-sensor combination, and window-derivation support.
- Bind all 38 active and 28 deprecated ST 0102.12 requirements to the
  checksum-pinned publication; add the Security Metadata Universal Set with
  all Table 1 ULs and exact Universal/Local representation conversion.
- Bind all eight active and one retired ST 1607.2 requirements to the
  checksum-pinned publication and diagnose absent root security in Segment and
  Amend hierarchies.
- Bind all 20 active ST 0107.5 baseline KLV requirements to the checksum-pinned
  publication, with explicit producer-context limits for generic optional-item
  cadence.
- Bind the complete ST 1206.1 active/deprecated requirement population to its
  checksum-pinned source and add validated effective-PRF and target-RCS
  exploitation helpers for ST 0601 Item 95 SAR metadata.
- Bind all 25 active ST 0806.4 requirements to the checksum-pinned publication
  and add opt-in producer time-of-birth validation for independent or embedded
  RVT Local Sets used by ST 0601 Item 73.
- Bind all six active ST 1601.2 requirements to the checksum-pinned publication
  and close the software-verifiable Geo-Registration profile used by ST 0601
  Item 98.
- Bind all ten active ST 1602.2 requirements to the checksum-pinned publication
  and close the software-verifiable Composite Imaging profile used by ST 0601
  Item 99.
- Bind all 20 active and five retired ST 1002.3 requirements to the
  checksum-pinned publication, enforce the exact version value, and close the
  software-verifiable Range Motion Imagery profile used by ST 0601 Item 97.

## 0.1.0 - 2026-09-05

- Add checksum-bound exact inventory and CI enforcement for all 65 active and
  78 deprecated ST 0903.6 requirement identifiers, and remove deprecated
  identifiers from the current conformance trace.
- Add an optional first-party PyAV video source that produces model-neutral
  `FrameEnvelope` values with exact 90 kHz PTS conversion, BGR or native frame
  output, deterministic stream selection, and safe container lifecycle; use it
  in the real YOLO-to-VMTI player tutorial.
- Resolve ST 0601 Item 42's MSL/HAE datum from receiver-current frame-center
  Items 25 and 78 across sparse Report-on-Change packets; expose the typed
  result through the core API, GeoJSON, and player, and report ambiguous datum
  state through the FMV verifier.

## 0.1.0a5 - 2026-09-05

- Enforce ST 1204.3's UUID version/variant and version-1 null-MAC rules for
  ST 0601 Item 94, add explicit version 1/4/5 generators, implement the exact
  namespace-free Appendix A.1 multi-UUID algorithm against normative Table 13,
  and prove the inclusive 30-second MIIS cadence through ST 0902.
- Pin and exercise ImpleoTV's public 21-file negative STANAG 4609 corpus, with
  authenticated member identities and 20 fault-specific verifier assertions;
  add conservative PCR-bracketed PAT/PMT blackout proof and DVB SDT CRC
  diagnostics, while retaining the in-bound PCR/PTS-drift member as a
  documented non-claim.
- Add a first-party JSON live-player benchmark, an explicit per-call input
  bound, fixed-window 100,000-event/10,000-fragment stress tests, and measured
  baselines across all three checksum-pinned public FMV recordings.
- Add a bounded low-latency reference-player gateway that incrementally fans
  live MPEG-TS into pure-Python ST 0601/VMTI state and FFmpeg fragmented MP4,
  with numbered MSE fragments, resumable SSE, stdin CLI input, explicit
  history-loss/backpressure semantics, and real-Chromium acceptance coverage.
- Exercise the reference player in real headless Chromium using deterministic
  local media and metadata, covering playback, seeking, synchronized fields,
  diagnostics, VMTI and map canvases, and SSE restart timing in dedicated CI.
- Reject both ST 1002.3 Table 2 retired-tag ranges (`2-10` and `51-54`) on
  decode and encode while retaining adjacent non-retired extension items.
- Resolve omitted ST 1002 SPRM row and column coordinates independently to
  the standard-mandated image center through an explicit frame-dimensions API,
  including decoded ST 0601 Item 97 values.
- Gate the shipped reference-player JavaScript with Node's parser in CI, so a
  parse-time browser failure cannot pass the existing HTML and timeline tests.
- Enforce ST 1601.2 uncertainty-to-geometry relationships for ST 0601 Item 98:
  pixel uncertainty now requires a four-row correspondence-point array, and
  geographic uncertainty requires latitude/longitude plus the matching
  three-row or six-row elevation form on both encode and decode.
- Bound live verifier PMT-validation memory to the most recently validated PMT
  identity per program, with a 128-revision churn regression, and acquire the
  exact official ST 0801.6 edition conditionally referenced by MISP-2019.1.
- Inventory all 86 active and 34 deprecated MISP-2019.1 requirement
  identifiers against the checksum-bound adopted source, and enforce the
  MISP-2015.1-49 rule that security metadata use only one synchronous or
  asynchronous MPEG-2 TS carriage mechanism per program.
- Add strict RFC 3550 RTCP framing, Sender/Receiver Reports, signed reception
  blocks, SDES CNAME and compound validation/read/write; packetizer uint32
  sender counters and SR+CNAME emission; plus exact wrap-safe RTP/NTP mapping
  for synchronizing separate ST 0804 imagery and metadata streams.
- Add a count-bounded RTP sequence reorder buffer with wrap-aware contiguous
  release, duplicate/late rejection, definite gap reporting under pressure or
  flush, SSRC session locking, and zero-copy handoff to the MP2T receiver.
- Add dependency-free, bounded H.262, AVC, and HEVC sequence-property
  inspection for dimensions, display ratio/frame rate, scan signalling,
  chroma/bit depth, profile/level, and adopted MISP codec checks, including
  HEVC VUI and bounded traversal of optional SPS structures.
- Add MISB ST 0604.6 embedded video timestamp read/write support for H.262,
  AVC, and HEVC, including microsecond/nanosecond payloads, ST 0603 Time Status,
  bounded Annex-B extraction, exact timestamp-to-access-unit association, and
  verifier missing/duplicate/unassociated coverage diagnostics.
- Add MISB ST 0804.4 / RFC 2250 MPEG-2 Transport Stream over RTP parsing and
  packetization, including complete RTP v2 header handling, 90 kHz clock and
  discontinuity semantics, bounded MP2T payload validation, sequence-loss and
  timestamp diagnostics, SSRC session locking, and late-packet protection.
- Add explicit, observable live-session reset APIs across the MPEG-TS demuxer,
  metadata decoder, and in-transit transformer, preventing partial TS, PSI,
  PES, KLV, Metadata AU, sequence, or topology state from crossing a reconnect.
- Retain the exact ST 0601 validation context on decoded metadata events and
  report per-service birth-time, IMAP-precision, and VMTI external-assurance
  counts in JSON, text, and HTML verifier output.
- Resolve typed ST 1601 Geo-Registration results at root or full ST 1607 MSID
  paths, retaining independent parallel Amend results across Report-on-Change
  packets and expiring them at the standard refresh boundary.
- Apply unique ST 1202 transformations in the mandatory Chipping,
  Child-Parent, Default Pixel-to-Image, and Optical order, with automatic
  reverse-order inverse execution for ground-to-image projection.
- Construct ST 1202 Chipping, centered Digital Zoom, and CSM default
  pixel-to-image transformations directly from their standard-defined physical
  parameters, with equation-based validation and dedicated API documentation.
- Gate the optional PyAV backend in CI with native decoding for every ST 1001
  codec profile and add real AAC FMV demux, timestamp, reconstruction, and PCM
  decode acceptance coverage.
- Add an objective-level completion audit and require every real Day/Night FMV
  player sample to expose synchronized timestamp, sensor, platform, frame-center,
  target-coordinate, and map-role telemetry.

## 0.1.0a4 - 2026-09-04

- Add bounded program selection for multi-program live inputs, selected-only
  PES/clock/metadata output, source-PMT PID identity, and atomic versioned PAT
  reassociation while retaining elementary-stream continuity.
- Assemble bounded multi-section PAT versions atomically, expose complete PAT
  cycles on demux events, support multiple program-map sections sharing a PID,
  and enforce H.222.0's zero-numbered single-section PMT rule.
- Reject CRC-valid PSI with invalid fixed/reserved bits, repeated PAT program
  numbers, or non-`0xFF` bytes after section stuffing begins.
- Apply PMT stream-route updates transactionally: compatible retained PIDs may
  finish an in-flight PES, while a rejected removal or redefinition leaves the
  complete previous routing topology active.
- Add dependency-free ST 1002.3 plane fitting, subtraction, and reverse
  reconstruction with one-based coordinates and unknown-range masking.
- Enforce ST 1602-04 Z-order uniqueness across report-on-change Segment
  siblings and surface violations in FMV verification reports.
- Enforce ST 1602.1-10 by requiring a direct typed ST 1204 MIIS Core Identifier
  in every child of a represented multi-sensor composite stream.
- Resolve an omitted ST 1602 Item 1 from the immediate parent timestamp while
  preserving an explicit source/process timestamp for synchronized consumers.
- Enforce ST 0902.8 Note 1's Item 75/104 exclusive-OR across distributed
  report-on-change packets, including the inclusive 30-second boundary, expiry,
  ZLI clearing, validator reset, and an MPEG-TS-to-verifier report regression.
- Let AI sidecar detections carry an optional typed ST 0903 absolute Location;
  the VMTI emitter now preserves its coordinates and uncertainty as VTarget
  Item 17 for player, GIS, and downstream FMV consumers.
- Replace illustrative AI overlays with actual YOLO11n vehicle inference over
  the checksum-pinned Esri Truck FMV, flowing every prediction through the
  public sidecar and ST 0903 VMTI encode/decode path before browser rendering.
- Add an opt-in, media-timed Server-Sent Events feed to the reference player,
  including current-state replay, bounded heartbeats and browser history,
  seek/pause/stall/rate synchronization, and tested HTTP error handling.

## 0.1.0a3 - 2026-09-04

- Add reproducible real-fixture CLI output plus browser-captured dashboard and
  verifier screenshots to the inspection and web-client tutorials.
- Add typed ST 0102 security classifications and caller-supplied enforcement
  for exact classification, classifying country, SCI/SHI, caveats, and required
  release/object countries across ST 0902 validators, the FMV verifier, and CLI.
- Add a versioned, searchable API-reference section generated from public
  docstrings for KLV, MISB metadata, transport, verification, AI sidecars,
  media exports, and the reference player, with strict target-resolution tests.
- Add explicit MISP/UTC conversion using ST 0601 Items 136 and 137, exact raw
  Item 2 access, correct player/GeoJSON/verifier time-scale labels, verifier
  Report-on-Change UTC evidence, and standards-safe UTC-to-MISP conversion in
  the first-party AI/VMTI producer.
- Encode the mandatory ST 0601 `Out of Range` sentinel automatically when a
  producer supplies a numeric measurement outside any applicable fixed mapped
  field domain, without clipping or weakening unrelated range validation.

## 0.1.0a2 - 2026-09-04

- Add five task-oriented tutorials covering real-fixture inspection, raw
  video/CSV FMV creation, parallel AI-to-VMTI emission, a custom video/map/feed
  dashboard, and live transform fan-out, plus an explicit continuation roadmap.
- Add an executable dependency-free web dashboard example built on the public
  player-asset boundary and browser-verify it against the daylight FMV fixture.
- Add an event-aware ST 0601 context provider to live metadata decoding and the
  FMV verifier, making producer-known time-of-birth, variable-IMAP precision,
  and embedded-VMTI facts enforceable per packet.
- Request a 20 ms PCR period when FFmpeg remuxes ordinary media for CSV/KLV
  injection, keeping generated FMV inside the ST 1402 100 ms interval.
- Add an exact, bounded incremental asynchronous metadata STD model with
  cross-batch occupancy, continuous output leakage, live watermarks, immediate
  diagnostics, finite-model parity, PCR-window stream validation, and optional
  per-program/PID FMV verifier configuration.
- Add conservative, bounded ST 1402-12 synchronous-metadata decoder-delay
  auditing with PCR-bracketed delay ranges and FMV verifier diagnostics.
- Add a typed H.222.0 metadata STD descriptor with exact physical-unit
  construction and direct synchronous-stream integration.
- Decode ST 0601 coded fields into integer-compatible enumerations, flags, and
  legacy weapon packs, and enforce the exact Item 62 Laser PRF digit profile.
- Render those coded platform/sensor states in the reference player with stable
  numeric values, operator labels, named flags, and decoded weapon components.
- Add a dependency-free, bounded JSON-over-HTTP inference adapter with typed
  model-schema hooks and a complete BYO-service example.
- Add thread-safe, bounded per-stage inference counters and monotonic latency
  metrics for successes, failures, timeouts, cancellations, and in-flight work.
- Keep reference-player telemetry and geospatial labels readable across desktop
  and narrow browser layouts.
- Surface per-sample metadata diagnostics in the reference player, keep future
  telemetry hidden before its timestamp, and distinguish empty, malformed, and
  unavailable timelines.
- Restrict production publishing to version tags and approval by the repository
  owner, and refresh artifact actions to their Node.js 24 releases.

## 0.1.0a1 - 2026-09-04

- Add the official Esri `Truck.ts` H.264/AAC/synchronous-KLVA fixture with
  checksum-verified ZIP extraction and negative conformance assertions.
- Prepare a protected publishing workflow, cross-version CI, PyPI
  discovery metadata, and versioned Read the Docs hosting.
- Audit the SMPTE RP 217:2001 transport-stream branch and reject asynchronous
  KLVA PES packets with an unbounded length or asserted ESCR flag.
- Catalogue locally validated STANAG 4609 Edition 5, MISP-2019.1,
  MISP-2023.2, SMPTE ST 336:2017, and SMPTE RP 217:2001 source documents.
- Add bounded constant-rate TS packet shaping with exact rational output slots,
  null-packet idle fill, and H.222.0 byte-position-aware retained PCR restamping.
- Add a drift-free live PCR writer scheduler that derives clock samples from
  actual output time and exposes skipped slots and noncompliant late gaps.
- Add per-elementary-stream ST 1402 PTS cadence validation with exact 0.7-second
  boundaries, rollover/reordering handling, discontinuity resets, and verifier
  integration.
- Add explicit ST 0903 VMTI frame/parent validation context for timestamp,
  target-count, frame-dimension, differing-image-source, and stale-offset
  requirements.
- Enforce ST 0903 two-corner and single-interior-area BoundarySeries geometry
  with exact, antimeridian-aware topology and a configurable resource bound.
- Add a bounded incremental FMV verifier and CLI with human and JSON reports,
  positive/negative findings, stream inventory, source context, configurable
  profile policy, and real public-stream integration coverage.
- Add streaming, atomic GeoJSON-sequence export for reconstructed ST 0601
  sensor, frame-center, target, and frame-footprint geometry.
- Add a synchronized, dependency-free local geospatial map to the reference
  player alongside its VMTI video overlay and decoded field panel.
- Add an optional first-party Ultralytics YOLO adapter for strict frame-bounded
  boxes, labels, confidence, algorithm attribution, and stable tracker IDs.
- Add an optional ONNX Runtime session adapter with strict named-input checks
  and explicit model-specific preprocessing/postprocessing hooks.
- Add an optional NVIDIA Triton HTTP/gRPC AsyncIO adapter with request IDs,
  timeout-compatible cancellation, and explicit tensor/result hooks.
- Add a high-level inference-result-to-VMTI emitter that preserves correlated
  ST 0601 parents or creates a minimal synchronous parent for media-only input.
- Add ST 1402-compliant UDP datagram packetization and receiver validation with
  a seven-TS-packet default and strict partial/sync/payload bounds.
- Add ST 1402-02 PAT/PMT recurrence validation with exact strict-boundary,
  initial-acquisition, per-program, reconfiguration, and multi-section checks.
- Add a drift-free ST 1402-02 PAT/PMT writer scheduler with an eight-Hz default
  and observable late polls, skipped repetitions, and mandatory-gap failures.
- Enforce asynchronous ST 1402 KLV alignment at every PES boundary and let the
  high-level muxer span one validated KLV item across bounded PES packets.
- Add structured ST 1402 metadata PMT conformance reports for same-program
  video, carriage types, `KLVA` registration, and synchronous descriptors.
- Integrate the exact ST 1402 PAT/PMT scheduler into timed live-transform calls,
  including idle polling, early source-table suppression, and batch metrics.
- Add bounded streaming MPEG-1/2 Layer II and MPEG-2 AAC-LC frame
  reconstruction with exact offsets, channels, samples, and durations.
- Add an optional direct PyAV/FFmpeg audio decoder with actual codec-context
  coverage for all three ST 1001 formats and explicit flush/lifecycle metrics.
- Add per-PID audio PES timing that honors first-access-unit PTS semantics,
  split frames, rollover, discontinuities, and exact rational sample duration.
- Add exact, program-aware ST 1402 PCR cadence auditing with 100 ms boundary,
  rollover, shared-PID, PID-change, discontinuity, and regression handling.
- Add deterministic adaptation-only PCR construction on a muxer's declared
  clock PID without consuming elementary-stream payload continuity counters.
- Render ST 0903 VMask polygon contours and compact row-major run masks in the
  synchronized reference player, retaining JSON-ready mask geometry for apps.

- Added normalized ST 0903 target geometry to player timelines and synchronized
  browser overlays for VMTI boxes, centroids, labels, confidence, and status.
- Added streaming and atomic ST 0601-to-ArcGIS CSV sidecar export with
  per-stream Report-on-Change reconstruction and a dedicated CLI.
- Added bounded ST 0903.6 cross-frame target lifecycle state with transition,
  missing-status, terminal-ID-reuse, live-join, and partial-list semantics.
- Made ST 0903 VMTI Item 13 a typed, structurally validated ST 1204 Core
  Identifier while retaining validated binary input for wire-oriented callers.
- Added an atomic first-party ArcGIS FMV CSV multiplexer that losslessly remuxes
  video/audio with FFmpeg, adds repeated synchronous KLVA signalling, aligns
  microsecond metadata to rollover-safe video PTS, and preserves media PES.
- Added MISB ST 1001.1 audio codec-profile validation, typed Layer II and AAC-LC
  ADTS frame-header inspection, and codec identity on live audio PES events.
- Added deterministic 33-bit PTS epoch unwrapping with rollover, reordering,
  ambiguity handling, and a forward live-timeline watermark.
- Added bounded per-program synchronization of decoded frame envelopes with
  synchronous KLV relevance PTS and absolute MISB Precision Time Stamps.
- Added bounded async decoded-frame queues with explicit overload policies,
  per-enqueue drop identity, and cumulative accepted/drop observability.
- Added typed ST 0903.6 VChip Local Sets, including constrained media types,
  image IRIs/embedded bytes, and VTarget single/multi-chip integration.
- Added typed ST 0903.6 VMask pixel contours and BER-framed run masks with
  official vectors, parent-frame bounds, and clockwise geometry validation.
- Added typed ST 0903.6 Location, Velocity, and Acceleration DLPs, bounded
  Location/Boundary Series framing, and VTarget absolute-location/contour fields.
- Added the typed ST 0903.6 VTracker Local Set and VTarget Item 104 integration,
  including UUIDs, observation times, tracks, motion, and algorithm references.
- Added typed ST 0903.6 VTarget parent-relative geospatial Items 10-16 with
  official IMAP vectors and standalone-versus-embedded context validation.
- Added first-PMT augmentation for introducing a fully signalled synchronous or
  asynchronous KLVA PID into a media-only live MPEG-2 transport stream.
- Added an FFmpeg-backed local web reference player and a reusable JSON timeline
  scanner synchronized to video PTS, including geospatial/state field panels.
- Added opt-out Metadata AU sequence validation for diagnostic consumers while
  keeping strict validation as the default.
- Completed ST 0902.8 Table 1 requirement tracking for checksum, timestamp, and
  UAS Local Set version; added finite-stream finalization, reset, malformed-field
  diagnostics, and explicit ST 0102 context policy for SCI/SHI, Caveats, and
  Releasing Instructions.
- Added bounded ST 0601/ST 0107 Report-on-Change receiver-state reconstruction
  with inclusive refresh expiry, immediate ZLI clearing, multi-use ZLI
  rejection, and sparse-state integration in the reference player.
- Added identity-aware ST 0601 Items 121/128 wavelength-table reconstruction,
  including independently expiring distributed records and validation of
  predefined, reserved, undefined, and duplicate-name references.
- Added identity-aware ST 0601 Items 138/139 payload-table reconstruction with
  stable table generations, sequential completeness, per-record expiry, active
  bit-reference validation, ZLI clearing, and bounded atomic updates.
- Added identity-aware ST 0601 Item 140 Weapons Stores reconstruction keyed by
  four-part physical address, with distributed merging, per-record expiry,
  replacement updates, ZLI clearing, and bounded atomic updates.
- Added identity-aware ST 0601 Item 141 Waypoint List reconstruction with
  independent record expiry, transient distributed-reorder visibility,
  cancellation/history views, historical-order validation, and bounded state.
- Added bounded ST 1607 Segment/Amend tree reconstruction with MSID lineage,
  per-level Report-on-Change state, nested union/override evaluation, persistent
  Amend deletions, branch expiry, and atomic identity/limit enforcement.
- Added context-correct ST 1607 child Security Local Sets, inherited effective
  security views, and structured validation of Items 12/13-only country
  overrides and prohibited root-security deletion.
- Added static ST 0902 minimum-profile validation for reconstructed receiver
  views and ST 1607 policy checks at Amend roots and terminal Segment unions.

- Added strict-versus-preserve ST 0601 field decoding with structured issues.
- Added typed ST 0601.19 Items 26-47 with official mapping vectors, coded
  value validation, and exact Off-Earth handling for corner/target locations.
- Added typed ST 0601.19 Items 49-64, including bounded call signs and exact
  Out-of-Range handling for legacy platform-attitude fields.
- Added typed ST 0601.19 alternate-platform, event-time, operational-mode,
  ellipsoid-height, and sensor-velocity Items 67-72 and 75-80.
- Added typed ST 0601.19 full corner-coordinate and platform-angle Items 82-93
  with exact Off-Earth and Out-of-Range identities.
- Added typed ST 0601.19 Items 105-114 with bounded text, variable-width
  integers, and per-item variable-length IMAP limits.
- Added typed ST 0601.19 sensor-rate, storage, navigation, platform-status,
  and sensor-control Items 117-120 and 123-126.
- Added typed ST 0601.19 Items 129 and 131-137, including minimal-width signed
  integers and variable-width take-off timestamps.
- Added a typed ST 1204.3 MIIS Core Identifier codec and ST 0601 Item 94 bridge.
- Added stateful ST 0902.8 minimum-item cadence validation with alternative
  fields, ZLI diagnostics, and Security/MIIS stage policies.
- Added a typed ST 0102.12 Security Local Set codec for all current local tags,
  including the ST 0601 Item 48 and ST 0902 profile bridges.
- Added the typed ST 0601 Item 81 Image Horizon Pixel Pack with truncatable
  WGS-84 endpoints and explicit error-indicator preservation.
- Added the typed ST 0601 Item 127 Sensor Frame Rate Pack with exact BER-OID
  ratios and canonical default-denominator truncation.
- Added typed ST 0601 Items 115-116 control-command packs, including repeated
  Item 115 instances, optional command timestamps, and acknowledgement lists.
- Added typed ST 0601 Item 121 Active Wavelength identifiers and Item 122
  country-code VLPs with explicit Unknown and truncation semantics.
- Added typed ST 0601 Item 128 custom wavelength records with nested VLP/FLP
  framing, exact IMAPB bounds, and duplicate identifier/name validation.
- Added typed ST 0601 Item 130 take-off and recovery locations with WGS-84
  IMAPB mappings, optional HAE, explicit Unknown, and round-trip truncation.
- Added typed ST 0601 Items 138-139 distributed payload-table records and
  active-payload bit sets, with sequential-ID and bounds validation.
- Added typed ST 0601 Item 140 Weapons Stores records with physical addresses,
  general status, engagement flags, reserved-bit checks, and UTF-8 types.
- Added typed ST 0601 Item 141 Waypoint List records with distributed-list
  semantics, optional info/location fields, and official-vector coverage.
- Added typed ST 0601 Item 142 View Domain pairs with variable IMAP precision,
  explicit unknowns, end truncation, and circular end-angle helpers.
- Added typed ST 0601 Item 143 Metadata Substream IDs in local and UUID forms,
  including enforcement of the standard's root-level prohibition.
- Added recursive ST 1607.2 Segment/Amend Local Sets for ST 0601 Items 100-101,
  including required MSIDs, hierarchy rules, unknown TLV preservation, and
  explicit Amend deletion semantics.
- Added a bounded ST 1010.3 SDCC-FLP codec with Mode 1/2 Parse Control,
  IEEE/IMAP values, full and sparse upper-triangle matrices, and ST 0601 Item
  102 Refined Source List ordering for repeated packs.
- Bound the complete ST 1010.3 population of 11 active and two deprecated
  requirements to the acquired standard digest and executable Item 102 evidence.
- Added a typed ST 0806.4 RVT codec for independent CRC-protected packets and
  embedded values, all Items 1-21, repeatable POI/AOI/User Defined subordinate
  sets, and the ST 0601 Item 73 bridge.
- Added a bounded ST 1303.2 MDAP codec with Natural, IMAP, Boolean, biased
  BER-OID unsigned, and ordered run-length algorithms, including byte-exact
  official vectors and lazy RLE cell access.
- Added the official ST 1202.3 publication to the local non-redistributed
  standards cache and corrected the ST 1303.2 manifest metadata.
- Added a typed ST 1202.3 Generalized Transformation codec with lossless
  IEEE-width preservation, projective forward/inverse evaluation, and strict
  ST 1010 Mode 2 Refined Source List processing.
- Added typed ST 1002.3 Range Image packets, Section Data VLPs, nested ST 1202
  transformations and ST 1303 arrays, CRC-16-CCITT, and the ST 0601 Item 97
  bridge.
- Added typed ST 1601.2 Geo-Registration metadata with tie-point MDAPs,
  binary16 elevation arrays, heterogeneous IMAP uncertainty rows, UUIDs, and
  the ST 0601 Item 98 bridge.
- Added typed ST 1602.2 Composite Imaging geometry, active-area helpers,
  transparency and Z-order fields, and the ST 0601 Item 99 bridge.
- Added all ST 1206.1 SAR Motion Imagery items, standalone/embedded framing,
  radiometric ST 1303 polynomial evaluation, and the ST 0601 Item 95 bridge.
- Added typed ST 0903.6 Algorithm, Ontology, VObject, and VFeature Local Sets,
  strict packet-local ID references, and ontology-backed sidecar label emission.
- Added the checksum-validated official ST 1607.2 publication to the local,
  non-redistributed standards manifest.
- Fixed IMAP normal-value decode/encode stability at quantization boundaries.
- Added reproducible full-file integration tests for two public FMV streams.
- Added a pixel-bounded, class-stacked detection timeline with logarithmic
  density, exact hover summaries, and pointer, range, and keyboard scrubbing.
- Added first-party AI frame/detection contracts, composable inference graphs,
  and a standards-aware ST 0903 pixel detection bridge.
- Added exact PCR/OPCR parsing, program-aware clock events, and full-file no-op
  transform fingerprints for the public daylight FMV fixture.
- Preserved source TS payload/adaptation layouts and every PCR/OPCR observation
  when remuxing unchanged video, audio, or data PES.

### Added

- Strict BER length and BER-OID primitives.
- Incremental Universal KLV and lossless Local Set parsing.
- Initial typed ST 0601 decoding, encoding, and checksum validation.
- ArcGIS FMV Multiplexer CSV adapter for the supplied development fixture.
- Enforcement of the mandatory ST 0601 timestamp/version/checksum structure
  and uniqueness for the currently supported singleton items.
- Byte-exact ST 0902.8 Annex C dynamic-packet regression vector.
- Distinct public values for MISB Unknown, Out-of-Range, Off-Earth, and
  Reserved semantics, including correct zero-length and empty-string encoding.
- Strict ST 0107 UTF-8 canonicalization checks for supported text fields.
- Bounded incremental MPEG-2 TS packet parsing with strict and recovery modes,
  adaptation/payload separation, exact byte offsets, and streaming iteration.
- Incremental PSI section reconstruction, CRC-32/MPEG-2 validation, typed PAT
  and PMT decoding, and synchronous/asynchronous KLVA PID discovery.
- Bounded per-PID PES reconstruction with exact packet lengths, 33-bit PTS/DTS
  decoding, unbounded video-PES boundary handling, and live-join behavior.
- Exact ST 1201.5 IMAPA length selection and IMAPB forward/reverse mapping,
  including zero offsets and lossless special/reserved code words.
- Typed variable-length IMAP support for ST 0601.19 Items 96, 103, and 104,
  validated against their official publication examples.
- Strict standalone and embedded ST 0903.6 VMTI decoding with bounded VTarget
  Series, lossless unknown tags, typed pixel fields, and lifecycle validation.
- Typed ST 0903.6 VMTI and VTarget encoding for pixel-space AI detections,
  including BER-OID target IDs, automatic target counts, lifecycle validation,
  explicit raw extensions, and encoder-owned standalone checksums.
- Automatic typed ST 0903.6 decoding and lossless re-encoding through ST 0601
  Item 74.
- Program-aware incremental transport demuxing that discovers PAT/PMT updates,
  classifies video, audio, KLVA, and other streams, and emits bounded PES events.
- Deterministic PAT/PMT/PES construction, 188-byte TS packetization with
  per-PID continuity, and asynchronous KLVA insertion.
- Synchronous KLVA Metadata AU cells with PTS, PMT descriptor helpers,
  sequence counters, and bounded complete/first/middle/last fragmentation.
- Incremental metadata stream reconstruction from asynchronous KLV fragments or
  synchronous AU cells into timed, typed ST 0601/ST 0903 events.
- Lossless ST 0601 packet updates with explicit add/replace/delete operations,
  raw extension values, mandatory-structure validation, and checksum repair.
- Immutable timed-KLV events and bounded processor chains with explicit
  pass/drop/replace/inject decisions, strict output parsing, typed replacement
  metadata, and deterministic multi-processor composition.
- Pull-driven single-program live TS transformation that preserves unchanged
  video/audio PES bytes and descriptors, processes sync/async KLV at its source
  timing, remuxes it, and exposes parallel typed metadata batches.
