# ADR 0002: Live demux–process–mux architecture

## Status

Accepted.

The timed-KLV event, bounded processor-decision layer, and program-selecting
pull-driven demux/process/remux orchestrator are implemented. Multi-program
inputs are deterministically reduced to a selected single-program output, with
safe PMT-PID reassociation. PCR/bitrate scheduling and restamping, PCR-to-UTC
mapping, optional frame/inference adapters, per-program PTS rollover handling,
and bounded synchronous frame/KLV relevance correlation are implemented.
Durable output sinks and deployment-specific session protocols remain
application boundaries under this decision.

## Context

The library must support more than file inspection. A representative workload
receives a live FMV transport stream, decodes selected video frames through an
optional codec backend, runs a truck detector, adds or updates ST 0903 VMTI in
ST 0601 Item 74, displays the detections, and forwards a conformant stream
without losing audio, unknown PIDs, descriptors, or metadata.

ArcGIS Pro's current FMV player consumes archived and live motion imagery with
KLV and exposes individual KLV streams. Its Video Multiplexer accepts separate
CSV, JSON, or GPX metadata for archived media, produces Transport Stream output,
and documents VMTI through MISB Tag 74. The multiplexer itself does not support
live input. These are interoperability targets, not dependencies:

- <https://pro.arcgis.com/en/pro-app/latest/help/analysis/image-analyst/the-full-motion-video-player.htm>
- <https://pro.arcgis.com/en/pro-app/latest/tool-reference/image-analyst/video-multiplexer.htm>

## Decision

The public architecture is a bounded, event-driven pipeline:

```text
byte source -> TS framing -> PAT/PMT discovery -> PES demux -> clock mapping
                                                        |
                                                        v
                              video / audio / KLV timed events
                                                        |
                                                        v
                      ordered processors (pass, drop, replace, inject)
                              |                 |                 |
                              v                 v                 v
                     TS remux/live output   KLV sidecar     JSON/CSV/GeoJSON
```

The dependency-free core owns MPEG-TS program discovery, PES reconstruction,
KLV parsing/writing, 33-bit timestamp rollover handling, UTC correlation,
metadata mutation, scheduling, and deterministic remuxing. Optional adapters
own compressed frame/audio decoding, browser delivery, and inference runtimes.
An adapter may attach decoded frames or detections to timed events, but it does
not become authoritative for transport clocks or KLV encoding.

Every event carries program, PID, stream description, source offset, and the
available PTS/DTS/PCR/UTC timing. Processors return explicit pass, drop,
replacement, or injection decisions. Immutable source objects and raw bytes
remain accessible so a no-op pipeline can reproduce or directly pass through
unmodified streams.

Processing is pull-driven by default: a caller does not receive more input
until it has consumed emitted events. Async sources and sinks use bounded
queues with an explicit overflow policy; silently unbounded buffering is never
the default. Video and every audio elementary stream pass through unless a
processor explicitly changes them.

The muxer will support two modes:

1. Repacketize changed PES/PSI while copying unaffected elementary payloads.
2. Build a new ST 1402 metadata elementary stream and update PMT descriptors
   when input media has no KLV stream, as with the supplied raw video fixture.

Output branching is first-class. One processing run may simultaneously emit a
live/archive TS, raw timestamped KLV, normalized JSON/NDJSON, GeoJSON features,
and an ArcGIS-compatible metadata sidecar. "ArcGIS-compatible" will only be
claimed for fixtures validated by ArcGIS or its documented schema.

## Consequences

AI inference latency can be isolated from ingestion and governed by a chosen
backpressure/drop policy. Metadata can be edited without transcoding video or
audio. The same pipeline powers the reference web UI, command-line tools, and
server applications. A standards-correct muxer is more work than byte-splicing:
PMT versioning, continuity counters, PCR/PTS timing, metadata access-unit rules,
cadence, and bitrate scheduling all require explicit tests and conformance
traces.
