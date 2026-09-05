from __future__ import annotations

import pytest

from stanag4609.errors import DecodeError
from stanag4609.transport.demux import PESStreamEvent, TransportDemuxer
from stanag4609.transport.metadata import (
    CellFragmentation,
    MetadataDescriptorHeader,
    MetadataSTDDescriptor,
    asynchronous_klv_stream,
    decode_metadata_descriptor_header,
    decode_metadata_std_descriptor,
    encode_metadata_au_cell,
    encode_metadata_std_descriptor,
    klva_metadata_service_ids,
    parse_metadata_au_cells,
    synchronous_klv_stream,
)
from stanag4609.transport.mux import TransportMuxer
from stanag4609.transport.psi import (
    Descriptor,
    ElementaryStreamInfo,
    KLVCarriage,
    find_klv_streams,
    parse_pmt,
)


def test_metadata_std_descriptor_round_trip_and_physical_units() -> None:
    value = MetadataSTDDescriptor(1_000, 4_096, 0)
    descriptor = encode_metadata_std_descriptor(value)
    assert descriptor.tag == 0x27
    assert descriptor.data == bytes.fromhex("C003E8 C01000 C00000")
    assert decode_metadata_std_descriptor(descriptor) == value
    assert value.input_bits_per_second == 400_000
    assert value.buffer_bytes == 4_194_304
    assert value.output_bits_per_second == 0

    physical = MetadataSTDDescriptor.from_physical(
        input_bits_per_second=400_000,
        buffer_bytes=4_194_304,
    )
    assert physical == value


@pytest.mark.parametrize(
    "descriptor, message",
    [
        (Descriptor(0x26, bytes(9)), "tag"),
        (Descriptor(0x27, bytes(8)), "nine bytes"),
        (Descriptor(0x27, bytes.fromhex("800000 C00000 C00000")), "reserved"),
    ],
)
def test_metadata_std_descriptor_rejects_malformed_wire_values(
    descriptor: Descriptor,
    message: str,
) -> None:
    with pytest.raises(DecodeError, match=message):
        decode_metadata_std_descriptor(descriptor)


def test_metadata_std_descriptor_validates_codes_and_exact_physical_units() -> None:
    with pytest.raises(TypeError, match="Descriptor"):
        decode_metadata_std_descriptor(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="MetadataSTDDescriptor"):
        encode_metadata_std_descriptor(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="input_leak_rate"):
        MetadataSTDDescriptor(0x400000, 1, 0)
    with pytest.raises(ValueError, match="buffer_size"):
        MetadataSTDDescriptor(1, -1, 0)
    with pytest.raises(ValueError, match="multiple of 400"):
        MetadataSTDDescriptor.from_physical(
            input_bits_per_second=1,
            buffer_bytes=1_024,
        )
    with pytest.raises(ValueError, match="multiple of 1024"):
        MetadataSTDDescriptor.from_physical(
            input_bits_per_second=400,
            buffer_bytes=1,
        )
    with pytest.raises(ValueError, match=r"output_bits_per_second.*multiple of 400"):
        MetadataSTDDescriptor.from_physical(
            input_bits_per_second=400,
            buffer_bytes=1_024,
            output_bits_per_second=1,
        )


def test_synchronous_stream_accepts_one_typed_std_descriptor() -> None:
    std = MetadataSTDDescriptor.from_physical(
        input_bits_per_second=400_000,
        buffer_bytes=4_194_304,
    )
    stream = synchronous_klv_stream(0x102, metadata_std=std)
    assert decode_metadata_std_descriptor(stream.descriptors[1]) == std

    with pytest.raises(ValueError, match="either metadata_std"):
        synchronous_klv_stream(
            0x102,
            metadata_std=std,
            metadata_input_leak_rate=1_000,
            metadata_buffer_size=4_096,
        )
    with pytest.raises(ValueError, match="required"):
        synchronous_klv_stream(0x102)
    with pytest.raises(TypeError, match="metadata_std"):
        synchronous_klv_stream(0x102, metadata_std=object())  # type: ignore[arg-type]

    multiple = synchronous_klv_stream(
        0x102,
        metadata_service_ids=(4, 9),
        metadata_std=std,
    )
    assert klva_metadata_service_ids(multiple) == frozenset({4, 9})
    assert [descriptor.tag for descriptor in multiple.descriptors] == [0x26, 0x26, 0x27]
    with pytest.raises(ValueError, match="must not be empty"):
        synchronous_klv_stream(0x102, metadata_service_ids=(), metadata_std=std)
    with pytest.raises(ValueError, match="unique"):
        synchronous_klv_stream(0x102, metadata_service_ids=(4, 4), metadata_std=std)
    with pytest.raises(ValueError, match="either metadata_service_id"):
        synchronous_klv_stream(
            0x102,
            metadata_service_id=4,
            metadata_service_ids=(4, 9),
            metadata_std=std,
        )


