# Verify and debug FMV

`stanag4609-verify` inspects a finite MPEG-2 transport stream and explains both
what passed and what is missing or malformed. The same engine is available as
an incremental Python API for upload handlers, pipes, and live capture tools.

!!! warning "Scope"

    A passing report means the stream passed the checks implemented by this
    library version and selected policy. It is not a certification of complete
    STANAG 4609 or every MISB standard. Consult the [conformance matrix](CONFORMANCE.md)
    for the exact implemented slices.

## Run the verifier

```console
stanag4609-verify mission.ts
```

The text report contains:

- an overall pass/fail result and input statistics;
- program and elementary-stream inventory with PID, type, carriage, codec,
  PES counts, payload sizes, and timestamp coverage;
- per-service ST 0601 tag inventory with names, packet/occurrence counts,
  zero-length items, decode issues, versions, the exact MISP Item 2 coordinate
  span, derived UTC span when Items 136/137 make conversion possible, maximum
  forward gap, duplicate timestamps, and transport-order regressions;
- explicit `PASS`, `WARN`, `ERROR`, and `N/A` findings;
- standard requirement identifiers, field tags, program/PID context, source
  offsets, and occurrence counts where available.

Generate stable, machine-readable JSON for CI, upload services, or a web UI:

```console
stanag4609-verify mission.ts --format json > verification.json
```

Generate a self-contained report that can be opened in any browser, printed,
or shared as a single file. It has no CDN, JavaScript, or runtime dependencies:

```console
stanag4609-verify mission.ts --format html > verification.html
```

Exit status `0` means no error finding, `1` means verification completed with
one or more errors, and `2` means the source or command configuration could not
be read. Warnings do not change a passing exit status.

## Select application policy

ST 0902 normally checks the Security Local Set and MIIS Core Identifier. A
known application profile can disable those two requirements explicitly:

```console
stanag4609-verify mission.ts \
  --no-require-security \
  --no-require-miis
```

Audio is optional under ST 1001 itself. Require it as an application policy
when the receiving workflow needs an audio channel:

```console
stanag4609-verify mission.ts --require-audio
```

The encoded bitstream exposes display properties, but it cannot prove the
aspect ratio acquired at the imager or the scan mode of upstream conversion
stages. Supply those producer-known facts when they are available:

```console
stanag4609-verify mission.ts \
  --source-aspect-ratio 16:9 \
  --source-scan progressive \
  --conversion-scan progressive \
  --source-form digital \
  --conversion-form digital
```

`--conversion-scan` and `--conversion-form` are repeatable in pipeline order.
The verifier checks the source aspect ratio against MISP-2015.1-01's inclusive
`[0.25, 4.0]` range and requires the source and every supplied conversion stage
to be progressive for MISP-2015.1-02. It records legacy analog input reaching
the verifier in digital form for MISP-2015.1-05, and rejects a caller-declared
analog stage in a digital-native pipeline for MISP-2015.1-06. Omitted facts
remain unclaimed rather than being inferred from encoded headers.

The same policy is available without the CLI:

```python
from fractions import Fraction

from stanag4609 import MISPImageContext, verify_fmv_file

report = verify_fmv_file(
    "mission.ts",
    image_context=MISPImageContext(
        source_aspect_ratio=Fraction(16, 9),
        source_progressive=True,
        conversion_progressive=(True,),
        source_digital=True,
        conversion_digital=(True,),
    ),
)
```

When the deployment has an authoritative marking policy, enforce it explicitly
instead of accepting any structurally valid ST 0102 value:

```console
stanag4609-verify mission.ts \
  --security-classification secret \
  --country-coding-method genc-three-letter \
  --classifying-country USA \
  --security-sci-shi 'SI/TK//' \
  --security-caveats 'FOUO' \
  --require-release-country USA \
  --require-release-country CAN \
  --require-object-country USA \
  --object-country-coding-method genc-three-letter \
  --minimum-security-metadata-version 12
```

Country options are repeatable uppercase codes without wire separators. Exact
SCI/SHI values retain ST 0102 slash framing. Coding-method options use the
lowercase, hyphenated `CountryCodingMethod` and `ObjectCountryCodingMethod`
member names. A mismatch, including an older-than-required Security Metadata
version, is an error finding with code `security_policy`; omitted policy fields
continue to receive structural, population, and cadence checks without the
library guessing a mission classification or code-list authority.

