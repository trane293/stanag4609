"""Constant-rate MPEG-2 TS slot scheduling and PCR restamping."""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction

from stanag4609.errors import DecodeError, LimitExceeded, TruncatedData
from stanag4609.transport.mpegts import (
    TS_PACKET_SIZE,
    ProgramClockReference,
    TransportPacket,
    encode_program_clock_reference,
    parse_transport_packet,
)
from stanag4609.transport.pcr import PCR_CLOCK_RATE, pcr_from_ticks

TS_PACKET_BITS = TS_PACKET_SIZE * 8
NULL_PACKET_PID = 0x1FFF
PCR_BASE_LAST_BIT_OFFSET = Fraction(11 * 8)


def _positive_rate(value: Fraction | int | float) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (Fraction, int, float)):
        raise TypeError("bit_rate must be a Fraction, integer, or float")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("bit_rate must be finite")
        result = Fraction(str(value))
    else:
        result = Fraction(value)
    if result <= 0:
        raise ValueError("bit_rate must be positive")
    return result


def _timeline_time(value: Fraction | int | float, *, name: str) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (Fraction, int, float)):
        raise TypeError(f"{name} must be a Fraction, integer, or float")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
        return Fraction(str(value))
    return Fraction(value)


def encode_null_packet(*, continuity_counter: int = 0) -> bytes:
    """Encode one H.222.0 payload-only null packet."""

    if (
        isinstance(continuity_counter, bool)
        or not isinstance(continuity_counter, int)
        or not 0 <= continuity_counter <= 0x0F
    ):
        raise ValueError("continuity_counter must be an integer from 0 to 15")
    return bytes((0x47, 0x1F, 0xFF, 0x10 | continuity_counter)) + b"\xff" * 184


def rewrite_packet_pcr(raw: bytes, pcr: ProgramClockReference) -> bytes:
    """Replace one packet's PCR while retaining all other bytes exactly."""

    if not isinstance(raw, bytes):
        raise TypeError("raw transport packet must be bytes")
    if not isinstance(pcr, ProgramClockReference):
        raise TypeError("pcr must be a ProgramClockReference")
    packet = parse_transport_packet(raw)
    if packet.pcr is None:
        raise ValueError("transport packet does not contain a PCR field")
    # PCR is the first optional field after the adaptation flags. Replacing its
    # fixed six bytes does not move OPCR or any later adaptation-field syntax.
    rewritten = raw[:6] + encode_program_clock_reference(pcr) + raw[12:]
    parse_transport_packet(rewritten)
    return rewritten


@dataclass(frozen=True, slots=True)
class ScheduledTransportPacket:
    """One transport packet assigned to an exact constant-rate output slot."""

    packet: bytes
    slot_index: int
    starts_at: Fraction
    ends_at: Fraction
    source: bool
    pid: int
    original_pcr: ProgramClockReference | None = None
    output_pcr: ProgramClockReference | None = None
    pcr_sample_at: Fraction | None = None


