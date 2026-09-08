# User-story audit

This audit looks at the library from the perspective of a developer arriving
from PyPI. It records where the current experience is already short and where a
future implementation could remove setup or glue code. These are product
recommendations, not current commands.

## What already works in one command

After installing `stanag4609` and FFmpeg, a developer can launch a generated
FMV stream directly:

```console
stanag4609-player --demo day
```

That is the strongest first-run experience in the project. It needs no source
checkout or third-party media and opens video, ST 0601 telemetry, and a map.

The other focused commands are also direct when the developer already has the
right input:

- `stanag4609-verify mission.ts` inspects a recording;
- `stanag4609-mux-esri video.mpeg metadata.csv mission.ts` creates FMV;
- `stanag4609-export-geojson mission.ts metadata.geojsonl` feeds GIS systems;
- `stanag4609-export-esri mission.ts metadata.csv` creates a parallel sidecar;
- `stanag4609-player mission.ts --udp-output 127.0.0.1:5000` demonstrates
  downstream relay; and
- `stanag4609-player - --live` accepts a transport stream on stdin.

## Where a new user still encounters friction

### Verification has no clean generated sample

The generated player demo is useful FMV, but its synthetic H.264 bitstream does
not contain ST 0604 embedded video timestamps. Running the verifier therefore
produces a legitimate profile error. A developer cannot currently go from
installation to a passing verifier report without obtaining a separate fixture
or understanding the profile boundary.

### AI and custom dashboard examples need a source checkout

The PyPI wheel contains the library and console entry points, while substantial
example programs are distributed in the source archive and repository. The
Ultralytics UI demo also needs PyAV, model weights, and a suitable FMV file.
These are reasonable dependencies, but the path is several commands longer
than the player demo.

### Public fixture discovery is repository-oriented

Fixture provenance is well documented and the repository has a checksum-aware
download helper. That helper is not a packaged console command, so a PyPI user
must manually download data or clone the project.

### The command family is discoverable but fragmented

Eight descriptive console commands work well in scripts. A new user must still
know which command to ask for before `--help` can guide them. There is no
top-level `stanag4609` command that lists workflows or delegates to subcommands.

### Live transformation starts at the Python API

The incremental API is the right boundary for services, but there is no generic
stdin-to-stdout transform command or declarative processor configuration. A
developer evaluating live pass-through, KLV filtering, or fan-out must write a
small Python service first.

## Recommended future improvements

### 1. Ship a verifier-clean generated fixture

Extend the demo generator with codec-correct ST 0604 timestamps and the minimum
metadata needed by a named verifier profile. Keep the current visual demo if
that work would complicate it; a separate `conformance` variant is clearer than
hiding required checks.

The intended experience would be:

```console
stanag4609-demo-samples demo --variant conformance
stanag4609-verify demo/stanag4609-conformance-demo.ts
```

This should be attempted only with authoritative placement rules and tests for
the produced elementary stream.

### 2. Add one top-level command

A `stanag4609` command with subcommands could make discovery easier while
keeping the existing entry points for compatibility:

```text
stanag4609 demo
stanag4609 verify mission.ts
stanag4609 play mission.ts
stanag4609 export geojson mission.ts metadata.geojsonl
```

The first screen should show tasks, not every parser option. The existing
commands can remain thin aliases to the same Python functions.

### 3. Package a fixture manager

Promote the checksum-aware repository helper into an optional user command,
with clear license and provenance output:

```text
stanag4609 samples list
stanag4609 samples fetch esri-truck
```

Downloaded media should stay outside the wheel and repository. The command
should verify size and SHA-256 before reporting a fixture as ready.

### 4. Package the AI demonstration

Provide a console entry point that prepares player assets, runs a selected
adapter, and opens the detection UI without a source checkout:

```text
python -m pip install 'stanag4609[ai-ultralytics,video-pyav]'
stanag4609 ai-demo mission.ts --weights yolo11n.pt --track
```

Keep model downloads explicit and show the selected classes, confidence,
device, and tracker before inference starts. A deterministic no-model mode
would remain useful for testing the VMTI path offline.

### 5. Package the custom dashboard template

The current reference player already covers most viewer needs. If developers
ask for a starter application, expose the custom dashboard as an installed
template or scaffolding command rather than a second permanent player product:

```text
stanag4609 init-web fmv-viewer
```

The generated project could contain the minimal HTML client and a small Python
server using `prepare_player_assets()`, ready to adapt to FastAPI, Django, or a
separate frontend.

### 6. Add a live pipeline starter

A generated service skeleton would reduce boilerplate without trying to encode
arbitrary policy in CLI flags:

```text
stanag4609 init-transform metadata-relay
```

It should include bounded input/output queues, reconnect epochs, a pass-through
processor, metrics hooks, and tests. Developers would replace the processor
body with redaction or enrichment logic.

### 7. Add an environment diagnostic

A `doctor` command could report the installed package version, Python version,
available optional runtimes, FFmpeg/FFprobe versions, and whether a model or
browser dependency is ready. It should not send telemetry or inspect files
unless the user names them.

## Suggested order

The verifier-clean generated fixture closes the most confusing gap in the
current learning path. A top-level command and packaged fixture manager would
then improve discovery for every workflow. Package the AI demo only after a
real VMTI fixture or design partner can exercise its defaults. Dashboard and
service scaffolding should follow demonstrated demand; the current player and
Python API are sufficient foundations in the meantime.
