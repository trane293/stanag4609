"""Bounded synchronization of decoded video frames with timed KLV metadata."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum

from stanag4609.sidecar.model import FrameEnvelope
from stanag4609.transport.processor import TimedKLVPacket
from stanag4609.transport.timing import PTS_CLOCK_RATE, PTS_MODULUS, PTSTimeline

_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class CorrelationMode(Enum):
    """Policy for selecting the metadata relevance time for a video frame."""

    EXACT = "exact"
    LATEST = "latest"
    NEAREST = "nearest"


@dataclass(frozen=True, slots=True)
class _ObservedPacket:
    effective_pts: int
    sequence: int
    packet: TimedKLVPacket
    timestamp_microseconds: int | None


def _timestamp_microseconds(packet: TimedKLVPacket) -> int | None:
    value_method = getattr(packet.decoded, "value", None)
    if not callable(value_method):
        return None
    value = value_method(2)
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    utc_value = value.astimezone(timezone.utc)
    delta = utc_value - _UNIX_EPOCH
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _ticks_to_microseconds(ticks: int) -> int:
    numerator = ticks * 1_000_000
    if numerator < 0:
        return -((-numerator + PTS_CLOCK_RATE // 2) // PTS_CLOCK_RATE)
    return (numerator + PTS_CLOCK_RATE // 2) // PTS_CLOCK_RATE


class FrameMetadataCorrelator:
    """Attach synchronous KLV to decoded frames using ST 1402 PTS semantics.

    ``LATEST`` implements the standard's "becomes relevant" wording: the most
    recent metadata time at or before the frame is selected until it exceeds
    ``maximum_delta_ticks``. ``EXACT`` requires equal effective PTS values;
    ``NEAREST`` can account for known sampling offsets and prefers the earlier
    metadata time when distances tie.

    Asynchronous KLV is counted but never attached automatically because ST
    1402 explicitly does not guarantee its display synchronization.
    """

    def __init__(
        self,
        *,
        mode: CorrelationMode = CorrelationMode.LATEST,
        maximum_delta_ticks: int = 63_000,
        metadata_pts_offset_ticks: int = 0,
        max_packets: int = 1024,
    ) -> None:
        if not isinstance(mode, CorrelationMode):
            raise TypeError("mode must be CorrelationMode")
        if (
            isinstance(maximum_delta_ticks, bool)
            or not isinstance(maximum_delta_ticks, int)
            or not 0 <= maximum_delta_ticks < PTS_MODULUS // 2
        ):
            raise ValueError(
                "maximum_delta_ticks must be an integer from 0 to less than half a PTS epoch"
            )
        if (
            isinstance(metadata_pts_offset_ticks, bool)
            or not isinstance(metadata_pts_offset_ticks, int)
            or not -(PTS_MODULUS // 2) < metadata_pts_offset_ticks < PTS_MODULUS // 2
        ):
            raise ValueError("metadata PTS offset must be within half a PTS epoch")
        if (
            isinstance(max_packets, bool)
            or not isinstance(max_packets, int)
            or max_packets < 1
        ):
            raise ValueError("max_packets must be a positive integer")
        self.mode = mode
        self.maximum_delta_ticks = maximum_delta_ticks
        self.metadata_pts_offset_ticks = metadata_pts_offset_ticks
        self.max_packets = max_packets
        self._timelines: dict[int, PTSTimeline] = {}
        self._packets: list[_ObservedPacket] = []
        self._sequence = 0
        self._dropped_packets = 0
        self._uncorrelated_async_packets = 0

    @property
    def buffered_packets(self) -> int:
        return len(self._packets)

    @property
    def dropped_packets(self) -> int:
        return self._dropped_packets

    @property
    def uncorrelated_async_packets(self) -> int:
        return self._uncorrelated_async_packets

    def reference_for(self, program_number: int) -> int | None:
        """Return the PTS watermark for one program, if it has been observed."""
        timeline = self._timelines.get(program_number)
        return None if timeline is None else timeline.reference

    def observe(self, packet: TimedKLVPacket) -> bool:
        """Store synchronous metadata; return false for intentionally skipped async KLV."""
        if not isinstance(packet, TimedKLVPacket):
            raise TypeError("packet must be TimedKLVPacket")
        if packet.pts is None:
            self._uncorrelated_async_packets += 1
            return False
        timeline = self._timelines.setdefault(packet.program_number, PTSTimeline())
        effective_pts = timeline.observe(packet.pts) + self.metadata_pts_offset_ticks
        self._packets.append(
            _ObservedPacket(
                effective_pts,
                self._sequence,
                packet,
                _timestamp_microseconds(packet),
            )
        )
        self._sequence += 1
        self._packets.sort(key=lambda item: (item.effective_pts, item.sequence))
        overflow = len(self._packets) - self.max_packets
        if overflow > 0:
            del self._packets[:overflow]
            self._dropped_packets += overflow
        return True

    def correlate(self, frame: FrameEnvelope) -> FrameEnvelope:
        """Return a new frame carrying the metadata selected by this policy."""
        if not isinstance(frame, FrameEnvelope):
            raise TypeError("frame must be FrameEnvelope")
        timeline = self._timelines.setdefault(frame.program_number, PTSTimeline())
        frame_pts = timeline.observe(frame.pts)
        candidates = tuple(
            item
            for item in self._packets
            if item.packet.program_number == frame.program_number
        )
        selected_pts = self._select_pts(candidates, frame_pts)
        if selected_pts is None:
            return frame
        selected = tuple(item for item in candidates if item.effective_pts == selected_pts)
        metadata = (*frame.metadata, *(item.packet for item in selected))
        timestamp = frame.timestamp_microseconds
        if timestamp is None:
            anchor = next(
                (
                    item.timestamp_microseconds
                    for item in selected
                    if item.timestamp_microseconds is not None
                ),
                None,
            )
            if anchor is not None:
                timestamp = anchor + _ticks_to_microseconds(frame_pts - selected_pts)
        return replace(frame, timestamp_microseconds=timestamp, metadata=metadata)

    def correlate_after_observing(
        self,
        frame: FrameEnvelope,
        packet: TimedKLVPacket,
    ) -> FrameEnvelope:
        """Convenience operation for the common one-packet/one-frame path."""
        self.observe(packet)
        return self.correlate(frame)

    def clear(self) -> None:
        """Discard buffered packets and reset PTS epoch state while retaining counters."""
        self._packets.clear()
        self._timelines.clear()

    def _select_pts(
        self,
        candidates: tuple[_ObservedPacket, ...],
        frame_pts: int,
    ) -> int | None:
        if not candidates:
            return None
        available = {item.effective_pts for item in candidates}
        if self.mode is CorrelationMode.EXACT:
            return frame_pts if frame_pts in available else None
        if self.mode is CorrelationMode.LATEST:
            earlier = tuple(value for value in available if value <= frame_pts)
            if not earlier:
                return None
            selected = max(earlier)
            return selected if frame_pts - selected <= self.maximum_delta_ticks else None
        selected = min(available, key=lambda value: (abs(value - frame_pts), value > frame_pts))
        return selected if abs(selected - frame_pts) <= self.maximum_delta_ticks else None
