from __future__ import annotations

from dataclasses import replace

import pytest

from stanag4609.errors import DecodeError, LimitExceeded, TruncatedData
from stanag4609.klv.ber import encode_ber_length
from stanag4609.klv.checksum import running_sum_16
from stanag4609.klv.model import KLVPacket
from stanag4609.st0601 import (
    ST0601_KEY,
    FieldDecodingMode,
    ST0601ValidationContext,
    UASLocalSet,
    encode_uas_local_set,
)
from stanag4609.transport.demux import PESStreamEvent, StreamKind, TransportDemuxer
from stanag4609.transport.metadata import (
    asynchronous_klv_stream,
    encode_metadata_au_cell,
    synchronous_klv_stream,
)
from stanag4609.transport.metadata_stream import (
    MetadataDecoderResetReport,
    MetadataStreamDecoder,
)
from stanag4609.transport.mux import TransportMuxer, encode_pes_packet
from stanag4609.transport.pes import parse_pes_packet
from stanag4609.transport.psi import KLVCarriage


def _muxer(
    *,
    synchronous: bool = False,
    metadata_service_id: int = 0,
) -> TransportMuxer:
    metadata = (
        synchronous_klv_stream(
            0x102,
            metadata_input_leak_rate=1000,
            metadata_buffer_size=200_000,
            metadata_service_id=metadata_service_id,
        )
        if synchronous
        else asynchronous_klv_stream(0x102)
    )
    return TransportMuxer(
        transport_stream_id=1,
        program_number=1,
        program_map_pid=0x100,
        pcr_pid=0x102,
        streams=(metadata,),
    )


def _demux(output: bytes, *, max_pes_length: int = 100_000) -> list[PESStreamEvent]:
    events = TransportDemuxer(max_pes_length=max_pes_length).feed(output)
    return [event for event in events if isinstance(event, PESStreamEvent)]


def test_async_klv_spanning_pes_packets_becomes_one_typed_event() -> None:
    klv = encode_uas_local_set({2: 1_700_000_000_000_000, 65: 19})
    muxer = _muxer()
    split = len(klv) // 2
    output = b"".join(muxer.program_tables())
    output += b"".join(
        muxer.mux_pes(
            0x102,
            encode_pes_packet(
                klv[:split], stream_id=0xBD, data_alignment_indicator=True
            ),
        )
    )
    output += b"".join(
        muxer.mux_pes(
            0x102,
            encode_pes_packet(
                klv[split:], stream_id=0xBD, data_alignment_indicator=False
            ),
        )
    )
    pes_events = _demux(output)
    decoder = MetadataStreamDecoder()
    assert decoder.feed(pes_events[0]) == []
    result = decoder.feed(pes_events[1])
    assert len(result) == 1
    assert bytes(result[0].packet) == klv
    assert isinstance(result[0].decoded, UASLocalSet)
    assert result[0].decoded.value(65) == 19
    assert result[0].pts is None
    decoder.finish()


def test_metadata_topology_reconfiguration_is_atomic_for_partial_async_klv() -> None:
    klv = encode_uas_local_set({2: 1_700_000_000_000_000, 65: 19})
    info = asynchronous_klv_stream(0x102)

    def event(payload: bytes, *, aligned: bool) -> PESStreamEvent:
        return PESStreamEvent(
            1,
            info,
            StreamKind.KLV,
            KLVCarriage.ASYNCHRONOUS,
            parse_pes_packet(
                encode_pes_packet(
                    payload,
                    stream_id=0xBD,
                    data_alignment_indicator=aligned,
                )
            ),
        )

    decoder = MetadataStreamDecoder()
    decoder.reconfigure({0x102: KLVCarriage.ASYNCHRONOUS})
    assert decoder.feed(event(klv[:20], aligned=True)) == []

    with pytest.raises(DecodeError, match="partial asynchronous"):
        decoder.validate_reconfiguration({})
    with pytest.raises(DecodeError, match="partial asynchronous"):
        decoder.reconfigure({})

    # Failed validation and mutation leave the buffered item intact.
    completed = decoder.feed(event(klv[20:], aligned=False))
    assert [bytes(item.packet) for item in completed] == [klv]
    decoder.reconfigure({})
    with pytest.raises(DecodeError, match="not in the active metadata topology"):
        decoder.feed(event(klv, aligned=True))


