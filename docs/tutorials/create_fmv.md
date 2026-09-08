# Create FMV from video and metadata

This tutorial combines an existing video/audio file with an ArcGIS FMV
Multiplexer CSV. You will produce MPEG-TS, inspect the result, open it in the
player, and export its metadata again.

## Prepare the inputs

The media input may use any format that FFmpeg can demultiplex. Its encoded
video and audio streams are copied into the output rather than decoded. The CSV
needs a timestamp column and supported MISB field columns; the
[command-line reference](../CLI.md#add-an-arcgis-style-csv-sidecar-to-video)
lists the relevant options.

`TimeStamp` is interpreted as the ST 0601 Item 2 microsecond count. It is not
silently converted between Unix and MISP epochs. See
[MISP and UTC timestamps](../TIMESTAMPS.md) when preparing current timestamps.

## Multiplex the stream

```console
stanag4609-mux-esri Raw_Video.mpeg Raw_Metadata.csv mission.ts
```

The command asks FFmpeg to create MPEG-TS, adds a synchronous KLVA stream,
aligns the first metadata timestamp with the first video PTS, and moves the
completed file into place. It refuses to replace `mission.ts` unless you pass
`--force`.

## Inspect and play the result

```console
stanag4609-verify mission.ts --profile structural
stanag4609-player mission.ts
stanag4609-export-geojson mission.ts mission.geojsonl
```

The repository's ArcGIS sample has 866 CSV rows. With that input, the muxer and
verifier report this compact summary:

```text
wrote 866 metadata records to mission.ts
Metadata: 866 KLV packet(s), 866 ST 0601, 0 ST 0903, 0 unknown
Summary: 1 error(s), 0 warning(s), 26 passed, 2 not applicable
```

The remaining finding is `st0604.timestamp.missing`. The source MPEG-2 video
does not carry embedded frame timestamps, and remultiplexing cannot add them
without rewriting the compressed video bitstream. This does not prevent the
player or metadata export from working, but it may matter to an ingest policy.

The `structural` profile focuses on whether the transport, streams, KLV framing,
and metadata can be read coherently. The default profile also enables the ST
0902 minimum-metadata policy. Read [verify and debug FMV](../VERIFIER.md) before
turning a profile's exit status into an automated accept/reject rule.

## Keep a machine-readable report

```console
stanag4609-verify mission.ts --format json > mission.verification.json
```

The command exits with status `0` when the selected profile passes, `1` when it
reports errors, and `2` when the command or input cannot be processed. Finding
codes are stable identifiers for application policy and dashboards.
