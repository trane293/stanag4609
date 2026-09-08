# Quickstart

In a few minutes you will generate an FMV stream, open it in the reference
player, and read its synchronized metadata from Python.

## 1. Install the package

`stanag4609` requires Python 3.10 or newer. The demo and player also require
[FFmpeg](https://ffmpeg.org/download.html) on your `PATH`.

```console
python -m venv .venv
source .venv/bin/activate
python -m pip install stanag4609
```

On Windows PowerShell, activate the environment with
`.venv\Scripts\Activate.ps1`.

## 2. Generate a sample stream

```console
stanag4609-demo-samples demo --duration 4
```

This creates day and thermal MPEG transport streams in `demo/`. Each file has
H.264 video, a Layer II audio channel, and synchronized ST 0601 metadata.

Expected output:

```text
.../demo/stanag4609-day-demo.ts: 8 synchronized ST 0601 packets
.../demo/stanag4609-thermal-demo.ts: 8 synchronized ST 0601 packets
```

## 3. Open the player

```console
stanag4609-player demo/stanag4609-day-demo.ts
```

The command opens a local page with video, current metadata, and a map. Stop the
server with Ctrl-C.

You can also generate and open a temporary demo in one command:

```console
stanag4609-player --demo thermal
```

## 4. Read the synchronized timeline

Create `read_timeline.py` next to the `demo/` directory:

```python
from stanag4609.player import scan_transport_file

timeline = scan_transport_file("demo/stanag4609-day-demo.ts")
sample = timeline.samples[0]

print(f"metadata samples: {len(timeline.samples)}")
print(f"first sample: {sample.time_seconds:.3f} seconds")
print(f"sensor latitude: {sample.fields['Sensor Latitude']['value']:.5f}")
print(f"sensor longitude: {sample.fields['Sensor Longitude']['value']:.5f}")
```

Run it:

```console
python read_timeline.py
```

Expected output:

```text
metadata samples: 8
first sample: 0.010 seconds
sensor latitude: 49.28270
sensor longitude: -123.12070
```

`scan_transport_file()` demultiplexes the transport, decodes its KLV packets,
reconstructs ST 0601 Report-on-Change state, and maps metadata time to the media
timeline. Each sample also contains diagnostics, overlay geometry, and GeoJSON
features when the stream provides the required fields.

## Try your own file

Inspect a finite MPEG transport stream from the command line:

```console
stanag4609-verify mission.ts
```

Exit status `0` means the selected checks passed. Status `1` means verification
completed and found errors. Status `2` means the input or command configuration
could not be read.

Open the same file in the player:

```console
stanag4609-player mission.ts
```

## Next steps

- [Choose a workflow](WORKFLOWS.md) for inspection, live processing, AI, web,
  or GIS applications.
- Follow the [examples](EXAMPLES.md) for complete programs.
- Read [common pitfalls](COMMON_PITFALLS.md) before handling timestamps or
  sparse metadata.
- Browse the [Python API](api/index.md) when you know which module you need.
