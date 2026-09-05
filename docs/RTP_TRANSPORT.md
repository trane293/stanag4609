# RTP transport

STANAG 4609 Edition 5 adopts MISP-2019.1. For MPEG-2 Transport Stream over RTP,
MISP-2019.1-76 selects MISB ST 0804, whose requirement 18 selects the RFC 2250
MP2T payload format.

## Send MPEG-2 TS over RTP/UDP

`RTPMPEG2TransportPacketizer` preserves the TS bytes and adds an RTP version 2
header. The default payload contains seven 188-byte TS packets and uses the
RFC 3551 static MP2T payload type 33.

```python
from stanag4609 import RTPMPEG2TransportPacketizer

packetizer = RTPMPEG2TransportPacketizer()

# `clock_90khz` must represent the target transmission time of the first
# payload byte and be synchronized to the transport stream PCR clock.
for chunk in source_chunks:
    for datagram in packetizer.feed(chunk, timestamp=clock_90khz()):
        udp_socket.sendto(datagram, destination)

for datagram in packetizer.finish(timestamp=clock_90khz()):
    udp_socket.sendto(datagram, destination)
```

When switching input sources or otherwise discontinuously changing the RTP
clock, pass `discontinuity=True` on the first packet. This sets the RFC 2250
marker bit. Normal 32-bit timestamp and 16-bit sequence-number wrap are handled.

For an application that already groups its TS datagrams, use `packetize`
directly:

```python
rtp_datagram = packetizer.packetize(
    seven_ts_packets,
    timestamp=clock_90khz(),
)
```

## Receive safely into the live demuxer

The receiver validates RTP, payload type, TS boundaries, and TS sync. It reports
forward sequence gaps but permits the later payload. It withholds late or
duplicate payloads because feeding those bytes out of order would corrupt an
incremental TS demuxer.

```python
from stanag4609 import RTPMPEG2TransportReceiver, TransportDemuxer

receiver = RTPMPEG2TransportReceiver()
demuxer = TransportDemuxer()

while True:
    datagram, _peer = udp_socket.recvfrom(2048)
    reception = receiver.receive(datagram)
    if reception.sequence_issue is not None:
        metrics.record(reception.sequence_issue)
    if reception.accepted_payload is not None:
        for event in demuxer.feed(reception.accepted_payload):
            handle(event)
```

If the sender reconnects or its SSRC changes, reset both the RTP receiver and
the transport demuxer at the same explicit input-session boundary.

## Recover bounded packet reordering

`RTPPacketReorderBuffer` holds a bounded sequence-number window, measured in
packets rather than wall-clock time. When the window is exceeded, or `flush()`
is called at a finite stream boundary, it reports each definite gap and emits
the retained packets in sequence order. Duplicate and already-late packets are
reported and withheld.

```python
from stanag4609 import (
    RTPMPEG2TransportReceiver,
    RTPPacketReorderBuffer,
    TransportDemuxer,
    parse_rtp_mpeg2_transport,
)

reorder = RTPPacketReorderBuffer(max_reorder_packets=16)
receiver = RTPMPEG2TransportReceiver()
demuxer = TransportDemuxer()

def consume(result):
    for issue in result.issues:
        metrics.record(issue)
    for packet in result.packets:
        reception = receiver.receive_packet(packet)
        if reception.timestamp_issue is not None:
            metrics.record(reception.timestamp_issue)
        if reception.accepted_payload is None:
            continue
        for event in demuxer.feed(reception.accepted_payload):
            handle(event)

while True:
    datagram, _peer = udp_socket.recvfrom(2048)
    packet = parse_rtp_mpeg2_transport(datagram)
    consume(reorder.push(packet))

# At an actual finite input boundary:
consume(reorder.flush())
```

The first received packet establishes the starting sequence number. A reorder
buffer cannot recover packets that preceded that session boundary. Reset the
reorder buffer, receiver, and demuxer together on a reconnect or intentional
source change. Choose the packet window from the deployment's measured network
reordering and latency budget; this class deliberately does not invent a
wall-clock playout deadline.

## Synchronize separate streams with RTCP Sender Reports

ST 0804.4-19/-20 requires RTCP Sender Reports when imagery and metadata use
separate RTP streams and need synchronized playback. Each report pairs the
sender's RTP timestamp with an NTP timestamp representing the same instant.
Build one clock mapping per SSRC, then convert both packet timelines into the
common NTP domain:

```python
from stanag4609 import RTPNTPClockMapping, parse_rtcp_sender_reports

video_sr = parse_rtcp_sender_reports(video_rtcp_datagram)[0]
metadata_sr = parse_rtcp_sender_reports(metadata_rtcp_datagram)[0]

video_clock = RTPNTPClockMapping.from_sender_report(
    video_sr,
    clock_rate=90_000,
)
metadata_clock = RTPNTPClockMapping.from_sender_report(
    metadata_sr,
    clock_rate=metadata_clock_rate,
)

video_time = video_clock.ntp_timestamp(video_rtp_packet.timestamp)
metadata_time = metadata_clock.ntp_timestamp(metadata_rtp_packet.timestamp)
offset_seconds = metadata_time - video_time  # exact Fraction, no float drift
```

The mapping handles normal 32-bit RTP timestamp wrap in either direction.
`rtp_timestamp(ntp_time)` performs the inverse mapping when the requested NTP
instant lands exactly on that RTP clock. The returned NTP value is deliberately
an exact `fractions.Fraction`. Its 32-bit seconds field does not identify the
NTP era, so session context must resolve an absolute civil date if one is
needed.

Use `validate_rtcp_compound` at a receiver boundary to require an SR/RR first
packet and a matching mandatory SDES CNAME. `parse_rtcp_packets` remains the
lower-level framing API when an application needs to retain packet types the
library does not yet interpret. Typed SDES and Receiver Report codecs are also
available.

The MPEG-TS packetizer counts generated RTP payload packets and octets using
the RFC 3550 unsigned 32-bit rollover semantics. After the corresponding RTP
datagrams have actually been sent, it can build an SR+CNAME compound packet:

```python
rtcp_datagram = packetizer.compound_sender_report(
    ntp_seconds=ntp_seconds,
    ntp_fraction=ntp_fraction,
    rtp_timestamp=current_rtp_timestamp,
    cname="fmv-sender@sensor.example",
)
rtcp_socket.sendto(rtcp_datagram, rtcp_destination)
```

The caller supplies NTP and RTP timestamps representing the same instant; a
packetizer cannot infer the host's synchronized wall clock. Its counters mean
*generated*, so the application must account separately for socket-send
failures. `encode_rtcp_sender_compound` provides the same SR+CNAME construction
for callers that own counters directly. Related video and metadata RTP sessions
should use the same stable CNAME for one participant so receivers can bind
them.

The compound codec does not invent adaptive report intervals. A complete live
RTCP participant still needs session-owned scheduling, membership/timeouts,
bandwidth policy, SSRC collision handling, and BYE behavior.

This transport surface implements multiplexed MPEG-2 TS over RTP plus the
reusable RTCP Sender Report synchronization core. Native H.264, MPEG-2 video,
KLV-over-RTP, adaptive RTCP participant/session management, and RTSP session
control are distinct ST 0804 profiles and are not claimed here.
