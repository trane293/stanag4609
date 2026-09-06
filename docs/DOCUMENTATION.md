# Documentation roadmap

Documentation is a release artifact. The hosted site must make the protocol
library usable by a KLV specialist, a Python application developer, a media
engineer, and an AI/GIS integrator without requiring them to reverse-engineer
the tests.

## Information architecture

The site is organized by reader intent:

- **Get started:** installation, a first decode, CLI recipes, and the player.
- **Work with FMV:** live processing, AI, adapters, audio, web clients, media
  runtimes, and GIS/OSINT interoperability.
- **Standards and assurance:** source provenance, requirement traces,
  conformance scope, fixtures, timing cadence, and limitations.
- **Architecture:** decisions that explain stable boundaries and tradeoffs.
- **Contribute:** TDD workflow, review expectations, and documentation work.

## Tutorial coverage

Tutorials are completed alongside the capability they exercise so every
copy-paste example stays truthful and testable. The first five end-to-end guides
cover inspection, FMV creation, AI-to-VMTI, a recorded web dashboard, and live
transform/fan-out. Remaining gaps are tracked below.

| Tutorial | Required outcome | Status |
| --- | --- | --- |
| Inspect recorded FMV | Discover streams, decode KLV, show current state | Complete; public-fixture integration |
| Read, change, and re-encode ST 0601 | Preserve unknowns and checksum behavior | Guide remains; codec tests exist |
| Demux, process, and remux live video | Replace/inject KLV while preserving media/audio | Complete; timed integration fixture |
| Bring your own AI model | Convert model output to VMTI | Complete; executable generic example |
| Compose AI pipelines | Parallel detectors, sequential fusion/tracking | Complete; graph and overload tests |
| Deploy PyAV, ONNX, Ultralytics, and Triton | Video decode plus local and remote inference choices | Examples exist; PyAV, a real ONNX session, and official Triton HTTP/gRPC client objects are dependency-gated; an Ultralytics runtime job remains |
| Build a recorded web viewer | Timeline, overlays, metadata panel, map, seeking | Complete; real-FMV browser smoke test |
| Build a low-latency web viewer | Media gateway plus synchronized side channel | Measured deployment remains |
| Integrate FFmpeg and GStreamer | Pipes, appsink/appsrc, clocks, EOS, failure | Versioned runtime integration tests remain |
| Export to GIS/OSINT/FMV tools | GeoJSON, CSV, native transport, adapter patterns | APIs/tutorial complete; product verification remains |
| Operate and troubleshoot | Metrics, latency, continuity, malformed streams | Observable failure examples remain |

## Quality bar

Before the first stable release, the documentation must provide:

- a versioned API reference generated from public docstrings (complete);
- tested snippets or runnable examples for every primary workflow;
- architecture and sequence diagrams where timing or fan-out is otherwise hard
  to understand;
- screenshots or short captures of the player and overlay states;
- support matrices for standards, Python, FFmpeg, GStreamer, model runtimes, and
  verified downstream applications;
- security and deployment guidance for non-local web services;
- performance/backpressure recipes and benchmark methodology;
- glossary, troubleshooting, upgrade, and release notes;
- accessible light/dark presentation, searchable navigation, stable anchors,
  link checking, and a warning-free strict site build.

No example may contain a fabricated API, and no interoperability page may
present “should work” as verified support.

## Tutorial evidence policy

Tutorials show evidence produced by the repository wherever a visible result
helps a reader confirm success:

- CLI excerpts are copied from the exact documented command and kept compact;
- UI screenshots come from a runnable repository example using a named,
  checksum-pinned fixture, never from a mockup;
- captions identify the fixture and explain any intentionally visible warning
  or failure;
- volatile paths may be shortened, but counts, PIDs, timestamps, stream types,
  and findings are not rewritten; and
- a behavior change that alters captured output must update its tutorial in the
  same pull request.

The current JPEG captures live in `docs/assets/screenshots/`. Recreate the
verifier capture by generating its self-contained HTML report, and recreate the
dashboard and AI-sidecar captures by running their tutorials against the pinned
Esri `Truck.ts` fixture. Before committing, rerun the tutorial commands, inspect
each image at full size, and run `mkdocs build --strict`. Screenshots should
supplement accessible text and alt text; they must not be the only place a
result is documented.

## Build and host

The site is a static MkDocs Material project and can be hosted by any static
file service.

```console
python -m pip install -e '.[docs]'
mkdocs serve
mkdocs build --strict
```

The generated site is written to `site/`, which is intentionally excluded from
version control. CI builds it strictly on every change, including resolution of
every API-reference module. Read the Docs publishes the versioned site from the
committed configuration; remaining external-link checking work is listed in
[the roadmap](ROADMAP.md).
