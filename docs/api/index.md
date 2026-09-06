# Python API reference

These pages are generated from the type-annotated public docstrings in the
installed `stanag4609` source tree. They are rebuilt by strict documentation CI
for every commit and by Read the Docs for each published version.

## Import policy

Prefer symbols re-exported by `stanag4609`, `stanag4609.transport`,
`stanag4609.sidecar`, `stanag4609.audio`, `stanag4609.klv`, or
`stanag4609.player`. Their `__all__` declarations define the supported public
surface. A name imported from a deeper implementation module can change during
the pre-1.0 series unless a guide or reference page explicitly presents it as a
public extension point.

```python
from stanag4609 import FMVVerifier, KLVStreamParser, decode_uas_local_set
from stanag4609.sidecar import Parallel, VMTIMetadataEmitter
from stanag4609.transport import LiveTransportTransformer, TransportDemuxer
```

## Find the right API

| Task | Reference |
| --- | --- |
| Parse KLV and Local Sets | [KLV and IMAP](klv.md) |
| Read, write, or reconstruct UAS metadata | [ST 0601 metadata](st0601.md) |
| Enforce the minimum metadata profile | [ST 0902 MISMMS](st0902.md) |
| Read or produce AI detections | [ST 0903 VMTI](st0903.md) |
| Read or reconstruct range imagery | [ST 1002 range imagery](range.md) |
| Transform image coordinates or resolve geo-registration | [Transformations and geo-registration](transformations.md) |
| Demux, mux, transform, or stream MPEG-2 TS | [MPEG-2 transport](transport.md) |
| Inspect compressed video sequence properties | [Compressed video](video.md) |
| Diagnose an FMV recording | [Verification](verifier.md) |
| Compose local or remote inference | [AI sidecars](sidecar.md) |
| Decode audio or export GIS data | [Audio and geospatial exports](media.md) |
| Serve synchronized browser assets | [Reference player](player.md) |

The [conformance matrix](../CONFORMANCE.md) records which behavior has
normative and executable evidence. API availability alone is not a conformance
claim. The [API stability policy](../API_STABILITY.md) defines compatibility
guarantees and the machine-checked public-surface baseline.
