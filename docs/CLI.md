# Command-line tools

The package installs eight focused commands. Each returns a non-zero exit status
on failure. Commands that create output files refuse to replace them unless
`--force` is present.

## Verify and debug an FMV stream

```console
stanag4609-verify mission.ts
stanag4609-verify mission.ts --format json > verification.json
stanag4609-verify mission.ts --format html > verification.html
stanag4609-verify legacy-mission.ts --profile structural
stanag4609-verify mission.ts --security-classification secret \
  --country-coding-method genc-three-letter --classifying-country USA \
  --require-release-country CAN \
  --object-country-coding-method genc-three-letter \
  --minimum-security-metadata-version 12
stanag4609-verify mission.ts --source-aspect-ratio 16:9 \
  --source-scan progressive --conversion-scan progressive \
  --source-form digital --conversion-form digital
```

The verifier reports successful, missing, malformed, warning, and not-applicable
checks along with stream inventory and source context. HTML reports are
self-contained and printable. See
[verify and debug FMV](VERIFIER.md) for policies and exit status behavior.
Classification, country-code vocabularies, classifying-country, exact SCI/SHI
and caveat values, repeatable release/object-country requirements, and a
minimum Security Metadata version let a deployment enforce its own
authoritative ST 0102 marking policy. Producer-known source aspect ratio,
scan-stage, and analog/digital conversion-history options close the MISP image
checks that encoded headers cannot prove by themselves.

## Play video and metadata

```console
stanag4609-player mission.ts
stanag4609-player mission.ts --no-open --host 127.0.0.1 --port 9000
stanag4609-player - --live --no-open
stanag4609-player mission.ts --simulate-live
stanag4609-player --demo day
```

Non-loopback binding is deliberately two-step and still does not add
authentication or TLS:

```console
stanag4609-player mission.ts --no-open --host 0.0.0.0 \
  --allow-remote --allowed-host fmv.example.internal
```

Repeat `--allowed-host` for every hostname or IP that a trusted reverse proxy
or client will send in the HTTP `Host` header. Prefer keeping the player on
loopback behind an authenticated application gateway.

The player requires an FFmpeg executable. Use `--ffmpeg /path/to/ffmpeg` when
it is not on `PATH`. `--live` accepts a growing TS file or `-` for a TS byte
stream on stdin and begins browser playback before end-of-input. See
[reference player](PLAYER.md).

Generate both redistributable synthetic demo streams without launching a
browser:

```console
stanag4609-demo-samples samples/demo --duration 30
```

## Measure the live gateway

```console
stanag4609-benchmark-live mission.ts --output live-benchmark.json
```

This runs one complete transport through the same bounded FFmpeg/KLV gateway
used by `stanag4609-player --live`. Its versioned JSON records source identity,
throughput, media-to-wall-clock headroom, production and retention counts,
traced Python heap, process RSS, and runtime identities. Read
[performance benchmarks](BENCHMARKS.md) before comparing machines or treating
finite-file throughput as live latency.

For a wall-clock stability campaign, run fresh gateway and FFmpeg process
epochs against the same identified source:

```console
stanag4609-soak-live mission.ts --epochs 20 --rate 1 \
  --output live-soak.json
```

`--rate 1` follows the source's average media rate; larger values accelerate a
campaign while reporting any accumulated pacing lag. A runtime failure is
written into the JSON report and makes the command exit with status 1. See the
[benchmark methodology](BENCHMARKS.md) before treating a replay as capture- or
network-layer evidence.

## Add an ArcGIS-style CSV sidecar to video

```console
stanag4609-mux-esri raw_video.mpeg metadata.csv mission.ts
stanag4609-mux-esri raw_video.mpeg metadata.csv mission.ts --force
```

`--metadata-pid` accepts decimal or Python-style hexadecimal notation. The
command remuxes existing media with FFmpeg, then adds synchronous KLVA without
transcoding the media elementary streams.
The CSV `TimeStamp` value is the exact ST 0601 Item 2 microsecond count. The
adapter does not guess a historical UTC/MISP offset; see
[MISP and UTC timestamps](TIMESTAMPS.md).

## Export parallel metadata streams

```console
stanag4609-export-esri mission.ts metadata.csv
stanag4609-export-geojson mission.ts metadata.geojsonl
```

The CSV export reconstructs Report-on-Change values into ArcGIS FMV
Multiplexer-compatible rows. The GeoJSON export writes a feature collection per
metadata observation and can represent sensor, frame center, target, and image
footprint geometry. See [GeoJSON streams](GEOJSON.md).

## Discover every option

```console
stanag4609-player --help
stanag4609-demo-samples --help
stanag4609-benchmark-live --help
stanag4609-soak-live --help
stanag4609-verify --help
stanag4609-mux-esri --help
stanag4609-export-esri --help
stanag4609-export-geojson --help
```

These commands are deliberately thin wrappers over public Python APIs, making
the same behavior available to services and desktop applications without
shelling out to the CLI.
