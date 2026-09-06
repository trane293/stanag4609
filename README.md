# stanag4609

An MIT-licensed, pure-Python toolkit for live and recorded STANAG 4609 motion
imagery and MISB KLV metadata.

The project is building the missing open foundation for applications that need
to inspect FMV, visualize live geospatial metadata, preserve audio and unknown
streams, add or modify KLV in transit, encode AI detections as ST 0903 VMTI,
and send the same timed metadata to independent GIS or analytics consumers.

> **Status: alpha.** The implemented protocol slices are strict, typed,
> lossless, and tested, but this release does not yet claim complete STANAG
> 4609, ST 0601.19, ST 0902.8, ST 0903.6, or ST 1001.1 conformance. See
> [conformance](https://stanag4609.readthedocs.io/en/latest/CONFORMANCE/) and
> [known limitations](https://stanag4609.readthedocs.io/en/latest/LIMITATIONS/).

## Why this library

- Pure Python and zero dependencies in the core package.
- Incremental APIs for files, sockets, pipes, and arbitrarily chunked live data.
- Standards-strict by default, with explicit lossless diagnostics for imperfect
  deployed streams.
- Immutable raw wire data remains available; unknown fields are not discarded.
- Video and every audio PES pass through metadata transforms without decoding
  or transcoding.
- Bounded resource limits and explicit processor decisions are part of the API.
- AI inference is a first-party integration boundary, not an application-only
  afterthought.

## Implemented today

- BER length and BER-OID codecs, streaming Universal KLV framing, lossless Local
  Sets, and ST 0601 running-sum checksums.
- Exact ST 1201 IMAPA/IMAPB mapping, including special and reserved code words.
- A growing audited ST 0601 codec with typed common fields, mandatory-structure
  validation, canonical encoding, and lossless field updates.
- ST 1010.3 uncertainty matrices and ST 0601 Item 102, including Mode 2
  IEEE/IMAP values, sparse correlations, and ordered Refined Source Lists.
- ST 0806.4 independent and embedded RVT Local Sets with source-bound complete
  requirement accounting, CRC-32, time-of-birth validation, POI, AOI, typed
  user data, and the ST 0601 Item 73 bridge.
- ST 1202.3 generalized projective transformations with IEEE binary16/32/64
  coefficients, forward/inverse coordinate mapping, formula-defined image
  transforms, standard-ordered multi-transform execution, and ST 1010
  uncertainty.
- ST 1206.1 SAR motion imagery metadata with source-bound requirement accounting,
  typed collection/image geometry, effective PRF and radiometric RCS evaluation,
  and the ST 0601 Item 95 bridge.
- ST 1303.2 Multi-Dimensional Array Packs with Natural, IMAP, Boolean,
  BER-OID unsigned, and ordered run-length representations.
- ST 1002.3 range imagery with source-bound complete requirement accounting,
  standalone/embedded CRC handling, SPRMs, dimension-aware center defaults for
  omitted SPRM coordinates, sectioned ST 1303 arrays, dependency-free plane
  subtraction/reconstruction, and the ST 0601 Item 97 bridge.
- ST 1601.2 geo-registration with source-bound complete requirement accounting,
  typed tie-point arrays, UUID configuration identity, heterogeneous uncertainty
  mappings, and the ST 0601 Item 98 bridge.
- ST 1602.2 composite imaging with source-bound complete requirement accounting,
  source/AOI/sub-image geometry, transparency, Z-order, the ST 0601 Item 99 bridge, and stateful sibling
  Z-order, multi-sensor child-identifier validation, and effective parent/child
  timestamp resolution.
- ST 1204.3 MIIS Core Identifiers with source-bound complete requirement
  accounting, binary and standalone Universal KLV, checked human-readable text,
  bounded XML, UUID generation, multi-sensor combination, window derivation,
  and the ST 0601 Item 94 bridge.
- ST 0903.6 standalone/embedded VMTI, VTarget packs, frame-bounded pixel
  centroids and bounding boxes,
  lifecycle status, and packet-scoped Algorithm/Ontology/VObject/VFeature
  metadata for standards-native AI labels and confidence, typed ST 1204 MIIS
  identity, optional absolute target locations from AI/geolocation stages, plus
  typed VChip image references/embeds, VMask polygon/run-length segmentations,
  and absolute Location/Boundary Series and embedded-parent offset geospatial
  geometry with VTracker UUIDs, timelines, track history, velocity,
  acceleration, and algorithm attribution, plus bounded cross-frame
  lifecycle/ID-reuse checks.
- MPEG-2 TS framing; CRC-checked, atomically activated multi-section PAT cycles;
  strict PSI fixed/reserved bits and stuffing; shared PMT-PID discovery; PES and
  PTS/DTS reconstruction; and classified video, audio, KLVA metadata, and other
  stream events.
- Strict ST 1402 PAT/PMT recurrence monitoring on exact PCR-derived, recorded,
  or live monotonic timelines, including multi-section table cycles.
- ST 1402 UDP packet grouping and validation with the recommended seven-packet
  Ethernet-MTU default, strict integer packet boundaries, and bounded payloads.
- ST 0804/RFC 2250 MPEG-2 TS over RTP with complete RTP v2 header parsing,
  90 kHz timestamps, discontinuity markers, sequence wrap/loss reporting,
  SSRC session locking, late/duplicate protection, and bounded packet
  reordering for live receivers; plus RFC 3550 RTCP Sender Reports and exact
  cross-stream RTP/NTP synchronization with SR/RR-first compound validation,
  SDES CNAME, and sender counters/emission.
- ST 1001.1 audio profile validation for MPEG-1 Layer II, MPEG-2 Layer II, and
  MPEG-2 AAC-LC, with Layer II/ADTS header inspection and channel configuration.
- Exact 27 MHz PCR/OPCR decoding and program-aware clock events. Unchanged
  media PES retain their source adaptation-field and PCR layout during remux;
  a per-program validator audits the ST 1402 100 ms PCR interval and rollover.
- ST 0604 embedded Absolute Time read/write for H.262 user data and AVC/HEVC
  unregistered SEI, including microsecond/nanosecond identifiers, ST 0603 clock
  status, bounded incremental extraction, and verifier frame-count deficits.
- Dependency-free H.262, AVC, and HEVC sequence inspection for dimensions,
  display ratio/frame rate, progressive/interlaced signalling, chroma/bit
  depth, and MISP codec profile/level diagnostics, including HEVC VUI.
- Conservative ST 1402-12 synchronous-metadata delay auditing that uses PCR
  brackets to distinguish proven violations, proven compliance, and unknowns.
- Exact finite-capture and bounded incremental metadata T-STD simulation for
  synchronous and asynchronous carriage, using descriptor leak rates, the
  fixed 512-byte transport buffer, aggregate main-buffer occupancy,
  instantaneous PTS removal or continuous output leakage, and H.222.0 PCR
  byte-time interpolation.
- Synchronous and asynchronous KLVA mux/demux, including Metadata AU
  fragmentation and boundary-correct asynchronous KLV spanning across PES.
- Bounded pass/drop/replace/inject KLV processors and a program-selecting live
  demux-process-remux transformer for single- or multi-program input.
- Dependency-free AI frame/detection contracts, nested sequential and
  bounded-parallel inference graphs, generic JSON-over-HTTP, optional
  Ultralytics YOLO, ONNX Runtime, and NVIDIA Triton AsyncIO adapters, and an
  ontology-aware AI-box-to-VMTI bridge.
- Bounded, per-program frame/KLV correlation with 33-bit PTS rollover,
  exact/latest/nearest policies, and Precision-Time-Stamp-to-frame UTC mapping.
- Thread-safe, cardinality-bounded inference health and latency snapshots for
  local, threaded, and remote graph stages.
- A graph-result-to-VMTI emitter that preserves correlated ST 0601 wire fields
  or creates a minimal timed parent for media-only sources.
- A synchronized reference overlay for VMTI boxes, centroids, polygon contours,
  compact bit masks, labels, confidence, lifecycle status, and geospatial state.
- Bounded async decoded-frame queues with explicit backpressure, drop-oldest,
  drop-newest, and fail-fast overload behavior plus observable loss counters.
- Per-PID streaming Layer II/AAC-LC compressed-frame reconstruction and an
  optional direct PyAV/FFmpeg decoder covering all three ST 1001 codecs.
- Bounded ST 0601 Report-on-Change receiver state with 30-second expiry,
  immediate zero-length clearing, malformed-update isolation, and sparse-stream
  reconstruction in the reference player.
- Atomic MPEG-TS creation from ordinary video plus ArcGIS FMV Multiplexer CSV,
  with lossless FFmpeg media remux, repeated KLVA signalling, and rollover-safe
  conversion from microsecond timestamps to synchronous 90 kHz PTS.
- Streaming line-delimited GeoJSON fan-out with Report-on-Change reconstruction,
  sensor/frame-center/target points, full or offset-derived image footprints,
  and antimeridian-safe longitude handling.
- An incremental FMV verifier and CLI with human/JSON reports covering transport
  structure and continuity, stream inventory, KLV carriage, ST 1402 declarations
  and PCR cadence, exact bounded-window metadata T-STD occupancy, ST 0601
  diagnostics, ST 0902 missing requirements, ST 0903
  lifecycle inventory, ST 1001 compressed audio, and bounded per-service ST 0601
  tag coverage.

## Install

Install the public alpha from PyPI:

```console
python -m pip install stanag4609
```

For local development:

```console
git clone git@github.com:trane293/stanag4609.git
cd stanag4609
pyenv install 3.10.13  # omit when already installed
pyenv virtualenv 3.10.13 stanag4609-dev
pyenv local stanag4609-dev
python -m pip install -e '.[dev]'
```

The committed `.python-version` selects the local environment. Packaging uses
PEP 517/518 and PEP 621 metadata with Hatchling and produces a universal wheel.

## Documentation

The documentation is a searchable, light/dark static site with task-oriented
guides for KLV, live transforms, BYOAI/VMTI, web clients, the CLI, FFmpeg and
GStreamer boundaries, audio, GIS/OSINT interoperability, and conformance.

```console
python -m pip install -e '.[docs]'
mkdocs serve
```

Start at the [documentation home](https://stanag4609.readthedocs.io/). The
[`Python API reference`](https://stanag4609.readthedocs.io/en/latest/api/) is
generated from the typed public docstrings during every strict documentation
build. The
[`documentation roadmap`](https://stanag4609.readthedocs.io/en/latest/DOCUMENTATION/)
records the tutorial and quality gates that must be met before the first stable
release.
The site is configured for versioned hosting on Read the Docs; PyPI will show
this README and link to the full documentation rather than hosting the complete
site itself. Maintainer setup and the release gates are in the
[`release process`](https://stanag4609.readthedocs.io/en/latest/RELEASING/).

End-to-end tutorials:

- [inspect and debug a real FMV recording](https://stanag4609.readthedocs.io/en/latest/tutorials/inspect_fmv/);
- [create FMV from ordinary video and metadata CSV](https://stanag4609.readthedocs.io/en/latest/tutorials/create_fmv/);
- [run parallel AI stages and emit ST 0903 VMTI](https://stanag4609.readthedocs.io/en/latest/tutorials/ai_to_vmti/);
- [build a video, map, telemetry, and activity dashboard](https://stanag4609.readthedocs.io/en/latest/tutorials/web_dashboard/);
- [transform live KLV and fan out independent consumers](https://stanag4609.readthedocs.io/en/latest/tutorials/live_fanout/).

The exact post-alpha continuation backlog is maintained in
the [roadmap](https://stanag4609.readthedocs.io/en/latest/ROADMAP/).

## Verify and debug an FMV file

```console
stanag4609-verify mission.ts
stanag4609-verify mission.ts --format json > verification.json
stanag4609-verify legacy-mission.ts --profile structural
```

The report identifies what passed, what is missing, malformed fields, transport
offsets, affected programs/PIDs, repeated issue counts, and VMTI target/lifecycle
statistics. It diagnoses impossible ST 0903 state transitions and reuse of an
identifier after `Dropped`, while treating an omitted optional detection status
as a warning. Declared MP2/AAC audio is parsed through complete compressed
frames so the report includes sample rate, channel count, samples, duration,
PTS coverage, malformed headers, and trailing truncation. Per-service ST 0601
inventories show every observed known or extension tag, how often it appears,
ZLIs, malformed values, versions, and timestamp span. An ST 0902 checklist marks
every selected minimum-item group current, missing, or overdue. Reports can be emitted as
terminal text, stable JSON, or a self-contained printable HTML file. Use the incremental
`FMVVerifier` API for upload services and live capture pipelines. The
[verifier guide](https://stanag4609.readthedocs.io/en/latest/VERIFIER/) defines current coverage, policies, exit
statuses, and known non-checked areas.

The verifier also keeps bounded state per program, metadata PID, and metadata
service for Control Commands, Wavelengths, Payloads, Weapons Stores, and
Waypoints. Cross-packet identifier/reference/order violations therefore appear
in the same report as wire-format and transport failures.

## Decode KLV incrementally

```python
from stanag4609 import KLVStreamParser, ST0601_KEY, decode_uas_local_set

parser = KLVStreamParser(key_prefix=ST0601_KEY)
for network_chunk in source:
    for packet in parser.feed(network_chunk):
        uas = decode_uas_local_set(packet)
        print(uas.value(2), uas.value(13), uas.value(14))
parser.finish()
```

For sparse live metadata, reconstruct the receiver-visible state prescribed by
ST 0601/ST 0107 Report-on-Change:

```python
from stanag4609 import ReportOnChangeState

state = ReportOnChangeState()
for packet in klv_packets:
    snapshot = state.observe(packet)
    print(snapshot.value(13), snapshot.value(14), snapshot.expired_tags)
```

The default 30-second window is inclusive. A ZLI clears a single-use value
immediately, invalid field updates leave the last valid value untouched, and
the state is bounded by the finite ST 0601 item registry.

ST 0601 permits legacy and newer representations of the same logical value in
one packet. Resolve the value applications should actually consume without
discarding the original wire fields:

```python
from stanag4609 import ST0601Semantic

height = snapshot.preferred_field(ST0601Semantic.SENSOR_HEIGHT)
if height is not None:
    print(height.tag, height.value, [field.definition.tag for field in height.ignored])

for field in snapshot.effective_fields:
    publish(field.definition.name, field.value)
```

The resolver implements the normative full-range, HAE-over-MSL, and
extended-over-restricted priority chains. `snapshot.fields` and
`uas.fields` remain lossless; `effective_fields` is the presentation/analytics
view.

`WavelengthTableState` adds identity-aware merging for distributed custom
Wavelengths List records and validates each Active Wavelength ID against the
predefined table or a current custom definition. Similar standard-specific
evaluators are used where treating a whole list as one scalar would be wrong.
`PayloadTableState` does the same for distributed Payload List records and
Active Payload bit references, while exposing completeness and missing IDs.
`WeaponsStoresState` reconstructs Item 140 fragments by their four-part
physical addresses, so status changes replace the right store while unrelated
stores retain their own refresh lifetimes:

```python
from stanag4609 import WeaponsStoresState

weapons = WeaponsStoresState()
for packet in klv_packets:
    snapshot = weapons.observe(packet)
    for address, store in snapshot.records.items():
        print(address, store.weapon_type, store.status.general_status)
```

`WaypointListState` merges Item 141 records by Waypoint ID and provides ordered
current, planned, historical, and cancelled views. Its `order_conflicts` view
intentionally exposes the temporary duplicate Prosecution Orders that the
standard permits receivers to observe while a distributed reorder is underway.

`ControlCommandState` validates the Item 115/116 lifecycle without retaining an
unbounded stream: new Command IDs increase and remain unique, repeats preserve
their original text and effective issue time, acknowledgements reference known
commands, and acknowledged commands cannot reappear.

```python
from stanag4609 import ControlCommandState

commands = ControlCommandState()
for packet in klv_packets:
    snapshot = commands.observe(packet)
    for issue in snapshot.issues:
        alert(issue.code, issue.command_ids, issue.message)
```

For ST 1607 metadata substreams, `MetadataTreeState` reconstructs every Segment
or Amend branch by its full MSID lineage and evaluates an effective metadata
view without changing the first-generation root values:

```python
from stanag4609 import MetadataTreeState

tree = MetadataTreeState()
for packet in klv_packets:
    snapshot = tree.observe(packet)
    for path in snapshot.branches:
        latitude = snapshot.effective_value(path, 13)
        longitude = snapshot.effective_value(path, 14)
        print(path, latitude, longitude)
```

Sparse values age independently at every level. Segment ZLIs reveal the parent
value, Amend deletions remain effective for their refresh lifetime, and a child
report refreshes only when its complete parent MSID lineage is present.
`snapshot.effective_security(path)` overlays a branch's permitted ST 0102
object-country Items 12 and 13 onto inherited root markings. Use
`validate_st1607_security(snapshot)` to report incomplete child country sets,
unexpected child security items, or an attempted deletion of root security.
`validate_st1607_mismms(snapshot)` applies the airborne MISP/ST 0902 minimum
profile at the root for Amend trees and to every terminal effective union for
Segment trees. For a non-hierarchical reconstructed view, use
`validate_mismms_current_state(snapshot.fields, field_issues=snapshot.issues)`
so preserve-mode decoding failures remain distinguishable from fields that
were never populated. A decoded `UASLocalSet` can be passed directly and
carries its diagnostics automatically. These static checks
are intended after the receiver has had a reporting interval to warm up, or
when finalizing a finite recording; `MISMMSValidator` remains the packet-level
cadence validator.

## Validate the ST 0902 minimum profile

```python
from stanag4609 import (
    MISMMSecurityContext,
    MISMMSValidator,
    SecurityClassification,
)

validator = MISMMSValidator(
    security_context=MISMMSecurityContext(
        expected_classification=SecurityClassification.SECRET,
        expected_classifying_country="USA",
        required_releasing_countries=frozenset({"USA", "CAN"}),
    ),
)

for klv_packet in packets:
    for issue in validator.observe(klv_packet):
        print(issue.code, issue.requirement, issue.tags)

# Required for finite recordings: reports Table 1 items that never appeared.
for issue in validator.finish():
    print(issue.code, issue.requirement, issue.tags)
```

The validator tracks alternative field groups and the inclusive 30-second
reporting window across packets. Its receiver state also enforces the Item 75
versus Item 104 sensor-altitude exclusive-OR when the alternatives arrive in
different packets, while respecting expiry and ZLI clearing. It emits
structured diagnostics for malformed or zero-length values and models ST 0102
SCI/SHI, Caveats, and Releasing Instructions as explicit mission-context
policy. It does not guess security markings.

## Process an FMV transport stream

```python
from time import monotonic

from stanag4609 import FieldDecodingMode, LiveTransportTransformer

transformer = LiveTransportTransformer(
    metadata_processors=(redact_sensitive_fields, add_vmti_detections),
    field_decoding=FieldDecodingMode.PRESERVE,
)

for chunk in live_transport_source:
    batch = transformer.feed(chunk, at=monotonic())
    output_transport.write(batch.transport)
    metadata_sidecar.write(batch.metadata)

# Keep PAT/PMT cadence alive even while the source is idle.
output_transport.write(transformer.poll_program_tables(at=monotonic()).transport)

final = transformer.finish()
output_transport.write(final.transport)
```

`STRICT` is the default and rejects malformed known fields. `PRESERVE` still
enforces KLV structure, checksums, required tags, and singleton rules, but keeps
an undecodable field's exact wire bytes and reports a `FieldDecodingIssue` so a
player can remain useful without pretending the stream is conformant.

See [the live pipeline guide](https://stanag4609.readthedocs.io/en/latest/LIVE_PIPELINE/) for an in-transit VMTI
example, scheduled program-table output, and exact remux constraints. Omitting
`at` preserves the input stream's PAT/PMT repetition behavior; supplying one
switches to the drift-free eight-Hz output scheduler.

When transformation changes packet positions, wrap output in
`TransportRateShaper`. It assigns exact constant-rate 188-byte slots, inserts
bounded PID `0x1FFF` padding for idle slots, and optionally rewrites every
retained PCR from a caller-anchored 27 MHz output clock at the H.222.0-defined
PCR-base byte position. See [transport-rate shaping](https://stanag4609.readthedocs.io/en/latest/TRANSPORT_RATE/).

Properly versioned live PMTs may add or remove streams and change a KLVA PID's
synchronous/asynchronous carriage. Retained PID continuity is preserved; an
affected KLVA change is rejected if it would discard a partial item or access
unit.

For UDP delivery, group output without changing its TS bytes:

```python
from stanag4609 import iter_udp_datagrams

for datagram in iter_udp_datagrams(remuxed_chunks):
    udp_socket.sendto(datagram, destination)
```

The default is seven 188-byte packets per payload, with a smaller integral
final datagram. See [UDP transport datagrams](https://stanag4609.readthedocs.io/en/latest/UDP_TRANSPORT/).

For ST 0804 MPEG-2 TS over RTP/UDP, add RFC 2250 headers while retaining the
same bounded seven-packet payloads:

```python
from stanag4609 import RTPMPEG2TransportPacketizer

packetizer = RTPMPEG2TransportPacketizer()
for chunk in remuxed_chunks:
    for datagram in packetizer.feed(chunk, timestamp=clock_90khz()):
        udp_socket.sendto(datagram, destination)
```

The application supplies the PCR-synchronized 90 kHz transmission clock. RTCP
Sender Reports can align separately transported video and metadata streams.
See [RTP transport](https://stanag4609.readthedocs.io/en/latest/RTP_TRANSPORT/) for receiver, reordering, and clock
synchronization behavior.

Validate live PAT/PMT acquisition cadence against ST 1402-02 independently of
the clock source used by the host application:

```python
from time import monotonic

from stanag4609 import PSICadenceValidator
from stanag4609.transport import PATEvent, PMTEvent

cadence = PSICadenceValidator()
cadence.start(at=monotonic())

for event in demuxer.feed(transport_chunk):
    now = monotonic()
    if isinstance(event, PATEvent):
        issues = tuple(
            issue
            for section in event.sections
            for issue in cadence.observe_pat(section, at=now)
        )
    elif isinstance(event, PMTEvent):
        issues = cadence.observe_pmt(event.table, at=now)
    else:
        issues = ()
    for issue in issues:
        print(issue.table, issue.program_number, issue.elapsed)

# Also call from an idle-loop timer to detect a table that never arrives.
for issue in cadence.check(at=monotonic()):
    print(issue.message)
```

Use exact `ProgramClockReference.seconds` fractions for deterministic recording
audits. The 250 ms boundary is a failure because the standard requires *more
than* four insertions per second; the exposed 125 ms interval is the standard's
recommendation. A multi-section PAT counts only after its complete cycle;
H.222.0 requires each program definition to occupy one zero-numbered PMT section.

For generated streams, `ProgramTableScheduler(muxer).poll(at=monotonic())`
emits an immediate PAT/PMT pair and then maintains the recommended exact 125 ms
schedule. Late polls expose skipped repetitions and mandatory-interval failure
without creating a misleading burst of stale tables. See
[PAT/PMT cadence](https://stanag4609.readthedocs.io/en/latest/PSI_CADENCE/).

Audit the encoded PCR time base independently for every program:

```python
from stanag4609 import PCRCadenceValidator, ProgramClockEvent

pcr_cadence = PCRCadenceValidator()
for event in demuxer.feed(transport_chunk):
    if isinstance(event, ProgramClockEvent):
        for issue in pcr_cadence.observe(event):
            print(issue.program_number, issue.elapsed, issue.message)
```

The exact 100 ms boundary passes; larger gaps and unannounced clock regressions
are diagnostics. Rollover, declared discontinuities, shared clock PIDs, and
PMT-driven PCR PID changes are handled explicitly. See
[PCR cadence](https://stanag4609.readthedocs.io/en/latest/PCR_CADENCE/).

For newly constructed streams, `muxer.mux_pcr(ProgramClockReference(...))`
emits an adaptation-only clock packet on the PMT-declared PCR PID without
advancing payload continuity. The application supplies PCR values from its
actual output schedule; the library does not pretend callback wall time is the
transport packet's decoder-arrival time.

Live writers with an authoritative output clock can use
`ProgramClockScheduler(muxer)`: `start()` anchors an encoder PCR and `poll()`
emits drift-free current clock packets at a 50 ms operational cadence. Late
polls expose skipped slots and whether the actual gap exceeded ST 1402's
inclusive 100 ms limit.

Audit each elementary stream's successive presentation timestamps as well:

```python
from stanag4609 import PESStreamEvent, PTSCadenceValidator

pts_cadence = PTSCadenceValidator()
for event in demuxer.feed(transport_chunk):
    if isinstance(event, PESStreamEvent):
        for issue in pts_cadence.observe(event):
            print(issue.pid, issue.difference, issue.message)
```

The ST 1402 §7.3 limit is 0.7 seconds. The validator handles 33-bit rollover,
presentation-order regressions, per-stream state, and declared
discontinuities. See [PTS cadence](https://stanag4609.readthedocs.io/en/latest/PTS_CADENCE/).

Audit the one-second synchronous-metadata decoder-delay limit without assuming
a constant bitrate between PCR samples:

```python
from stanag4609 import MetadataDelayValidator, PESStreamEvent, ProgramClockEvent

metadata_delay = MetadataDelayValidator()
for event in demuxer.feed(transport_chunk):
    if isinstance(event, ProgramClockEvent):
        issues = metadata_delay.observe_clock(event)
    elif isinstance(event, PESStreamEvent):
        issues = metadata_delay.observe_pes(event)
    else:
        issues = ()
    for issue in issues:
        print(issue.minimum_delay, issue.maximum_delay, issue.message)
```

The complete delay range must remain between zero and one second to count as
compliant. Ranges crossing a boundary are retained as indeterminate; missing
PCR brackets are counted as unverifiable.

Validate the metadata declaration itself before accepting or publishing a
program map:

```python
from stanag4609 import KLVCarriage, validate_st1402_metadata_program

issues = validate_st1402_metadata_program(
    pmt,
    expected_carriage={0x120: KLVCarriage.SYNCHRONOUS},
)
for issue in issues:
    print(issue.requirement, issue.elementary_pid, issue.message)
```

Supplying the expected PID lets the report diagnose a missing or misplaced
identifier that automatic discovery could not safely infer. The report checks
same-program motion imagery, carriage stream type, `KLVA` identification, and
the synchronous metadata/STD descriptor rules.

## Create FMV from video and metadata CSV

The first-party CSV multiplexer turns ordinary video, including MPEG Program
Streams, into an MPEG-2 Transport Stream with the original video/audio and a
properly signalled synchronous KLVA PID:

```console
stanag4609-mux-esri \
  "/path/to/Raw_Video.mpeg" \
  "/path/to/Raw_Metadata.csv" \
  "/path/to/output.ts"
```

FFmpeg remuxes the media without transcoding; the Python library then aligns
the first CSV timestamp to the first video PTS, encodes every row as ST 0601,
and injects it as ST 1402 synchronous metadata. Existing output files are
protected unless `--force` is given. The same workflow is available through
`multiplex_esri_fmv()` and the lower-level `inject_esri_csv_metadata()` API for
an input that is already MPEG-TS.

The supplied 149-second MPEG-2/MP2 sample has been exercised end to end: all
866 CSV records decode back from the generated KLVA stream and feed the player
timeline from 0.000 to 144.961 seconds while both media streams remain present.

The reverse path writes the same metadata as a separate ArcGIS/Esri-compatible
sidecar. It reconstructs sparse Report-on-Change values independently for each
program/PID and streams the transport through bounded buffers:

```console
stanag4609-export-esri "/path/to/input.ts" "/path/to/metadata.csv"
```

Applications can use `iter_esri_metadata_rows()` for live fan-out or
`export_esri_metadata_csv()` for an atomic file export. Special/unknown numeric
sentinels become empty cells rather than fabricated coordinates.

For map services, spatial databases, and independent analytics consumers, the
same timed stream can be exported as line-delimited GeoJSON:

```console
stanag4609-export-geojson "/path/to/input.ts" "/path/to/metadata.geojsonl"
```

Each metadata packet becomes one streaming `FeatureCollection` containing the
available sensor, frame-center, target, and image-footprint geometry. Use
`iter_geojson_feature_collections()` to publish these records directly to a
message bus or web backend while the FMV transport continues independently.
See [GeoJSON metadata streams](https://stanag4609.readthedocs.io/en/latest/GEOJSON/).

Audio stays independent of video and metadata throughout the live pipeline:

```python
from stanag4609 import StreamKind, TransportDemuxer, parse_aac_adts_header

demuxer = TransportDemuxer()
for event in demuxer.feed(transport_chunk):
    if getattr(event, "kind", None) is StreamKind.AUDIO:
        print(event.pid, event.audio_codec, event.pes.pts_seconds)
        if event.audio_codec and event.audio_codec.value == "mpeg-2-aac-lc":
            adts = parse_aac_adts_header(event.pes.payload)
            print(adts.sample_rate, adts.channel_count, adts.has_crc)
```

For PES payloads that split or combine compressed audio frames, keep one
bounded, timestamp-aware parser per audio PID:

```python
from stanag4609 import AudioPESFrameParser

parsers = {}
for event in audio_events:
    if event.audio_codec is None:
        continue
    parser = parsers.setdefault(event.pid, AudioPESFrameParser())
    for timed in parser.feed(event):
        frame = timed.frame
        print(frame.offset, timed.presentation_seconds, frame.channel_count)
```

This reconstructs MPEG-1/2 Layer II and MPEG-2 AAC-LC ADTS frames across
arbitrary chunks, honors the H.222.0 first-access-unit PTS rule at split PES
boundaries, unwraps 33-bit timestamps, and advances time with exact rational
sample durations. See
[`examples/audio_frames.py`](https://github.com/trane293/stanag4609/blob/main/examples/audio_frames.py)
for a complete TS walk.

Decode completed frames to native FFmpeg-backed PyAV audio frames without a
subprocess or probe delay:

```python
from stanag4609 import PyAVAudioDecoder

decoder = PyAVAudioDecoder(codec)
for timed in frame_parser.feed(event):
    for audio_frame in decoder.decode(timed.frame):
        audio_sink.consume(audio_frame)
```

Install with `pip install 'stanag4609[audio-pyav]'`. Keep one codec context per
audio PID and flush it at end of stream. See [audio decoding](https://stanag4609.readthedocs.io/en/latest/AUDIO/).

## Bring your own AI

```python
from stanag4609.sidecar import (
    InferenceContext,
    InferenceStage,
    Parallel,
    Sequential,
)

graph = Sequential(
    Parallel(
        InferenceStage("local-yolo", local_detector, threaded=True),
        InferenceStage("remote-triton", triton_detector, timeout_seconds=0.150),
        max_concurrency=2,
    ),
    InferenceStage("fusion", fuse_detections),
    InferenceStage("tracker", track_objects),
)

result = await graph.run(InferenceContext(frame))
```

Turn a named detector or tracker result back into synchronized ST 0601 Item 74
without hand-assembling transport context:

```python
from stanag4609.sidecar import VMTIMetadataEmitter

packet = VMTIMetadataEmitter("tracker", metadata_pid=0x120)(result)
transport_sink.write(transformer.emit_metadata(packet).transport)
```

If the frame carries correlated ST 0601, the emitter preserves its unrelated
and unknown fields while refreshing the timestamp and VMTI. For a media-only
input it creates a minimal parent on the explicitly declared KLVA PID.

Local synchronous models can run off the event loop; remote clients can use
the core `HTTPJSONAdapter` or be native async callables. Parallel outputs are
deterministic, later stages can read earlier named results, and common AI
bounding boxes convert directly to ST 0903 VMTI. Ultralytics YOLO, ONNX Runtime,
NVIDIA Triton, VMTI injection, and nested graph examples are in
[AI sidecars](https://stanag4609.readthedocs.io/en/latest/AI_SIDECARS/).

The first packaged runtime adapter is optional, so the core stays dependency
free:

```console
pip install 'stanag4609[ai-ultralytics]'
pip install 'stanag4609[ai-onnx]'
pip install 'stanag4609[ai-triton-grpc]'
pip install 'stanag4609[audio-pyav]'
```

## Public FMV test data

The project uses FFmpeg's public `Day Flight.mpg` and `Night Flight IR.mpg`
real-UAS streams, Esri's audio-bearing `Truck.ts`, and ImpleoTV's public
negative-conformance corpus as opt-in integration fixtures. The fetcher
verifies exact byte sizes and SHA-256 identities before the library processes
them; the negative corpus additionally authenticates each asserted ZIP member.

```console
python scripts/fetch_public_fixtures.py
pytest -m integration tests/integration/test_public_fmv.py
```

Large media and the conformance ZIP are not committed. Fixture provenance,
hashes, and expected results are recorded in `references/fixtures.json`; see
[public fixture details](https://stanag4609.readthedocs.io/en/latest/PUBLIC_FIXTURES/).

## Reference player

With FFmpeg installed, launch the bundled local player against any MPEG-2 TS
FMV file:

```console
stanag4609-player "/path/to/Truck.ts"
stanag4609-player "/path/to/Truck.ts" --stream-metadata
stanag4609-player - --live
stanag4609-benchmark-live "/path/to/Truck.ts"
```

For separate video and CSV inputs, run `stanag4609-mux-esri` first and pass its
output to the player.

It serves a browser-compatible video and synchronizes the live side panel to
the source KLV PTS. Its time origin is the earliest mapped audio/video PTS, so
FFmpeg-preserved audio lead-in does not shift metadata against the transcoded
video. The original first-video PTS remains available in the timeline API. The panel exposes timestamps, platform/sensor state, sensor
and frame-center coordinates, altitude, target coordinates, VMTI, and field
diagnostics. A synchronized canvas map uses an attributed OpenStreetMap
baselayer, with a network-free grid fallback, and plots the sensor, frame
center, target, full or offset-derived image footprint, and footprint-projected
AI boxes. Typed VMTI targets with pixel geometry are drawn as synchronized
bounding boxes and centroid markers, including labels and confidence when the
packet carries Algorithm/Ontology metadata. A scrubber under the video groups
detections into bounded time buckets and stacks the five most prevalent classes
plus `other`; log-scaled density keeps both quiet and busy periods visible.
Hover reveals the exact bucket interval and class counts, while click, drag,
keyboard, and range controls seek the shared media clock. The canvas never
creates one element per detection, and a capped activity feed groups detections
around the playhead.
Sparse Report-on-Change packets inherit still-current values, so
the display does not lose coordinates merely because a packet omits an
unchanged item. Before the first timestamped metadata sample, the map, fields,
and overlays remain empty rather than showing future telemetry; per-sample
decode/state diagnostics appear in a visible warning panel. See
[reference player details](https://stanag4609.readthedocs.io/en/latest/PLAYER/).

`--stream-metadata` keeps the seekable prepared MP4 but sends telemetry to the
browser incrementally over Server-Sent Events. The feed replays the effective
sample at the current media time, follows future samples at the selected
playback rate, emits bounded keepalives, and reconnects from the playhead after
seeks or playback stalls. A sparse server-generated 2,048-bin summary preserves
the complete detection overview while the browser retains at most 512 detailed
samples; paused timeline scrubs fetch only the newly selected effective sample.
`--live` is the complete low-latency reference path:
it reads MPEG-TS from a file or stdin as bytes arrive, decodes KLV incrementally,
transcodes video/audio through FFmpeg into one-second fragmented MP4 units, and
feeds those units to the bundled Media Source Extensions client without waiting
for end-of-input. Media and metadata histories are both bounded.

## Architecture

```text
TS bytes -> framing -> PAT/PMT -> PES demux -> timed video/audio/KLV events
                                                |             |
                                                v             v
                                        AI sidecar graph   KLV processors
                                                |             |
                                                +---- VMTI ---+
                                                              |
                                  +---------------------------+----------+
                                  v                           v          v
                              TS remux                   KLV/NDJSON    GIS/UI
```

The dependency-free core owns transport and metadata truth. Optional adapters
will own compressed video/audio decoding, model runtimes, browser delivery, and
third-party GIS formats. See the accepted
[live architecture decision](https://stanag4609.readthedocs.io/en/latest/adr/0002-live-transform-pipeline/).

## Development and verification

Protocol work follows specification-led TDD: cite the edition and requirement,
add a failing normative or adversarial test, implement the smallest coherent
slice, run every quality gate, review the diff, and make a focused Conventional
Commit.

```console
ruff check .
mypy src
pytest --cov=stanag4609 --cov-branch --cov-report=term-missing
python -m build
```

See [CONTRIBUTING.md](https://github.com/trane293/stanag4609/blob/main/CONTRIBUTING.md) for branch/PR conventions, required PR
evidence, compatibility review, and rollback expectations. The standards
manifest records the exact MISB and ITU editions used; normative documents are
not redistributed in Git.

## Roadmap

The next work includes completing the remaining ST 0601/ST 0902/ST 0903
requirements, adding an independent real VMTI fixture, whole-multiplex live
rewrite policies, measured low-latency browser delivery, and wider interoperability
and performance evidence. See the maintained
[remaining-work guide](https://stanag4609.readthedocs.io/en/latest/ROADMAP/) for priorities and continuation steps.

## License

[MIT](https://github.com/trane293/stanag4609/blob/main/LICENSE)
