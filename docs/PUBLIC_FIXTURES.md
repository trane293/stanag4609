# Public FMV integration fixtures

The repository uses three publicly downloadable MPEG-2 Transport Streams and
one independent negative-conformance bundle for opt-in end-to-end tests:

- `Day Flight.mpg`: 1280x720 H.264 daylight video and asynchronous KLVA.
- `Night Flight IR.mpg`: 1280x720 H.264 infrared video and asynchronous KLVA.
- `Truck.ts`: Esri's official FMV tutorial sample with H.264 video, AAC audio,
  and synchronous KLVA carried as fragmented Metadata Access Units.
- `testfiles.zip`: ImpleoTV STANAG4609 Inspector's public corpus of 21
  deliberately damaged MPEG-2 Transport Streams.

The large media files are installed into the ignored `samples/private/`
directory rather than committed. The fetcher verifies Esri's source ZIP before
extracting only the recorded video member and retains the ImpleoTV corpus as a
ZIP so each test member can be authenticated before use. Source URLs, exact
sizes, SHA-256 identities, stream observations, and expected results are
recorded in `references/fixtures.json`.

```console
python scripts/fetch_public_fixtures.py
pytest -m integration tests/integration/test_public_fmv.py
```

The FFmpeg-hosted flight files contain genuine, checksum-valid ST 0601 Local Sets. They also expose
a useful deployed-system compatibility case: Target Width (Tag 22) is encoded
in four bytes although the standard defines a two-byte value. The default
strict decoder rejects this. `FieldDecodingMode.PRESERVE` retains the complete
packet, decodes the conforming fields, and reports Tag 22 as a structured
issue. This distinction prevents a permissive player from being mistaken for a
conformance validator.

The daylight fixture also drives the complete `FMVVerifier` report. The
integration test confirms that valid transport continuity, video/KLV discovery,
and PCR cadence are reported as passes while every nonconforming four-byte
Target Width is coalesced into one counted, tag-specific error.

The full daylight fixture is additionally passed through the no-op live
transformer. The integration test fingerprints every reconstructed H.264 PES
and complete KLV packet on both sides, proving elementary-stream identity. It
also verifies all 13,331 source PCR observations are surfaced to the clock tap
and retained in the transformed output through source-layout repacketization.
The same real fixtures also pass through the constant-rate shaper without a
clock anchor. Their complete output hashes remain byte-identical, proving that
slot planning does not alter source packets unless PCR restamping is explicitly
enabled.

Both fixtures satisfy the ST 1402.2 100 ms PCR interval under the exact
program-aware clock validator, with no interval or regression diagnostics over
35,395 observed clock references.
Both also build complete browser-player assets: FFmpeg produces playable H.264
MP4 media, the library produces exactly 6 daylight and 18 infrared timeline
samples, and every sample exposes sensor, frame-center, and target geometry to
the map and side panel.

The Esri fixture adds an independent audio-bearing, synchronous-KLVA case. It
is also a valuable negative conformance sample: the current file has no
metadata standard descriptor, contains Metadata AU sequence discontinuities,
and exceeds the ST 1402 PCR interval in places. The integration test therefore
asserts both successful H.264/AAC/KLV discovery and those concrete diagnostics;
when the `audio-pyav` extra is installed, it also decodes all 6,946 timestamped
AAC access units to 7,112,704 stereo PCM samples at 48 kHz through the public
demux, audio timing, frame parser, and decoder APIs. Being playable in an FMV
product is not treated as proof of conformance.
The same fixture drives a complete reference-player asset build: all 711
displayable metadata samples are decoded, H.264/AAC is transcoded to a seekable
MP4, the static UI is copied, and the first metadata sample is checked against
the earliest audio/video PTS. This retains the source's 2,777-tick AAC lead-in
instead of displaying every metadata update early relative to the MP4 clock.

## Independent negative-conformance corpus

ImpleoTV publishes a small test-data-only release asset alongside its
proprietary Inspector product. The project downloads only that public ZIP, not
the application or installer. The integration suite verifies the ZIP identity,
then separately verifies the size and SHA-256 of 20 representative members
before asserting a fault-specific library diagnostic for each one. Covered
families include corrupt KLV checksum/key/length, duplicate and missing ST 0601
items, missing KLV payload, continuity-counter errors, duplicate transport
packets, excessive PTS/PCR/PAT/PMT intervals, PCR regression, invalid PES
length, missing PAT/PMT, corrupt DVB SDT CRC, corrupt TS sync, and the
transport-error indicator.

One archive member remains explicitly unasserted in the manifest:
`mpegts-pcr-pts-drift.ts`. Its H.264 PTS leads PCR by approximately 0.600 to
0.899 seconds, which remains inside ST 1402 §7.4's published maximum ten-second
lead. The corpus label alone is not treated as a normative failure, and the
verifier does not invent an unsupported drift-rate threshold.

The corpus contains no ST 0903/VMTI packets. A separately examined
[OpenSensorHub UAS sample stream](https://github.com/opensensorhub/osh-addons/blob/f35dae8df258a10f8e6883b7085cde338fbdc480/sensors/aviation/sensorhub-driver-misb-uas/src/test/resources/org/sensorhub/impl/sensor/uas/sample-stream.ts)
also contains 277 ST 0601 packets but no nested Item 74 or standalone ST 0903
Local Set. Although it is used by an upstream `VmtiTest`, that test permits zero
targets, so it is not accepted as VMTI interoperability evidence here.

Additional public fixtures, especially ST 0903 VMTI and ST 1001-labelled audio
streams, should be added to the manifest when found.

The separate ArcGIS multiplexer tutorial's `Raw_Video.mpeg` is intentionally
not listed as an FMV fixture because it is an MPEG Program Stream with video
and audio but no KLV. Its 866-row `Raw_Metadata.csv` companion is exercised as
a producer input. The generated transport contains all 866 decodable ST 0601
packets, MPEG-2 video, and MPEG-1 Layer II audio; it passes the verifier's
structural profile without warnings or errors and builds a player timeline with
866 geospatial samples. The FFmpeg remux command explicitly requests a 20 ms
PCR period so the generated output remains within ST 1402's 100 ms limit.
