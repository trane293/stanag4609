from __future__ import annotations

from fractions import Fraction

import pytest

from stanag4609.errors import LimitExceeded
from stanag4609.transport import (
    AsynchronousMetadataSTDByte,
    AsynchronousMetadataSTDModel,
    AsynchronousMetadataSTDModelResult,
    IncrementalAsynchronousMetadataSTDModel,
    asynchronous_metadata_std_bytes_from_pes,
    simulate_asynchronous_metadata_pes,
)
from stanag4609.transport.demux import PESStreamEvent, ProgramClockEvent, StreamKind
from stanag4609.transport.metadata import MetadataSTDDescriptor
from stanag4609.transport.mpegts import parse_transport_packet
from stanag4609.transport.mux import encode_pcr_packet, encode_pes_packet, packetize_pes
from stanag4609.transport.pcr import pcr_from_ticks
from stanag4609.transport.pes import parse_pes_packet
from stanag4609.transport.psi import ElementaryStreamInfo, KLVCarriage


def _descriptor(
    *,
    input_rate: int = 8_000,
    output_rate: int = 4_000,
    buffer: int = 1_024,
) -> MetadataSTDDescriptor:
    return MetadataSTDDescriptor.from_physical(
        input_bits_per_second=input_rate,
        buffer_bytes=buffer,
        output_bits_per_second=output_rate,
    )


def _bytes(
    count: int,
    *,
    arrival: Fraction = Fraction(0),
    spacing: Fraction = Fraction(0),
    enters_main_buffer: bool = True,
) -> tuple[AsynchronousMetadataSTDByte, ...]:
    return tuple(
        AsynchronousMetadataSTDByte(
            arrival + index * spacing,
            enters_main_buffer=enters_main_buffer,
            source_offset=index,
        )
        for index in range(count)
    )


def _clock_event(ticks: int, *, offset: int) -> ProgramClockEvent:
    packet = parse_transport_packet(
        encode_pcr_packet(pid=0x101, pcr=pcr_from_ticks(ticks)),
        offset=offset,
    )
    assert packet.pcr is not None
    return ProgramClockEvent(1, packet.pcr, None, False, packet)


def _async_pes_event(data: bytes, *, offset: int = 188) -> PESStreamEvent:
    pes_raw = encode_pes_packet(
        data,
        stream_id=0xBD,
        data_alignment_indicator=True,
    )
    raw_packets, _ = packetize_pes(pes_raw, pid=0x102)
    packets = tuple(
        parse_transport_packet(raw, offset=offset + index * 188)
        for index, raw in enumerate(raw_packets)
    )
    pes = parse_pes_packet(pes_raw, offset=offset, transport_packets=packets)
    return PESStreamEvent(
        1,
        ElementaryStreamInfo(0x06, 0x102, ()),
        StreamKind.KLV,
        KLVCarriage.ASYNCHRONOUS,
        pes,
    )


def test_exact_asynchronous_std_applies_both_descriptor_leak_rates() -> None:
    values = (
        AsynchronousMetadataSTDByte(0, source_offset=0),
        AsynchronousMetadataSTDByte(0, enters_main_buffer=True, source_offset=1),
    )

    result = AsynchronousMetadataSTDModel(_descriptor()).simulate(values)

    assert isinstance(result, AsynchronousMetadataSTDModelResult)
    assert result.conformant
    assert result.transport_bytes == 2
    assert result.main_buffer_bytes == 1
    assert result.maximum_transport_buffer_fullness == 2
    assert result.maximum_main_buffer_fullness == 1
    assert result.final_transport_buffer_fullness == 0
    assert result.final_main_buffer_fullness == 0
    assert result.maximum_transport_busy_interval == Fraction(2, 1_000)
    assert result.maximum_decoder_delay == Fraction(4, 1_000)
    assert result.minimum_decoder_delay == Fraction(4, 1_000)


def test_incremental_asynchronous_std_matches_finite_model_across_batches() -> None:
    values = _bytes(
        40,
        arrival=Fraction(1, 10),
        spacing=Fraction(1, 20_000),
    )
    expected = AsynchronousMetadataSTDModel(_descriptor()).simulate(values)
    model = IncrementalAsynchronousMetadataSTDModel(_descriptor())

    observed = (*model.feed(values[:13]), *model.feed(values[13:29]))
    observed += model.feed(values[29:])
    observed += model.advance(Fraction(1, 2))
    result = model.finish()

    assert observed == expected.issues
    assert result == expected
    assert model.pending_events == 0