The default `mismms` profile applies ST 0902 minimum-metadata and cadence
requirements. For ST 1607 metadata trees, it reconstructs Report-on-Change
state and checks the effective union at every terminal Segment as required by
ST 1607-06. When an Amend is present, it separately checks the unamended root
under ST 1607-05; an Amend therefore cannot conceal missing root metadata.
Finding messages identify the full Metadata Substream ID path. For an older,
vendor-specific, or exploratory stream, separate transport/KLV/ST 0601
structural health from that modern application profile:

```console
stanag4609-verify legacy-mission.ts --profile structural
```

Structural mode reports the ST 0902 profile as not applicable; it does not
describe an unchecked profile as passing. Malformed ST 0601 fields, transport
errors, carriage errors, and the other structural checks still fail.

## Implemented checks

| Area | Checks |
| --- | --- |
| MPEG-2 TS | Packet framing, invalid sync offsets, trailing truncation, payload continuity, duplicates, scrambling |
| PSI and programs | CRC-valid current PAT/PMT presence, program/stream inventory, and DVB SDT CRC-32 diagnostics |
| ST 1402 | KLV declarations, PCR/PTS cadence, PCR-bracketed PAT/PMT blackout proof, conservative synchronous decoder-delay proof, and exact bounded-window synchronous or configured-asynchronous metadata TBn/Bn occupancy |
| KLV carriage | PES rules, synchronous AU cells, asynchronous boundaries, fragmentation, sequence, BER framing and registered keys |
| ST 0601 | Running checksum, Local Set structure, lossless typed-field diagnostics, bounded per-service field coverage inventory, Item 42 receiver-current vertical datum, Item 115/116 command lifecycle, and distributed wavelength/payload/weapon/waypoint state |
| ST 0902 | Required minimum fields, alternatives, ZLI, invalid values, interval, end-of-recording absence, selected security context, and ST 1607 Amend-root/terminal-Segment effective-state completeness |
| ST 0903 | Successful standalone or embedded VMTI decode, optional OWL/entity/exact-label resolution, target observations/unique-ID inventory, cross-frame state transitions, dropped-ID reuse, and missing-status diagnostics |
| ST 1001 | Permitted audio stream types, optional application-required audio, complete MP2/AAC-LC frame parsing, PTS anchoring, sample rate, channel count, sample/frame totals, cumulative duration, malformed headers, and trailing truncation |
| ST 0604 | Incremental H.262 user-data and AVC/HEVC unregistered-SEI parsing, Time Status validation, micro/nanosecond inventory, timestamp-to-access-unit association, and missing/duplicate/unassociated diagnostics |
| MISP video profile | H.262/AVC/HEVC coded dimensions, available display ratio/frame rate, scan signalling, chroma/bit depth, profile/level, property changes, stream-wide adopted MISP profile/scan checks across every observed sequence property set, the Class 1 eight-bit-per-band limit, and explicit producer source-aspect/scan context |

VMTI identities are scoped by program, metadata PID, and metadata service ID.
Reusing an identifier after `Dropped` or taking an impossible state-machine
transition is an error. Because Item 23 is optional, a missing detection status
is a warning: the packet is still structurally valid, but lifecycle verification
for that target is incomplete. A finite recording can begin mid-lifecycle, so
the verifier does not assume the first observed state is target creation.

Control Command history is scoped by the same program/PID/service identity.
The verifier reports non-increasing new command IDs, changed command text or
issue time on a repeat, unknown or duplicate acknowledgements, and any Item 115
repeat after Item 116 has acknowledged that command.

The same service-scoped receiver model validates current custom Wavelength
definitions and Active Wavelength references, distributed Payload definitions
and Active Payload bits, independently refreshed Weapon Store records, and
Waypoint historical ordering. Reports identify the affected Item tags and
record IDs; state memory remains bounded.

PAT/PMT recurrence needs a reliable monotonic source timeline; the file
verifier checks presence but does not infer cadence from byte position or bit
rate. The ST 0604 check associates timestamp messages with recognizable coded
access units according to H.262 picture, AVC prefix-SEI, and HEVC prefix/suffix
SEI placement. It reports timestamp-free access units, multiple timestamps on
one access unit, and messages left without a frame; equal aggregate counts are
therefore no longer accepted as sufficient evidence. Broader codec-profile
conformance and decoded PCM quality analysis are later verifier increments.

