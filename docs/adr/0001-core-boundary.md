# ADR 0001: Pure-Python core and optional video backend

## Status

Accepted.

## Decision

KLV, MISB metadata, MPEG-TS/PES demultiplexing, validation, indexing, and
writing belong to a dependency-free pure-Python core. Compressed video decoding
is behind an optional adapter because practical H.262/H.264/H.265/JPEG 2000
decoding requires a codec implementation and is not a reasonable pure-Python
requirement. The reference UI will not make its backend a core dependency.

## Consequences

Metadata-only workflows remain portable and auditable. Player installations
must separately account for the license and deployment properties of their
chosen codec backend.
