# Build web clients

The library separates media transport, timed metadata, and rendering so a web
application can choose its own backend and browser delivery technology.

## Start from the reference player

```console
stanag4609-player mission.ts
```

The bundled player is a working integration example: it serves prepared or
live fragmented H.264/AAC media, synchronized metadata, canvas VMTI overlays,
an engineering-values panel, and a local map. It binds to localhost by default.
Its CLI enforces an explicit remote-binding opt-in and trusted HTTP Host list,
and responses include browser hardening headers. Those controls do not provide
identity, permissions, encryption, audit logging, or tenant isolation.
See [reference player](PLAYER.md) for its exact capabilities and limitations.

Application backends can call `scan_transport_file()` or
`scan_transport_timeline()` directly instead of launching the server. Timeline
samples contain current Report-on-Change state, overlay geometry, and GeoJSON
features suitable for serialization.

## Recorded-file architecture

1. Demux the original MPEG-TS and build the metadata timeline.
2. Prepare browser-compatible media without altering the source metadata clock.
3. Deliver media over normal HTTP range requests.
4. Deliver timeline JSON once or page it by media time for large recordings.
5. On each browser media-time update, select the latest applicable sample and
   draw overlays in intrinsic video coordinates.

This is deterministic and seekable. It is the default recorded-file mode.

## Live architecture

For live viewing, keep two explicitly synchronized paths:

```text
MPEG-TS input -> demux/process/remux -> media gateway -> browser video
                      |
                      +-> bounded metadata/GeoJSON feed -> browser overlays
```

The reference player implements this architecture with a bounded fragmented-
MP4/MSE gateway and numbered SSE metadata. Run `stanag4609-player - --live` and
pipe TS bytes into stdin. Applications can instead choose WebRTC, HLS, or
another media gateway according to latency and deployment needs. Preserve
program/PID, PTS/UTC, discontinuity, and sequence identity in the side channel;
arrival time is not a safe substitute for media time.

## Browser rendering rules

- Scale pixel geometry from the VMTI frame dimensions, not CSS layout size.
- Draw boxes using the library's normalized half-open pixel convention.
- Apply rotation/cropping in the same coordinate transform as the video.
- Treat missing sparse fields as unchanged only through Report-on-Change state.
- Clear ZLI values immediately and expire stale state at its defined boundary.
- Keep map updates and inference overlays keyed to the same timeline sample.

## Custom dashboard example

Run the repository's complete plain-HTML dashboard against any readable FMV
file:

```console
python examples/web_dashboard/server.py mission.ts
```

It uses the public asset API and displays video, a geospatial map, current
telemetry, diagnostics, and a playhead-driven activity feed. Follow the
[dashboard tutorial](tutorials/web_dashboard.md) to reuse the same files from a
different backend.

## Framework integration

The JSON/GeoJSON boundary is deliberately framework-neutral. A FastAPI,
Starlette, Django, Flask, Node, or Rust gateway can expose it without changing
the protocol core; React, Vue, Svelte, and plain DOM/canvas clients can consume
the same representation. The first checked-in client deliberately uses the
plain DOM so its timing and rendering behavior remain visible without a
framework build step.
