# Common pitfalls

FMV combines clocks, sparse metadata, compressed media, and geospatial fields.
Most integration bugs come from confusing two related concepts rather than from
KLV parsing itself.

## Treating MISP time as Unix time

ST 0601 Item 2 uses the MISP time system. A Unix timestamp placed directly into
Item 2 represents a different instant.

Use the conversion helpers described in [MISP and UTC timestamps](TIMESTAMPS.md).
Conversion to UTC requires the applicable leap-second information, normally
carried in Items 136 and 137 or supplied by the application.

## Displaying one packet instead of current state

ST 0601 uses Report-on-Change. A packet may omit a field because its value has
not changed.

Use `ReportOnChangeState` for a UI, export, or rule that needs the current sensor
state. Reading each packet in isolation will make stable fields appear to blink
in and out.

## Synchronizing on arrival time

Network arrival order is not presentation time. Buffering, retransmission, and
processing latency can move packets relative to their video frames.

Use PTS for media synchronization. Use MISP time when you need the observation
instant or correlation across systems. The [timestamp guide](TIMESTAMPS.md)
explains how those clocks relate.

## Assuming playback means conformance

A decoder can often play a stream that violates a profile requirement. The
reverse is also possible: a structurally valid transport may use media that a
particular browser cannot decode.

Use the verifier for standards and application-policy checks. Use the player or
your codec stack for media compatibility. Treat them as separate acceptance
decisions.

## Applying the wrong verifier profile

The default verifier profile includes the ST 0902 minimum-metadata policy. A
legacy or application-specific stream may intentionally omit those fields.

Use `--profile structural` when you want transport, carriage, and metadata
structure checks without the ST 0902 population policy. Choose profile settings
from the receiving application's actual requirements.

## Treating a pixel box as a ground polygon

A VMTI bounding box describes image pixels unless the metadata also supplies a
geospatial position or the application projects the box through a camera and
terrain model.

The reference player can interpolate a box into the current four-corner frame
footprint for visualization. That approximation is useful on a map but is not a
terrain-aware geolocation result.

## Updating a decoded mapping and losing unknown fields

Converting a packet to an ordinary dictionary can discard wire order, duplicate
extension fields, and unknown values.

Use the lossless Local Set model and update helpers when modifying an existing
packet. They retain the original representation for fields you do not change.

## Injecting KLV on an undeclared PID

An MPEG-TS receiver discovers metadata through the program map. Writing KLV to
an arbitrary PID without updating the PMT does not create a usable metadata
stream.

Declare `additional_metadata_stream` when constructing the transformer for a
media-only input. Existing metadata PIDs can be selected from the demultiplexed
program information.

## Letting a slow consumer grow memory

A live inference service, browser, GIS sink, or network destination can fall
behind the source.

Give each consumer its own queue and choose a policy: backpressure for data that
must be retained, or an explicit drop strategy for latency-sensitive views.
The [live pipeline guide](LIVE_PIPELINE.md) and [AI sidecar guide](AI_SIDECARS.md)
show the available controls.
