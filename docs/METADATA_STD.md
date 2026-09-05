# Metadata System Target Decoder

Synchronous metadata is not conforming merely because its PES packet has a
plausible PTS. H.222.0 defines two buffers and exact byte-time constraints:

- every complete TS packet for the metadata PID enters a fixed 512-byte
  transport buffer (`TBn`);
- bytes leave `TBn` at `metadata_input_leak_rate` and PES header/content bytes
  enter the descriptor-sized metadata buffer (`Bn`);
- all bytes associated with a synchronous metadata access unit are removed
  instantaneously when the system clock reaches its PTS;
- `TBn` must not overflow and must empty at least once per second;
- `Bn` must not overflow or underflow; and
- ST 1402-12 limits access-unit decoder delay to one second.

The library provides two complementary assurance paths. The regular FMV
verifier retains conservative PCR delay ranges and also runs exact occupancy
for every fully bracketed window with bounded memory. The exact model requires
enough PCR evidence to derive every transport byte's `t(i)` value. It never
substitutes packet position or wall-clock time when that evidence is missing.

## Exact recorded-stream audit

Collect demux events from a finite capture, retrieve the stream's typed
metadata STD descriptor, and run the aggregate model:

```python
from pathlib import Path

from stanag4609.transport import (
    KLVCarriage,
    PESStreamEvent,
    PMTEvent,
    ProgramClockEvent,
    StreamKind,
    TransportDemuxer,
    decode_metadata_std_descriptor,
    simulate_synchronous_metadata_pes,
)

demuxer = TransportDemuxer()
events = []
with Path("mission.ts").open("rb") as source:
    while chunk := source.read(1024 * 1024):
        events.extend(demuxer.feed(chunk))
events.extend(demuxer.finish())

clocks = [event for event in events if isinstance(event, ProgramClockEvent)]
metadata = [
    event
    for event in events
    if isinstance(event, PESStreamEvent)
    and event.kind is StreamKind.KLV
    and event.klv_carriage is KLVCarriage.SYNCHRONOUS
]
pmt = next(event.table for event in events if isinstance(event, PMTEvent))
stream = next(item for item in pmt.streams if item.elementary_pid == metadata[0].pid)
descriptor = decode_metadata_std_descriptor(
    next(item for item in stream.descriptors if item.tag == 0x27)
)

result = simulate_synchronous_metadata_pes(descriptor, metadata, clocks)
for issue in result.issues:
    print(issue.code, issue.requirement, issue.message)
```

`simulate_synchronous_metadata_pes` evaluates all supplied PES packets in one
model. This matters because metadata from several PES packets can coexist in
`Bn`; checking each packet independently can miss aggregate overflow.

The PCR sequence must bracket every source TS byte and must not cross a
declared timebase discontinuity. H.222.0 equations 2-4 and 2-5 determine
piecewise-constant transport rate between adjacent PCR samples. PTS and PCR
rollover are unwrapped on the same program timeline using exact rational
arithmetic.

## Lower-level exact input

Applications that already know transport byte arrival times can use
`SynchronousMetadataSTDModel` directly. Supply one `MetadataSTDByte` for every
TS byte on the metadata PID:

```python
from fractions import Fraction

from stanag4609.transport import (
    MetadataSTDByte,
    MetadataSTDDescriptor,
    SynchronousMetadataSTDModel,
)

descriptor = MetadataSTDDescriptor.from_physical(
    input_bits_per_second=1_600_000,
    buffer_bytes=16 * 1024,
)
events = (
    MetadataSTDByte(Fraction(0)),  # TS header: enters TBn only
    MetadataSTDByte(
        Fraction(1, 1_000),
        enters_main_buffer=True,
        removal_time=Fraction(1, 2),
        access_unit_byte=True,
    ),
)
result = SynchronousMetadataSTDModel(descriptor).simulate(events)
assert result.conformant
```

PES headers set `enters_main_buffer=True` but leave `access_unit_byte=False`.
That keeps PES overhead in occupancy while excluding it from the access-unit
delay calculation. The first-party PCR/PES adapter performs this classification
automatically and excludes each five-byte Metadata AU Cell wrapper from the
access-unit byte set.

