# Reference player

`stanag4609-player` is a local browser application for exploring recorded or
live FMV. It combines video, current ST 0601 state, map geometry, metadata
diagnostics, and ST 0903 detections on one media timeline.

FFmpeg is required for browser-compatible video. The Python package keeps
FFmpeg behind this optional application boundary.

## Play a recording

```console
stanag4609-player mission.ts
```

The command prepares a seekable H.264/AAC asset, decodes KLV from the original
transport, opens a loopback server, and launches the browser. Stop it with
Ctrl-C.

Use a generated sample if you do not have an FMV recording yet:

```console
stanag4609-player --demo day
stanag4609-player --demo thermal
```

For a long recording, stream detailed metadata around the playhead instead of
loading the complete timeline JSON:

```console
stanag4609-player mission.ts --stream-metadata
```

The player keeps a sparse mission overview for the detection timeline and a
bounded window of detailed samples. Seeking restarts that window at the new
playhead.

## Read the display

The metadata panel reconstructs the current Report-on-Change state for each
program, PID, and service. It expires stale values and clears a field
immediately when the stream sends a zero-length item.

The map can show sensor position, frame center, target locations, and image
footprints. Pixel-space VMTI boxes are projected into the current four-corner
frame footprint for visualization. These projected polygons are approximate;
a target with an absolute or parent-relative VMTI location is shown separately
at its resolved WGS-84 position.

The overlay toolbar filters boxes, centers, confidence, and classes without
changing the metadata. Trails join observations that share a positive ST 0903
target ID. They represent object tracks only when the producer assigns
persistent IDs.

The detection timeline aggregates observations into a bounded number of time
buckets. Hover for the interval and class counts, or click, drag, and use the
keyboard to seek. The activity feed groups nearby observations and keeps a
rolling window instead of adding one row for every detection in the mission.

Add `?basemap=off` to the player URL for the network-free coordinate grid. The
default OpenStreetMap tiles are intended for this low-volume local tool. A
deployed application should use a tile service appropriate to its traffic and
license requirements.

## Process a live stream

`--live` starts playback while MPEG-TS bytes are still arriving. Read a growing
file or pipe a transport through stdin:

```console
stanag4609-player mission.ts --live
ffmpeg -i 'udp://0.0.0.0:5000' -map 0 -c copy -f mpegts - \
  | stanag4609-player - --live
```

To exercise the same bounded live path from a complete file, pace it at wall
clock speed:

```console
stanag4609-player mission.ts --simulate-live
stanag4609-player mission.ts --simulate-live --playback-rate 2
```

The live gateway applies backpressure when FFmpeg cannot consume input. An
upstream UDP or service adapter must decide whether to block, restart the input
epoch, or provision more capacity. It must not discard arbitrary TS bytes.

Single-program input is selected automatically. Choose the service explicitly
for a multi-program transport:

```console
stanag4609-player multiplex.ts --program-number 7
```

The same selection is used for media and metadata, preventing video from one
service from being paired with telemetry from another.

## Relay MPEG-TS over UDP

Configure one operator-approved destination when starting the player:

```console
stanag4609-player mission.ts --udp-output 127.0.0.1:5000
```

The browser then exposes start and stop controls for that destination. Receive
the complete transport in VLC with:

```console
vlc udp://@:5000
```

Recorded mode starts a paced replay from the beginning. Live and simulated-live
modes tee the currently arriving transport. The relay sends only complete
188-byte TS packets and normally groups seven packets per datagram.

The same output can feed another FMV receiver that accepts MPEG-TS over UDP.
Discovery, multicast routing, session setup, and product-specific security
remain deployment concerns.

## Use the player assets in an application

Recorded applications can prepare the same media and JSON boundary without
starting the bundled server:

```python
from stanag4609.player.server import prepare_player_assets

assets = prepare_player_assets("mission.ts", "build/player")
print(assets.media, assets.timeline)
```

`scan_transport_file()` and `scan_transport_timeline()` provide the synchronized
timeline directly. Each sample contains JSON-ready fields, diagnostics,
overlays, and GeoJSON features. `summarize_detection_timeline()` provides the
same sparse detection overview used by the player.

Live services can use `stanag4609.player.LivePlayerGateway` and feed transformed
transport bytes directly. Its media and metadata buffers are typed boundaries
for another HTTP framework.

## Bind it safely

The server binds to `127.0.0.1:8765` by default. Override the port or FFmpeg
path as needed:

```console
stanag4609-player mission.ts --no-open --port 9000
stanag4609-player mission.ts --ffmpeg /opt/ffmpeg/bin/ffmpeg
```

Non-loopback binding requires an explicit opt-in and allowed Host values:

```console
stanag4609-player mission.ts --no-open --host 0.0.0.0 \
  --allow-remote --allowed-host fmv.example.internal
```

These controls reduce accidental exposure and DNS-rebinding risk; they are not
authentication. Put the player behind an authenticated, authorized,
TLS-terminating gateway with connection, request, process, and bandwidth limits
before using it beyond a trusted host.

See [player runtime](PLAYER_RUNTIME.md) for live retention, reconnect behavior,
HTTP endpoints, timing anchors, and rendering semantics. The
[dashboard tutorial](tutorials/web_dashboard.md) shows how to build a custom UI
from the same assets.
