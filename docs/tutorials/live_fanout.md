# Transform live KLV and fan it out

An FMV processor often needs three simultaneous outputs: a transformed transport
for another FMV product, typed metadata for an operator UI, and geospatial events
for a GIS or message bus. `LiveTransportTransformer` exposes those views from
one incremental parse.

```python
from time import monotonic

from stanag4609 import MetadataDecision, TimedKLVPacket, UASLocalSet
from stanag4609.transport import LiveTransportTransformer


def policy(event: TimedKLVPacket) -> MetadataDecision:
    if not isinstance(event.decoded, UASLocalSet):
        return MetadataDecision.pass_through()
    # Validate, redact, enrich, or replace here. Passing through preserves the
    # original KLV bytes exactly.
    return MetadataDecision.pass_through()


transformer = LiveTransportTransformer((policy,))
for chunk in source_chunks:
    batch = transformer.feed(chunk, at=monotonic())
    fmv_output.write(batch.transport)
    for packet in batch.metadata:
        ui_bus.publish(packet)

    # Call from a timer too when the input is silent.
    fmv_output.write(transformer.poll_program_tables(at=monotonic()).transport)

last = transformer.finish()
fmv_output.write(last.transport)
```

Processors return an explicit pass, drop, replace, or inject decision. Injected
packets continue through later processors, so a pipeline can separate policy,
AI enrichment, schema validation, and observability. Unchanged video, audio, and
other PES streams are not decoded or transcoded.

## Feed GIS independently

For recorded data or a parallel input branch, the streaming GeoJSON API consumes
arbitrary transport chunks and yields line-sized feature collections:

```python
from stanag4609.geojson import iter_geojson_feature_collections

for feature_collection in iter_geojson_feature_collections(source_chunks):
    gis_bus.publish(feature_collection)
```

This keeps the GIS consumer independent from the MPEG-TS sink. In an application
that modifies metadata, generate the UI/GIS representation from the transformed
`batch.metadata` events or from the transformed transport so every consumer sees
the same policy result.

## Operational rules

- Bound every input queue and define an overload policy.
- Use PTS/UTC for correlation; never substitute wall-clock arrival order.
- Call `finish()` on finite streams so truncation is reported.
- Preserve unknown KLV fields when updating a known field.
- Declare a KLVA PID before injecting into media-only input.
- Measure PAT/PMT and PCR cadence on the actual output clock.
- Use `TransportRateShaper` when inserted data changes the required physical
  constant-rate schedule.

For a multi-program input, pass `program_number=` and run one transformer per
selected service. Each instance emits a self-contained single-program
transport and ignores unrelated program PES/clock events. A versioned move of
that selected service to a new PMT PID is staged until its replacement PMT
arrives. Unrelated programs and null packets are not retained; the exact
boundaries are recorded in [known limitations](../LIMITATIONS.md).