class TransportRateShaper:
    """Assign TS packets to constant-rate slots and optionally restamp PCR.

    Input may use arbitrary byte chunks. When ``at`` is supplied to ``feed``,
    all empty slots strictly before that source-availability time are filled
    with null packets. Every completed source packet then occupies the next
    slot. The default bound prevents one delayed callback from allocating an
    unbounded null-packet burst.

    Supplying ``clock_anchor`` enables PCR restamping. ``clock_anchor_at`` is
    the time represented by that PCR on the same timeline as ``start_at`` and
    ``at``. Each rewritten value samples the clock at the byte containing the
    last bit of ``PCR_base``, as defined by H.222.0, rather than at the
    beginning of its transport packet.
    """

    __slots__ = (
        "_bit_rate",
        "_buffer",
        "_clock_anchor_at",
        "_clock_anchor_ticks",
        "_clock_origins",
        "_fill_observed_at",
        "_finished",
        "_max_fill_packets",
        "_null_counter",
        "_packet_duration",
        "_pending_discontinuity_pids",
        "_slot_index",
        "_start_at",
    )

    def __init__(
        self,
        *,
        bit_rate: Fraction | int | float,
        start_at: Fraction | int | float = 0,
        clock_anchor: ProgramClockReference | None = None,
        clock_anchor_at: Fraction | int | float | None = None,
        max_fill_packets: int = 100_000,
    ) -> None:
        rate = _positive_rate(bit_rate)
        start = _timeline_time(start_at, name="start_at")
        if clock_anchor is not None and not isinstance(
            clock_anchor, ProgramClockReference
        ):
            raise TypeError("clock_anchor must be a ProgramClockReference or None")
        if clock_anchor is None and clock_anchor_at is not None:
            raise ValueError("clock_anchor_at requires clock_anchor")
        if (
            isinstance(max_fill_packets, bool)
            or not isinstance(max_fill_packets, int)
            or max_fill_packets < 1
        ):
            raise ValueError("max_fill_packets must be a positive integer")
        self._bit_rate = rate
        self._packet_duration = Fraction(TS_PACKET_BITS, 1) / rate
        self._start_at = start
        self._clock_anchor_at = (
            start
            if clock_anchor is not None and clock_anchor_at is None
            else _timeline_time(clock_anchor_at, name="clock_anchor_at")
            if clock_anchor_at is not None
            else None
        )
        self._clock_anchor_ticks = (
            clock_anchor.ticks if clock_anchor is not None else None
        )
        self._clock_origins: dict[int, tuple[int, Fraction]] = {}
        self._max_fill_packets = max_fill_packets
        self._slot_index = 0
        self._null_counter = 0
        self._pending_discontinuity_pids: set[int] = set()
        self._fill_observed_at: Fraction | None = None
        self._buffer = bytearray()
        self._finished = False

    @property
    def bit_rate(self) -> Fraction:
        return self._bit_rate

    @property
    def packet_duration(self) -> Fraction:
        return self._packet_duration

    @property
    def next_slot_at(self) -> Fraction:
        return self._start_at + self._slot_index * self._packet_duration

    @property
    def scheduled_packets(self) -> int:
        return self._slot_index

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(
        self,
        data: bytes | bytearray | memoryview,
        *,
        at: Fraction | int | float | None = None,
    ) -> tuple[ScheduledTransportPacket, ...]:
        """Schedule completed source packets, optionally filling prior idle slots."""

        if self._finished:
            raise RuntimeError("cannot feed a finished transport-rate shaper")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("data must be bytes-like")
        output: list[ScheduledTransportPacket] = []
        if at is not None:
            output.extend(self.fill_until(at=at))
        self._buffer.extend(data)
        while len(self._buffer) >= TS_PACKET_SIZE:
            raw = bytes(self._buffer[:TS_PACKET_SIZE])
            del self._buffer[:TS_PACKET_SIZE]
            source_packet = parse_transport_packet(raw)
            output.append(self._schedule(source_packet, source=True))
        return tuple(output)

    def fill_until(
        self,
        *,
        at: Fraction | int | float,
    ) -> tuple[ScheduledTransportPacket, ...]:
        """Fill every empty output slot strictly before ``at`` with null packets."""

        if self._finished:
            raise RuntimeError("cannot fill a finished transport-rate shaper")
        timestamp = _timeline_time(at, name="timestamp")
        if self._fill_observed_at is not None and timestamp < self._fill_observed_at:
            raise DecodeError("transport-rate shaper timestamps must be monotonic")
        next_slot = self.next_slot_at
        if next_slot >= timestamp:
            self._fill_observed_at = timestamp
            return ()
        ratio = (timestamp - next_slot) / self._packet_duration
        count = (ratio.numerator + ratio.denominator - 1) // ratio.denominator
        if count > self._max_fill_packets:
            raise LimitExceeded(
                f"idle fill requires {count} packets, exceeding configured limit "
                f"{self._max_fill_packets}"
            )
        self._fill_observed_at = timestamp
        output: list[ScheduledTransportPacket] = []
        for _ in range(count):
            raw = encode_null_packet(continuity_counter=self._null_counter)
            self._null_counter = (self._null_counter + 1) & 0x0F
            output.append(self._schedule(parse_transport_packet(raw), source=False))
        return tuple(output)

    def finish(self) -> tuple[ScheduledTransportPacket, ...]:
        """Finish the stream, rejecting a trailing partial transport packet."""

        if self._finished:
            return ()
        self._finished = True
        if self._buffer:
            raise TruncatedData(
                f"transport stream ends with {len(self._buffer)} trailing byte(s)"
            )
        return ()

    def _schedule(
        self,
        source_packet: TransportPacket,
        *,
        source: bool,
    ) -> ScheduledTransportPacket:
        starts_at = self.next_slot_at
        ends_at = starts_at + self._packet_duration
        raw = source_packet.raw
        output_pcr = source_packet.pcr
        sample_at: Fraction | None = None
        if (
            source
            and source_packet.discontinuity_indicator
            and self._clock_anchor_ticks is not None
        ):
            self._pending_discontinuity_pids.add(source_packet.pid)
        if source_packet.pcr is not None and self._clock_anchor_ticks is not None:
            assert self._clock_anchor_at is not None
            sample_at = starts_at + PCR_BASE_LAST_BIT_OFFSET / self._bit_rate
            if source_packet.pid in self._pending_discontinuity_pids:
                self._clock_origins[source_packet.pid] = (
                    source_packet.pcr.ticks,
                    sample_at,
                )
                self._pending_discontinuity_pids.remove(source_packet.pid)
            origin_ticks, origin_at = self._clock_origins.get(
                source_packet.pid,
                (self._clock_anchor_ticks, self._clock_anchor_at),
            )
            elapsed_ticks = (sample_at - origin_at) * PCR_CLOCK_RATE
            ticks = origin_ticks + (
                elapsed_ticks.numerator // elapsed_ticks.denominator
            )
            output_pcr = pcr_from_ticks(ticks)
            raw = rewrite_packet_pcr(raw, output_pcr)
        scheduled = ScheduledTransportPacket(
            raw,
            self._slot_index,
            starts_at,
            ends_at,
            source,
            source_packet.pid,
            source_packet.pcr,
            output_pcr,
            sample_at,
        )
        self._slot_index += 1
        return scheduled
