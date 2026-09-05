# Inspect compressed video properties

The dependency-free core can incrementally inspect H.262/MPEG-2 Video sequence
headers/extensions plus AVC/H.264 and HEVC/H.265 sequence parameter sets. It
reports coded dimensions, display aspect ratio and frame rate (including from
HEVC VUI), progressive/interlaced source capability, chroma and bit depth,
profile, and level without decoding image pixels.

```python
from stanag4609 import HEVCVideoPropertiesParser

parser = HEVCVideoPropertiesParser()
for pes_payload in video_pes_payloads:
    for properties in parser.feed(pes_payload):
        print(properties.to_dict())
for properties in parser.finish():
    print(properties.to_dict())
```

`properties.misp_profile_level` checks H.262 Main Profile at Main/High Level or
AVC Constrained Baseline/Main/High at Level 1 through 4, or HEVC Main 10 from
Level 1 through 5.1, according to the selected stream parser. It does not
certify the entire codec bitstream. MISP's source aspect ratio describes
imagery at the sensor; a coded display aspect ratio cannot prove that
producer-owned fact.

All three parsers accept arbitrarily split PES payloads and apply an explicit
bound to retained start-code units. The FMV verifier records the latest
properties, detects changes, and reports scan and MISP profile/level results.
HEVC VUI parsing boundedly walks scaling-list, PCM, reference-picture-set, and
long-term-reference syntax to reach Annex E aspect-ratio and timing fields.
Whole-bitstream codec certification remains outside this property inspector.