## Live verification boundary

An exact input time for a byte between PCR samples is known only after the next
PCR arrives. `MetadataSTDStreamValidator` holds a bounded set of PES packets
until that sample arrives, derives every byte time, feeds a persistent
carriage-specific incremental model, and retires processed transport and
main-buffer events at the new clock watermark. Occupancy therefore remains
continuous across PCR windows without retaining the whole recording.

The state has independent bounds for PCR history, pending PES count and bytes,
timeline events, and future PTS-removal groups. Exceeding a bound, losing a
PCR bracket, changing the STD descriptor mid-epoch, or crossing a timebase
discontinuity makes affected PES packets explicitly unverifiable. The CLI emits
`st1402.metadata_std.*` errors for exact violations, a warning for incomplete
coverage, and an exact pass only when every observed configured metadata PES
was included and no model violation occurred.

`MetadataDelayValidator` remains alongside the exact model. Its closed PCR
delay ranges distinguish proven compliance, proven violation,
boundary-straddling, and unverifiable timing even when an exact occupancy audit
cannot be completed.

## Asynchronous output-leak audit

H.222.0 uses the same 512-byte `TBn` and descriptor-sized `Bn` for
asynchronous metadata, but bytes leave `Bn` continuously at
`metadata_output_leak_rate` instead of being removed at a PTS. When those
descriptor parameters are known, use the exact finite model directly:

```python
from fractions import Fraction

from stanag4609.transport import (
    AsynchronousMetadataSTDByte,
    AsynchronousMetadataSTDModel,
    IncrementalAsynchronousMetadataSTDModel,
    MetadataSTDDescriptor,
)

descriptor = MetadataSTDDescriptor.from_physical(
    input_bits_per_second=1_600_000,
    output_bits_per_second=800_000,
    buffer_bytes=16 * 1024,
)
result = AsynchronousMetadataSTDModel(descriptor).simulate(
    (
        AsynchronousMetadataSTDByte(Fraction(0)),
        AsynchronousMetadataSTDByte(
            Fraction(1, 1_000),
            enters_main_buffer=True,
        ),
    )
)

# A live receiver retains only scheduled future leak events.
live_model = IncrementalAsynchronousMetadataSTDModel(descriptor)
for byte_batch, clock_watermark in live_byte_windows:
    for issue in live_model.feed(byte_batch):
        report(issue)
    for issue in live_model.advance(clock_watermark):
        report(issue)
final_result = live_model.finish()
```

`asynchronous_metadata_std_bytes_from_pes` derives the lower-level byte events
for one PCR-bracketed asynchronous PES, and
`simulate_asynchronous_metadata_pes` audits several PES packets in one
aggregate model. The result reports `TBn` overflow/one-second emptying,
zero input or output rates, `Bn` overflow, and end-to-end decoder delay. The
incremental model preserves exactly the same leak and occupancy state across
arbitrary batches, bounds retained future departures with
`max_pending_events`, rejects arrivals behind the processed watermark, and
emits failures as soon as the available live timeline proves them.

ST 1402 asynchronous KLV uses the legacy SMPTE RP 217 registration profile,
which ordinarily signals `KLVA` but does not include a metadata STD descriptor.
Consequently the verifier does not invent buffer parameters or claim this check
automatically for such a stream. Applications that know the negotiated or
deployment-profile values can supply them per program and PID:

```python
from stanag4609 import FMVVerifier
from stanag4609.transport import MetadataSTDDescriptor

descriptor = MetadataSTDDescriptor.from_physical(
    input_bits_per_second=1_600_000,
    output_bits_per_second=800_000,
    buffer_bytes=16 * 1024,
)
verifier = FMVVerifier(
    asynchronous_std_descriptors={(1, 0x102): descriptor},
)
for chunk in incoming_chunks:
    verifier.feed(chunk)
report = verifier.finish()
```

The same mapping is accepted by `verify_fmv_stream` and `verify_fmv_file`.
Configured asynchronous PES packets use the same bounded PCR-window path and
report codes as synchronous occupancy. A missing mapping remains an explicit
`st1402.metadata_std.unverifiable` coverage warning.