For ST 1402-12, adjacent PCR samples form conservative lower and upper arrival
times for each synchronous metadata PES. A delay range entirely above one
second, or entirely below zero, is an error. A range that straddles either
boundary is a warning, as is a PES without both sides of a PCR bracket. This
deliberately prevents sparse timing evidence from becoming a false pass.

For the exact metadata T-STD audit, metadata PES packets wait for the next
program PCR. H.222.0 byte-time interpolation then drives a persistent 512-byte
transport buffer and descriptor-sized main buffer across PCR windows. The
report diagnoses transport/main-buffer overflow, PTS underflow, excessive or
late access-unit delivery, and failure to empty the transport buffer within one
second. Missing brackets, descriptor changes, discontinuities, and configured
resource limits produce `st1402.metadata_std.unverifiable`; an exact pass is
emitted only with complete PES coverage and no model failure.

Synchronous streams declare their input rate and buffer size in the PMT.
Ordinary ST 1402/RP 217 asynchronous signaling does not declare the input rate,
output rate, or buffer size. If those values are known from negotiation or a
deployment profile, pass a per-program/PID mapping through the Python API:

```python
from stanag4609 import verify_fmv_file
from stanag4609.transport import MetadataSTDDescriptor

async_std = MetadataSTDDescriptor.from_physical(
    input_bits_per_second=1_600_000,
    output_bits_per_second=800_000,
    buffer_bytes=16 * 1024,
)
report = verify_fmv_file(
    "mission.ts",
    asynchronous_std_descriptors={(1, 0x102): async_std},
)
```

The CLI intentionally has no guessed defaults. An unconfigured asynchronous
PES is reported as unverifiable rather than silently omitted or treated as a
conformance pass.

For each permitted audio stream, JSON stream inventory includes an `audio`
object with complete frame and sample counts, cumulative sample duration,
sample rates, known channel counts, timestamped-frame count, and the number of
AAC frames whose channel layout is defined by an in-band Program Config
Element. This is compressed access-unit validation; it does not decode PCM or
judge audible quality.

The top-level JSON `st0601_streams` array separates synchronous metadata
service IDs and asynchronous streams even when they share a transport PID.
Each entry contains a `tags` object keyed by decimal local tag. Known tags carry
their ST 0601 name; unknown extension tags remain visible with `name: null`.
`packets_present` differs intentionally from `occurrences` for items that permit
multiple instances. ZLIs and typed-decoding issues are counted independently,
so a consumer can distinguish “never sent,” “explicitly cleared,” and “sent but
malformed.” The default cap is 4,096 distinct tags per service; customize it
with `--max-st0601-tags-per-stream` or `max_st0601_tags_per_stream`.
The same service entry counts valid and invalid/missing timestamps and exposes
transport-order chronology. `timestamp_time_scale` is `MISP`; the exact
unsigned Item 2 values are available as `first_misp_timestamp_microseconds`
and `last_misp_timestamp_microseconds`. `first_utc_timestamp` and
`last_utc_timestamp` are populated only while a valid Item 136 leap-second
value is current. Item 137 correction is applied when present and otherwise
defaults to zero. Both values follow the normal Report-on-Change lifetime:
they carry for the inclusive 30-second refresh interval and a ZLI clears them.
The `utc_timestamped_packets` and `utc_conversion_unavailable_packets` counts
make missing UTC evidence explicit instead of treating MISP time as UTC.
Regressions produce a warning rather than a conformance error because live
joins and upstream reordering policies vary; a regressing packet is not used
to mutate the UTC conversion state.

When the `mismms` profile is enabled, every service also carries a
`mismms_coverage` checklist. Each selected minimum-item group reports its
candidate ST 0601 tags, `current`, `missing`, or `overdue` population state,
last valid observation, and age at stream end. This is an end-state inventory;
the findings list remains authoritative for historical violations that were
later corrected. Structural mode emits `null` because it did not evaluate the
profile. Every required ST 0102 Security item is tracked independently and uses
a nested `tag_paths` value such as `[48, 1]`; seeing Item 48 alone never marks
missing children current. The HTML report renders the same checklist below each
field inventory.

## Incremental Python API