def test_incremental_asynchronous_std_preserves_occupancy_across_feeds() -> None:
    model = IncrementalAsynchronousMetadataSTDModel(
        _descriptor(input_rate=1_600_000, output_rate=400, buffer=1_024),
        transport_buffer_size=2_048,
    )

    assert model.feed(_bytes(600)) == ()
    model.feed(
        tuple(
            AsynchronousMetadataSTDByte(
                Fraction(1, 100_000),
                enters_main_buffer=True,
                source_offset=600 + index,
            )
            for index in range(425)
        )
    )
    issues = model.advance(1)
    result = model.finish()

    assert any(issue.code == "main_buffer_overflow" for issue in issues)
    assert result.main_buffer_bytes == 1_025
    assert result.maximum_main_buffer_fullness == 1_025


def test_incremental_asynchronous_std_bounds_and_lifecycle() -> None:
    with pytest.raises(TypeError, match="MetadataSTDDescriptor"):
        IncrementalAsynchronousMetadataSTDModel(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="transport_buffer_size"):
        IncrementalAsynchronousMetadataSTDModel(_descriptor(), transport_buffer_size=0)
    with pytest.raises(ValueError, match="maximum_delay"):
        IncrementalAsynchronousMetadataSTDModel(_descriptor(), maximum_delay=0)
    with pytest.raises(ValueError, match="max_pending_events"):
        IncrementalAsynchronousMetadataSTDModel(_descriptor(), max_pending_events=0)

    model = IncrementalAsynchronousMetadataSTDModel(
        _descriptor(), max_pending_events=2
    )
    with pytest.raises(LimitExceeded, match="pending events"):
        model.feed(_bytes(3))
    assert model.pending_events == 0

    with pytest.raises(TypeError, match="iterable"):
        model.feed(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="AsynchronousMetadataSTDByte"):
        model.feed([object()])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="nondecreasing"):
        model.feed(
            (AsynchronousMetadataSTDByte(1), AsynchronousMetadataSTDByte(0))
        )

    model.feed(_bytes(1, arrival=1))
    with pytest.raises(ValueError, match="latest byte arrival"):
        model.advance(0)
    model.advance(2)
    with pytest.raises(ValueError, match="processed watermark"):
        model.feed(_bytes(1, arrival=1))
    model.finish()
    with pytest.raises(RuntimeError, match="already finished"):
        model.feed(())
    with pytest.raises(RuntimeError, match="already finished"):
        model.advance(3)
    with pytest.raises(RuntimeError, match="already finished"):
        model.finish()


def test_incremental_asynchronous_std_reports_impossible_zero_rates_live() -> None:
    no_input = IncrementalAsynchronousMetadataSTDModel(_descriptor(input_rate=0))
    issues = no_input.feed(_bytes(1))
    assert [issue.code for issue in issues] == ["zero_input_leak_rate"]
    assert no_input.finish().final_transport_buffer_fullness == 1

    no_output = IncrementalAsynchronousMetadataSTDModel(_descriptor(output_rate=0))
    no_output.feed(_bytes(1))
    issues = no_output.advance(1)
    assert [issue.code for issue in issues] == ["zero_output_leak_rate"]
    assert no_output.finish().final_main_buffer_fullness == 1


def test_asynchronous_std_reports_zero_output_rate_with_retained_bytes() -> None:
    result = AsynchronousMetadataSTDModel(_descriptor(output_rate=0)).simulate(
        _bytes(3)
    )

    issue = next(issue for issue in result.issues if issue.code == "zero_output_leak_rate")
    assert issue.requirement == "ITU-T H.222.0 §§2.6.63, 2.12.10"
    assert result.final_main_buffer_fullness == 3


def test_asynchronous_std_enforces_transport_buffer_constraints() -> None:
    result = AsynchronousMetadataSTDModel(
        _descriptor(input_rate=400, output_rate=400)
    ).simulate(_bytes(513, enters_main_buffer=False))

    assert result.maximum_transport_buffer_fullness == 513
    assert {issue.code for issue in result.issues} == {
        "transport_buffer_overflow",
        "transport_buffer_not_emptied",
    }
    assert result.maximum_transport_busy_interval == Fraction(513, 50)


def test_asynchronous_std_reports_zero_input_rate_without_moving_pes_bytes() -> None:
    result = AsynchronousMetadataSTDModel(
        _descriptor(input_rate=0, output_rate=400)
    ).simulate(_bytes(2))

    assert {issue.code for issue in result.issues} == {"zero_input_leak_rate"}
    assert result.final_transport_buffer_fullness == 2
    assert result.main_buffer_bytes == 0
    assert result.final_main_buffer_fullness == 0


