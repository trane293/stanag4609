# Transformations and geo-registration API

Typed MISB ST 1202 image transformations, ST 1601 geo-registration results,
ST 1602 composite-image metadata, and ST 1607 Segment/Amend receiver state.

## Build formula-defined transformations

Use the named constructors for the parameterizations defined by ST 1202. They
validate finite positive dimensions and populate the correct transformation
enumeration and coefficients.

```python
from stanag4609 import GeneralizedTransformation

chip_to_original = GeneralizedTransformation.for_chipping(
    scale_factor=2.0,
    center_line=100.0,
    center_sample=200.0,
    chip_height=40.0,
    chip_width=60.0,
)
assert chip_to_original.transform(20.0, 30.0) == (100.0, 200.0)

pixel_to_image = GeneralizedTransformation.for_csm_pixel_to_image(
    pixel_size_x=0.01,
    pixel_size_y=0.02,
    image_height=480,
    image_width=640,
)
```

`for_digital_zoom()` creates the centered special case of a chipping
transformation. Construct `GeneralizedTransformation` directly for a general
Child-Parent, Optical, or projective transformation received from an external
calibration process.

## Apply a transformation chain

Pass transformations in the ST 1202 image-to-ground order. The helper rejects
duplicates, undefined production types, and incorrect ordering. Set
`inverse=True` for ground-to-image conversion; the helper reverses the chain
and applies each inverse automatically.

```python
from stanag4609 import apply_transformation_sequence

line, sample = 20.0, 30.0
image_xy = apply_transformation_sequence(
    (chip_to_original, pixel_to_image),
    line,
    sample,
)
line_sample = apply_transformation_sequence(
    (chip_to_original, pixel_to_image),
    *image_xy,
    inverse=True,
)
```

## Resolve parallel geo-registration results

`MetadataTreeState` reconstructs sparse ST 1607 Report-on-Change branches. A
full MSID path identifies each parallel Amend result; the snapshot applies
root and parent inheritance before returning typed ST 1601 Item 98 metadata.

```python
from stanag4609 import MetadataSubstreamID, MetadataTreeState

state = MetadataTreeState()
snapshot = state.observe(uas_packet)
algorithm_path = (MetadataSubstreamID(7),)
registration = snapshot.effective_geo_registration(algorithm_path)

if registration is not None:
    print(registration.algorithm_name, registration.algorithm_version)
```

::: stanag4609.st1202
    options:
      members: true

::: stanag4609.st1601
    options:
      members: true

::: stanag4609.st1602
    options:
      members: true

The complete `MetadataTreeState` and `MetadataTreeSnapshot` reference remains
on the canonical [ST 0601 metadata API](st0601.md) page.