def test_metadata_decoder_reset_discards_async_fragment_and_topology() -> None:
    klv = encode_uas_local_set({2: 1_700_000_000_000_000, 65: 19})
    info = asynchronous_klv_stream(0x102)

    def event(payload: bytes, *, aligned: bool) -> PESStreamEvent:
        return PESStreamEvent(
            1,
            info,
            StreamKind.KLV,
            KLVCarriage.ASYNCHRONOUS,
            parse_pes_packet(
                encode_pes_packet(
                    payload,
                    stream_id=0xBD,
                    data_alignment_indicator=aligned,
                )
            ),
        )

    decoder = MetadataStreamDecoder()
    decoder.reconfigure({0x102: KLVCarriage.ASYNCHRONOUS})
    assert decoder.feed(event(klv[:20], aligned=True)) == []

    report = decoder.reset()

    assert report == MetadataDecoderResetReport(
        asynchronous_klv_bytes=20,
        asynchronous_partial_items=1,
        synchronous_fragment_bytes=0,
        synchronous_partial_access_units=0,
        metadata_pids=1,
        metadata_services=0,
    )
    assert [bytes(item.packet) for item in decoder.feed(event(klv, aligned=True))] == [
        klv
    ]
    assert decoder.reset() == MetadataDecoderResetReport(metadata_pids=1)


def test_metadata_decoder_reset_discards_sync_fragment_and_sequence_epoch() -> None:
    large = ST0601_KEY + encode_ber_length(70_000) + bytes(70_000)
    fragmented_muxer = _muxer(synchronous=True, metadata_service_id=9)
    fragmented = _demux(
        b"".join(fragmented_muxer.program_tables())
        + b"".join(
            fragmented_muxer.mux_sync_klv(
                0x102,
                large,
                pts=123_456,
                metadata_service_id=9,
            )
        )
    )
    decoder = MetadataStreamDecoder(max_klv_value_length=80_000)
    decoder.reconfigure({0x102: KLVCarriage.SYNCHRONOUS})
    assert decoder.feed(fragmented[0]) == []

    report = decoder.reset()

    assert report.synchronous_fragment_bytes > 0
    assert report.synchronous_partial_access_units == 1
    assert report.metadata_pids == 1
    assert report.metadata_services == 1

    klv = encode_uas_local_set({2: 1_700_000_000_000_000, 65: 19})
    restarted_muxer = _muxer(synchronous=True, metadata_service_id=9)
    restarted = _demux(
        b"".join(restarted_muxer.program_tables())
        + b"".join(
            restarted_muxer.mux_sync_klv(
                0x102,
                klv,
                pts=0,
                metadata_service_id=9,
            )
        )
    )
    assert [bytes(item.packet) for item in decoder.feed(restarted[0])] == [klv]


def test_metadata_topology_reconfiguration_rejects_partial_sync_access_unit() -> None:
    key = bytes.fromhex("060E2B34020B01010E0103017F000000")
    klv = key + encode_ber_length(70_000) + bytes(70_000)
    muxer = _muxer(synchronous=True, metadata_service_id=9)
    output = b"".join(muxer.program_tables()) + b"".join(
        muxer.mux_sync_klv(0x102, klv, pts=123_456, metadata_service_id=9)
    )
    events = _demux(output)
    assert len(events) > 1

    decoder = MetadataStreamDecoder(max_klv_value_length=80_000)
    decoder.reconfigure({0x102: KLVCarriage.SYNCHRONOUS})
    assert decoder.feed(events[0]) == []
    with pytest.raises(DecodeError, match="partial synchronous"):
        decoder.reconfigure({})

    completed = [item for event in events[1:] for item in decoder.feed(event)]
    assert [bytes(item.packet) for item in completed] == [klv]
    decoder.reconfigure({})


def test_unchanged_metadata_topology_preserves_sequence_validation_state() -> None:
    klv = encode_uas_local_set({2: 1_700_000_000_000_000, 65: 19})
    muxer = _muxer(synchronous=True)
    output = b"".join(muxer.program_tables())
    output += b"".join(muxer.mux_sync_klv(0x102, klv, pts=0))
    output += b"".join(muxer.mux_sync_klv(0x102, klv, pts=90_000))
    first, second = _demux(output)

    decoder = MetadataStreamDecoder()
    decoder.reconfigure({0x102: KLVCarriage.SYNCHRONOUS})
    assert len(decoder.feed(first)) == 1
    decoder.reconfigure({0x102: KLVCarriage.SYNCHRONOUS})
    broken = replace(
        second,
        pes=replace(
            second.pes,
            payload=second.pes.payload[:1] + b"\x02" + second.pes.payload[2:],
        ),
    )
    with pytest.raises(DecodeError, match=r"is 2, expected 1"):
        decoder.feed(broken)


