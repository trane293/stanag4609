# PAT/PMT cadence

MISB ST 1402.2 requirement ST 1402-02 requires both the Program Association
Table and every Program Map Table to occur more than four times per second
throughout a program. Eight repetitions per second are recommended. Therefore,
an interval equal to 250 ms is not compliant; the recommended interval is
125 ms.

## Writing

`ProgramTableScheduler` wraps a `TransportMuxer` and preserves an exact rational
schedule without cumulative floating-point drift:

```python
from time import monotonic

from stanag4609 import ProgramTableScheduler

scheduler = ProgramTableScheduler(muxer)

while running:
    emission = scheduler.poll(at=monotonic())
    if emission is not None:
        transport_sink.write(b"".join(emission.packets))
        if not emission.interval_compliant:
            metrics.increment("st1402.psi_interval_violation")
```

Poll at least every 125 ms to meet the recommended cadence. The first poll
emits immediately. A late poll emits one current PAT/PMT pair rather than a
burst of stale repetitions. `missed_repetitions`, `late_by`, and
`interval_compliant` make the resulting timing quality explicit. Continuity
counters advance through the wrapped muxer exactly as they do for manual table
emission.

Calling `reset()` reanchors scheduling and clears metrics but intentionally does
not rewind the muxer's continuity counters. A custom interval is accepted only
when positive and strictly below 250 ms.

`LiveTransportTransformer` owns this scheduler in timed mode. Pass the same
monotonic clock to `feed(..., at=...)`, `emit_metadata(..., at=...)`, and
`poll_program_tables(at=...)`. Untimed calls intentionally continue to mirror
source PAT/PMT cadence for compatibility.

## Receiving and auditing

`PSICadenceValidator` accepts integer, finite float, or exact `Fraction` seconds
from any one monotonic timeline. Use host monotonic time for a live receiver or
PCR-derived `ProgramClockReference.seconds` for deterministic recording audits.

Call `start(at=...)` when the beginning of a program is known. This allows
`check(at=...)` to diagnose a PAT that never arrived. Without an explicit start,
the first observation or check establishes the monitoring baseline. Observing a
current PAT discovers the PMTs that must recur; removed programs stop producing
PMT diagnostics and newly announced programs receive independent acquisition
deadlines.

Only current tables count. For a multi-section PAT, every section in the cycle
must arrive before the table occurrence satisfies cadence. H.222.0 requires a
program definition to fit one `TS_program_map_section`, with both PMT section
numbers set to zero. `check()` is a snapshot and may be called by an idle timer
to diagnose silence even when no transport events are arriving.

`TransportDemuxer` emits a `PATEvent` only after a complete current PAT cycle is
available. Iterate `event.sections` when passing that cycle to this validator;
`event.programs` is the combined program loop and `event.table` remains the
section-zero compatibility view.

For a finite recording without an external arrival clock, use
`PCRBracketedPSICadenceValidator`. Feed `PATEvent`, `PMTEvent`, and
`ProgramClockEvent` objects in demux order:

```python
from stanag4609 import PCRBracketedPSICadenceValidator, TransportDemuxer

demuxer = TransportDemuxer()
cadence = PCRBracketedPSICadenceValidator()

for event in demuxer.feed(transport_bytes):
    for issue in cadence.observe(event):
        print(issue.table, issue.minimum_interval, issue.current_source_offset)
```

The adapter never estimates packet time from file byte position or an assumed
bit rate. Instead, each table occurrence receives the PCR interval that bounds
its arrival. It reports only when the smallest possible separation between two
occurrences is at least 250 ms. Thus, an uncertain interval is left
unreported, an exact 250 ms minimum fails the strict “more than four times per
second” rule, and clock discontinuities, regressions, or PCR-PID changes reset
the proof. Pending occurrences are bounded per program for live use. This
conservative receiver is integrated into `FMVVerifier`.
