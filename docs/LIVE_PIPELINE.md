# Live transform pipeline

`LiveTransportTransformer` changes timed KLV in an MPEG-2 transport stream
without transcoding its media elementary streams. Feed it arbitrary byte chunks
and consume each `TransformBatch` before reading more input.

Start with [Transform live KLV and fan it out](tutorials/live_fanout.md) for a
complete processing loop.

## Read a transform batch

Each batch contains independent views of the same input interval:

- `transport` contains remultiplexed MPEG-TS for the downstream FMV receiver;
- `streams` contains source PES events for optional video and audio adapters;
- `metadata` contains transformed, typed `TimedKLVPacket` values;
- `clocks` contains 27 MHz PCR observations and discontinuity state; and
- `table_emission` describes a scheduled PAT/PMT emission, when one was due.

Video, audio, and other unchanged PES payloads remain byte-for-byte intact.
Transport packets are rebuilt where needed to keep continuity and signalling
valid.

## Select one FMV service

Single-program input is selected automatically. A multi-program transport needs
an explicit program number:

```python
from stanag4609.transport import LiveTransportTransformer

transformer = LiveTransportTransformer(
    program_number=7,
    max_programs=32,
)
```

The result is a self-contained single-program transport. The same selection
scopes PES, metadata, and clock events. An ambiguous multi-program PAT is
rejected instead of pairing media from one service with metadata from another.

Multi-section PAT versions activate only after every section arrives. Until
then, the prior complete routes remain active.

## Change metadata

A processor receives one timed KLV packet and returns an explicit decision:
pass, drop, replace, inject before, or inject after. This example replaces ST
0601 Item 74 while preserving unrelated and unknown fields:

```python
from stanag4609 import (
    MetadataDecision,
    TimedKLVPacket,
    UASLocalSet,
    update_uas_local_set,
)


def add_vmti(event: TimedKLVPacket) -> MetadataDecision:
    if not isinstance(event.decoded, UASLocalSet):
        return MetadataDecision.pass_through()
    changed = update_uas_local_set(event.decoded, {74: encoded_vmti})
    return MetadataDecision.replace(changed)
```

`update_uas_local_set()` repairs the ST 0601 checksum. Use preserve-mode field
decoding when a relay must carry malformed known fields as opaque bytes.

New packets continue through later processors. A pipeline can therefore keep
redaction, AI enrichment, validation, and observability in separate stages.
`emit_metadata()` accepts an independently produced `TimedKLVPacket`, such as
the output of `VMTIMetadataEmitter` at a decoded frame's PTS.

To view transformed bytes immediately, pipe the output to the live player:

```console
your-fmv-source-or-transformer \
  | stanag4609-player - --live --no-open
```

## Add a metadata stream

When a media-only input has no KLVA PID, declare one while constructing the
transformer:

```python
from stanag4609.transport import (
    LiveTransportTransformer,
    MetadataSTDDescriptor,
    synchronous_klv_stream,
)

metadata_std = MetadataSTDDescriptor.from_physical(
    input_bits_per_second=400_000,
    buffer_bytes=4_194_304,
)
transformer = LiveTransportTransformer(
    additional_metadata_stream=synchronous_klv_stream(
        0x120,
        metadata_std=metadata_std,
        metadata_service_id=7,
    )
)
```

The output PMT is versioned and includes the ST 1402 descriptors before the
first injected packet. If the input already has a synchronous metadata stream,
emit on that PID. A second synchronous elementary stream is rejected.

Several metadata services may share one synchronous PID when each service is
declared in the PMT:

```python
stream = synchronous_klv_stream(
    0x120,
    metadata_service_ids=(4, 9),
    metadata_std=metadata_std,
)

muxer.mux_sync_klv(
    0x120, telemetry_klv, pts=pts, metadata_service_id=4
)
muxer.mux_sync_klv(
    0x120, detections_klv, pts=pts, metadata_service_id=9
)
```

H.222.0 encodes leak rates in 400-bit/s units and buffer capacity in 1,024-byte
units. `MetadataSTDDescriptor.from_physical()` requires values that map exactly
to those units.

## Schedule tables and transport rate

Pass a monotonic `at` value to `feed()` and `emit_metadata()` to enable output
PAT/PMT scheduling. A table pair is emitted after program discovery and then at
the recommended 125 ms interval. During input silence, call
`poll_program_tables()` from the event-loop timer.

`ProgramTableEmission` reports lateness, skipped recommended slots, and whether
the observed gap stayed below the mandatory 250 ms limit. Omitting `at` keeps
the source table cadence.

Inserted metadata changes the physical packet schedule. Use
`TransportRateShaper` when the output must remain constant rate. The shaper
places packets on caller-anchored clock slots, adds bounded null padding, and
restamps retained PCR. The transformer does not choose a bit rate or output
clock epoch because those belong to the deployment. See
[transport rate and PCR restamping](TRANSPORT_RATE.md).

## Handle program-table changes

Versioned PMT updates may add, remove, or redefine streams, change KLVA
carriage, or select another declared PCR PID. A versioned PAT may move the
selected service to a new PMT PID; the prior route remains active until the
replacement PMT arrives and can be applied atomically.

A route change is rejected while it would discard an incomplete PES, KLV item,
or metadata access unit. Compatible updates allow an in-flight PES on an
unchanged stream to complete. Metadata sequence numbers continue only while PID
and carriage remain unchanged.

The transformer preserves source PCR, OPCR, random-access indicators, and
program or elementary descriptors while repacketizing unchanged PES.

## Inspect adaptation fields

Transport packets expose an immutable typed adaptation-field view. Use
`dataclasses.replace()` and rebuild the packet when you intend to change it:

```python
from dataclasses import replace

from stanag4609 import parse_transport_packet, rebuild_transport_packet

packet = parse_transport_packet(packet_bytes)
if packet.adaptation is not None:
    adaptation = replace(packet.adaptation, random_access_indicator=True)
    packet_bytes = rebuild_transport_packet(packet, adaptation=adaptation)
```

Calling `rebuild_transport_packet(packet)` without overrides returns the source
bytes. A modified adaptation field uses canonical `0xFF` stuffing.

## Start a new source epoch

A reconnect, failover, or source restart is not an ordinary chunk boundary.
Reset the transformer before feeding the replacement source:

```python
report = transformer.reset()
print(report.demux.buffered_transport_bytes)
print(report.demux.buffered_pes_bytes)
print(report.metadata.asynchronous_klv_bytes)

for chunk in replacement_source:
    sink.write(transformer.feed(chunk).transport)
```

Reset discards and reports partial transport, PSI, PES, KLV, metadata access
units, sequence state, and discovered topology. The replacement source must
provide fresh PAT and PMT tables, and downstream consumers should treat the
output as a new continuity epoch. Configured limits, program selection,
processors, and caller-owned processor state remain in place.

The transformer intentionally emits only the selected program and omits
unrelated programs and null packets. Use a separate whole-multiplex rewriter if
your application must preserve the entire MPTS.