def test_metadata_topology_validation_rejects_invalid_entries() -> None:
    decoder = MetadataStreamDecoder()
    with pytest.raises(TypeError, match="mapping"):
        decoder.reconfigure([])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="metadata PID"):
        decoder.reconfigure({True: KLVCarriage.ASYNCHRONOUS})
    with pytest.raises(TypeError, match="metadata carriage"):
        decoder.reconfigure({0x102: "async"})  # type: ignore[dict-item]


def test_async_muxer_fragments_one_klv_with_boundary_alignment_signals() -> None:
    klv = encode_uas_local_set(
        {2: 1_700_000_000_000_000, 3: "fragmented-mission", 65: 19}
    )
    muxer = _muxer()
    output = b"".join(muxer.program_tables())
    output += b"".join(muxer.mux_async_klv(0x102, klv, max_pes_payload=19))

    events = _demux(output)
    assert len(events) > 1
    assert [event.pes.data_alignment_indicator for event in events] == [
        True,
        *([False] * (len(events) - 1)),
    ]
    assert all(event.pes.stream_id == 0xBD for event in events)
    assert all(event.pes.pts is None and event.pes.dts is None for event in events)

    decoder = MetadataStreamDecoder()
    decoded = [packet for event in events for packet in decoder.feed(event)]
    assert len(decoded) == 1
    assert bytes(decoded[0].packet) == klv
    decoder.finish()


def test_async_muxer_rejects_invalid_klv_and_fragment_bound() -> None:
    muxer = _muxer()
    with pytest.raises((DecodeError, TruncatedData), match=r"incomplete|ended"):
        muxer.mux_async_klv(0x102, ST0601_KEY)
    with pytest.raises(ValueError, match="exactly one"):
        muxer.mux_async_klv(0x102, b"")
    valid = encode_uas_local_set({2: 1_700_000_000_000_000, 65: 19})
    with pytest.raises(ValueError, match="max_pes_payload"):
        muxer.mux_async_klv(0x102, valid, max_pes_payload=0)
    with pytest.raises(ValueError, match="max_pes_payload"):
        muxer.mux_async_klv(
            0x102,
            valid,
            max_pes_payload=65_533,
        )


def test_metadata_decoder_can_preserve_nonconforming_known_fields_with_diagnostics() -> None:
    value = bytes.fromhex(
        "020800046050584e0180"  # timestamp
        "1604000001c9"  # nonconforming four-byte Target Width
        "410101"  # version
        "01020000"  # checksum placeholder
    )
    packet = ST0601_KEY + bytes((len(value),)) + value
    packet = packet[:-2] + running_sum_16(packet[:-2]).to_bytes(2, "big")
    muxer = _muxer()
    output = b"".join(muxer.program_tables()) + b"".join(
        muxer.mux_async_klv(0x102, packet)
    )
    stream = _demux(output)[0]

    with pytest.raises(DecodeError, match="tag 22"):
        MetadataStreamDecoder().feed(stream)

    event = MetadataStreamDecoder(
        field_decoding=FieldDecodingMode.PRESERVE
    ).feed(stream)[0]
    assert isinstance(event.decoded, UASLocalSet)
    assert event.decoded.issues[0].tag == 22
    assert bytes(event.packet) == packet


def test_metadata_decoder_applies_event_aware_st0601_context() -> None:
    timestamp = 1_700_000_000_000_000
    klv = encode_uas_local_set({2: timestamp, 65: 19})
    muxer = _muxer()
    source = _demux(
        b"".join(muxer.program_tables())
        + b"".join(muxer.mux_async_klv(0x102, klv))
    )[0]
    observed: list[tuple[PESStreamEvent, bytes]] = []

    def matching_context(
        event: PESStreamEvent, packet: KLVPacket
    ) -> ST0601ValidationContext:
        observed.append((event, bytes(packet)))
        return ST0601ValidationContext(metadata_birth_timestamp=timestamp)

    decoded = MetadataStreamDecoder(
        st0601_context_provider=matching_context
    ).feed(source)

    assert isinstance(decoded[0].decoded, UASLocalSet)
    assert observed == [(source, klv)]
    assert decoded[0].validation_context == ST0601ValidationContext(
        metadata_birth_timestamp=timestamp
    )

    def mismatching_context(
        _event: PESStreamEvent, _packet: KLVPacket
    ) -> ST0601ValidationContext:
        return ST0601ValidationContext(metadata_birth_timestamp=timestamp + 1)

    with pytest.raises(DecodeError, match="time of birth"):
        MetadataStreamDecoder(
            st0601_context_provider=mismatching_context
        ).feed(source)


