## Why

<!-- What FMV interoperability or user problem does this solve? -->

## Standard basis

<!-- List exact editions and requirement, section, table, or official vector. -->

## What changed

<!-- Describe public API, wire behavior, bounds, and error behavior. -->

## Verification

<!-- List new tests and paste a concise result for each quality gate. -->

## Compatibility

<!-- Cover API, timing, transport, audio/video passthrough, and unknown KLV. -->

## Risk and rollback

<!-- State known limitations, operational risks, and the safe revert point. -->

## Checklist

- [ ] PR title uses `type(optional-scope): imperative summary`.
- [ ] A failing test or reproducible case preceded the implementation.
- [ ] The exact standard requirement or official vector is traceable.
- [ ] Inputs, memory, buffering, latency, and error paths are bounded.
- [ ] Unknown KLV data remains lossless unless explicitly documented.
- [ ] Audio/video passthrough and timestamps remain unchanged or are tested.
- [ ] Ruff, strict MyPy, branch coverage, and isolated build pass.
- [ ] Public behavior, conformance status, limitations, and changelog are updated.
- [ ] No licensed standard, proprietary code, restricted fixture, or secret is added.
