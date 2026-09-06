# Production readiness

Last audited: 5 September 2026 against `main` after release `0.3.0`.

## Verdict

The library is production-ready for the profiles marked **Complete
software-verifiable profile** in the [conformance matrix](CONFORMANCE.md), and
for the explicitly bounded transport, verifier, transformation, and sidecar
APIs described in the documentation.

This verdict does not mean universal STANAG 4609 certification. It does not
claim producer measurement truth, security-authority policy, every codec or
transport profile, an authenticated hosted video service, or independently
recorded VMTI interoperability. Those boundaries remain visible in
[known limitations](LIMITATIONS.md).

The public Python API remains pre-1.0. Patch releases are compatibility-safe;
minor-release evolution follows the guarded policy in
[API stability](API_STABILITY.md).

## Final audit evidence

| Area | Evidence | Result |
| --- | --- | --- |
| Core quality | strict Ruff and MyPy; Python 3.10 through 3.14; branch coverage above 90% | Pass |
| Public API | six supported import modules; 753 total exported names; existence, uniqueness, and reviewed-surface hashes checked in CI | Pass |
| Packaging | dependency-free universal wheel; wheel and sdist builds; strict Twine metadata/README validation; all eight entry points smoke-tested | Pass |
| Documentation | strict MkDocs build, generated API references, executable tutorial tests, conformance and limitation links | Pass |
| AI runtime | a real dependency-installed Ultralytics YOLO CPU prediction and two persistent ByteTrack calls without downloading weights | Pass |
| UI detections | Chromium tests for video overlays, attributed map and projected geometry, track trails, grouped activity feed, binned interactive timeline, filters, seeking, and live reconnect | Pass |
| Optional adapters | real PyAV, ONNX Runtime, Ultralytics, and official Triton HTTP/gRPC client gates | Pass |
| Supply chain | exact-SHA GitHub Actions, read-only default workflow permissions, non-persistent checkout credentials, owner-approved PyPI environment | Pass with the credential migration below |
| Repository security | secret scanning, push protection, Dependabot alerts/security updates, weekly dependency updates, and pinned CodeQL analysis | Pass |
| Dependency audit | clean installed Ultralytics/ByteTrack runtime after updating the disposable environment's package installer | Pass |
| Reference server boundary | loopback default, explicit remote opt-in and trusted hosts, security headers, fixed IP-literal UDP destination, per-process control token | Pass for a localhost reference; hosted deployment remains application-owned |

The audit deliberately excludes multi-hour soak campaigns because there is not
yet a suitable representative deployment dataset. That is future deployment
evidence, not a blocker for the bounded library APIs above.

## One release-operation follow-up

The PyPI workflow still uses a long-lived, environment-scoped API token. The
protected `pypi` environment requires approval by `trane293`, so a pull request
or ordinary push cannot publish. Nevertheless, migrate to PyPI Trusted
Publishing and revoke the bootstrap token as documented in
[the release guide](RELEASING.md). This is a supply-chain improvement, not an
unreported library-runtime limitation.

## Re-audit triggers

Repeat this audit before a stable `1.0`, after a public-surface change, after a
new parser accepts attacker-controlled structure, after changing the player
network boundary, or before claiming a new standard/profile as complete.