```python
from pathlib import Path

from stanag4609 import (
    CountryCodingMethod,
    FMVVerifier,
    MISMMSecurityContext,
    ObjectCountryCodingMethod,
    ST0601FieldExpectation,
    ST0601ValidationContext,
    SecurityClassification,
)

def context_for_packet(event, packet):
    return ST0601ValidationContext(
        imap_system_precisions={104: 0.5},
        field_expectations={
            13: ST0601FieldExpectation(40.1234, absolute_tolerance=1e-6),
            14: ST0601FieldExpectation(-75.4321, absolute_tolerance=1e-6),
        },
    )

verifier = FMVVerifier(
    require_security=True,
    require_miis=True,
    require_audio=False,
    validate_mismms=True,
    security_context=MISMMSecurityContext(
        expected_classification=SecurityClassification.SECRET,
        expected_country_coding_method=CountryCodingMethod.GENC_THREE_LETTER,
        expected_classifying_country="USA",
        required_releasing_countries=frozenset({"USA", "CAN"}),
        expected_object_country_coding_method=(
            ObjectCountryCodingMethod.GENC_THREE_LETTER
        ),
        minimum_security_metadata_version=12,
    ),
    st0601_context_provider=context_for_packet,
    asynchronous_std_descriptors=known_async_std_by_program_and_pid,
    max_findings=10_000,
    max_st0601_tags_per_stream=4_096,
)

for chunk in incoming_chunks:
    verifier.feed(chunk)

report = verifier.finish(source="upload.ts")
print(report.ok)
print(report.format_text())
Path("verification.html").write_text(report.to_html(), encoding="utf-8")
payload = report.to_dict()
```

`st0601_context_provider(event, packet)` is optional and is called for each
complete ST 0601 KLV packet. It may return an `ST0601ValidationContext` with
the producer-known metadata time of birth, negotiated variable-IMAP system
precisions, authoritative expected singleton field values, and embedded-VMTI
frame facts, or `None` when those facts are not available. Use
`ST0601FieldExpectation.absolute_tolerance` for mapped numeric fields whose wire
quantization prevents exact equality. Expected fields must be present and
known in that packet. Context failures become normal `metadata.decode`
findings with the program, PID, and source offset. The same option is accepted
by `verify_fmv_stream` and `verify_fmv_file`.

Each ST 0601 service summary records this external assurance separately from
wire-observable checks. Its `validation_context` object reports the number of
packets receiving any context, packets whose metadata birth time was checked,
variable-IMAP items whose encoded precision was checked, packets whose embedded
VMTI was checked against external frame facts, and fields compared with
producer ground truth. The text and HTML reports show the same counts. A zero
therefore means “not externally proven,” not that the sensor fact was inferred
from its own KLV value. Decoded `KLVMetadataEvent` values retain the exact
`validation_context` used for the same reason.

`ontology_resolver` is optional. When supplied, it is applied to both
standalone VMTI and ST 0601 Item 74, and failures appear in the report with the
affected stream and field. The resolver controls ontology acquisition; the
verifier never fetches an IRI itself. See [AI and VMTI sidecars](AI_SIDECARS.md#validate-ontology-semantics-without-hidden-network-access)
for the small resolver protocol and a complete local-map example.

The verifier retains partial parser state, counters, active program tables,
only the latest validated PMT identity per program, bounded profile,
field-inventory and VMTI lifecycle state, and coalesced findings—not the
complete media file or a history of PMT revisions. Repeated
findings carry a `count`, `first_offset`, and `last_offset`. When the configured
unique-finding bound is reached, errors take priority and the report states how
many findings were suppressed.

Packet framing is strict: after an invalid sync byte or malformed packet, the
report preserves the failure offset and stops semantic demuxing instead of
scanning an arbitrary non-transport file byte by byte. This makes wrong-format
inputs fail quickly and prevents false packet recovery from elementary-stream
payload bytes.

For ordinary files, use the convenience wrapper:

```python
from stanag4609 import verify_fmv_file

report = verify_fmv_file("mission.ts")
if not report.ok:
    for finding in report.errors:
        print(finding.code, finding.requirement, finding.message)
```

## Use it in CI

```console
stanag4609-verify artifacts/mission.ts --format json > verification.json
```

Archive the JSON even when the command returns `1`; it is the diagnostic
artifact explaining the failure. Consumers should check `schema_version`
before relying on fields and should use `code` for automation rather than
matching human-readable messages.
