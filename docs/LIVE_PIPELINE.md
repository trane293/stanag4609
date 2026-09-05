# Live transform pipeline

`LiveTransportTransformer` is the first high-level, pull-driven API for changing
KLV in transit without transcoding the media elementary streams. Each call
returns a `TransformBatch` with four independent views:

- `transport`: remuxed MPEG-2 TS bytes for a downstream FMV consumer;
- `streams`: source PES events for optional video/audio codec adapters; and
- `metadata`: transformed, typed, timed KLV packets for a UI or sidecar writer.
- `clocks`: exact 27 MHz PCR observations associated with their active program,
  PID, source offset, OPCR, and discontinuity state.
- `table_emission`: scheduling metrics for a PAT/PMT repetition emitted by the
  timed mode, or `None` when no repetition was due.

For a multi-program transport, select the FMV service explicitly. The output is
a standards-shaped single-program transport containing only that program; the
same selection scopes `streams`, `metadata`, and `clocks`:

```python
transformer = LiveTransportTransformer(
    (add_truck,),
    program_number=7,
    max_programs=32,
)
```

A multi-program PAT without `program_number` is rejected instead of choosing a
service by ordering. A missing selection and an input exceeding `max_programs`
are likewise explicit errors. Multi-section PAT versions activate only after
all sections arrive, in any order; until then, the prior complete program routes
remain active. Multiple program definitions may share a source PMT PID.

The following processor adds an ST 0903.6 truck detection to ST 0601 Item 74.
It preserves every unrelated and unknown ST 0601 item byte-for-byte and repairs
the ST 0601 checksum.

```python
from stanag4609 import (
    DetectionStatus,
    MetadataDecision,
    TimedKLVPacket,
    UASLocalSet,
    VTargetData,
    encode_vmti_local_set,
    update_uas_local_set,
)
from stanag4609.transport import LiveTransportTransformer

vmti = encode_vmti_local_set(
    {4: 6, 8: 1920, 9: 1080},
    targets=(
        VTargetData(
            42,
            {
                1: 409_600,
                2: 400_000,
                3: 420_000,
                5: 97,
                19: 872,
                20: 1137,
                23: DetectionStatus.ACTIVE_MOVING,
            },
        ),
    ),
)


def add_truck(event: TimedKLVPacket) -> MetadataDecision:
    if not isinstance(event.decoded, UASLocalSet):
        return MetadataDecision.pass_through()
    changed = update_uas_local_set(event.decoded, {74: vmti})
    return MetadataDecision.replace(changed)


from time import monotonic

transformer = LiveTransportTransformer((add_truck,))

for chunk in live_source:
    batch = transformer.feed(chunk, at=monotonic())
    transport_sink.write(batch.transport)
    ui_metadata_sink.consume(batch.metadata)

# Call from the event-loop timer when no input chunk arrives.
transport_sink.write(transformer.poll_program_tables(at=monotonic()).transport)

final = transformer.finish()
transport_sink.write(final.transport)
ui_metadata_sink.consume(final.metadata)
```

To visualize a resulting TS stream immediately in the bundled browser client,
pipe it to the live player instead of a file sink:

```console
your-fmv-source-or-transformer \
  | stanag4609-player - --live --no-open
```

Python services can instantiate `stanag4609.player.LivePlayerGateway` and call
`feed(batch.transport)` directly. Its media and metadata buffers are public,
typed integration boundaries for another HTTP framework. `feed()` propagates
FFmpeg pipe backpressure; it never hides overload by dropping partial TS data.
Call `finish()` only for a clean source EOF and `close()` when a connection
epoch is abandoned.

Processors can also drop a packet or inject one or more packets before or after
it. New packets continue through later processors, so validation, redaction,
enrichment, and fan-out logic compose deterministically. `emit_metadata()`
accepts an independently produced `TimedKLVPacket`, allowing an inference
adapter to emit a detection at a video frame's PTS on an already-declared KLVA
PID.

For decoded-frame inference, `VMTIMetadataEmitter` is the recommended bridge
from a named `InferenceContext` result to that packet. It preserves a matching
correlated ST 0601 parent or creates a minimal one for a media-only source, and
always emits at the frame's synchronous PTS. The frame timestamp is UTC; the
emitter requires a mission leap-second value or correlated parent Item 136 and
writes the corresponding MISP timestamp into ST 0601/ST 0903.

For a media-only input, declare a new first-party KLVA stream when constructing
the transformer. The output PMT is version-bumped (including the 31-to-0 wrap),
retains every source stream and descriptor, and includes the requested ST 1402
descriptors before any injected metadata is emitted:

```python
from stanag4609.transport import MetadataSTDDescriptor, synchronous_klv_stream

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

If the input already declares a synchronous metadata elementary stream, emit
new packets on that PID instead. Supplying another synchronous
`additional_metadata_stream` is rejected under ST 1402-13 rather than creating
a non-conforming second stream.

Several metadata services can share that one PID. Declare each service in the
PMT and use its ID when emitting; both the muxer and decoder enforce the
ST 1402-15 relationship:

```python
stream = synchronous_klv_stream(
    0x120,
    metadata_service_ids=(4, 9),
    metadata_std=metadata_std,
)

