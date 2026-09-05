# Presentation timestamp cadence

MISB ST 1402.2 §7.3 limits the difference between successive coded PTS values
to 0.7 seconds for each elementary stream. `PTSCadenceValidator` applies that
rule directly to the `PESStreamEvent` objects emitted by the demuxer:

```python
from stanag4609 import PESStreamEvent, PTSCadenceValidator

validator = PTSCadenceValidator()

for event in demuxer.feed(transport_chunk):
    if isinstance(event, PESStreamEvent):
        for issue in validator.observe(event):
            log.error(issue.message)
```

The exact 0.7-second boundary passes; one additional 90 kHz tick fails. State
is isolated by program and PID. The comparison uses the shortest signed
distance across the 33-bit PTS rollover, so rollover and modest presentation-
order reordering do not look like multi-hour gaps. An adaptation-field
discontinuity or a changed stream type on a reused PID starts a new baseline.

PES packets without PTS do not themselves establish a successive-PTS pair.
The separate H.222.0 §2.7.5 rule requires a PTS for the first access unit of
every elementary stream. `VideoAccessUnitPTSValidator` recognizes the
mandatory transport access-unit markers for MPEG-1/2 Video, AVC/H.264, and
HEVC/H.265 while retaining only three bytes per stream:

```python
from stanag4609 import PESStreamEvent, VideoAccessUnitPTSValidator

video_pts = VideoAccessUnitPTSValidator()

for event in demuxer.feed(transport_chunk):
    if isinstance(event, PESStreamEvent):
        for issue in video_pts.observe(event):
            log.error(issue.message)

for issue in video_pts.finish():
    log.error(issue.message)
```

Start-code prefixes split across PES packets are attributed to the PES that
contains their first byte. A transport discontinuity starts a new baseline,
because §2.7.5 also requires PTS on the first access unit after a decoding
discontinuity. The verifier applies the same rule to reconstructed Layer II
and AAC-LC audio frames.

The same clause permits a video PTS only when the PES packet contains the first
byte of a picture or access unit. A PTS-bearing PES with no complete marker is
held for at most three subsequent payload bytes, allowing a split start code
to complete without a false error. Once disproved—or at `finish()` for a finite
capture—it is reported as `st1402.pts.pts_without_access_unit`.

ST 1402.2 labels “PTS on every Motion Imagery frame” as a **MISB usability
recommendation**, not a mandatory conformance requirement. H.222.0 also has
conditional rules and explicit exceptions for later AVC access units, still
pictures, and very-low-frame-rate AVC with timing information. The library
does not turn the recommendation into an error and does not yet claim the full
conditional AVC timing model.

`FMVVerifier` runs this check automatically and reports an error such as
`st1402.pts.interval` or `st1402.pts.first_access_unit` with the program, PID,
source offset, and requirement citation.
