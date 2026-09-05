# Range imagery API

Typed MISB ST 1002 Range Image Local Sets, Section Data packs, embedded
ST 0601 Item 97 values, and dependency-free plane-subtraction helpers.

::: stanag4609.st1002
    options:
      members: true

## Resolve omitted SPRM coordinates

ST 1002 makes the image center the effective row or column when the respective
SPRM coordinate is omitted. The Local Set does not carry the Collaborative
Sensor Image dimensions, so supply the actual decoded-frame dimensions:

```python
range_image = uas_local_set.value(97)
row, column = range_image.effective_sprm_coordinates(
    image_rows=1080,
    image_columns=1920,
)
```

An explicitly transmitted row or column wins independently; only its omitted
counterpart uses the corresponding center value. Invalid or non-integral image
dimensions are rejected rather than silently guessed.

## Reconstruct planar-fit samples

Decoded Section Data exposes its adjusted range MDAP and the three transmitted
plane parameters. Equation 10 reconstructs the original samples in the same
flattened MDAP order:

```python
from stanag4609 import reverse_range_plane

section = range_image.sections[0]
if section.plane is not None:
    original_ranges = reverse_range_plane(section.range_values, section.plane)
```

For a producer, `subtract_range_plane()` calculates the Equation 8 least-squares
fit when no plane is supplied and returns the Equation 9 residuals. IEEE NaNs
and IMAP special values remain unchanged. The caller still chooses the residual
MDAP's element width, IMAP bounds, and precision because those are properties of
the producer and its data—not facts the library can safely invent.