muxer.mux_sync_klv(0x120, telemetry_klv, pts=pts, metadata_service_id=4)
muxer.mux_sync_klv(0x120, detections_klv, pts=pts, metadata_service_id=9)
```

H.222.0 encodes leak rates in units of 400 bits/s and buffer capacity in units
of 1,024 bytes. `from_physical()` requires exact multiples and never silently
rounds a deployment value. Existing code-oriented arguments remain available
for applications that already own the 22-bit descriptor fields.

Decoded frames from an optional codec adapter can be joined to synchronous KLV
with `FrameMetadataCorrelator`. The correlator keeps independent rollover-safe
PTS epochs per program and supports exact, latest-relevant, or nearest-within-
tolerance policies. It deliberately does not attach asynchronous KLV because
ST 1402 does not guarantee its display synchronization. See
[AI sidecars](AI_SIDECARS.md#correlate-klv-with-decoded-frames) for a complete
example and offset semantics.

Passing monotonic `at` values to `feed()` and `emit_metadata()` enables the
ST 1402-02 output scheduler. It emits a PAT/PMT pair immediately after program
discovery and then at the recommended exact 125 ms interval. Repeated source
tables that arrive before the next scheduled slot are suppressed. During input
silence, call `poll_program_tables()` from an event-loop timer. The returned
`ProgramTableEmission` exposes lateness, skipped recommended slots, and whether
the actual gap remained strictly below the mandatory 250 ms boundary. Omitting
`at` retains the legacy/source cadence exactly.

This API emits one stable selected program from a single- or multi-program
input. It can retain an existing KLVA PID or add one while accepting the first PMT. Later,
properly versioned PMT updates may add, remove, or redefine any elementary
stream, change KLVA carriage, change descriptors, and select a different
declared PCR PID. A versioned PAT may also move the selected program to a new
PMT PID: the old association remains active until the replacement PMT arrives,
then the output switches atomically. The transformer preserves PAT and retained
elementary PID continuity counters; a new PMT PID begins at counter zero.
Synchronous metadata sequence numbers survive only for an unchanged
PID/carriage pair and restart at zero after a carriage change or new PID. A
KLVA removal or carriage change is accepted only when that PID has no partial
asynchronous KLV item or synchronous metadata access unit; otherwise the update
fails before buffered metadata is discarded. A changed stream definition is
likewise rejected while its PES is incomplete. The demuxer validates the whole
new route set before committing any removal or replacement, so a rejected PMT
leaves every prior route active. An in-flight PES on an unchanged PID and stream
definition remains valid across a compatible PMT update and completes under the
new current table. PAT and PMT version numbers remain independent.

The transformer repacketizes unchanged media PES
byte-for-byte using their original TS payload and adaptation-field boundaries,
thereby retaining source PCR/OPCR and random-access indicators. It preserves
program/elementary descriptors. Feed its output through `TransportRateShaper`
when inserted metadata changes the intended decoder-arrival schedule. The
shaper assigns exact constant-rate packet slots, emits bounded null padding,
and rewrites retained PCR in place from a caller-anchored 27 MHz output clock;
see [transport-rate shaping and PCR restamping](TRANSPORT_RATE.md). The
transformer does not automatically choose a bitrate or clock epoch because
those are properties of the deployment's physical output.

## Inspect and modify adaptation fields

Transport packets expose a complete typed adaptation-field view. Use
`dataclasses.replace()` to make an explicit immutable change, then encode the
adaptation bytes canonically:

```python
from dataclasses import replace

from stanag4609 import encode_adaptation_field, parse_transport_packet

packet = parse_transport_packet(packet_bytes)
if packet.adaptation is not None:
    changed = replace(packet.adaptation, random_access_indicator=True)
    adaptation_bytes = encode_adaptation_field(changed)
```

`adaptation_bytes` excludes the one-byte `adaptation_field_length` that belongs
in the transport-packet header. This low-level writer is intended for custom
packetizers and remuxers. Pass `stuffing_length` and
`extension_stuffing_length` when a fixed packet layout requires explicit
`0xFF` fill. Existing raw transport packets remain byte-preserved unless an
application deliberately rebuilds them.

## Reconnect without cross-session splicing

A socket reconnect, source restart, or failover is not an ordinary chunk
boundary. Call `reset()` before feeding bytes from the replacement source:

```python
report = transformer.reset()
logger.info(
    "FMV input reconnected",
    extra={
        "discarded_ts_bytes": report.demux.buffered_transport_bytes,
        "discarded_pes_bytes": report.demux.buffered_pes_bytes,
        "discarded_klv_bytes": report.metadata.asynchronous_klv_bytes,
        "discarded_metadata_au_bytes": report.metadata.synchronous_fragment_bytes,
    },
)

# The new session must provide fresh PAT and PMT tables.
for chunk in replacement_source:
    sink.write(transformer.feed(chunk).transport)
```

Reset is deliberately lossy and returns a typed `TransformerResetReport`. It
discards partial TS packets, PSI sections, PAT cycles, PES packets,
asynchronous KLV items, synchronous Metadata AU fragments, sequence state, and
the discovered input/output topology. The next input therefore cannot be
spliced to stale bytes and must establish a fresh PAT/PMT topology. It also
starts a fresh output continuity epoch, so the downstream transport connection
must be treated as a new session. A finished transformer can be reopened this
way. Configured limits, program selection, metadata processors, and
caller-owned processor state are retained.

Lower-level receivers expose the same boundary directly through
`TransportDemuxer.reset()` and `MetadataStreamDecoder.reset()`. Their reports
make intentional truncation observable without weakening the strict
end-of-stream checks performed by `finish()`.

The transformer does not yet retain unrelated source programs or null packets,
move an affected partial metadata item to a different PID/carriage, or switch
the selected program number. It intentionally converts a selected MPTS service
to SPTS rather than claiming lossless whole-multiplex rewriting. Those
constraints are enforced or documented rather than hidden behind a conformance
claim.