def test_metadata_descriptor_header_exposes_klva_service_identity() -> None:
    stream = synchronous_klv_stream(
        0x102,
        metadata_service_id=4,
        metadata_input_leak_rate=1_000,
        metadata_buffer_size=4_096,
    )
    assert decode_metadata_descriptor_header(stream.descriptors[0]) == (
        MetadataDescriptorHeader(
            application_format=0x0100,
            application_format_identifier=None,
            metadata_format=0xFF,
            metadata_format_identifier=b"KLVA",
            metadata_service_id=4,
            decoder_config_flags=0,
            dsm_cc=False,
        )
    )
    assert klva_metadata_service_ids(stream) == frozenset({4})
    with pytest.raises(TypeError, match="ElementaryStreamInfo"):
        klva_metadata_service_ids(object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "descriptor, message",
    [
        (Descriptor(0x27, b""), "tag"),
        (Descriptor(0x26, b"\x01\x00\xffKLV"), "truncated"),
        (Descriptor(0x26, b"\x01\x00\xffKLVA\x04\x00"), "reserved"),
    ],
)
def test_metadata_descriptor_header_rejects_malformed_prefix(
    descriptor: Descriptor,
    message: str,
) -> None:
    with pytest.raises(DecodeError, match=message):
        decode_metadata_descriptor_header(descriptor)


def test_metadata_au_cell_round_trip_and_exact_header() -> None:
    raw = encode_metadata_au_cell(
        b"KLV",
        metadata_service_id=4,
        sequence_number=255,
        fragmentation=CellFragmentation.COMPLETE,
        random_access=True,
    )
    assert raw == bytes.fromhex("04 FF DF 00 03") + b"KLV"
    cell = parse_metadata_au_cells(raw)[0]
    assert cell.metadata_service_id == 4
    assert cell.sequence_number == 255
    assert cell.fragmentation is CellFragmentation.COMPLETE
    assert cell.random_access
    assert not cell.decoder_config
    assert cell.data == b"KLV"
    assert cell.raw == raw


def test_metadata_au_wrapper_validates_reserved_length_and_sequence() -> None:
    first = encode_metadata_au_cell(
        b"a", sequence_number=255, fragmentation=CellFragmentation.FIRST
    )
    last = encode_metadata_au_cell(
        b"b", sequence_number=0, fragmentation=CellFragmentation.LAST
    )
    assert b"".join(cell.data for cell in parse_metadata_au_cells(first + last)) == b"ab"
    with pytest.raises(DecodeError, match="reserved"):
        parse_metadata_au_cells(first[:2] + b"\x80" + first[3:])
    with pytest.raises(DecodeError, match="overruns"):
        parse_metadata_au_cells(first[:-1])
    with pytest.raises(DecodeError, match="sequence"):
        parse_metadata_au_cells(first + last[:1] + b"\x01" + last[2:])


