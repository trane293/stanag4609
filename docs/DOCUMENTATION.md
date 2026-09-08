# Work on the documentation

The documentation is part of the release. A Python developer should be able to
install the package, complete a workflow, and find the relevant protocol limits
without reading the source or test suite.

Read the [writing style](STYLE_GUIDE.md) before editing a user-facing page.

## Put information in the right place

The site is organized by reader intent:

- **Get started** covers installation, the first working stream, workflow
  selection, common pitfalls, and command discovery.
- **Tutorials** teach complete tasks with runnable examples.
- **User guide** explains concepts, choices, and operational behavior.
- **API reference** documents the public Python surface from docstrings.
- **Standards and assurance** records editions, requirement traces, evidence,
  conformance scope, and limitations.
- **Architecture** explains stable boundaries and runtime design.
- **Contribute** covers repository development, documentation, and releases.

A page should serve one of these purposes. When a tutorial needs conformance
detail, link to the assurance page instead of embedding the requirement ledger.
When a user guide needs a signature, link to the API reference.

## Add or change an example

Small examples may live directly in a page. Include enough setup and imports to
make the boundary clear. Put multi-step programs under `examples/` and keep the
documentation focused on how to run them and interpret the result.

When an example needs a source checkout, say so before the command. Otherwise,
show installation from PyPI. The first-run path should use generated or
redistributable data whenever possible.

Output excerpts should help readers recognize success or diagnose a likely
failure. Omit temporary paths, incidental byte counts, and long inventories
unless the workflow depends on them.

## Update screenshots

Screenshots live in `docs/assets/screenshots/` and supplement the surrounding
text. Use descriptive alt text, capture the current UI from a named fixture,
and inspect the image at full resolution before committing it.

The current captures are produced by these workflows:

- the verifier HTML report from `stanag4609-verify --format html`;
- the reference player from `stanag4609-player`;
- the custom dashboard from `examples/web_dashboard/server.py`; and
- the Ultralytics sidecar from `examples/tutorials/ai_sidecar_ui.py`.

Fixture identities and download instructions belong in
[public fixtures](PUBLIC_FIXTURES.md), not in image captions.

## Build the site locally

From a source checkout:

```console
python -m pip install -e '.[docs]'
mkdocs serve
```

The development server prints a local URL and reloads changed Markdown. Before
committing, build with the same strict setting used by CI:

```console
mkdocs build --strict
```

The generated site is written to `site/` and is not committed. Read the Docs
uses `.readthedocs.yaml` to install the documentation extra and publish the
same MkDocs site.

## Run documentation checks

```console
pytest -q tests/unit/test_documentation.py tests/unit/test_tutorial_examples.py
```

These checks compile Python fences, resolve package imports, parse shell fences,
load documented console entry points, exercise tutorial programs, inspect
assets, and keep navigation and support summaries consistent. `mkdocs build
--strict` checks internal links and anchors while resolving every API target.

Run the real-fixture integration tests when a page changes a claim about a
downloaded recording. Run browser tests when a screenshot or description
depends on player interaction.

## Review the rendered result

Automated checks cannot judge whether a page flows naturally. Read the rendered
page from top to bottom and confirm that:

1. the first paragraph tells the reader what the page is for;
2. the shortest useful path appears before advanced options;
3. commands can be copied in the environment the page describes;
4. limitations sit beside the decision they affect;
5. links lead to deeper detail instead of repeating it; and
6. headings, tables, screenshots, and code are easy to scan on a narrow window.
