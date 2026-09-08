# Build a video, map, and activity dashboard

The repository includes a small web application that combines prepared video,
playhead-synchronized MISB metadata, map geometry, telemetry, and an activity
feed. It uses the same public player assets that another Python, Node, or Rust
backend can serve.

From a source checkout, run:

```console
python -m pip install -e '.[dev]'
python examples/web_dashboard/server.py \
  "samples/private/esri-fmv/Truck.ts"
```

FFmpeg prepares `media.mp4`, while the library builds `timeline.json` from the
original transport. The server binds to `127.0.0.1:8766` by default and prints:

```text
FMV dashboard: http://127.0.0.1:8766/
```

![FMV operations dashboard with video, geospatial view, telemetry, and activity feed](../assets/screenshots/fmv-operations-dashboard.jpg)

## Follow one media clock

The page consumes three files:

- `media.mp4`, a browser-compatible representation of the source media;
- `timeline.json`, ordered metadata samples keyed by media-relative time; and
- `index.html`, the application itself.

On each video time update, the page selects the newest metadata sample that is
not in the future. That sample drives the map, telemetry, diagnostics, activity
feed, and overlays together. Browser arrival time never substitutes for media
time.

The example page is
[`examples/web_dashboard/index.html`](https://github.com/trane293/stanag4609/blob/main/examples/web_dashboard/index.html).
The bundled `stanag4609-player` adds a pannable OpenStreetMap layer, frame
footprints, VMTI boxes, a grouped activity feed, and an interactive detection
timeline.

The timeline aggregates observations into screen-sized time buckets rather
than creating one DOM node per detection. Each bucket keeps bounded class
counts, exposes the precise interval on hover, and can be clicked or dragged to
seek. This keeps long or detection-heavy missions responsive.

Use `http://127.0.0.1:8765/?basemap=off` with the bundled player when testing
without network access. Public OpenStreetMap tiles are suitable for this
low-volume reference UI; deploy a properly licensed tile service for a
production application.

## Prepare assets in your own backend

```python
from stanag4609.player.server import prepare_player_assets

assets = prepare_player_assets("mission.ts", "build/fmv-player")
print(assets.media)
print(assets.timeline)
print(assets.root)
```

Serve the returned files with normal HTTP range support. For long recordings,
page metadata by time or use the incremental interface instead of loading the
entire timeline into one response.

The reference player exposes that incremental mode directly:

```console
stanag4609-player mission.ts --stream-metadata
```

Its `/metadata/summary` endpoint provides a sparse mission overview. The
`/metadata/events` endpoint supplies detailed samples around the current
playhead using server-sent events. Seeking restarts the stream from the new
playhead, and bounded client storage prevents an all-day mission from becoming
an all-day browser heap.

## Adapt the pattern for live video

This tutorial prepares a complete file, which makes it deterministic and
seekable. A live deployment normally keeps synchronized media and metadata
paths:

```text
MPEG-TS -> transform/remux -> WebRTC, MSE, or HLS gateway -> video element
                 |
                 +-> bounded PTS/UTC metadata feed -> map, overlay, activity UI
```

Carry program, PID, PTS or UTC, discontinuity, and sequence identity over the
side channel. Put authentication, authorization, TLS, request limits, and audit
logging in the application or gateway; the tutorial server is local development
tooling.

Continue with [web clients](../WEB_CLIENTS.md) for rendering rules and
[live transforms](../LIVE_PIPELINE.md) for incremental MPEG-TS processing.