def test_klva_stream_descriptor_helpers_match_pmt_discovery() -> None:
    sync = synchronous_klv_stream(
        0x103,
        metadata_service_id=4,
        metadata_input_leak_rate=1000,
        metadata_buffer_size=4096,
    )
    async_stream = asynchronous_klv_stream(0x102)
    muxer = TransportMuxer(
        transport_stream_id=1,
        program_number=1,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=(async_stream, sync, asynchronous_klv_stream(0x101)),
    )
    pmt_packet = muxer.program_tables()[1]
    adaptation_length = pmt_packet[4]
    payload = pmt_packet[5 + adaptation_length :]
    pmt = parse_pmt(payload[1:])
    assert find_klv_streams(pmt) == (
        (0x102, KLVCarriage.ASYNCHRONOUS),
        (0x103, KLVCarriage.SYNCHRONOUS),
        (0x101, KLVCarriage.ASYNCHRONOUS),
    )
    assert sync.descriptors[0].raw == bytes.fromhex("26090100FF4B4C5641040F")
    assert sync.descriptors[1].tag == 0x27
    assert len(sync.descriptors[1].data) == 9


def test_sync_mux_fragments_large_access_unit_and_preserves_pts() -> None:
    sync = synchronous_klv_stream(
        0x103,
        metadata_input_leak_rate=1000,
        metadata_buffer_size=131_072,
    )
    video = ElementaryStreamInfo(0x1B, 0x101, ())
    muxer = TransportMuxer(
        transport_stream_id=1,
        program_number=1,
        program_map_pid=0x100,
        pcr_pid=0x101,
        streams=(video, sync),
    )
    access_unit = bytes(range(256)) * 550
    output = b"".join(muxer.program_tables())
    output += b"".join(muxer.mux_sync_klv(0x103, access_unit, pts=90_000))
    events = TransportDemuxer(max_pes_length=70_000).feed(output)
    metadata = [event for event in events if isinstance(event, PESStreamEvent)]
    assert len(metadata) == 3
    assert all(event.pes.stream_id == 0xFC for event in metadata)
    assert all(event.pes.pts == 90_000 for event in metadata)
    cells = tuple(
        cell for event in metadata for cell in parse_metadata_au_cells(event.pes.payload)
    )
    assert [cell.fragmentation for cell in cells] == [
        CellFragmentation.FIRST,
        CellFragmentation.MIDDLE,
        CellFragmentation.LAST,
    ]
    assert [cell.sequence_number for cell in cells] == [0, 1, 2]
    assert b"".join(cell.data for cell in cells) == access_unit


def test_sync_mux_requires_matching_stream_and_pts() -> None:
    async_stream = asynchronous_klv_stream(0x102)
    muxer = TransportMuxer(
        transport_stream_id=1,
        program_number=1,
        program_map_pid=0x100,
        pcr_pid=0x102,
        streams=(async_stream,),
    )
    with pytest.raises(DecodeError, match="synchronous KLVA"):
        muxer.mux_sync_klv(0x102, b"klv", pts=0)
    with pytest.raises(ValueError, match="must not be empty"):
        encode_metadata_au_cell(b"")


def test_sync_mux_rejects_service_missing_from_metadata_descriptors() -> None:
    sync = synchronous_klv_stream(
        0x102,
        metadata_service_id=4,
        metadata_input_leak_rate=1_000,
        metadata_buffer_size=4_096,
    )
    muxer = TransportMuxer(
        transport_stream_id=1,
        program_number=1,
        program_map_pid=0x100,
        pcr_pid=0x102,
        streams=(sync,),
    )

    with pytest.raises(DecodeError, match="ST 1402-15"):
        muxer.mux_sync_klv(0x102, b"klv", pts=0, metadata_service_id=9)


def test_sync_mux_accepts_multiple_services_declared_on_one_pid() -> None:
    stream = synchronous_klv_stream(
        0x102,
        metadata_service_ids=(4, 9),
        metadata_input_leak_rate=1_000,
        metadata_buffer_size=4_096,
    )
    muxer = TransportMuxer(
        transport_stream_id=1,
        program_number=1,
        program_map_pid=0x100,
        pcr_pid=0x102,
        streams=(stream,),
    )

    output = b"".join(muxer.program_tables())
    output += b"".join(muxer.mux_sync_klv(0x102, b"one", pts=0, metadata_service_id=4))
    output += b"".join(muxer.mux_sync_klv(0x102, b"two", pts=0, metadata_service_id=9))
    services = [
        cell.metadata_service_id
        for event in TransportDemuxer().feed(output)
        if isinstance(event, PESStreamEvent)
        for cell in parse_metadata_au_cells(event.pes.payload)
    ]
    assert services == [4, 9]
