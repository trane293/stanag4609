# Performance and bounded-memory benchmarks

The installed `stanag4609-benchmark-live` command measures the complete live
reference-player gateway against a finite MPEG-2 transport stream. It reports a
versioned JSON record so results can be compared without copying numbers from
terminal prose.

```console
stanag4609-benchmark-live \
  "samples/private/ffmpeg-mpegts-klv/Day Flight.mpg" \
  --output day-flight-live.json
```

Install the checksum-pinned inputs with `python scripts/fetch_public_fixtures.py`.
Use `--program-number N` for MPTS, and use the same `--chunk-bytes`, media
fragment window, and metadata window intended for deployment.

## Method

The timer includes gateway startup, incremental Python TS/KLV processing,
FFmpeg H.264/AAC transcoding, fragmented-MP4 parsing, and clean end-of-stream.
It excludes the preliminary SHA-256, `ffprobe`, and FFmpeg-version probes. The
source is read without real-time pacing, so media-seconds per wall-second shows
processing headroom—not glass-to-glass latency. Run a paced soak separately
when validating a particular capture interface or network.

The default 1,316-byte reads are exactly seven 188-byte TS packets. The
benchmark does not consume the broadcast histories while ingesting; therefore
`dropped_*` means intentionally evicted late-join history, not failed decoding
or missing transcoder output. A correct bounded run retains exactly the chosen
window after producing more items than fit.

`python_traced_peak_bytes` is the Python allocation peak observed by
`tracemalloc`. Process and child RSS are OS-reported high-water marks and
include interpreter/FFmpeg baseline memory. RSS units are normalized to bytes
on macOS and Linux. Compare runs on the same OS, architecture, Python, and
FFmpeg version.

## Pinned baseline

Measured 5 September 2026 on Apple Silicon, macOS 26.5.2, Python 3.10.13, and
FFmpeg 7.1.1 with the default 12-fragment/512-sample histories:

| Fixture | Media duration | Wall time | Headroom | Input rate | Python traced peak | Retained media |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Day Flight | 194.9 s | 16.35 s | 11.92× | 49.93 Mbit/s | 13.73 MB | 12 / 195 fragments, 7.64 MB |
| Night Flight IR | 370.8 s | 20.97 s | 17.68× | 64.91 Mbit/s | 26.52 MB | 12 / 371 fragments, 11.89 MB |
| Esri Truck | 148.2 s | 17.56 s | 8.44× | 47.01 Mbit/s | 24.52 MB | 12 / 149 fragments, 13.02 MB |

The exact machine-readable records retain source checksums, counts, platform,
and runtime identities:

- [Day Flight result](assets/benchmarks/live-player-day-flight.json)
- [Night Flight IR result](assets/benchmarks/live-player-night-flight-ir.json)
- [Esri Truck result](assets/benchmarks/live-player-esri-truck.json)

All six Day Flight and 18 Night Flight metadata samples remained in the
512-sample window. Esri Truck produced its expected 711 player-timeline samples;
512 remained and 199 older samples were explicitly reported as evicted. These
results establish bounded finite-file headroom on this machine. They do not yet
establish multi-day stability, network-loss behavior, viewer concurrency, or a
universal hardware performance guarantee.

CI separately exercises eight simultaneous HTTP viewers while 16 media
fragments and 16 metadata samples are published. Every viewer must receive the
same ordered stream and clean closure. Chromium also survives three consecutive
forced media-epoch conflicts by disposing and recreating its MediaSource. These
are deterministic correctness checks; they are not throughput or maximum-client
benchmarks.

Transport fault tests additionally drive 20,000 RTP packets across the 16-bit
sequence wrap while injecting deterministic losses, eight-packet delivery
permutations, and duplicates. They require exact ordered recovery of every
surviving packet, exact loss/duplicate accounting, and a hard 16-packet reorder
ceiling. A separate 2,000-write gateway campaign blocks the downstream media
pipe and verifies that the producer stops at that write with no hidden input
queue or premature byte accounting. These tests prove bounded control-flow
semantics, not the timing behavior of a real impaired network or FFmpeg process.

## Interpreting a deployment run

A useful acceptance run should record:

1. the source checksum or capture identity and media duration;
2. exact Python, FFmpeg, OS, architecture, chunk, and history settings;
3. throughput greater than the incoming transport rate with adequate margin;
4. retained counts no larger than the configured windows;
5. whether evictions were expected because no consumer advanced its cursor;
6. process RSS across repeated or multi-hour epochs; and
7. receiver continuity, loss, reorder, and reconnect findings alongside these
   gateway measurements.

Do not compare the headline rate alone. A deployment is acceptable only when
its latency, resource ceiling, loss policy, media correctness, and synchronized
metadata behavior all meet that deployment's requirements.
