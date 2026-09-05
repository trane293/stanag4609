# Program Clock Reference cadence

MISB ST 1402.2 §7.2 requires a Program Clock Reference at least once every
100 milliseconds. PCR is a sample of the encoder's 27 MHz System Time Clock
and is the time base used to synchronize the program's video, audio, and
metadata.

`TransportDemuxer` associates each PCR with every active program whose PMT
declares that PCR PID. Feed those events to `PCRCadenceValidator`:

```python
from stanag4609 import PCRCadenceValidator, ProgramClockEvent

validator = PCRCadenceValidator()

for event in demuxer.feed(transport_chunk):
    if isinstance(event, ProgramClockEvent):
        for issue in validator.observe(event):
            metrics.increment(f"transport.pcr.{issue.code}")
            log.warning(issue.message)
```

The validator uses encoded PCR values rather than wall time, so network jitter
does not contaminate a recording audit. The 100 ms boundary is accepted and a
single additional 27 MHz tick is reported. State is independent per program,
including when multiple programs share one PCR PID.

The composite 42-bit PCR clock wraps after its 33-bit base epoch.
`unwrap_pcr_ticks` maps it onto an unbounded integer timeline. An unannounced
regression is reported separately from an excessive interval. A transport
time-base discontinuity, or a PMT change that moves a program to another PCR
PID, reanchors that program without a false gap.

This receiver-side primitive detects intervals between observed PCRs. It cannot
prove that a program with no PCR observations is compliant, and it does not
measure the ±500 ns PCR accuracy/jitter constraint or rewrite clock references
after a bitrate-changing remux.

## Writing clock packets

`TransportMuxer.mux_pcr()` emits one adaptation-only packet on the PCR PID
declared by its PMT:

```python
from stanag4609 import ProgramClockReference

clock = ProgramClockReference(base=90_000, extension=0)
transport_sink.write(muxer.mux_pcr(clock))
```

The packet has exact 188-byte framing, PCR reserved bits and adaptation
stuffing, and the correct payload-continuity behavior. A PCR before the first
media packet shares its initial counter; later PCR-only packets retain the
last media payload counter and do not consume the next value. Pass
`discontinuity=True` only when beginning a declared new time base.

The muxer intentionally requires the application to provide each PCR value.
The correct value describes the scheduled arrival instant of that packet in
the resulting transport stream, so synthesizing it from a callback's wall
clock would be incorrect for buffered, file, or bitrate-shaped outputs.

For a live writer that does have the real output schedule, use
`ProgramClockScheduler`. Anchor its 27 MHz encoder clock once, then poll it on
the same monotonic timeline used to write transport packets:

```python
from time import monotonic

from stanag4609 import ProgramClockReference, ProgramClockScheduler

clock = ProgramClockScheduler(muxer)  # 50 ms operational default
first = clock.start(ProgramClockReference(base=0, extension=0), at=monotonic())
transport_sink.write(first.packet)

while running:
    emission = clock.poll(at=monotonic())
    if emission is not None:
        transport_sink.write(emission.packet)
        if not emission.interval_compliant:
            metrics.increment("transport.pcr.late")
```

The PCR value is derived from the actual poll instant, not the ideal deadline,
so late output is never disguised with a stale timestamp. The default 50 ms
cadence is a library operational choice that leaves headroom inside the
standard's inclusive 100 ms maximum. Custom intervals up to 100 ms are
accepted. The scheduler emits at most once per poll and reports skipped slots,
lateness, exact elapsed time, and compliance. Its rational schedule does not
accumulate fractional drift; sub-tick instants are rounded down by less than
one 27 MHz tick.
