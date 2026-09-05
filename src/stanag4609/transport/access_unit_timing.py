"""Bounded H.222.0 access-unit timestamp diagnostics."""

from __future__ import annotations

from dataclasses import dataclass

from stanag4609.transport.demux import PESStreamEvent, StreamKind


@dataclass(frozen=True, slots=True)
class VideoAccessUnitPTSIssue:
    """A video PTS/access-unit alignment violation."""

    code: str
    requirement: str
    program_number: int
    pid: int
    stream_type: int
    source_offset: int
    message: str


@dataclass(frozen=True, slots=True)
class _ByteOrigin:
    source_offset: int
    has_pts: bool


@dataclass(frozen=True, slots=True)
class _PendingPTS:
    source_offset: int
    remaining_lookahead: int


@dataclass(slots=True)
class _VideoState:
    stream_type: int
    tail: bytes = b""
    origins: tuple[_ByteOrigin, ...] = ()
    found_first_access_unit: bool = False
    pending_pts: tuple[_PendingPTS, ...] = ()


class VideoAccessUnitPTSValidator:
    """Verify PTS/access-unit alignment for common video streams.

    H.222.0 §2.7.5 requires a PTS for the first access unit of every elementary
    stream. This bounded receiver recognizes the access-unit markers mandated
    for MPEG-1/2 Video, AVC, and HEVC carried in MPEG-2 transport streams. It
    also says a video PTS may only occur when the PES contains the first byte
    of a picture/access unit. This receiver retains at most three payload bytes
    and three unresolved non-empty PTS-bearing PES records per stream, so
    start-code prefixes split across PES boundaries are attributed correctly.

    The validator deliberately does not interpret the separate conditional PTS
    rules for later AVC access units. Those rules have in-band and descriptor
    exceptions that require a fuller AVC timing model.
    """

    __slots__ = ("_states",)

    _SUPPORTED_STREAM_TYPES = frozenset({0x01, 0x02, 0x1B, 0x24})

    def __init__(self) -> None:
        self._states: dict[tuple[int, int], _VideoState] = {}

    @property
    def streams(self) -> tuple[tuple[int, int], ...]:
        """Return program/PID keys for video streams with retained state."""

        return tuple(sorted(self._states))

    @property
    def completed_streams(self) -> tuple[tuple[int, int], ...]:
        """Return streams whose first access unit has been detected."""

        return tuple(
            sorted(key for key, state in self._states.items() if state.found_first_access_unit)
        )

    def observe(self, event: PESStreamEvent) -> tuple[VideoAccessUnitPTSIssue, ...]:
        """Observe one PES packet and return newly provable alignment issues."""

        if not isinstance(event, PESStreamEvent):
            raise TypeError("event must be a PESStreamEvent")
        if (
            event.kind is not StreamKind.VIDEO
            or event.stream.stream_type not in self._SUPPORTED_STREAM_TYPES
        ):
            return ()

        key = (event.program_number, event.pid)
        discontinuity = any(
            packet.discontinuity_indicator for packet in event.pes.transport_packets
        )
        state = self._states.get(key)
        if state is None or state.stream_type != event.stream.stream_type or discontinuity:
            state = _VideoState(event.stream.stream_type)
            self._states[key] = state

        current_origin = _ByteOrigin(event.pes.offset, event.pes.pts is not None)
        combined = state.tail + event.pes.payload
        markers = _find_access_unit_markers(combined, state.stream_type)
        marker_origins = tuple(
            state.origins[marker] if marker < len(state.origins) else current_origin
            for marker in markers
        )
        marker_offsets = {origin.source_offset for origin in marker_origins}
        issues: list[VideoAccessUnitPTSIssue] = []

        pending: list[_PendingPTS] = []
        for candidate in state.pending_pts:
            if candidate.source_offset in marker_offsets:
                continue
            remaining = candidate.remaining_lookahead - len(event.pes.payload)
            if remaining <= 0:
                issues.append(
                    _issue(event, "pts_without_access_unit", candidate.source_offset)
                )
            else:
                pending.append(_PendingPTS(candidate.source_offset, remaining))

        if marker_origins and not state.found_first_access_unit:
            origin = marker_origins[0]
            state.found_first_access_unit = True
            if not origin.has_pts:
                issues.append(
                    VideoAccessUnitPTSIssue(
                        "first_access_unit",
                        "ITU-T H.222.0 (10/2014) §2.7.5",
                        event.program_number,
                        event.pid,
                        event.stream.stream_type,
                        origin.source_offset,
                        (
                            f"program {event.program_number} PID {event.pid} first video "
                            "access unit begins in a PES packet without PTS"
                        ),
                    )
                )

        if event.pes.pts is not None and event.pes.offset not in marker_offsets:
            if event.pes.payload:
                pending.append(_PendingPTS(event.pes.offset, 3))
            else:
                issues.append(_issue(event, "pts_without_access_unit", event.pes.offset))
        state.pending_pts = tuple(pending)

        retained = min(3, len(combined))
        if retained:
            state.tail = combined[-retained:]
            prior_count = max(0, retained - len(event.pes.payload))
            state.origins = (
                state.origins[-prior_count:] if prior_count else ()
            ) + (current_origin,) * (retained - prior_count)
        else:
            state.tail = b""
            state.origins = ()
        return tuple(issues)

    def finish(self) -> tuple[VideoAccessUnitPTSIssue, ...]:
        """Finalize the finite stream and reject unresolved PTS-bearing PES packets."""

        issues: list[VideoAccessUnitPTSIssue] = []
        for (program_number, pid), state in sorted(self._states.items()):
            for pending in state.pending_pts:
                issues.append(
                    VideoAccessUnitPTSIssue(
                        "pts_without_access_unit",
                        "ITU-T H.222.0 (10/2014) §2.7.5",
                        program_number,
                        pid,
                        state.stream_type,
                        pending.source_offset,
                        (
                            f"program {program_number} PID {pid} PES packet carries PTS "
                            "but does not contain the start of a video access unit"
                        ),
                    )
                )
            state.pending_pts = ()
        return tuple(issues)

    def reset(
        self,
        *,
        program_number: int | None = None,
        pid: int | None = None,
    ) -> None:
        """Forget all state, one program, or one program/PID stream."""

        if program_number is None:
            if pid is not None:
                raise ValueError("pid requires program_number")
            self._states.clear()
            return
        if (
            isinstance(program_number, bool)
            or not isinstance(program_number, int)
            or not 1 <= program_number <= 0xFFFF
        ):
            raise ValueError("program_number must be an integer from 1 to 65535")
        if pid is None:
            for key in tuple(self._states):
                if key[0] == program_number:
                    del self._states[key]
            return
        if isinstance(pid, bool) or not isinstance(pid, int) or not 0 <= pid <= 0x1FFF:
            raise ValueError("pid must be an integer from 0 to 8191")
        self._states.pop((program_number, pid), None)


