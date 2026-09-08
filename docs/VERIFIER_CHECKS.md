# Verifier checks

This page describes the evidence gathered by `FMVVerifier` and the boundaries
of the larger checks. See [Verify and debug FMV](VERIFIER.md) for day-to-day
commands and policy examples.

## Coverage by area

| Area | Checks |
| --- | --- |
| MPEG-2 TS | Packet framing, sync offsets, trailing truncation, payload continuity, duplicates, and scrambling |
| PSI and programs | CRC-valid current PAT/PMT presence, program and stream inventory, and DVB SDT CRC-32 diagnostics |
| ST 1402 | KLV declarations, PCR/PTS cadence, PAT/PMT blackout intervals, synchronous decoder delay, and metadata buffer occupancy |
| KLV carriage | PES rules, synchronous access-unit cells, asynchronous boundaries, fragmentation, sequence, BER framing, and registered keys |
| ST 0601 | Checksum, Local Set structure, typed-field diagnostics, field coverage, Report-on-Change state, distributed record state, and selected semantic contexts |
| ST 0902 | Minimum fields and alternatives, ZLI, invalid values, refresh interval, end-of-recording state, security context, and ST 1607 metadata trees |
| ST 0903 | Standalone and embedded VMTI decode, references, target inventory, lifecycle transitions, dropped-ID reuse, and optional ontology resolution |
| ST 1001 | Permitted audio types, MP2/AAC-LC framing, PTS anchors, sample rate, channel count, duration, malformed headers, and truncation |
| ST 0604 | H.262 user data and AVC/HEVC unregistered SEI, Time Status, timestamp association, duplicates, and missing timestamps |
| MISP video | Class 1 codecs, coded and displayed properties, scan/chroma/bit depth, profile/level limits, and producer-supplied image context |

The authoritative requirement-by-requirement record is the
[conformance matrix](CONFORMANCE.md). It distinguishes implemented,
partially implemented, and unsupported behavior.

## Timing and buffer evidence

PAT and PMT recurrence requires a reliable monotonic timeline. The file
verifier checks table presence but does not estimate cadence from byte position
or a guessed bit rate.

For synchronous ST 1402 decoder delay, adjacent PCR observations bracket the
arrival of each metadata PES. A range entirely outside zero to one second is an
error. A range that crosses either boundary, or a PES without both PCR sides,
is a warning because the timing evidence is incomplete.

The Metadata T-STD audit uses H.222.0 byte-time interpolation, a persistent
512-byte transport buffer, and the descriptor-sized main buffer across PCR
windows. It reports overflow, PTS underflow, late delivery, and failure to empty
the transport buffer within one second. Descriptor changes, discontinuities,
resource limits, or missing PCR brackets make the result unverifiable.

Synchronous streams declare their rate and buffer size in the PMT. Ordinary
asynchronous signalling does not. If a deployment negotiates those values,
provide them through the Python API:

```python
from stanag4609 import verify_fmv_file
from stanag4609.transport import MetadataSTDDescriptor

descriptor = MetadataSTDDescriptor.from_physical(
    input_bits_per_second=1_600_000,
    output_bits_per_second=800_000,
    buffer_bytes=16 * 1024,
)
report = verify_fmv_file(
    "mission.ts",
    asynchronous_std_descriptors={(1, 0x102): descriptor},
)
```

No defaults are guessed for an asynchronous stream.

## Video and audio checks

ST 0604 messages are associated with recognizable coded access units according
to H.262 picture, AVC prefix-SEI, and HEVC prefix/suffix-SEI placement. The
verifier detects access units without a timestamp, multiple timestamps on one
unit, and timestamp messages without a frame. Equal aggregate counts alone are
not sufficient.

Video level checks apply the relevant H.262, AVC, or HEVC picture-size and
sample-rate limits to every observed sequence property set. H.262 checks also
cover Main-profile chroma, constrained parameters, frame-rate extensions,
declared bit rate, and VBV buffer size. Missing or ambiguous sequence timing is
reported as unresolved rather than being replaced with transport timing.

Audio inspection parses compressed MP2 or AAC-LC frames. JSON stream inventory
includes frame and sample totals, cumulative duration, sample rates, channel
counts, timestamped frames, and AAC Program Config Element use. The verifier
does not decode PCM or assess audible quality.

## ST 0601 state and inventory

The JSON `st0601_streams` array separates metadata service IDs and asynchronous
streams even when they share a PID. Its `tags` object is keyed by decimal local
tag. Known tags carry their ST 0601 name; unknown extensions remain visible
with `name: null`.

Packet presence, occurrences, ZLIs, and decode issues are counted separately.
This lets a consumer distinguish a field that was never sent, explicitly
cleared, or present with malformed bytes. Distinct tags per service are bounded
by `max_st0601_tags_per_stream`, which defaults to 4,096.

Item 2 values are reported in the MISP time scale. UTC is derived only while a
valid Item 136 leap-second value is current; Item 137 is applied when present.
Both follow Report-on-Change lifetime and a ZLI clears either value. Timestamp
regressions are warnings and do not mutate UTC conversion state.

The `mismms_coverage` object records whether each selected minimum-metadata
group is current, missing, or overdue at the end of the stream. Historical
violations remain in the findings list even if a later packet restored the
field. Nested ST 0102 fields are tracked by their full tag path.

Control Commands, wavelength definitions, payload definitions, weapon stores,
and waypoints use service-scoped bounded state. The verifier checks references,
acknowledgements, ordering, refresh behavior, and record identity where the
standard defines them.

## VMTI lifecycle and ontology checks

VMTI target identity is scoped by program, metadata PID, and metadata service
ID. Impossible status transitions and reusing an ID after `Dropped` are errors.
A missing status is a warning because lifecycle behavior cannot be checked, but
the target may still be structurally valid. A finite recording may begin in the
middle of a lifecycle, so the first observed state is not assumed to be target
creation.

An optional ontology resolver can validate VObject references and labels
without giving the KLV decoder hidden network access. The application controls
the trusted local map, cache, database, or service:

```python
from stanag4609 import OntologyEntityResolution


class VehicleOntologyResolver:
    def resolve_entity(self, ontology_iri: str, entity_iri: str):
        if entity_iri.endswith("#Truck"):
            return OntologyEntityResolution(
                ontology_iri=ontology_iri,
                entity_iri=entity_iri,
                is_owl_ontology=True,
                rdfs_labels=frozenset({"truck"}),
            )
        return None
```

Returning `None` rejects an unknown entity. Omitting the resolver retains
offline structural validation.

## Add producer-known validation context

Some ST 0601 and ST 0903 requirements depend on facts that are not encoded in
the packet being checked. A context provider can supply system precision,
expected sensor values, metadata time of birth, or video-frame facts:

```python
from stanag4609 import (
    FMVVerifier,
    ST0601FieldExpectation,
    ST0601ValidationContext,
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
    st0601_context_provider=context_for_packet,
    max_findings=10_000,
)
```

Use an absolute tolerance for mapped numeric fields whose wire quantization
prevents equality with the source value. Context failures become ordinary
`metadata.decode` findings with stream and source context.

## Bounded failure behavior

The verifier retains partial parser state, counters, current program tables,
bounded field and lifecycle state, and coalesced findings. Repeated findings
carry a count plus first and last offsets. Once `max_findings` is reached,
errors take priority and the report records how many findings were suppressed.

Packet framing is strict. After an invalid sync byte or malformed packet, the
verifier records the offset and stops semantic demultiplexing instead of
searching arbitrary payload bytes for another apparent packet boundary.
