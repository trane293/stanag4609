# Development method

## Definition of done for a protocol increment

1. Name the exact standard edition and requirement, table row, or official
   worked example that motivates the behavior.
2. Add a failing test. Include valid boundary values, malformed input, arbitrary
   streaming chunk boundaries, and lossless round trips where applicable.
3. Implement a bounded pure-Python solution with explicit resource limits.
4. Run unit/integration tests, Ruff, strict MyPy, branch coverage, and an
   isolated source/wheel build.
5. Review the diff for accidental coupling, copied third-party code, misleading
   conformance claims, and unbounded allocations.
6. Make one focused local Git commit.

Independent implementations may supply differential-test ideas, but official
standards and their published vectors determine expected behavior. Any conflict
is documented and resolved in favor of the normative source.

## Browser acceptance

The normal suite skips real-browser acceptance when Playwright is absent. To
exercise the reference player exactly as the dedicated CI job does:

```console
python -m pip install -e '.[test,browser-test]'
python -m playwright install chromium
pytest -q -m browser tests/browser
```

The fixture generates a tiny local H.264 video with FFmpeg and uses synthetic
metadata. It needs neither private recordings nor network access at test time.

## Planned increments

- Core KLV and lossless binary model.
- Maintain the source-bound complete ST 1201.5 mapping and ST 0601.19 active
  item registry as their invoking profiles evolve.
- ST 0102 and other nested/structured sets referenced by ST 0601.
- ST 0902.8 packet and stream-cadence validation.
- MPEG-2 TS, PSI, PES, video/audio/KLV elementary-stream discovery,
  synchronous/asynchronous KLV carriage, and indexing.
- ST 0903.6 VMTI detections, bounding boxes, AI/ML labels and confidence,
  geospatial positions, and track history.
- Deterministic muxing, demuxing, validation, and CLI interfaces.
- Pull-driven live processor chains with explicit pass/drop/replace/inject
  decisions, bounded async adapters, and simultaneous transport/sidecar sinks.
- Optional video/audio codec player adapter and synchronized metadata, geo,
  and VMTI overlay UI.
- Reference-stream integration, differential testing, fuzzing, benchmarks, and
  a final requirement-by-requirement conformance audit.
