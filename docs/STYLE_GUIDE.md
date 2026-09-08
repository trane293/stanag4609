# Documentation writing style

Write for a developer who wants to complete a task, understand the result, and
then learn the protocol details that affect their design.

## Choose the right page type

- **Installation** gets a released package into a clean environment.
- **Quickstart** produces a useful result with no prior FMV knowledge.
- **Tutorial** teaches by building one complete application.
- **User guide** explains concepts, choices, and operational behavior.
- **API reference** states signatures, types, parameters, returns, and errors.
- **Standards and assurance** records editions, requirements, evidence, and
  limitations.
- **Contributor documentation** describes repository work, tests, and releases.

Do not make one page serve several of these roles. Link to the next level of
detail instead.

## Write in a developer voice

- Lead with the task or result.
- Prefer active voice and concrete verbs.
- Use “Use `X` when `Y`” to explain choices.
- Keep one main idea in each paragraph.
- Define an acronym on first use and link to the glossary when useful.
- Describe behavior before implementation details.
- Address the reader as “you” when giving a direct instruction. Otherwise name
  the application, receiver, parser, or command.

Avoid promotional or defensive language. Words such as “exact,” “bounded,”
“verified,” and “complete” are useful only when the property changes a design or
supports a scoped conformance statement. Do not narrate the test suite in a
tutorial.

## Present limitations where they matter

State a limitation when it changes the reader's decision. Use a short note or
warning and link to the detailed conformance or limitations page.

Prefer an affirmative contract:

> Supply PCR values from the output clock. Callback time is not the
> decoder-arrival timestamp.

Avoid arguing with an imagined reader:

> The library does not pretend callback time is the real timestamp.

Keep broad certification scope on the conformance pages instead of repeating it
throughout the learning path.

## Write examples that run

- Include imports, inputs, and setup needed to copy the example.
- Use generated or openly redistributable data for the first-run path.
- Put substantial examples in `examples/` and test the same source shown in the
  documentation.
- Label incomplete control-flow sketches as conceptual code.
- Show only the output needed to recognize success.
- Use “Expected output” for stable excerpts.
- Do not hard-code temporary paths, volatile timing, or incidental byte counts.

Every Python fence must compile. Imports from the package must resolve. Shell
fences must parse, documented commands must exist, and primary workflows must
run against a built wheel or the released package.

## Use screenshots as results

A screenshot should help the reader understand the outcome or interface. Give
it descriptive alt text and a short caption when the surrounding prose does not
identify the state. Put capture provenance and fixture hashes in the fixture or
release documentation.

## Review checklist

Before merging a documentation change, check that:

1. The page has one audience and one purpose.
2. The first paragraph says what the reader will accomplish or learn.
3. Commands use the PyPI installation unless the page is for contributors.
4. Examples are runnable or clearly marked as conceptual.
5. Output explains a result instead of serving as test evidence.
6. Caveats are specific and located beside the affected decision.
7. Detailed standards claims link to a requirement trace or conformance page.
8. Local links, navigation, snippets, screenshots, and the strict site build
   pass their documentation tests.

This structure follows the same separation of installation, learning material,
examples, API reference, and contributor guidance used by mature scientific
Python projects such as [scikit-learn](https://scikit-learn.org/stable/).
