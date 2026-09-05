# UDP transport datagrams

MISB ST 1402.2 requires every UDP datagram carrying an MPEG-2 Transport Stream
to contain an integer number of 188-byte TS packets. `UdpTransportPacketizer`
enforces that boundary while accepting arbitrarily chunked bytes from a file,
pipe, remuxer, or async transport bridge.

```python
from stanag4609 import UdpTransportPacketizer

packetizer = UdpTransportPacketizer(packets_per_datagram=7)
for source_chunk in transport_chunks:
    for datagram in packetizer.feed(source_chunk):
        udp_socket.sendto(datagram, destination)
for datagram in packetizer.finish():
    udp_socket.sendto(datagram, destination)
```

Seven TS packets produce a 1,316-byte payload, the value recommended by ST
1402.2 for a 1,500-byte Ethernet MTU. A final datagram may contain fewer
packets. The configured limit cannot exceed 348 packets because 349 complete
TS packets would exceed the maximum UDP payload size.

By default, every packet boundary is also checked for the MPEG-2 TS sync byte.
Set `validate_sync=False` only when a lower layer already validated framing and
the application deliberately wants boundary-only operation. A trailing partial
TS packet raises `TruncatedData`; it is never padded or silently discarded.

Receivers can use `validate_udp_datagram()` before passing each payload to
`TransportDemuxer`. It rejects empty, non-integral, oversized, or misaligned
payloads and returns the packet count for metrics. `iter_udp_datagrams()` is a
convenience wrapper for finite chunk iterables.