def test_metadata_decoder_rejects_invalid_st0601_context_provider() -> None:
    with pytest.raises(TypeError, match="st0601_context_provider"):
        MetadataStreamDecoder(st0601_context_provider=object())  # type: ignore[arg-type]

    klv = encode_uas_local_set({2: 1_700_000_000_000_000, 65: 19})
    muxer = _muxer()
    source = _demux(
        b"".join(muxer.program_tables())
        + b"".join(muxer.mux_async_klv(0x102, klv))
    )[0]
    decoder = MetadataStreamDecoder(
        st0601_context_provider=lambda _event, _packet: object()  # type: ignore[return-value]
    )
    with pytest.raises(TypeError, match="must return"):
        decoder.feed(source)


def test_sync_fragmented_access_unit_preserves_pts_and_service() -> None:
    key = bytes.fromhex("060E2B34020B01010E0103017F000000")
    klv = key + encode_ber_length(70_000) + bytes(70_000)
    muxer = _muxer(synchronous=True, metadata_service_id=9)
    output = b"".join(muxer.program_tables())
    output += b"".join(
        muxer.mux_sync_klv(
            0x102,
            klv,
            pts=123_456,
            metadata_service_id=9,
            random_access=True,
        )
    )
    decoder = MetadataStreamDecoder(max_klv_value_length=80_000)
    result = [item for event in _demux(output) for item in decoder.feed(event)]
    assert len(result) == 1
    assert bytes(result[0].packet) == klv
    assert result[0].decoded is None
    assert result[0].pts == 123_456
    assert result[0].pts_seconds == pytest.approx(1.3717333333)
    assert result[0].metadata_service_id == 9
    assert result[0].random_access
    decoder.finish()


def test_sync_decoder_rejects_service_missing_from_metadata_descriptors() -> None:
    klv = encode_uas_local_set({2: 1_700_000_000_000_000, 65: 19})
    muxer = _muxer(synchronous=True)
    undeclared = encode_metadata_au_cell(klv, metadata_service_id=9)
    output = b"".join(muxer.program_tables()) + b"".join(
        muxer.mux_pes(
            0x102,
            encode_pes_packet(undeclared, stream_id=0xFC, pts=90_000),
        )
    )

    with pytest.raises(DecodeError, match="ST 1402-15"):
        MetadataStreamDecoder().feed(_demux(output)[0])


def test_metadata_decoder_enforces_carriage_specific_pes_rules() -> None:
    async_info = asynchronous_klv_stream(0x102)
    sync_info = synchronous_klv_stream(
        0x103,
        metadata_input_leak_rate=1,
        metadata_buffer_size=1024,
    )
    async_with_pts = PESStreamEvent(
        1,
        async_info,
        StreamKind.KLV,
        KLVCarriage.ASYNCHRONOUS,
        parse_pes_packet(encode_pes_packet(b"x", stream_id=0xBD, pts=0)),
    )
    async_escr_raw = bytearray(
        encode_pes_packet(b"x", stream_id=0xBD, data_alignment_indicator=True)
    )
    async_escr_raw[7] |= 0x20
    async_with_escr = replace(
        async_with_pts,
        pes=parse_pes_packet(bytes(async_escr_raw)),
    )
    async_unbounded_raw = bytearray(
        encode_pes_packet(b"x", stream_id=0xBD, data_alignment_indicator=True)
    )
    async_unbounded_raw[4:6] = b"\x00\x00"
    async_unbounded = replace(
        async_with_pts,
        pes=parse_pes_packet(bytes(async_unbounded_raw)),
    )
    sync_without_pts = PESStreamEvent(
        1,
        sync_info,
        StreamKind.KLV,
        KLVCarriage.SYNCHRONOUS,
        parse_pes_packet(encode_pes_packet(b"x", stream_id=0xFC)),
    )
    decoder = MetadataStreamDecoder()
    with pytest.raises(DecodeError, match=r"asynchronous.*PTS"):
        decoder.feed(async_with_pts)
    with pytest.raises(DecodeError, match=r"asynchronous.*ESCR"):
        decoder.feed(async_with_escr)
    with pytest.raises(DecodeError, match=r"asynchronous.*non-zero PES_packet_length"):
        decoder.feed(async_unbounded)
    with pytest.raises(DecodeError, match=r"synchronous.*PTS"):
        decoder.feed(sync_without_pts)
    with pytest.raises(ValueError, match="KLV PES"):
        decoder.feed(replace(async_with_pts, klv_carriage=None))
