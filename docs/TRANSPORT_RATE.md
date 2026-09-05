# Transport-rate shaping and PCR restamping

Changing or inserting metadata changes transport packet positions. A remuxer
cannot keep old PCR values and simultaneously claim that they describe the new
decoder-arrival timeline. `TransportRateShaper` assigns every 188-byte packet
to an exact rational output slot, fills idle slots with H.222.0 null packets,
and can rewrite retained PCR values against that authoritative schedule.

```python
from time import monotonic, sleep

from stanag4609 import ProgramClockReference, TransportRateShaper

started = monotonic()
shaper = TransportRateShaper(
    bit_rate=8_000_000,
    start_at=started,
    clock_anchor=ProgramClockReference(base=0, extension=0),
    clock_anchor_at=started,
    max_fill_packets=100_000,
)

for input_chunk in live_source:
    transformed = transformer.feed(input_chunk, at=monotonic()).transport
    for scheduled in shaper.feed(transformed, at=monotonic()):
        sleep(max(0.0, float(scheduled.starts_at) - monotonic()))
        transport_sink.write(scheduled.packet)

shaper.finish()
```

`starts_at` is the intended start of a packet on the constant-rate timeline;
successive slots are exactly `188 * 8 / bit_rate` seconds apart. A caller must
pace the physical sink accordingly. The class returns plans and bytes—it does
not sleep, take ownership of a socket, or pretend that an immediate buffered
write has exact decoder-arrival timing.

When `at` is provided to `feed()`, slots strictly before that availability time
are filled with payload-only PID `0x1FFF` packets. The exact slot at `at`
remains available to the source packet. `fill_until()` supports an idle-loop
without source bytes. Null continuity values increment for observability even
though H.222.0 leaves them undefined. A configurable packet bound rejects a
large delayed callback before allocating a large fill burst.

## PCR sample position

H.222.0 §2.4.2.2 defines PCR as the intended arrival time of the byte
containing the final bit of `program_clock_reference_base`. That byte ends 88
transmitted bits after the start of a normal TS packet. The shaper therefore
samples its anchored 27 MHz clock at:

```text
packet starts_at + 88 / bit_rate
```

The rational result is rounded down by less than one 27 MHz tick, matching the
integer division in H.222.0 equations 2-2 and 2-3. The six-byte PCR encoding is
replaced in place; PID, continuity, discontinuity, OPCR, adaptation data,
payload, and every other byte remain unchanged. Clock arithmetic wraps through
the MPEG 33-bit PCR-base epoch. A declared source discontinuity preserves its
flag and reanchors the shaped timeline at that PID's PCR. If the marker is on
an earlier packet without PCR, reanchoring is deferred until the next PCR on
the same PID; it cannot leak into another program or elementary stream.

Omit `clock_anchor` to use constant-rate slot/null scheduling while preserving
source PCR bytes. Supply it only when its value and `clock_anchor_at` describe
the same output timeline. This is still not a proof of ±500 ns physical PCR
accuracy: operating-system, socket, network, and receiver jitter remain beyond
the byte planner's control.
