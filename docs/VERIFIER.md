# Verify and debug FMV

`stanag4609-verify` reads a finite MPEG-2 transport stream and reports its
structure, metadata, timing, and conformance findings. Use it while developing
a producer, at an ingest boundary, or when a recording behaves differently
across FMV applications.

## Run a first check

```console
stanag4609-verify mission.ts
```

The report starts with an overall result and a stream inventory. Findings then
identify the affected standard requirement, program, PID, field, and source
offset when that context is available.

Four finding states appear in text, JSON, and HTML reports:

| State | Meaning |
| --- | --- |
| `PASS` | The available evidence satisfies an implemented check. |
| `WARN` | The stream is usable, but evidence is incomplete or operationally risky. |
| `ERROR` | The input violates an implemented check in the selected profile. |
| `N/A` | The check does not apply to this input or profile. |

The command exits with status `0` when no error findings exist, `1` after a
completed verification with errors, and `2` when the input or command cannot be
processed. Warnings do not change a passing exit status.

## Choose an output format

Text is designed for a terminal. JSON is the stable integration boundary for
CI and services:

```console
stanag4609-verify mission.ts --format json > verification.json
```

HTML produces a self-contained, printable report with no CDN or JavaScript
dependency:

```console
stanag4609-verify mission.ts --format html > verification.html
```

Applications should check the JSON `schema_version` and make decisions from
finding `code` values. Human-readable messages may become clearer over time.

## Select a profile

The default `mismms` profile applies the ST 0902 minimum-metadata policy as well
as the transport, KLV, ST 0601, media, and timing checks. Use `structural` for an
older or application-specific stream when the immediate question is whether
its transport and metadata can be read coherently:

```console
stanag4609-verify legacy-mission.ts --profile structural
```

Structural mode does not evaluate the ST 0902 application profile. It still
reports malformed transport, KLV, ST 0601 fields, carriage, and other
wire-observable errors.

A successful report applies only to the checks in that library version and
profile. The [conformance matrix](CONFORMANCE.md) lists the implemented
requirements, while [verifier checks](VERIFIER_CHECKS.md) explains how the
larger checks are evaluated.

## Add application policy

ST 0902 normally requires Security metadata and an MIIS Core Identifier. A
known application profile can disable either requirement:

```console
stanag4609-verify mission.ts \
  --no-require-security \
  --no-require-miis
```

Audio is optional in ST 1001. Require it when your receiving application needs
an audio channel:

```console
stanag4609-verify mission.ts --require-audio
```

When your deployment owns a marking policy, state it explicitly:

```console
stanag4609-verify mission.ts \
  --security-classification secret \
  --country-coding-method genc-three-letter \
  --classifying-country USA \
  --security-sci-shi 'SI/TK//' \
  --security-caveats FOUO \
  --require-release-country USA \
  --require-release-country CAN \
  --object-country-coding-method genc-three-letter \
  --minimum-security-metadata-version 12
```

Country options are repeatable uppercase codes. SCI/SHI values keep their ST
0102 slash framing. Omitted fields still receive structural and cadence checks;
the verifier does not guess a classification or code-list authority.

Some image-profile facts cannot be recovered from the encoded video. Supply
producer-known acquisition and conversion facts when you have them:

```console
stanag4609-verify mission.ts \
  --source-aspect-ratio 16:9 \
  --source-scan progressive \
  --conversion-scan progressive \
  --source-form digital \
  --conversion-form digital
```

`--conversion-scan` and `--conversion-form` may be repeated in pipeline order.
If these facts are omitted, the corresponding checks remain unresolved rather
than being inferred from transport timestamps or codec headers.

## Use the Python API

For a file, the convenience function returns the same report object used by the
CLI:

```python
from stanag4609 import verify_fmv_file

report = verify_fmv_file("mission.ts")
if not report.ok:
    for finding in report.errors:
        print(finding.code, finding.requirement, finding.message)
```

For uploads, pipes, or a live capture service, keep one verifier for the input
epoch and feed it arbitrary chunks:

```python
from collections.abc import Iterable

from stanag4609 import FMVVerifier


def verify_chunks(chunks: Iterable[bytes]):
    verifier = FMVVerifier(max_findings=10_000)
    for chunk in chunks:
        verifier.feed(chunk)
    return verifier.finish(source="upload.ts")
```

The verifier retains bounded parser and finding state, not the complete media
file. Call `finish()` at a finite boundary so trailing truncation and incomplete
KLV can be reported. A reconnect or replacement source should use a new
verifier instance.

Advanced producers can pass `MISPImageContext`, asynchronous Metadata STD
descriptors, ST 0601 field expectations, or an ontology resolver. These options
are described in [verifier checks](VERIFIER_CHECKS.md) and the
[Python API reference](api/verifier.md).

## Use the result in CI

```console
stanag4609-verify artifacts/mission.ts --format json > verification.json
```

Archive the JSON report even when the command returns `1`; it is the artifact
that explains the failed gate. Build policy from the profile and finding codes,
not from whether a video player happened to open the file.

For a real-file walkthrough, continue with
[Inspect and debug an FMV recording](tutorials/inspect_fmv.md).
