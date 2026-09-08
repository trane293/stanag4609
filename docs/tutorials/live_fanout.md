# Transform live KLV and fan it out

An FMV processor often needs several outputs at once: a transformed transport
for another FMV system, typed metadata for an operator UI, and geospatial events
for a GIS or message bus. `LiveTransportTransformer` produces those views from
one incremental parse while carrying video, audio, and unrelated PES streams
through unchanged.

## Build the transform loop

The function below accepts any iterable of byte chunks, a binary output, and a
metadata callback. The policy is intentionally a pass-through; replace it with
redaction, validation, enrichment, or replacement logic for your application.

```python
from collections.abc import Callable, Iterable
from time import monotonic
from typing import BinaryIO

from stanag4609 import MetadataDecision, TimedKLVPacket, UASLocalSet
from stanag4609.transport import LiveTransportTransformer


def transform_stream(
    chunks: Iterable[bytes],
    output: BinaryIO,
    publish: Callable[[TimedKLVPacket], None],
) -> None:
    def policy(event: TimedKLVPacket) -> MetadataDecision:
        if isinstance(event.decoded, UASLocalSet):
            # Inspect or replace known fields here.
            return MetadataDecision.pass_through()
        return MetadataDecision.pass_through()

    transformer = LiveTransportTransformer((policy,))
    for chunk in chunks:
        batch = transformer.feed(chunk, at=monotonic())
        output.write(batch.transport)
        for event in batch.metadata:
            publish(event)

    final_batch = transformer.finish()
    output.write(final_batch.transport)
```

Processors return an explicit pass, drop, replace, or inject decision. Injected
packets continue through later processors, so separate stages can own policy,
AI enrichment, schema checks, and observability.

For a continuously connected source, call `poll_program_tables()` from the
service timer when no input arrives. This keeps PAT and PMT scheduling tied to
the output clock rather than the arrival cadence.

## Feed GIS independently

The streaming GeoJSON adapter also accepts arbitrary transport chunks:

```python
from collections.abc import Iterable, Iterator

from stanag4609.geojson import iter_geojson_feature_collections


def geojson_messages(chunks: Iterable[bytes]) -> Iterator[str]:
    for feature_collection in iter_geojson_feature_collections(chunks):
        yield feature_collection
```

When your policy changes metadata, create UI and GIS events from
`batch.metadata` or parse the transformed output. That keeps every downstream
consumer on the same version of the stream.

## Operate the pipeline safely

- Bound every input queue and choose what happens under overload.
- Correlate using PTS or UTC, never wall-clock arrival order.
- Call `finish()` for finite streams so truncation is reported.
- Preserve unknown KLV fields when changing known fields.
- Declare a KLVA PID before injecting into a media-only transport.
- Measure PAT, PMT, and PCR cadence at the output.
- Add `TransportRateShaper` when inserted data changes a constant-rate schedule.

For a multi-program transport, select `program_number` and use one transformer
per output service. Each transformer emits a self-contained single-program
transport. It does not retain unrelated programs or null packets; see
[known limitations](../LIMITATIONS.md) before using this as a whole-multiplex
rewriter.

Continue with [live transforms](../LIVE_PIPELINE.md) for injection, rate
shaping, discontinuities, and service changes, or [AI sidecars](../AI_SIDECARS.md)
for frame correlation and VMTI emission.
