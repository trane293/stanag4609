# Agent development instructions

These instructions apply to the entire repository. Read this file before making
changes, then read the documents linked under **Required context**.

## Project objective

Build a production-quality, standards-accurate, pure-Python toolkit for
contemporary STANAG 4609 Full Motion Video and the MISB standards commonly used
with it. Prioritize correctness, interoperability, bounded live processing,
clear evidence, and useful developer workflows over feature count.

Do not chase archaic, obscure, unavailable, or untestable profiles merely to
increase claimed coverage. Add standards support when authoritative text,
credible current usage, and representative tests justify it. Record unsupported
or externally owned behavior honestly rather than guessing.

## TDD is mandatory

Always use test-driven development for behavioral changes:

1. Identify the exact requirement, defect, or user-visible outcome.
2. Write the smallest failing test that proves the missing behavior. Run it and
   confirm that it fails for the intended reason.
3. Implement the smallest coherent change that makes the test pass.
4. Add boundary, malformed-input, round-trip, and arbitrarily chunked-stream
   tests where applicable.
5. Run focused tests during development, followed by the complete quality gates
   before committing.

Do not write production behavior first and add tests afterward. Documentation,
comment-only changes, generated metadata refreshes, and emergency restoration of
a broken test harness are the only reasonable exceptions; explain any exception
in the commit or pull-request description.

For protocol work, tests must cite the authoritative edition and requirement,
section, table, or official vector. A third-party implementation may help expose
an interoperability question, but it is not the normative source and must not be
copied.

## Required context

Before changing code, read the relevant portions of:

- `docs/ROADMAP.md` for the paused handoff and prioritized remaining work.
- `docs/DEVELOPMENT.md` for the development method and local environment.
- `docs/CONFORMANCE.md` for the public standards-support claims.
- `docs/STANDARDS.md` and `references/standards/manifest.json` for provenance.
- `docs/LIMITATIONS.md` for explicit non-claims and practical boundaries.
- `CONTRIBUTING.md` for branches, commits, pull requests, and review evidence.
- `docs/RELEASING.md` before changing versions, tags, or distributions.

When changing an implemented standard, update its trace under
`docs/requirements/`, the conformance matrix, limitations, and public API docs
in the same increment as appropriate. Never claim certification or complete
support without auditable evidence for the stated profile.

## Engineering invariants

- Keep the core package pure Python with no mandatory runtime dependencies.
  Codec, inference, GIS, and UI runtimes belong behind optional extras or
  adapters.
- Preserve unknown wire data losslessly unless a documented strict mode rejects
  it.
- Treat stream input as untrusted. Bound lengths, buffers, queues, reordering,
  retained histories, subprocess behavior, and recovery time.
- Define discontinuity, truncation, backpressure, timestamp-wrap, and malformed
  input behavior for live APIs.
- Protect video, audio, timing, metadata, and unknown-data round trips when
  modifying or enriching streams.
- Avoid speculative abstractions. Prefer a small typed public API backed by a
  demonstrated end-to-end workflow.
- Preserve pre-1.0 API compatibility according to `docs/API_STABILITY.md`, and
  update the public API stability baseline deliberately when exports change.
- Keep standards PDFs and large media fixtures out of Git unless redistribution
  is clearly permitted. Record their identity and provenance in the reference
  manifests.

## Quality gates

Run focused tests first. Before committing a behavioral change, run:

```console
ruff check .
mypy src
pytest --cov=stanag4609 --cov-branch --cov-report=term-missing
python -m build
python -m twine check dist/*
mkdocs build --strict
```

Install the applicable development extras before running these commands. The
repository-wide branch-coverage floor is 90%, while parsers, encoders, security
boundaries, and protocol state machines should have stronger focused coverage.
Run optional-adapter and real-fixture gates whenever the changed boundary uses
them.

## Change discipline

- Keep increments small, reviewable, and independently reversible.
- Use the Conventional Commit and branch conventions in `CONTRIBUTING.md`.
- Do not combine protocol behavior, unrelated cleanup, and generated artifacts
  in one commit.
- Never weaken a test, validator, security control, or conformance claim merely
  to make a gate pass.
- Leave the worktree clean and record newly discovered gaps in the roadmap when
  stopping.

## Practical stopping rule

Stop expanding and document the boundary when the remaining work lacks an
authoritative accessible specification, representative data, credible current
demand, or software-verifiable behavior; belongs in an optional adapter or
deployment; or needs a real design partner. A smaller evidence-backed profile is
preferable to broad but unreliable nominal support.

