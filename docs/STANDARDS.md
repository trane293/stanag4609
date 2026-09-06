# Standards baseline

Every conformance claim in this project must trace to a named edition and an
automated test. The publications below were downloaded from their issuing
registries or identified archival sources and validated as real PDF/XLS files.
Their exact provenance and SHA-256 digests are recorded in
`references/standards/manifest.json`; the files themselves are gitignored until
their redistribution terms have been reviewed.

The exact active and inactive normative-requirement populations for ST 0102.12, ST 0107.5,
ST 0601.19, ST 0806.4, ST 0902.8, ST 0903.6, ST 1002.3, ST 1010.3, ST 1201.5, ST 1204.3, ST 1206.1,
ST 1601.2, ST 1602.2, ST 1607.2, and MISP-2019.1 are also recorded in the machine-readable
`references/requirements.json`. Tests bind that inventory to the acquired
source digests, require every active identifier to appear in its human-readable
trace, and compare the ST 0902 Table 1 tag paths with the validator's default
runtime profile. This prevents a prose-only “complete” claim from silently
omitting a requirement or minimum metadata group.

| Document | Edition/date | Role | Access state |
|---|---:|---|---|
| NATO STANAG 4609 | Edition 5, 30 July 2020 | NATO adoption/profile | Content-validated copy from a secondary public archive; NATO registry identity corroborated |
| MISP | 2019.1, November 2018 | Technical profile adopted by STANAG 4609 Edition 5 | Content-validated copy from a secondary public archive |
| MISP | 2023.2, March 2023 | Historical profile cited by ST 0601.19 | Downloaded from official NGA registry |
| MISP | 2025.1, 11 June 2025 | Current overall profile | Downloaded from official registry |
| MISB ST 0601 | 0601.19, 2 March 2023 | UAS Datalink Local Set | Downloaded from official registry |
| MISB ST 0902 | 0902.8, 1 November 2018 | Minimum metadata profile | Downloaded from official registry |
| MISB ST 0903 | 0903.6, 21 October 2021 | VMTI detections, AI/ML labels, and tracks | Downloaded from official registry |
| MISB ST 0801 | 0801.6, 22 February 2018 | Photogrammetry metadata conditionally referenced by MISP-2019.1 | Downloaded from the official historical registry record; implementation depends on the ST 1107 profile and remains pending |
| MISB ST 0804 | 0804.4, 27 February 2014 | RTP carriage of motion imagery and metadata | Downloaded from official registry |
| IETF RFC 3550 | July 2003 | RTP/RTCP framing, Sender Reports, and inter-media clock synchronization | Downloaded from official RFC Editor |
| MISB ST 1001 | 1001.1, 27 February 2014 | MISP audio codec profile | Downloaded from official registry |
| MISB ST 0107 | 0107.5, 21 October 2021 | KLV rules and byte order | Downloaded from official registry |
| MISB ST 1201 | 1201.5, 24 June 2021 | IMAP integer mapping | Downloaded from official registry |
| MISB ST 1202 | 1202.3, 2 March 2023 | Generalized image transformations | Downloaded from official registry |
| MISB ST 1303 | 1303.2, 25 June 2020 | Multi-dimensional array packing | Downloaded from official registry |
| MISB ST 1402 | 1402.2, 27 October 2016 | MPEG-2 TS carriage | Downloaded from official registry |
| ITU-T H.262 / ISO/IEC 13818-2 | 02/2000 | MPEG-2 Video sequence syntax | Downloaded from ITU |
| ITU-T H.264 / ISO/IEC 14496-10 | 04/2017 (MISP-adopted); 02/2014 retained | AVC sequence syntax and exact level signalling | Downloaded from ITU |
| ITU-T H.265 / ISO/IEC 23008-2 | 02/2018 | HEVC sequence syntax | Downloaded from ITU |
| ITU-T H.222.0 / ISO/IEC 13818-1 | 10/2014 | MPEG-2 Systems and metadata AU syntax | Downloaded from ITU |
| MISB ST 0603 / 0604 / 0607 | Current registry versions | Time and transport compliance | Downloaded from official registry |
| MISB ST 0102 / 1204 | ST 0102.12 / ST 1204.3 | Nested Security metadata and MIIS identity | Downloaded from official registry |
| MISB ST 1607 | 1607.2, 11 June 2025 | Segment/Amend metadata hierarchy and security/MISP rules | Downloaded from official registry |
| MISB ST 0807 | 0807.27 | KLV registry | Downloaded from official registry |
| ITU-T X.690 / ISO/IEC 8825-1 | 11/2008 | BER Length and Object Identifier encoding used by ST 336 | Downloaded from ITU |
| SMPTE ST 298 | 2009 edition cited by ST 336:2017 | Universal Label structure and encoding | Downloaded from official SMPTE publication archive |
| SMPTE ST 336 | 2017 edition cited by ST 0601.19 | KLV data encoding | Downloaded from official SMPTE publication archive |
| SMPTE RP 217 | 2001 edition cited by ST 1402.2 | Asynchronous KLV carriage | Downloaded from official SMPTE publication archive |
| SMPTE ST 170, 274, 296, 2036-1 | Editions cited by MISP-2025.1 | Analog, HD, and UHD image formats | Downloaded from official SMPTE publication/archive URLs |
| SMPTE ST 291-1 | 2011 edition cited by MISP-2025.1 | Ancillary data packet framing | Downloaded from official SMPTE publication archive |
| SMPTE ST 377-1, 378, 391, 379-1, 381-1 | Editions cited by MISP-2025.1 | MXF file, operational-pattern, generic-container, and MPEG mapping profiles | Downloaded from official SMPTE publication/archive URLs |
| SMPTE ST 259, 424, 425-1, 435-2 | Historical editions cited by retired MISP requirements | Legacy SDI transports and mappings | Downloaded from official SMPTE publication/archive URLs; retained for historical validation only |

STANAG 4609 Edition 5 is a short standardization agreement that adopts
MISP-2019.1 as its technical standard. Both documents are now present locally.
The STANAG PDF's own reproduction restriction is why neither file is committed.
The later MISP-2023.2 edition cited by ST 0601.19 is also retained so edition
differences can be tested rather than inferred from MISP-2025.1.
The direct normative dependencies for the applicable ST 336 KLV subset are
also cached: ST 298:2009 defines the Universal Label and X.690:2008 defines the
BER Length and Object Identifier encodings referenced by ST 336:2017. The
broader MISP image-format, ancillary-data, and MXF SMPTE references are cached
too, including the historical editions named by retired requirements.

SMPTE EG 28 is an informative glossary and SMPTE RP 210 is a superseded
metadata-dictionary publication whose role is covered by the cached MISB ST
0807 registry. The remaining retired SDI references (ST 292-1, ST 297, and ST
349) are no longer directly downloadable from the public SMPTE archive. NATO
STANAG 4545, 4559, 4586, and 4676 are adjacent-system references rather than
requirements of this package's current FMV/KLV runtime; their current technical
publications require NATO/ASSIST authentication or licensed distribution. They
are therefore recorded as access gaps, not silently substituted with unrelated
or preview material.

## Source hierarchy

1. Normative edition from the issuing standards body.
2. Official corrigenda, registries, and conformance artifacts.
3. Worked examples published with the normative document.
4. Independent implementations, used only for differential testing.

The independent MIT-licensed `klv-decoder`, `klvdata`, and `jmisb` projects and
the MIT/Apache `ts-transformer` project are research references. None is treated
as normative evidence, and no source code is copied into this implementation.
