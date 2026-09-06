# KLV and IMAP API

Low-level, dependency-free parsing primitives. Most applications should start
with `KLVStreamParser` or `iter_klv`, then pass recognized packets to a typed
MISB decoder.

ST 1201 mappings preserve special wire values by default because an invoking
standard can assign them item-specific semantics. Apply the standard's
parent-unspecified overflow default explicitly when that is your context:

```python
from stanag4609 import IMAPB, IMAPOverflowPolicy

height = IMAPB(-900, 19_000, 3)
encoded = height.encode(1_250.5)
decoded = height.decode(encoded)

# E0... and E1... resolve to -900 and 19,000 only when the parent document
# does not define another meaning.
bounded = height.decode(
    bytes.fromhex("e00000"),
    overflow_policy=IMAPOverflowPolicy.CLAMP,
)
```

::: stanag4609.klv
    options:
      members: true

::: stanag4609.imap
    options:
      members: true

## Multi-dimensional arrays

ST 1303 MDAPs keep their dimensions and row-major ordering explicit. The
invoking standard supplies the meaning of Natural and RLE element bytes; the
self-describing IMAP, Boolean, and unsigned-integer APAs select their own
element types.

```python
from stanag4609 import MDAP, MDAPAlgorithm, MDAPElementType, decode_mdap, encode_mdap

mask = MDAP(
    dimensions=(2, 3),
    element_size=1,
    algorithm=MDAPAlgorithm.BOOLEAN,
    elements=(True, False, True, False, False, True),
    element_type=MDAPElementType.BOOLEAN,
)
wire = encode_mdap(mask)
assert decode_mdap(wire).element_at(1, 2) is True

# EBytes zero is ST 1303's payload-free empty-array signal. The dimensions and
# selected APA remain present on the wire.
empty = MDAP((480, 640), 0, MDAPAlgorithm.BOOLEAN)
assert decode_mdap(encode_mdap(empty)).materialize() == ()
```

RLE packs remain compact after decoding. Use `element_at()` for random access;
`materialize(max_elements=...)` expands only under an explicit resource bound.

::: stanag4609.st1303
    options:
      members: true