def test_async_live_join_waits_for_alignment_and_finish_rejects_partial_klv() -> None:
    info = asynchronous_klv_stream(0x102)
    decoder = MetadataStreamDecoder()
    continuation = PESStreamEvent(
        1,
        info,
        StreamKind.KLV,
        KLVCarriage.ASYNCHRONOUS,
        parse_pes_packet(
            encode_pes_packet(b"tail", stream_id=0xBD, data_alignment_indicator=False)
        ),
    )
    assert decoder.feed(continuation) == []
    partial = replace(
        continuation,
        pes=parse_pes_packet(
            encode_pes_packet(
                ST0601_KEY[:8], stream_id=0xBD, data_alignment_indicator=True
            )
        ),
    )
    assert decoder.feed(partial) == []
    with pytest.raises(TruncatedData, match="incomplete"):
        decoder.finish()


def test_async_decoder_enforces_alignment_indicator_at_each_pes_boundary() -> None:
    klv = encode_uas_local_set({2: 1_700_000_000_000_000, 65: 19})
    info = asynchronous_klv_stream(0x102)

    def event(payload: bytes, *, aligned: bool) -> PESStreamEvent:
        return PESStreamEvent(
            1,
            info,
            StreamKind.KLV,
            KLVCarriage.ASYNCHRONOUS,
            parse_pes_packet(
                encode_pes_packet(
                    payload,
                    stream_id=0xBD,
                    data_alignment_indicator=aligned,
                )
            ),
        )

    boundary_decoder = MetadataStreamDecoder()
    assert len(boundary_decoder.feed(event(klv, aligned=True))) == 1
    with pytest.raises(DecodeError, match="must be set"):
        boundary_decoder.feed(event(klv, aligned=False))

    continuation_decoder = MetadataStreamDecoder()
    assert continuation_decoder.feed(event(klv[:20], aligned=True)) == []
    with pytest.raises(DecodeError, match="must be clear"):
        continuation_decoder.feed(event(klv[20:], aligned=True))

    empty_decoder = MetadataStreamDecoder()
    assert empty_decoder.feed(event(b"", aligned=True)) == []


def test_sync_fragment_sequence_and_resource_limits_are_enforced() -> None:
    muxer = _muxer(synchronous=True)
    klv = ST0601_KEY + encode_ber_length(70_000) + bytes(70_000)
    output = b"".join(muxer.program_tables()) + b"".join(
        muxer.mux_sync_klv(0x102, klv, pts=0)
    )
    events = _demux(output)
    broken = replace(
        events[1],
        pes=replace(
            events[1].pes,
            payload=events[1].pes.payload[:1]
            + b"\x09"
            + events[1].pes.payload[2:],
        ),
    )
    decoder = MetadataStreamDecoder(max_klv_value_length=80_000)
    decoder.feed(events[0])
    with pytest.raises(DecodeError, match="sequence"):
        decoder.feed(broken)

    tolerant = MetadataStreamDecoder(
        max_klv_value_length=80_000,
        validate_sequence=False,
    )
    tolerant.feed(events[0])
    with pytest.raises(DecodeError, match="checksum"):
        tolerant.feed(broken)

    bounded = MetadataStreamDecoder(max_access_unit_length=100)
    with pytest.raises(LimitExceeded, match="access unit"):
        bounded.feed(events[0])

    with pytest.raises(TypeError, match="validate_sequence"):
        MetadataStreamDecoder(validate_sequence=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ontology_resolver"):
        MetadataStreamDecoder(ontology_resolver=object())  # type: ignore[arg-type]


def test_complete_sync_klv_is_decoded_without_fragment_state() -> None:
    klv = encode_uas_local_set({2: 1_700_000_000_000_000, 65: 19})
    muxer = _muxer(synchronous=True, metadata_service_id=3)
    output = b"".join(muxer.program_tables()) + b"".join(
        muxer.mux_sync_klv(0x102, klv, pts=90_000, metadata_service_id=3)
    )
    event = _demux(output)[0]
    result = MetadataStreamDecoder().feed(event)
    assert len(result) == 1
    assert isinstance(result[0].decoded, UASLocalSet)
    assert result[0].metadata_service_id == 3
    assert result[0].random_access


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"max_klv_value_length": -1}, "max_klv_value_length"),
        ({"max_access_unit_length": 0}, "max_access_unit_length"),
        ({"max_services_per_pid": 0}, "max_services_per_pid"),
    ],
)
def test_metadata_stream_decoder_validates_bounds(
    kwargs: dict[str, int], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        MetadataStreamDecoder(**kwargs)
