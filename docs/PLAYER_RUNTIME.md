# Player runtime

This page documents the reference player's streaming and rendering contracts.
Start with [Reference player](PLAYER.md) for commands and common workflows.

## Recorded and live paths

Recorded mode prepares a deterministic, seekable MP4 before the server starts.
Metadata is decoded from the original MPEG-TS and keyed to media-relative PTS.

Live mode feeds each input chunk to both the KLV decoder and an FFmpeg pipe.
FFmpeg uses a bounded input probe, low-latency H.264 encoding, optional AAC, and
regular forced keyframes. Complete fragmented-MP4 units are numbered and
broadcast as soon as they are available; playback does not wait for input EOF.

The first observed media PTS becomes the session origin. Metadata keeps its
source PTS, so the browser and video element select state on the same clock.
`MetadataTimeline.media_start_pts` uses the earliest mapped audio or video PTS;
`video_start_pts` remains available separately. This preserves audio lead-in
without shifting metadata against the prepared media.

## Retention and late joins

The default live window retains 12 complete media fragments and 512 metadata
samples. `--live-media-fragments` and `--live-metadata-samples` change those
bounds.

A viewer behind the retained media window receives HTTP 409 and rejoins at a
current initialization segment and keyframe. Metadata events carry sequence
IDs. If history was lost, the server sends an explicit reset before replaying
the retained window. Browser SourceBuffer history is trimmed to 30 seconds.

After a gateway restart, a prior SSE cursor may be ahead of the new numbering
epoch. The server reports `cursor_ahead` and sends its current metadata window.
A future media cursor receives HTTP 409. The bundled browser re-creates its
MediaSource and rejoins automatically. Transient failures use bounded
exponential retries; unsupported media and transcoder failures remain terminal.

Media and metadata histories are broadcast buffers, not consumed queues. The
reference server is suitable for development and small trusted deployments.
Measure viewer capacity on the intended front end rather than treating its
functional fan-out behavior as a capacity estimate.

## Backpressure and source epochs

`LivePlayerGateway.feed()` blocks when FFmpeg cannot accept more input. It does
not place transport bytes in an unbounded queue. Call `finish()` only for a
clean source EOF. Use `close()` when a source epoch is abandoned.

A reconnect, capture restart, or failover begins a new epoch. Do not join the
new source to partial MPEG-TS, PES, or KLV retained from the prior connection.
The upstream transform and demux APIs expose `reset()` methods for this
boundary.

## Metadata endpoints

In streaming-metadata mode,
`GET /metadata/events?start=<seconds>&rate=<rate>` returns server-sent events.
It sends the sample effective at the requested playhead, then paces later
samples by media time. Keepalives bound idle periods. The browser closes the
connection while paused or stalled and reconnects after seeking or a playback
rate change. A paused seek requests only the effective sample.

`GET /metadata/summary?bins=<1..2048>&duration=<seconds>` returns a sparse
mission-wide detection overview. Only non-empty bins are included. Each bin
keeps at most four heavy-hitter labels plus `other_count`, preserving the total
without unbounded label state. The browser fetches this overview once, resamples
it to the canvas width, and retains detailed events only around the playhead.

Invalid, non-finite, repeated, negative, or unsupported query values receive
HTTP 400.

## Metadata presentation

The timeline scanner uses strict transport and KLV framing while retaining
malformed ST 0601 fields as per-sample diagnostics. Report-on-Change state is
scoped by program, PID, and service. Values expire after the refresh period and
a zero-length item clears them immediately. Before the first timestamped
sample, the display remains empty rather than showing future telemetry.

MISP Item 2 is labelled as MISP time and retains its integer microsecond count.
UTC is shown only when Item 136 supplies leap-second context; Item 137 is
applied when present. The UI does not relabel an unconverted MISP value as UTC.

When legacy and newer scalar representations coexist, the presentation layer
uses the representation selected by ST 0601.19 precedence. The decoded packet
continues to retain every field for diagnostics and round-trip encoding.

Normalized VMTI boxes, centroids, contours, and compact masks are drawn over
video when frame dimensions are available. Mask runs stay compact in timeline
JSON and expand only while painting. `OverlayDetection` exposes the original
mask runs, normalized contour, ground polygon, and ground-polygon source for
custom renderers.

Pixel boxes are projected through the four-corner frame footprint using
bilinear interpolation. The result is an approximate visualization, not a
terrain-aware camera intersection. Absolute VTarget Location takes precedence
over parent-relative offsets; either is presented as a resolved ground point.

## HTTP and browser security

Loopback is the default. Remote binding requires `--allow-remote` and at least
one `--allowed-host`; other Host values receive HTTP 421. UDP output accepts an
IP literal chosen when the process starts, and browser control calls require a
random per-process token.

Responses include same-origin, MIME-sniffing, framing, referrer, permissions,
and Content Security Policy headers. The CSP permits the packaged inline UI,
blob media, and the configured OpenStreetMap tile host. These headers are
defence in depth for a local reference tool, not a replacement for application
authentication and transport security.

## Deployment boundary

The included MSE gateway is useful for local tools and bounded low-latency
demonstrations. Sub-second glass-to-glass latency, adaptive bitrate, NAT
traversal, large viewer populations, and browser-to-browser media typically
belong in WebRTC or a production streaming service. Keep the same PTS-keyed
metadata boundary when substituting that media layer.
