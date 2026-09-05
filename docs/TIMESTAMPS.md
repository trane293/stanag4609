# MISP and UTC timestamps

## Embedded video timestamps

MISP requires Absolute Time inside compressed Class 1/Class 2 imagery
independently of metadata timestamps and MPEG presentation timestamps. MISB
ST 0604 carries it in H.262 user data or AVC/HEVC unregistered SEI messages.

```python
from stanag4609 import VideoTimestampStreamParser

parser = VideoTimestampStreamParser(stream_type=0x1B)  # H.264/AVC
for pes_payload in video_pes_payloads:
    for timestamp in parser.feed(pes_payload):
        print(timestamp.value, timestamp.resolution, timestamp.time_status.locked)
for timestamp in parser.finish():
    print(timestamp.value, timestamp.resolution)
```

Use `encode_h262_timestamp_user_data`, `encode_avc_timestamp_sei`, or
`encode_hevc_timestamp_sei` when producing elementary-stream units. These APIs
encode the mandatory identifier, ST 0603 Time Status, modified 64-bit value,
SEI message framing, and emulation prevention. Inserting the returned unit at
the codec-correct point for a particular frame remains the encoder/muxer's
responsibility.

`stanag4609-verify` inventories embedded timestamp resolution and lock status.
It reports an error when valid ST 0604 messages are fewer than recognized
video access units. PES PTS and ST 0601 Item 2 do not satisfy this separate
video requirement.

ST 0601 Item 2 is a sampled, microsecond-resolution coordinate on the
continuous MISP Time System. It is not a POSIX or UTC timestamp. Treating its
integer as `datetime.fromtimestamp(...)` without a leap-second adjustment can
produce a plausible-looking but incorrect civil time.

The library keeps Item 2's exact wire count available as
`uas.misp_timestamp_microseconds`. For compatibility, `uas.value(2)` remains an
aware `datetime` coordinate representing that same unadjusted count. The UTC
marker on that coordinate is a Python representation detail, not a claim that
the MISP value is already civil UTC.

## Decode MISP time as UTC

Use the packet convenience method when Item 136 is present:

```python
uas = decode_uas_local_set(packet)

print(uas.misp_timestamp_microseconds)  # Exact uint64 Item 2 value.
print(uas.utc_timestamp())              # Applies Items 136 and 137.
```

Item 137 is first added to the Item 2 count as the post-flight correction.
Item 136 is then subtracted after converting seconds to microseconds. If Item
136 is absent, supply the offset applicable at the timestamp from an
application-controlled historical table:

```python
utc = uas.utc_timestamp(leap_seconds=mission_leap_seconds)
```

The method intentionally raises instead of guessing. Using today's offset for
an old recording is not necessarily correct.

The lower-level conversion works without a decoded packet:

```python
from stanag4609 import misp_timestamp_to_utc

utc = misp_timestamp_to_utc(
    misp_microseconds,
    leap_seconds=mission_leap_seconds,
    correction_offset=post_flight_correction_microseconds,
)
```

## Produce Item 2 from UTC

Convert an aware UTC datetime before encoding:

```python
from stanag4609 import encode_uas_local_set, utc_to_misp_timestamp

misp_microseconds = utc_to_misp_timestamp(
    frame_utc,
    leap_seconds=mission_leap_seconds,
    correction_offset=post_flight_correction_microseconds,
)
packet = encode_uas_local_set(
    {
        2: misp_microseconds,
        65: 19,
        136: mission_leap_seconds,
        137: post_flight_correction_microseconds,
    }
)
```

`utc_to_misp_timestamp()` and `misp_timestamp_to_utc()` are exact inverses for
the same leap-second and correction inputs. They use integer microseconds
throughout.

## Player, GeoJSON, and AI behavior

The reference player labels Item 2 as MISP and shows its exact count. It adds a
separate UTC field only when current Item 136 state makes conversion possible.
GeoJSON records follow the same rule with `timestamp_time_scale` and optional
`utc_timestamp` properties.

`FrameEnvelope.timestamp_microseconds` is UTC/POSIX time because decoded-frame
and inference runtimes commonly use that representation. `VMTIMetadataEmitter`
requires `leap_seconds` for a media-only stream, or obtains it from a correlated
ST 0601 parent's Item 136. It also accounts for a retained parent Item 137 and
emits matching MISP timestamps in the parent and embedded VMTI set.

This separation is deliberate: MPEG PTS is a relative presentation clock,
MISP Item 2 is a continuous absolute time coordinate, and UTC is civil time.
Applications should not substitute one for another.
