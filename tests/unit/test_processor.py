from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest

from stanag4609.errors import DecodeError, LimitExceeded
from stanag4609.st0601 import UASLocalSet, encode_uas_local_set
from stanag4609.st0903 import DetectionStatus, Location, VTargetData, encode_vmti_local_set
from stanag4609.st1204 import MIISCoreIdentifier
from stanag4609.transport.processor import (
    MetadataDecision,
    MetadataProcessorChain,
    TimedKLVPacket,
)
from stanag4609.transport.psi import KLVCarriage


def _timed(data: bytes, *, pts: int = 90_000) -> TimedKLVPacket:
    return TimedKLVPacket.from_bytes(
        data,
        program_number=1,
        pid=0x102,
        carriage=KLVCarriage.SYNCHRONOUS,
        pts=pts,
        metadata_service_id=7,
        random_access=True,
    )


def _uas(timestamp: int = 1_700_000_000_000_000) -> bytes:
    return encode_uas_local_set({2: timestamp, 65: 19})


def test_empty_processor_chain_passes_timed_packet_without_copying() -> None:
    event = _timed(_uas())
    result = MetadataProcessorChain(()).process(event)
    assert result == (event,)
    assert result[0] is event
    assert result[0].pts_seconds == 1.0
    assert isinstance(result[0].decoded, UASLocalSet)


def test_processors_compose_drop_replace_and_injection_in_order() -> None:
    first = _uas(1_700_000_000_000_000)
    replacement = _uas(1_700_000_000_000_001)
    vmti = encode_vmti_local_set(
        {
            2: 1_700_000_000_000_000,
            4: 6,
            8: 1920,
            11: 12.5,
            12: 10.0,
            13: MIISCoreIdentifier(
                version=1,
                minor_id=UUID("01020304-0506-4708-890a-0b0c10111213"),
            ),
        },
        targets=(
            VTargetData(
                42,
                {
                    1: 100,
                    2: 90,
                    3: 110,
                    5: 96,
                    17: Location(0, 0, 0),
                    23: DetectionStatus.ACTIVE_MOVING,
                },
            ),
        ),
        standalone=True,
    )
    seen: list[bytes] = []

    def replace_uas(event: TimedKLVPacket) -> MetadataDecision:
        return MetadataDecision.replace(replacement)

    def inject_detection(event: TimedKLVPacket) -> MetadataDecision:
        seen.append(bytes(event.packet))
        return MetadataDecision.inject_after(vmti)

    result = MetadataProcessorChain((replace_uas, inject_detection)).process(_timed(first))
    assert seen == [replacement]
    assert [bytes(event.packet) for event in result] == [replacement, vmti]
    assert all(event.pts == 90_000 for event in result)
    assert result[1].decoded.targets[0].target_id == 42

    dropped = MetadataProcessorChain((lambda _: MetadataDecision.drop(),)).process(
        _timed(first)
    )
    assert dropped == ()


def test_injected_packets_continue_through_later_processors() -> None:
    one = _uas(1)
    two = _uas(2)
    observed: list[int] = []

    def inject_before(_: TimedKLVPacket) -> MetadataDecision:
        return MetadataDecision.inject_before(one)

    def observe(event: TimedKLVPacket) -> MetadataDecision:
        assert isinstance(event.decoded, UASLocalSet)
        observed.append(event.decoded.value(2).microsecond)
        return MetadataDecision.pass_through()

    result = MetadataProcessorChain((inject_before, observe)).process(_timed(two))
    assert observed == [1, 2]
    assert [bytes(item.packet) for item in result] == [one, two]


def test_replacement_retains_transport_context_but_can_override_random_access() -> None:
    event = _timed(_uas())
    changed = event.with_bytes(_uas(2), random_access=False)
    assert changed.program_number == event.program_number
    assert changed.pid == event.pid
    assert changed.carriage is event.carriage
    assert changed.pts == event.pts
    assert changed.metadata_service_id == event.metadata_service_id
    assert not changed.random_access


def test_processor_chain_rejects_invalid_decisions_packets_and_unbounded_expansion() -> None:
    event = _timed(_uas())
    with pytest.raises(TypeError, match="MetadataDecision"):
        MetadataProcessorChain((lambda _: object(),)).process(event)
    with pytest.raises(DecodeError, match="exactly one"):
        MetadataProcessorChain(
            (lambda _: MetadataDecision.replace(_uas() + _uas()),)
        ).process(event)
    with pytest.raises(LimitExceeded, match="packet length"):
        MetadataProcessorChain(
            (lambda _: MetadataDecision.replace(_uas()),), max_packet_length=8
        ).process(event)
    with pytest.raises(LimitExceeded, match="packets per input"):
        MetadataProcessorChain(
            (lambda _: MetadataDecision.inject_after(_uas(), _uas()),),
            max_packets_per_input=2,
        ).process(event)


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"max_processors": 0}, "max_processors"),
        ({"max_packets_per_input": 0}, "max_packets_per_input"),
        ({"max_packet_length": 0}, "max_packet_length"),
    ],
)
def test_processor_chain_validates_resource_bounds(
    kwargs: dict[str, int], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        MetadataProcessorChain((), **kwargs)

    processors = tuple(lambda event: MetadataDecision.pass_through() for _ in range(2))
    if "max_processors" in kwargs:
        with pytest.raises(LimitExceeded, match="processor count"):
            MetadataProcessorChain(processors, max_processors=1)


def test_timed_packet_is_immutable_and_requires_consistent_timing() -> None:
    event = _timed(_uas())
    with pytest.raises(ValueError, match="PTS"):
        TimedKLVPacket.from_bytes(
            _uas(),
            program_number=1,
            pid=0x102,
            carriage=KLVCarriage.SYNCHRONOUS,
            pts=None,
        )
    with pytest.raises(ValueError, match="must not carry PTS"):
        replace(event, carriage=KLVCarriage.ASYNCHRONOUS)