def test_asynchronous_std_reports_main_buffer_overflow() -> None:
    result = AsynchronousMetadataSTDModel(
        _descriptor(input_rate=1_600_000, output_rate=400)
    ).simulate(_bytes(1_025, spacing=Fraction(1, 200_000)))

    issue = next(issue for issue in result.issues if issue.code == "main_buffer_overflow")
    assert issue.fullness == 1_025
    assert issue.capacity == 1_024
    assert result.maximum_main_buffer_fullness == 1_025


def test_asynchronous_std_reports_end_to_end_decoder_delay() -> None:
    result = AsynchronousMetadataSTDModel(
        _descriptor(input_rate=8_000, output_rate=400),
    ).simulate(_bytes(51))

    issue = next(issue for issue in result.issues if issue.code == "excessive_delay")
    assert issue.requirement == "ITU-T H.222.0 §2.4.2.6"
    assert issue.delay == Fraction(1_001, 1_000)
    assert issue.permitted_delay == 1
    assert result.maximum_decoder_delay == Fraction(1_021, 1_000)


def test_asynchronous_std_validates_values_and_model_configuration() -> None:
    with pytest.raises(TypeError, match="boolean"):
        AsynchronousMetadataSTDByte(0, enters_main_buffer=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="source_offset"):
        AsynchronousMetadataSTDByte(0, source_offset=-1)
    with pytest.raises(TypeError, match="MetadataSTDDescriptor"):
        AsynchronousMetadataSTDModel(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="transport_buffer_size"):
        AsynchronousMetadataSTDModel(_descriptor(), transport_buffer_size=0)
    with pytest.raises(ValueError, match="maximum_delay"):
        AsynchronousMetadataSTDModel(_descriptor(), maximum_delay=0)
    with pytest.raises(TypeError, match="iterable"):
        AsynchronousMetadataSTDModel(_descriptor()).simulate(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="AsynchronousMetadataSTDByte"):
        AsynchronousMetadataSTDModel(_descriptor()).simulate([object()])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="nondecreasing"):
        AsynchronousMetadataSTDModel(_descriptor()).simulate(
            (
                AsynchronousMetadataSTDByte(1),
                AsynchronousMetadataSTDByte(0),
            )
        )


def test_async_pcr_adapter_classifies_transport_and_pes_bytes() -> None:
    event = _async_pes_event(b"metadata")
    following_offset = 188 + len(event.pes.transport_packets) * 188

    values = asynchronous_metadata_std_bytes_from_pes(
        event,
        (
            _clock_event(27_000_000, offset=0),
            _clock_event(54_000_000, offset=following_offset),
        ),
    )

    assert len(values) == len(event.pes.transport_packets) * 188
    assert sum(value.enters_main_buffer for value in values) == len(event.pes.raw)
    assert values[0].arrival_time == 1 + Fraction(178, following_offset)


def test_async_pcr_adapter_rejects_wrong_carriage_and_missing_bracket() -> None:
    event = _async_pes_event(b"metadata")
    first = _clock_event(0, offset=0)
    with pytest.raises(ValueError, match="bracket"):
        asynchronous_metadata_std_bytes_from_pes(
            event,
            (first, _clock_event(1, offset=188)),
        )
    wrong = PESStreamEvent(
        event.program_number,
        event.stream,
        event.kind,
        KLVCarriage.SYNCHRONOUS,
        event.pes,
    )
    with pytest.raises(ValueError, match="asynchronous"):
        asynchronous_metadata_std_bytes_from_pes(
            wrong,
            (first, _clock_event(1, offset=1_000)),
        )


def test_recorded_async_pes_audit_aggregates_buffer_occupancy() -> None:
    first = _async_pes_event(b"a" * 600)
    second_offset = first.pes.transport_packets[-1].offset + 188
    second = _async_pes_event(b"b" * 600, offset=second_offset)
    following_offset = second.pes.transport_packets[-1].offset + 188

    result = simulate_asynchronous_metadata_pes(
        _descriptor(input_rate=1_600_000, output_rate=400),
        (first, second),
        (
            _clock_event(27_000_000, offset=0),
            _clock_event(54_000_000, offset=following_offset),
        ),
    )

    assert result.main_buffer_bytes > 1_024
    assert any(issue.code == "main_buffer_overflow" for issue in result.issues)
