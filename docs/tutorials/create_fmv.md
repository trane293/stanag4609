# Create FMV from video and metadata

This end-to-end workflow combines ordinary video/audio with an ArcGIS FMV
Multiplexer CSV, verifies the generated MPEG-TS, plays it, and exports the timed
geospatial stream again.

## Inputs

The video may be any input FFmpeg can demux. Its elementary video and audio
streams are copied rather than decoded. The CSV must contain a timestamp column
and supported MISB fields; see the [command-line reference](../CLI.md) for schema
and timestamp options.

The ArcGIS `TimeStamp` column is treated as the exact ST 0601 Item 2 count, not
silently reinterpreted as civil UTC. For a current MISP-time producer, convert
UTC first as described in [MISP and UTC timestamps](../TIMESTAMPS.md).

```console
stanag4609-mux-esri Raw_Video.mpeg Raw_Metadata.csv mission.ts
```

The muxer asks FFmpeg for MPEG-TS with a 20 ms PCR period, adds one synchronous
KLVA stream, aligns the first metadata timestamp to the first video PTS, and
writes atomically. The source media streams remain encoded in their original
codecs; browser preparation may transcode them later.

## Prove the generated transport is usable

```console
stanag4609-verify mission.ts --profile structural
stanag4609-player mission.ts
stanag4609-export-geojson mission.ts mission.geojsonl
```

The repository's ArcGIS tutorial input has 866 CSV rows. The integration
baseline requires the generated file to expose 866 decodable, geospatial ST 0601
samples, MPEG-2 video, and MPEG-1 Layer II audio with no structural warnings or
errors. This is a regression guarantee for that fixture, not a claim that every
codec accepted by FFmpeg is part of the project's supported profile.

### Actual output: ArcGIS multiplexer fixture

This compact excerpt was captured by running the commands above with the
repository's ignored `Raw_Video.mpeg` and `Raw_Metadata.csv` fixture files:

```text
wrote 866 metadata records to mission.ts (PTS 138286..13184770)

FMV verification report
Source: mission.ts
Result: PASS
Input: 646431232 bytes, 3438464 TS packets, 1 program(s)
Metadata: 866 KLV packet(s), 866 ST 0601, 0 ST 0903, 0 unknown

Streams:
  program 1 PID 0x0100 video: stream_type=0x02, PES=4275
  program 1 PID 0x0101 audio: stream_type=0x03, PES=3108, mpeg-1-layer-ii, frames=6215, 48000 Hz, 2 channel(s)
  program 1 PID 0x0120 klv: stream_type=0x15, PES=866, synchronous

Summary: 0 error(s), 0 warning(s), 14 passed, 2 not applicable
```

File size, PIDs, timestamps, and packet counts are fixture observations, not
values that applications should hard-code.

## Choose the right conformance gate

`--profile structural` answers whether the transport, streams, KLV framing, and
decoded metadata are internally readable. The default profile also checks the
implemented STANAG/MISB requirements. Run both in production: reject structural
failures, then apply a deployment-specific policy to standard findings.

For automated producers, write the JSON report next to the generated asset:

```console
stanag4609-verify mission.ts --format json > mission.verification.json
```

The exit status is `0` for a passing selected profile, `1` for reported errors,
and `2` for invocation or input failures. See [verify and debug](../VERIFIER.md)
before making those statuses an ingest policy.
