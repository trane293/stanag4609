# Glossary

## Access unit

A coded video frame or other independently timed media unit. MISB timestamp
messages are associated with video access units.

## Class 1 motion imagery

The MPEG-2 Transport Stream form commonly associated with live STANAG 4609 FMV.
MISP also defines other classes and storage profiles.

## FMV

Full Motion Video: motion imagery carried with synchronized metadata and,
optionally, audio.

## IMAP

Integer Mapping: the MISB method for representing physical values as compact
integers. ST 1201 defines IMAPA and IMAPB mappings and their special values.

## KLV

Key-Length-Value encoding defined by SMPTE ST 336. The key identifies the data,
the BER length gives its size, and the value contains a primitive value or a
nested set.

## KLVA

The MPEG registration used to identify KLV metadata carried in an MPEG
transport stream.

## Local Set

A KLV value containing shorter tag-length-value items whose tag meanings are
defined by a parent standard, such as ST 0601.

## MISP

Motion Imagery Standards Profile: the technical profile adopted by STANAG 4609
Edition 5.

## MISMMS

Motion Imagery Standards Minimum Metadata Set. MISB ST 0902 defines the minimum
metadata population and cadence used by the corresponding profile.

## MISB

Motion Imagery Standards Board, publisher of the metadata and motion-imagery
standards used by this project.

## MPTS

Multiple Program Transport Stream: one MPEG-TS multiplex containing more than
one program.

## PCR

Program Clock Reference: the MPEG-TS clock samples a decoder uses to recover the
program time base.

## PES

Packetized Elementary Stream: the MPEG Systems layer that groups video, audio,
or metadata payloads and may carry PTS and DTS timestamps.

## PID

Packet Identifier: the 13-bit number that identifies a packet stream inside
MPEG-TS.

## PMT

Program Map Table: the PSI table that declares a program's PCR, video, audio,
metadata, and other elementary streams.

## PSI

Program Specific Information: MPEG-TS tables used to discover programs and
their streams. The PAT and PMT are the central tables for this library.

## PTS

Presentation Time Stamp: the media time at which a PES access unit is presented.
MPEG-TS PTS uses a 90 kHz clock and wraps at 33 bits.

## Report-on-Change

A metadata rule in which unchanged values may be omitted from later packets.
Receivers retain a value until it is replaced, cleared, or expires.

## SDP

Session Description Protocol. An SDP document can describe the address, port,
and RTP format used to receive an MPEG transport stream.

## SPTS

Single Program Transport Stream: an MPEG-TS multiplex containing one program.
`LiveTransportTransformer` emits the selected program as SPTS.

## ST 0601

The MISB UAS Datalink Local Set, widely used for aircraft, sensor, frame, and
target metadata in FMV.

## ST 0902

The MISB Motion Imagery Standards Minimum Metadata Set profile.

## ST 0903

The MISB Video Moving Target Indicator standard. It represents detections,
targets, tracks, masks, chips, algorithms, ontologies, and related geometry.

## STANAG 4609

The NATO standardization agreement for digital motion imagery. Edition 5 adopts
MISP-2019.1 as its technical profile.

## Universal Set

A KLV set whose child items use 16-byte Universal Labels rather than short local
tags.

## VMTI

Video Moving Target Indicator metadata defined by MISB ST 0903.

## ZLI

Zero Length Item: a Local Set item with an empty value. In Report-on-Change
metadata it clears the receiver's current value for that item.
