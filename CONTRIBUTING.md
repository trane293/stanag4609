# Contributing to stanag4609

Thank you for helping build a dependable, open FMV interoperability library.
Protocol changes are specification-led, test-driven, and deliberately small so
that every compatibility claim can be audited.

## Ground rules

- Do not commit or redistribute licensed standards, proprietary source code, or
  restricted media. Record standards provenance in
  `references/standards/manifest.json` and fixture provenance in
  `references/fixtures.json`.
- Base behavior on the exact published standard edition. Cite the requirement,
  section, table, or official vector in the test or requirement trace.
- Preserve unknown wire data losslessly. Typed encoders must reject unaudited
  values unless the public API requires an explicit raw-value wrapper.
- Keep live processing bounded: additions need explicit limits, backpressure,
  discontinuity behavior, and tests across arbitrary chunk boundaries.
- Keep the core package pure Python with no runtime dependencies. Optional
  codec, inference, GIS, and UI integrations belong behind extras or adapters.

## Development workflow

Create a short-lived branch from `main` using one of these forms:

- `feat/<short-description>` for a new capability
- `fix/<short-description>` for a bug correction
- `docs/<short-description>` for documentation only
- `test/<short-description>` for test infrastructure or coverage
- `refactor/<short-description>` for behavior-preserving restructuring
- `perf/<short-description>` for measured performance work
- `chore/<short-description>` for maintenance

Use lowercase kebab-case after the slash, for example
`feat/live-vmti-injection` or `fix/pes-discontinuity`.

Follow Conventional Commits for commit and pull-request titles:

```text
<type>(optional-scope): <imperative summary>
```

Allowed types are `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `build`,
`ci`, and `chore`. Useful scopes include `klv`, `st0601`, `st0903`,
`transport`, `audio`, `ui`, and `docs`. Use `fix`, rather than `bug`, as the
type for bug corrections. Mark an intentional breaking API or wire-behavior
change with `!` and explain it in the commit body.

Keep commits focused and independently understandable. Do not mix formatting,
generated artifacts, or unrelated cleanup into a protocol change.

## Test-driven protocol changes

1. Add or update the edition-specific requirement trace under
   `docs/requirements/`.
2. Add a failing normative, boundary, malformed-input, or integration test.
3. Implement the smallest coherent bounded behavior.
4. Verify lossless round trips and live chunk boundaries where relevant.
5. Run every quality gate before requesting review.

```console
ruff check .
mypy src
pytest --cov=stanag4609 --cov-branch --cov-report=term-missing
python -m build
```

The test suite requires at least 90% branch coverage, but protocol-critical
paths should be covered beyond that repository-wide floor.

Documentation changes also run a strict static-site build:

```console
python -m pip install -e '.[docs]'
mkdocs build --strict
```

API pages use `mkdocstrings` directives and resolve source modules from `src/`.
Keep user-facing symbols documented with type-complete docstrings; the strict
build must resolve every directive before a documentation change is accepted.

Examples must call real public APIs and either run in tests or name their
reproducible verification procedure. Clearly distinguish implemented,
experimental, and planned integrations; never imply vendor certification.

## Pull requests

The PR title follows the same Conventional Commit form and becomes the squash
commit subject. A PR description must include:

- **Why:** the user or interoperability problem being solved.
- **Standard basis:** exact editions and requirement/table/vector references.
- **What changed:** public API, wire behavior, limits, and error behavior.
- **Verification:** tests added and the quality-gate results.
- **Compatibility:** effects on API, transport streams, timing, audio/video
  passthrough, and unknown KLV preservation.
- **Risk and rollback:** known risks, limitations, and the safe revert point.

Use the repository PR template and remove no checklist item without explaining
why it does not apply. Keep draft PRs out of conformance tables; only merged,
tested behavior may be listed as implemented.

Reviewers should trace claims back to official standards, look for unbounded
allocation or latency, verify malformed-stream behavior, and confirm that no
third-party implementation was copied. At least one approval and passing CI are
expected before merging once repository protections are enabled.
