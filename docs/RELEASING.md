# Release and hosting process

The Python distribution name and import package are both `stanag4609`. This is
the exact technical term users search for and leaves language-neutral room for
future implementations. `OpenSTANAG` is a suitable umbrella identity for the
open-source ecosystem, but it is not the Python import name and should not be
conflated with a future commercial product brand.

## Current publisher configuration

The release workflow currently reads `PYPI_API_TOKEN` from the protected
`pypi` GitHub environment. Keep that environment restricted to version tags and
maintainer approval. Never put the token in a commit, issue, workflow file, or
documentation.

PyPI Trusted Publishing removes the long-lived upload credential. When making
that migration:

1. Verify the maintainer email and keep two-factor authentication enabled on
   PyPI.
2. On the PyPI project's **Publishing** page, add this GitHub Trusted
   Publisher:

   | Field | Value |
   | --- | --- |
   | PyPI project | `stanag4609` |
   | Owner | `trane293` |
   | Repository | `stanag4609` |
   | Workflow | `publish-to-pypi.yml` |
   | Environment | `pypi` |

3. Remove the `user` and `password` inputs from the PyPI publish action, add
   its `id-token: write` permission, delete the GitHub secret, and revoke the
   API token on PyPI.

Read the Docs publishes project slug `stanag4609` from the committed
`.readthedocs.yaml`, which installs the `docs` extra and builds the Material for
MkDocs site.

## Release gates

Choose a PEP 440 version that reflects the project's maturity. Publish normal
`0.x` releases so default package-index resolution remains useful; communicate
API maturity through the pre-1.0 version, package classifier, README status,
and explicit conformance boundaries. Do not publish development versions or
relabel the current API as stable.

1. Update the version in `pyproject.toml` and `src/stanag4609/__init__.py`.
2. Move the relevant `CHANGELOG.md` entries under the dated version heading.
3. Run the full unit and integration suite, static checks, strict docs build,
   package build, and `twine check --strict`.
4. Install both the wheel and source distribution in fresh virtual
   environments and smoke-test every console entry point.
5. Commit the release as `chore(release): prepare <version>`.
6. Create and publish a GitHub release from the reviewed commit with tag
   `v<version>`. The protected `pypi` environment requires approval before the
   immutable PyPI upload occurs.
7. Verify the PyPI page, installation, package provenance, documentation link,
   and Read the Docs version.

Before calling a release production-ready for its documented profiles, also
confirm that the public-API baseline changed only intentionally, CodeQL and all
optional-runtime jobs are green, secret scanning and push protection are on,
the protected `pypi` environment still requires the maintainer, and
`SECURITY.md`, the conformance matrix, and limitations match the shipped code.

PyPI files and versions cannot be replaced. If a released artifact is wrong,
yank it and publish a new version; never try to reuse its version number.

## Cross-language naming

Use the protocol number as the stable package identity and the language's
native registry conventions:

- Python distribution/import: `stanag4609`
- Rust crate: `stanag4609`
- JavaScript/TypeScript: `@openstanag/stanag4609`
- C library/repository: `openstanag-4609-c` / `libstanag4609`
- future standards: sibling packages such as `stanag4676`, with shared
  conformance vectors kept independent of any one language implementation

Keep a future hosted FMV product under its own customer-facing brand. It can
say “powered by OpenSTANAG” without making the neutral library look like a
thin client for one vendor's backend.
