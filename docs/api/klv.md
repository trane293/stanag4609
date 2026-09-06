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
