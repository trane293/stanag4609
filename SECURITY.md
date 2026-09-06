# Security policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository. Do not
open a public issue for a suspected vulnerability or include sensitive FMV,
credentials, operational locations, or classified metadata in a report.

Include the affected version, the smallest safe reproduction, impact, and any
known mitigation. Maintainers will acknowledge a complete report as promptly as
possible, coordinate a fix and disclosure, and publish a security advisory when
the issue affects released users.

## Supported versions

Security fixes target the latest release. Users of an older release should
upgrade before reporting a result that is already corrected on `main`.

## Deployment boundary

The core parsers accept untrusted bytes under explicit size and element bounds.
The bundled player is a hardened localhost reference server, not an
authenticated multi-tenant service. Keep it on loopback by default. A remote
deployment must add TLS, authentication, authorization, origin policy, request
and process limits, logging, and network isolation appropriate to the data.

Model files, FFmpeg/GStreamer binaries, remote inference services, map tiles,
security-marking policy, and AI outputs are caller-controlled trust boundaries.
Do not load untrusted model artifacts or treat syntactically valid MISB Security
Metadata as authorization to disclose its contents.

