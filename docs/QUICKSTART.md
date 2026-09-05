# Quickstart

This tour parses a live byte stream, reconstructs sparse ST 0601 state, and
shows the two ready-to-run ways to inspect a complete transport file.

## Install

```console
python -m pip install -e .
```

Install only the optional integrations an application needs:

```console
python -m pip install -e '.[audio-pyav,ai-onnx]'
```

## Parse arbitrarily chunked KLV

`KLVStreamParser` retains incomplete keys and values between calls. Call
`finish()` when a finite stream ends so truncation cannot pass silently.

```python
from stanag4609 import KLVStreamParser, ST0601_KEY, decode_uas_local_set

parser = KLVStreamParser(key_prefix=ST0601_KEY)

for network_chunk in source:
    for packet in parser.feed(network_chunk):
        uas = decode_uas_local_set(packet)
        print(uas.value(2), uas.value(13), uas.value(14))

parser.finish()
```

When producer facts are available, make otherwise-unobservable conformance
rules executable. This checks both the shared metadata time of birth and the
shortest IMAP length that retains the sensor's declared half-metre precision:

```python
from stanag4609 import (
    ST0601ValidationContext,
    VMTIValidationContext,
    decode_uas_local_set,
)

validation = ST0601ValidationContext(
    metadata_birth_timestamp=frame_misp_microseconds,
    imap_system_precisions={104: 0.5},
    vmti_context=VMTIValidationContext(
        vmti_frame_timestamp=detector_frame_misp_microseconds,
        frame_period_microseconds=1_000_000 / 30,
        frame_width=1920,
        frame_height=1080,
    ),
)
uas = decode_uas_local_set(packet, context=validation)
```

Item 2 is continuous MISP time, not UTC/POSIX time. Convert deliberately when
an application needs civil time, using packet Item 136 or a trusted historical
leap-second value:

```python
utc = uas.utc_timestamp()  # Uses Item 136 and optional Item 137.
raw_misp_microseconds = uas.misp_timestamp_microseconds

# If Item 136 is absent, the caller must supply the value for this instant.
utc = uas.utc_timestamp(leap_seconds=mission_leap_seconds)
```

See [MISP and UTC timestamps](TIMESTAMPS.md) before creating Item 2 from a
Python `datetime`; assigning a POSIX timestamp directly produces the wrong
instant even though the wire value is structurally valid.

The same facts can be selected per packet in a live MPEG-TS decoder. The
provider receives both the demultiplexed PES event and complete lossless KLV
packet, so an application can key negotiated precision or capture metadata by
program, PID, service, time, or packet identity without global mutable state:

```python
from stanag4609 import MetadataStreamDecoder, ST0601ValidationContext

def st0601_context(event, packet):
    facts = deployment_facts[(event.program_number, event.pid)]
    return ST0601ValidationContext(
        metadata_birth_timestamp=facts.birth_time_for(packet),
        imap_system_precisions=facts.imap_precisions,
    )

decoder = MetadataStreamDecoder(st0601_context_provider=st0601_context)
for pes_event in demultiplexed_metadata:
    for metadata_event in decoder.feed(pes_event):
        consume(metadata_event.decoded)
```

Returning `None` explicitly means no external facts are available for that
packet. Invalid provider values fail rather than silently disabling contextual
validation.

For Item 74, the enclosing Item 2 timestamp becomes the VMTI parent timestamp.
This detects a missing embedded timestamp when the detector processed a
different frame and rejects parent-relative target offsets that are more than
two frame periods stale.

Coded values remain integer-compatible while exposing the names and component
fields defined by ST 0601:

```python
from stanag4609 import (
    GenericFlagData,
    LaserPRFCode,
    PlatformStatus,
    WeaponLoad,
)

assert uas.value(125) is PlatformStatus.EGRESS
assert uas.value(47) & GenericFlagData.IMAGE_INVALID

load: WeaponLoad = uas.value(60)
print(load.station_number, load.substation_number,
      load.weapon_type, load.weapon_variant)

# Raises ValueError: every digit must be in 1..8, with three or four digits.
LaserPRFCode(1809)
```

This typed surface covers the icing, field-of-view, operational, positioning,
platform-status, sensor-control, legacy weapon, and Laser PRF coded items.
Existing comparisons, JSON serializers, and integer-oriented applications
continue to see integer-compatible values.

When producing a fixed mapped field that defines the ST 0601 `Out of Range`
sentinel, a numeric measurement beyond the representable physical domain is
encoded as that sentinel rather than clipped or rejected:

```python
from stanag4609 import SpecialValue, encode_field_value

assert encode_field_value(6, 25.0) == encode_field_value(
    6, SpecialValue.OUT_OF_RANGE
)
```

Other invalid values still fail. Supply `SpecialValue.OFF_EARTH` explicitly for
an applicable geospatial item because the encoder cannot infer whether an
invalid coordinate means an off-earth observation or bad producer data.

ST 0601 uses Report-on-Change: many packets intentionally omit unchanged
values. Reconstruct the receiver-visible view when presenting current state.

```python
from stanag4609 import ReportOnChangeState

state = ReportOnChangeState()
for packet in klv_packets:
    snapshot = state.observe(packet)
    print(snapshot.value(13), snapshot.value(14), snapshot.expired_tags)
```

## Inspect a transport stream

For visual inspection, launch the bundled localhost-only reference player. It
uses FFmpeg to prepare browser-compatible media while the Python demuxer builds
the synchronized metadata timeline.

```console
stanag4609-player mission.ts
stanag4609-player mission.ts --stream-metadata
stanag4609-player - --live
```

The second form replays telemetry incrementally through a bounded SSE channel
and resynchronizes it when the browser seeks, pauses, stalls, or changes speed.
The third form reads TS bytes from stdin, incrementally transcodes media into
one-second fragmented MP4 units, and delivers live metadata through SSE; it
does not wait for the source to end.

For data workflows, export one JSON feature collection per metadata update:

```console
stanag4609-export-geojson mission.ts metadata.geojsonl
```

The output is line-delimited GeoJSON, so consumers can process it incrementally
without loading the entire mission into memory.

## Continue

- Use [the live pipeline](LIVE_PIPELINE.md) to change KLV in flight.
- Use [AI sidecars](AI_SIDECARS.md) to correlate decoded frames and emit VMTI.
- Use [the adapter guide](ADAPTERS.md) to keep third-party dependencies outside
  the core.