def _find_access_unit_markers(data: bytes, stream_type: int) -> tuple[int, ...]:
    markers: list[int] = []
    cursor = 0
    while True:
        marker = data.find(b"\x00\x00\x01", cursor)
        if marker < 0 or marker + 3 >= len(data):
            return tuple(markers)
        header = data[marker + 3]
        if _is_access_unit_header(stream_type, header):
            markers.append(marker)
        cursor = marker + 3


def _is_access_unit_header(stream_type: int, header: int) -> bool:
    if stream_type in {0x01, 0x02}:
        return header == 0x00
    if stream_type == 0x1B:
        return header & 0x1F == 9
    return stream_type == 0x24 and (header >> 1) & 0x3F == 35


def _issue(
    event: PESStreamEvent,
    code: str,
    source_offset: int,
) -> VideoAccessUnitPTSIssue:
    return VideoAccessUnitPTSIssue(
        code,
        "ITU-T H.222.0 (10/2014) §2.7.5",
        event.program_number,
        event.pid,
        event.stream.stream_type,
        source_offset,
        (
            f"program {event.program_number} PID {event.pid} PES packet carries PTS "
            "but does not contain the start of a video access unit"
        ),
    )


# Compatibility names retained for the first public development series.
FirstAccessUnitPTSIssue = VideoAccessUnitPTSIssue
FirstVideoAccessUnitPTSValidator = VideoAccessUnitPTSValidator
