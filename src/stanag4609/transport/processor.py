"""Bounded, composable processing decisions for timed KLV metadata."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any, TypeAlias

from stanag4609.errors import DecodeError, LimitExceeded
from stanag4609.klv.model import KLVPacket
from stanag4609.klv.stream import KLVStreamParser
from stanag4609.transport.metadata_stream import KLVMetadataEvent, decode_known_klv
from stanag4609.transport.psi import KLVCarriage


@dataclass(frozen=True, slots=True)
class TimedKLVPacket:
    """One immutable KLV packet plus its transport synchronization context."""

    program_number: int
    pid: int
    carriage: KLVCarriage
    pts: int | None
    metadata_service_id: int | None
    random_access: bool
    packet: KLVPacket
    decoded: Any

    def __post_init__(self) -> None:
        if not 1 <= self.program_number <= 0xFFFF:
            raise ValueError("program_number must be between 1 and 65535")
        if not 0 <= self.pid <= 0x1FFF:
            raise ValueError("PID must be between 0 and 8191")
        if not isinstance(self.carriage, KLVCarriage):
            raise TypeError("carriage must be KLVCarriage")
        if self.pts is not None and (
            isinstance(self.pts, bool)
            or not isinstance(self.pts, int)
            or not 0 <= self.pts <= 2**33 - 1
        ):
            raise ValueError("PTS must be an unsigned 33-bit integer")
        if self.carriage is KLVCarriage.SYNCHRONOUS:
            if self.pts is None:
                raise ValueError("synchronous KLV requires PTS")
            if self.metadata_service_id is None:
                raise ValueError("synchronous KLV requires metadata_service_id")
        elif self.pts is not None:
            raise ValueError("asynchronous KLV must not carry PTS")
        if self.metadata_service_id is not None and (
            isinstance(self.metadata_service_id, bool)
            or not isinstance(self.metadata_service_id, int)
            or not 0 <= self.metadata_service_id <= 0xFF
        ):
            raise ValueError("metadata_service_id must be an integer from 0 to 255")
        if not isinstance(self.random_access, bool):
            raise TypeError("random_access must be bool")
        if not isinstance(self.packet, KLVPacket):
            raise TypeError("packet must be KLVPacket")

    @property
    def pts_seconds(self) -> float | None:
        return None if self.pts is None else self.pts / 90_000

    @classmethod
    def from_bytes(
        cls,
        data: bytes,
        *,
        program_number: int,
        pid: int,
        carriage: KLVCarriage,
        pts: int | None,
        metadata_service_id: int | None = None,
        random_access: bool = False,
        max_packet_length: int = 64 * 1024 * 1024,
    ) -> TimedKLVPacket:
        """Parse exactly one complete KLV packet and attach transport context."""

        packet = _parse_one_packet(data, max_packet_length=max_packet_length)
        return cls(
            program_number,
            pid,
            carriage,
            pts,
            metadata_service_id,
            random_access,
            packet,
            decode_known_klv(packet),
        )

    @classmethod
    def from_event(cls, event: KLVMetadataEvent) -> TimedKLVPacket:
        """Detach a metadata decoder event from its source PES object."""

        return cls(
            event.program_number,
            event.pid,
            event.carriage,
            event.pts,
            event.metadata_service_id,
            event.random_access,
            event.packet,
            event.decoded,
        )

    def with_bytes(
        self,
        data: bytes,
        *,
        random_access: bool | None = None,
        max_packet_length: int = 64 * 1024 * 1024,
    ) -> TimedKLVPacket:
        """Replace the KLV packet while retaining its program, PID, and PTS."""

        return TimedKLVPacket.from_bytes(
            data,
            program_number=self.program_number,
            pid=self.pid,
            carriage=self.carriage,
            pts=self.pts,
            metadata_service_id=self.metadata_service_id,
            random_access=(
                self.random_access if random_access is None else random_access
            ),
            max_packet_length=max_packet_length,
        )


def _parse_one_packet(data: bytes, *, max_packet_length: int) -> KLVPacket:
    if not isinstance(data, bytes):
        raise TypeError("emitted KLV packet must be bytes")
    if len(data) > max_packet_length:
        raise LimitExceeded(
            f"KLV packet length {len(data)} exceeds configured limit {max_packet_length}"
        )
    parser = KLVStreamParser(max_value_length=max_packet_length)
    packets = parser.feed(data)
    packets.extend(parser.finish())
    if len(packets) != 1 or bytes(packets[0]) != data:
        raise DecodeError(
            f"processor output must contain exactly one KLV packet, got {len(packets)}"
        )
    return packets[0]


class MetadataAction(Enum):
    """How a processor changes one timed metadata packet."""

    PASS = "pass"
    DROP = "drop"
    REPLACE = "replace"
    INJECT_BEFORE = "inject_before"
    INJECT_AFTER = "inject_after"


@dataclass(frozen=True, slots=True)
class MetadataDecision:
    """An explicit, auditable result returned by a metadata processor."""

    action: MetadataAction
    packets: tuple[bytes, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.action, MetadataAction):
            raise TypeError("action must be MetadataAction")
        if self.action in {MetadataAction.PASS, MetadataAction.DROP} and self.packets:
            raise ValueError(f"{self.action.value} decision must not contain packets")
        if self.action is MetadataAction.REPLACE and len(self.packets) != 1:
            raise ValueError("replace decision requires exactly one packet")
        if self.action in {MetadataAction.INJECT_BEFORE, MetadataAction.INJECT_AFTER} and not (
            self.packets
        ):
            raise ValueError(f"{self.action.value} decision requires at least one packet")
        if any(not isinstance(packet, bytes) for packet in self.packets):
            raise TypeError("decision packets must be bytes")

    @classmethod
    def pass_through(cls) -> MetadataDecision:
        return cls(MetadataAction.PASS)

    @classmethod
    def drop(cls) -> MetadataDecision:
        return cls(MetadataAction.DROP)

    @classmethod
    def replace(cls, packet: bytes) -> MetadataDecision:
        return cls(MetadataAction.REPLACE, (packet,))

    @classmethod
    def inject_before(cls, *packets: bytes) -> MetadataDecision:
        return cls(MetadataAction.INJECT_BEFORE, packets)

    @classmethod
    def inject_after(cls, *packets: bytes) -> MetadataDecision:
        return cls(MetadataAction.INJECT_AFTER, packets)


MetadataProcessor: TypeAlias = Callable[[TimedKLVPacket], MetadataDecision]


class MetadataProcessorChain:
    """Apply processors sequentially with a strict bound on fan-out."""

    def __init__(
        self,
        processors: Iterable[MetadataProcessor],
        *,
        max_processors: int = 64,
        max_packets_per_input: int = 256,
        max_packet_length: int = 64 * 1024 * 1024,
    ) -> None:
        if max_processors < 1:
            raise ValueError("max_processors must be positive")
        if max_packets_per_input < 1:
            raise ValueError("max_packets_per_input must be positive")
        if max_packet_length < 1:
            raise ValueError("max_packet_length must be positive")
        self.processors = tuple(processors)
        if len(self.processors) > max_processors:
            raise LimitExceeded(
                f"processor count {len(self.processors)} exceeds configured limit "
                f"{max_processors}"
            )
        self.max_packets_per_input = max_packets_per_input
        self.max_packet_length = max_packet_length

    def process(self, event: TimedKLVPacket) -> tuple[TimedKLVPacket, ...]:
        """Transform one input; new packets continue through remaining processors."""

        current: tuple[TimedKLVPacket, ...] = (event,)
        for processor in self.processors:
            next_events: list[TimedKLVPacket] = []
            for current_event in current:
                decision = processor(current_event)
                if not isinstance(decision, MetadataDecision):
                    raise TypeError("metadata processor must return MetadataDecision")
                next_events.extend(self._apply(current_event, decision))
                if len(next_events) > self.max_packets_per_input:
                    raise LimitExceeded(
                        "metadata processor chain exceeds configured packets per input "
                        f"limit {self.max_packets_per_input}"
                    )
            current = tuple(next_events)
        return current

    def _apply(
        self,
        event: TimedKLVPacket,
        decision: MetadataDecision,
    ) -> tuple[TimedKLVPacket, ...]:
        if decision.action is MetadataAction.PASS:
            return (event,)
        if decision.action is MetadataAction.DROP:
            return ()
        replacements = tuple(
            event.with_bytes(packet, max_packet_length=self.max_packet_length)
            for packet in decision.packets
        )
        if decision.action is MetadataAction.REPLACE:
            return replacements
        if decision.action is MetadataAction.INJECT_BEFORE:
            return (*replacements, event)
        return (event, *replacements)
