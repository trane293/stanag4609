"""Deterministic MPEG-2 TS/PES construction primitives."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from fractions import Fraction
from types import MappingProxyType

from stanag4609.errors import DecodeError
from stanag4609.klv.stream import KLVStreamParser
from stanag4609.transport.metadata import (
    CellFragmentation,
    encode_metadata_au_cell,
    klva_metadata_service_ids,
)
from stanag4609.transport.mpegts import (
    ProgramClockReference,
    TransportPacket,
    encode_program_clock_reference,
)
from stanag4609.transport.psi import (
    ST1402_MAX_PSI_INTERVAL,
    ST1402_RECOMMENDED_PSI_INTERVAL,
    Descriptor,
    ElementaryStreamInfo,
    KLVCarriage,
    ProgramAssociation,
    ProgramMapTable,
    find_klv_streams,
    mpeg2_crc32,
    parse_pmt,
)

_SPECIAL_STREAM_IDS = {0xBC, 0xBE, 0xBF, 0xF0, 0xF1, 0xF2, 0xF8, 0xFF}
MAX_ASYNC_PES_PAYLOAD = 0xFFFF - 3


def _validate_uint(value: int, maximum: int, *, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be an integer from 0 to {maximum}")


def encode_pts(value: int, *, prefix: int) -> bytes:
    """Encode a 33-bit MPEG PTS/DTS value with its four-bit field prefix."""

    _validate_uint(value, 2**33 - 1, name="PTS/DTS")
    if prefix not in {0x1, 0x2, 0x3}:
        raise ValueError("PTS/DTS prefix must be 1, 2, or 3")
    return bytes(
        (
            (prefix << 4) | (((value >> 30) & 0x07) << 1) | 1,
            (value >> 22) & 0xFF,
            (((value >> 15) & 0x7F) << 1) | 1,
            (value >> 7) & 0xFF,
            ((value & 0x7F) << 1) | 1,
        )
    )


def encode_pes_packet(
    payload: bytes,
    *,
    stream_id: int,
    pts: int | None = None,
    dts: int | None = None,
    data_alignment_indicator: bool = True,
    unbounded: bool = False,
) -> bytes:
    """Build one MPEG-2 PES packet for video, audio, or metadata payload."""

    _validate_uint(stream_id, 0xFF, name="stream_id")
    if stream_id in _SPECIAL_STREAM_IDS:
        raise ValueError("stream_id uses special PES syntax not supported by this builder")
    if dts is not None and pts is None:
        raise ValueError("DTS requires PTS")
    if pts is not None:
        _validate_uint(pts, 2**33 - 1, name="PTS")
    if dts is not None:
        _validate_uint(dts, 2**33 - 1, name="DTS")
    if unbounded and not 0xE0 <= stream_id <= 0xEF:
        raise ValueError("unbounded PES_packet_length is only supported for video stream IDs")

    if dts is not None:
        assert pts is not None
        timing = encode_pts(pts, prefix=0x3) + encode_pts(dts, prefix=0x1)
        pts_dts_flags = 0xC0
    elif pts is not None:
        timing = encode_pts(pts, prefix=0x2)
        pts_dts_flags = 0x80
    else:
        timing = b""
        pts_dts_flags = 0
    flags = 0x80 | (0x04 if data_alignment_indicator else 0)
    optional_header = bytes((flags, pts_dts_flags, len(timing))) + timing
    packet_length = len(optional_header) + len(payload)
    if not unbounded and packet_length > 0xFFFF:
        raise ValueError("PES packet content exceeds the 65535-byte PES_packet_length")
    encoded_length = 0 if unbounded else packet_length
    return (
        b"\x00\x00\x01"
        + bytes((stream_id,))
        + encoded_length.to_bytes(2, "big")
        + optional_header
        + payload
    )


def _packetize_payload(
    payload: bytes,
    *,
    pid: int,
    continuity_counter: int,
) -> tuple[tuple[bytes, ...], int]:
    _validate_uint(pid, 0x1FFF, name="PID")
    _validate_uint(continuity_counter, 0x0F, name="continuity_counter")
    if not payload:
        raise ValueError("TS payload must not be empty")

    packets: list[bytes] = []
    cursor = 0
    counter = continuity_counter
    first = True
    while cursor < len(payload):
        chunk = payload[cursor : cursor + 184]
        cursor += len(chunk)
        byte1 = (pid >> 8) | (0x40 if first else 0)
        byte2 = pid & 0xFF
        if len(chunk) == 184:
            packet = bytes((0x47, byte1, byte2, 0x10 | counter)) + chunk
        else:
            adaptation_length = 183 - len(chunk)
            adaptation = (
                b""
                if adaptation_length == 0
                else b"\x00" + b"\xff" * (adaptation_length - 1)
            )
            packet = (
                bytes((0x47, byte1, byte2, 0x30 | counter, adaptation_length))
                + adaptation
                + chunk
            )
        packets.append(packet)
        counter = (counter + 1) & 0x0F
        first = False
    return tuple(packets), counter


def packetize_pes(
    pes: bytes,
    *,
    pid: int,
    continuity_counter: int = 0,
) -> tuple[tuple[bytes, ...], int]:
    """Split a complete PES packet into 188-byte TS packets."""

    if len(pes) < 6 or pes[:3] != b"\x00\x00\x01":
        raise DecodeError("PES packetizer requires a complete PES start header")
    return _packetize_payload(pes, pid=pid, continuity_counter=continuity_counter)


def encode_pcr_packet(
    *,
    pid: int,
    pcr: ProgramClockReference,
    continuity_counter: int = 0,
    discontinuity: bool = False,
) -> bytes:
    """Build one adaptation-only TS packet carrying a PCR.

    Adaptation-only packets do not advance elementary-stream payload
    continuity. Callers should therefore supply the counter value of the last
    payload packet, or the first payload counter before payload has started.
    """

    _validate_uint(pid, 0x1FFF, name="PID")
    _validate_uint(continuity_counter, 0x0F, name="continuity_counter")
    if not isinstance(pcr, ProgramClockReference):
        raise TypeError("pcr must be a ProgramClockReference")
    if not isinstance(discontinuity, bool):
        raise TypeError("discontinuity must be a boolean")
    flags = 0x10 | (0x80 if discontinuity else 0)
    adaptation = (
        bytes((flags,)) + encode_program_clock_reference(pcr) + b"\xff" * 176
    )
    return (
        bytes(
            (
                0x47,
                (pid >> 8) & 0x1F,
                pid & 0xFF,
                0x20 | continuity_counter,
                len(adaptation),
            )
        )
        + adaptation
    )


def packetize_pes_with_layout(
    pes: bytes,
    *,
    pid: int,
    layout: Iterable[TransportPacket],
    continuity_counter: int = 0,
) -> tuple[tuple[bytes, ...], int]:
    """Repacketize PES using exact source payload and adaptation boundaries.

    This is intended for unchanged elementary streams during a metadata-only
    transform. PCR, OPCR, random-access flags, private adaptation data,
    extensions, and stuffing are retained while continuity counters are rebuilt.
    """

    if len(pes) < 6 or pes[:3] != b"\x00\x00\x01":
        raise DecodeError("PES packetizer requires a complete PES start header")
    _validate_uint(pid, 0x1FFF, name="PID")
    _validate_uint(continuity_counter, 0x0F, name="continuity_counter")
    source_packets = tuple(layout)
    if not source_packets:
        raise ValueError("PES source layout must not be empty")
    if any(packet.pid != pid for packet in source_packets):
        raise ValueError("PES source layout PID does not match output PID")
    if not source_packets[0].payload_unit_start or any(
        packet.payload_unit_start for packet in source_packets[1:]
    ):
        raise ValueError("PES source layout must contain exactly one start packet first")
    if any(packet.transport_error_indicator for packet in source_packets):
        raise ValueError("PES source layout contains a transport error")
    if any(packet.scrambling_control for packet in source_packets):
        raise ValueError("scrambled PES source layouts are not supported")
    if sum(len(packet.payload) for packet in source_packets) != len(pes):
        raise ValueError("PES source layout payload lengths do not match PES length")

    packets: list[bytes] = []
    cursor = 0
    counter = continuity_counter
    had_payload = False
    for source in source_packets:
        if source.has_payload:
            chunk = pes[cursor : cursor + len(source.payload)]
            cursor += len(chunk)
            packet_counter = counter
            counter = (counter + 1) & 0x0F
            had_payload = True
        else:
            chunk = b""
            packet_counter = (counter - 1) & 0x0F if had_payload else counter

        byte1 = (pid >> 8) | (0x40 if source.payload_unit_start else 0)
        if source.transport_priority:
            byte1 |= 0x20
        byte3 = (source.adaptation_field_control << 4) | packet_counter
        header = bytes((0x47, byte1, pid & 0xFF, byte3))
        if source.has_adaptation_field:
            body = bytes((len(source.adaptation_field),)) + source.adaptation_field + chunk
        else:
            body = chunk
        packet = header + body
        if len(packet) != 188:
            raise ValueError("PES source layout does not describe complete TS packets")
        packets.append(packet)
    return tuple(packets), counter


def _descriptor_bytes(descriptors: Iterable[Descriptor]) -> bytes:
    encoded: list[bytes] = []
    for descriptor in descriptors:
        if len(descriptor.data) > 0xFF:
            raise ValueError("descriptor data exceeds 255 bytes")
        encoded.append(descriptor.raw)
    return b"".join(encoded)


def _section_header(table_id: int, section_length: int) -> bytes:
    if section_length > 1021:
        raise ValueError("PSI section_length exceeds 1021 bytes")
    return bytes(
        (table_id, 0xB0 | ((section_length >> 8) & 0x0F), section_length & 0xFF)
    )


def build_pat_section(
    *,
    transport_stream_id: int,
    programs: Iterable[ProgramAssociation],
    version_number: int = 0,
    section_number: int = 0,
    last_section_number: int = 0,
) -> bytes:
    """Build one current Program Association Table section."""

    _validate_uint(transport_stream_id, 0xFFFF, name="transport_stream_id")
    _validate_uint(version_number, 0x1F, name="version_number")
    _validate_uint(section_number, 0xFF, name="section_number")
    _validate_uint(last_section_number, 0xFF, name="last_section_number")
    if section_number > last_section_number:
        raise ValueError("section_number must not exceed last_section_number")
    entries: list[bytes] = []
    seen_programs: set[int] = set()
    for program in programs:
        if not 1 <= program.program_number <= 0xFFFF:
            raise ValueError("program_number must be from 1 to 65535")
        _validate_uint(program.program_map_pid, 0x1FFF, name="program_map_pid")
        if program.program_number in seen_programs:
            raise ValueError(f"program_number {program.program_number} occurs twice")
        seen_programs.add(program.program_number)
        entries.append(
            program.program_number.to_bytes(2, "big")
            + bytes(
                (
                    0xE0 | (program.program_map_pid >> 8),
                    program.program_map_pid & 0xFF,
                )
            )
        )
    body = (
        transport_stream_id.to_bytes(2, "big")
        + bytes(
            (
                0xC1 | (version_number << 1),
                section_number,
                last_section_number,
            )
        )
        + b"".join(entries)
    )
    without_crc = _section_header(0x00, len(body) + 4) + body
    return without_crc + mpeg2_crc32(without_crc).to_bytes(4, "big")


def build_pmt_section(
    *,
    program_number: int,
    pcr_pid: int,
    streams: Iterable[ElementaryStreamInfo],
    descriptors: Iterable[Descriptor] = (),
    version_number: int = 0,
) -> bytes:
    """Build a current single-section Program Map Table."""

    if not 1 <= program_number <= 0xFFFF:
        raise ValueError("program_number must be from 1 to 65535")
    _validate_uint(pcr_pid, 0x1FFF, name="pcr_pid")
    _validate_uint(version_number, 0x1F, name="version_number")
    program_descriptors = _descriptor_bytes(descriptors)
    if len(program_descriptors) > 0x0FFF:
        raise ValueError("program descriptor loop exceeds 4095 bytes")

    stream_entries: list[bytes] = []
    seen_pids: set[int] = set()
    for stream in streams:
        _validate_uint(stream.stream_type, 0xFF, name="stream_type")
        _validate_uint(stream.elementary_pid, 0x1FFF, name="elementary_pid")
        if stream.elementary_pid in seen_pids:
            raise ValueError(f"elementary_pid {stream.elementary_pid} occurs twice")
        seen_pids.add(stream.elementary_pid)
        stream_descriptors = _descriptor_bytes(stream.descriptors)
        if len(stream_descriptors) > 0x0FFF:
            raise ValueError("elementary descriptor loop exceeds 4095 bytes")
        stream_entries.append(
            bytes(
                (
                    stream.stream_type,
                    0xE0 | (stream.elementary_pid >> 8),
                    stream.elementary_pid & 0xFF,
                    0xF0 | (len(stream_descriptors) >> 8),
                    len(stream_descriptors) & 0xFF,
                )
            )
            + stream_descriptors
        )

    body = (
        program_number.to_bytes(2, "big")
        + bytes((0xC1 | (version_number << 1), 0, 0))
        + bytes((0xE0 | (pcr_pid >> 8), pcr_pid & 0xFF))
        + bytes(
            (
                0xF0 | (len(program_descriptors) >> 8),
                len(program_descriptors) & 0xFF,
            )
        )
        + program_descriptors
        + b"".join(stream_entries)
    )
    without_crc = _section_header(0x02, len(body) + 4) + body
    return without_crc + mpeg2_crc32(without_crc).to_bytes(4, "big")


class TransportMuxer:
    """Stateful single-program TS packetizer with deterministic continuity."""

    def __init__(
        self,
        *,
        transport_stream_id: int,
        program_number: int,
        program_map_pid: int,
        pcr_pid: int,
        streams: Iterable[ElementaryStreamInfo],
        descriptors: Iterable[Descriptor] = (),
        version_number: int = 0,
        pat_version_number: int | None = None,
        pmt_version_number: int | None = None,
    ) -> None:
        _validate_uint(version_number, 0x1F, name="version_number")
        if pat_version_number is None:
            pat_version_number = version_number
        if pmt_version_number is None:
            pmt_version_number = version_number
        stream_tuple = tuple(streams)
        descriptor_tuple = tuple(descriptors)
        stream_pids = {stream.elementary_pid for stream in stream_tuple}
        if len(stream_pids) != len(stream_tuple):
            raise ValueError("elementary stream PIDs must be unique")
        if pcr_pid not in stream_pids:
            raise ValueError("pcr_pid must identify a declared elementary stream")
        if program_map_pid in stream_pids or program_map_pid == 0:
            raise ValueError("program_map_pid must not collide with PAT or elementary PIDs")

        self.transport_stream_id = transport_stream_id
        self.program_number = program_number
        self.program_map_pid = program_map_pid
        self.pcr_pid = pcr_pid
        self.streams = stream_tuple
        self.descriptors = descriptor_tuple
        self.version_number = pmt_version_number
        self.pat_version_number = pat_version_number
        self.pmt_version_number = pmt_version_number
        self._stream_by_pid = {stream.elementary_pid: stream for stream in stream_tuple}
        self._continuity = {0: 0, program_map_pid: 0}
        self._continuity.update({pid: 0 for pid in stream_pids})
        self._payload_started: set[int] = set()

        self._pat = build_pat_section(
            transport_stream_id=transport_stream_id,
            programs=(ProgramAssociation(program_number, program_map_pid),),
            version_number=pat_version_number,
        )
        self._pmt = build_pmt_section(
            program_number=program_number,
            pcr_pid=pcr_pid,
            streams=stream_tuple,
            descriptors=descriptor_tuple,
            version_number=pmt_version_number,
        )
        self._klv_by_pid = dict(find_klv_streams(parse_pmt(self._pmt)))
        self._metadata_sequences = {pid: 0 for pid in self._klv_by_pid}

    @property
    def continuity_counters(self) -> MappingProxyType[int, int]:
        return MappingProxyType(self._continuity)

    @property
    def program_map(self) -> ProgramMapTable:
        """Return the exact PMT represented by this muxer."""
        return parse_pmt(self._pmt)

    def reconfigure(
        self,
        *,
        pcr_pid: int,
        streams: Iterable[ElementaryStreamInfo],
        descriptors: Iterable[Descriptor] = (),
        program_map_pid: int | None = None,
        pat_version_number: int | None = None,
        pmt_version_number: int | None = None,
    ) -> None:
        """Replace the active program map without resetting retained PID state.

        PAT/PMT and elementary-stream continuity counters survive when their
        PIDs remain active. A changed PMT PID begins with continuity counter
        zero. Synchronous metadata sequence numbers survive only for retained
        PID/carriage pairs; newly introduced PIDs and PIDs whose KLVA carriage
        changes start at zero. Callers must finish or reject partial
        PES/access-unit state before changing the topology.
        """

        if program_map_pid is None:
            program_map_pid = self.program_map_pid

        replacement = TransportMuxer(
            transport_stream_id=self.transport_stream_id,
            program_number=self.program_number,
            program_map_pid=program_map_pid,
            pcr_pid=pcr_pid,
            streams=streams,
            descriptors=descriptors,
            pat_version_number=(
                self.pat_version_number
                if pat_version_number is None
                else pat_version_number
            ),
            pmt_version_number=(
                self.pmt_version_number
                if pmt_version_number is None
                else pmt_version_number
            ),
        )
        replacement._continuity = {
            pid: self._continuity.get(pid, 0) for pid in replacement._continuity
        }
        replacement._payload_started = self._payload_started & set(
            replacement._stream_by_pid
        )
        replacement._metadata_sequences = {
            pid: (
                self._metadata_sequences.get(pid, 0)
                if self._klv_by_pid.get(pid) is replacement._klv_by_pid[pid]
                else 0
            )
            for pid in replacement._metadata_sequences
        }
        self.__dict__.update(replacement.__dict__)

    def program_tables(self) -> tuple[bytes, ...]:
        """Packetize one PAT and PMT repetition and advance their counters."""

        pat_packets, self._continuity[0] = _packetize_payload(
            b"\x00" + self._pat,
            pid=0,
            continuity_counter=self._continuity[0],
        )
        pmt_packets, self._continuity[self.program_map_pid] = _packetize_payload(
            b"\x00" + self._pmt,
            pid=self.program_map_pid,
            continuity_counter=self._continuity[self.program_map_pid],
        )
        return pat_packets + pmt_packets

    def mux_pes(
        self,
        pid: int,
        pes: bytes,
        *,
        layout: Iterable[TransportPacket] | None = None,
    ) -> tuple[bytes, ...]:
        """Packetize one PES for a declared elementary PID."""

        if pid not in self._stream_by_pid:
            raise DecodeError(f"PID {pid} is not declared in this muxer's PMT")
        if layout is None:
            packets, next_counter = packetize_pes(
                pes,
                pid=pid,
                continuity_counter=self._continuity[pid],
            )
        else:
            packets, next_counter = packetize_pes_with_layout(
                pes,
                pid=pid,
                layout=layout,
                continuity_counter=self._continuity[pid],
            )
        self._continuity[pid] = next_counter
        self._payload_started.add(pid)
        return packets

    def mux_pcr(
        self,
        pcr: ProgramClockReference,
        *,
        discontinuity: bool = False,
    ) -> bytes:
        """Insert an adaptation-only PCR packet on the PMT-declared clock PID."""

        next_counter = self._continuity[self.pcr_pid]
        counter = (
            (next_counter - 1) & 0x0F
            if self.pcr_pid in self._payload_started
            else next_counter
        )
        return encode_pcr_packet(
            pid=self.pcr_pid,
            pcr=pcr,
            continuity_counter=counter,
            discontinuity=discontinuity,
        )

    def mux_async_klv(
        self,
        pid: int,
        klv: bytes,
        *,
        max_pes_payload: int = MAX_ASYNC_PES_PAYLOAD,
    ) -> tuple[bytes, ...]:
        """Packetize one complete KLV item using ST 1402 asynchronous carriage.

        Large items span multiple PES packets. Only the first PES asserts data
        alignment because only its first payload byte begins the KLV item.
        """

        if self._klv_by_pid.get(pid) is not KLVCarriage.ASYNCHRONOUS:
            raise DecodeError(f"PID {pid} is not declared as asynchronous KLVA metadata")
        if (
            isinstance(max_pes_payload, bool)
            or not isinstance(max_pes_payload, int)
            or not 1 <= max_pes_payload <= MAX_ASYNC_PES_PAYLOAD
        ):
            raise ValueError(
                f"max_pes_payload must be an integer from 1 to {MAX_ASYNC_PES_PAYLOAD}"
            )
        parser = KLVStreamParser(max_value_length=max(len(klv), 1))
        items = parser.feed(klv)
        items.extend(parser.finish())
        if len(items) != 1 or bytes(items[0]) != klv:
            raise ValueError("mux_async_klv requires exactly one complete KLV item")

        packets: list[bytes] = []
        for offset in range(0, len(klv), max_pes_payload):
            pes = encode_pes_packet(
                klv[offset : offset + max_pes_payload],
                stream_id=0xBD,
                data_alignment_indicator=offset == 0,
            )
            packets.extend(self.mux_pes(pid, pes))
        return tuple(packets)

    def mux_sync_klv(
        self,
        pid: int,
        klv: bytes,
        *,
        pts: int,
        metadata_service_id: int = 0,
        random_access: bool = True,
    ) -> tuple[bytes, ...]:
        """Create frame-timed synchronous KLVA PES packets, fragmenting if needed."""

        if self._klv_by_pid.get(pid) is not KLVCarriage.SYNCHRONOUS:
            raise DecodeError(f"PID {pid} is not declared as synchronous KLVA metadata")
        _validate_uint(metadata_service_id, 0xFF, name="metadata_service_id")
        declared_services = klva_metadata_service_ids(self._stream_by_pid[pid])
        if metadata_service_id not in declared_services:
            raise DecodeError(
                f"ST 1402-15 requires metadata service {metadata_service_id} on PID "
                f"{pid} to have a matching PMT metadata_descriptor"
            )
        if not klv:
            raise ValueError("KLV metadata access unit must not be empty")

        max_fragment = 0xFFFF - 8 - 5
        fragments = [
            klv[index : index + max_fragment]
            for index in range(0, len(klv), max_fragment)
        ]
        packets: list[bytes] = []
        for index, fragment in enumerate(fragments):
            if len(fragments) == 1:
                fragmentation = CellFragmentation.COMPLETE
            elif index == 0:
                fragmentation = CellFragmentation.FIRST
            elif index == len(fragments) - 1:
                fragmentation = CellFragmentation.LAST
            else:
                fragmentation = CellFragmentation.MIDDLE
            sequence = self._metadata_sequences[pid]
            cell = encode_metadata_au_cell(
                fragment,
                metadata_service_id=metadata_service_id,
                sequence_number=sequence,
                fragmentation=fragmentation,
                random_access=random_access and index == 0,
            )
            self._metadata_sequences[pid] = (sequence + 1) & 0xFF
            pes = encode_pes_packet(
                cell,
                stream_id=0xFC,
                pts=pts,
                data_alignment_indicator=True,
            )
            packets.extend(self.mux_pes(pid, pes))
        return tuple(packets)


@dataclass(frozen=True, slots=True)
class ProgramTableEmission:
    """One scheduled PAT/PMT insertion and its observable timing quality."""

    packets: tuple[bytes, ...]
    scheduled_at: Fraction
    emitted_at: Fraction
    previous_emitted_at: Fraction | None
    missed_repetitions: int

    @property
    def late_by(self) -> Fraction:
        return self.emitted_at - self.scheduled_at

    @property
    def elapsed_since_emission(self) -> Fraction | None:
        if self.previous_emitted_at is None:
            return None
        return self.emitted_at - self.previous_emitted_at

    @property
    def interval_compliant(self) -> bool:
        """Whether this insertion stayed inside the strict ST 1402-02 gap."""

        elapsed = self.elapsed_since_emission
        return elapsed is None or elapsed < ST1402_MAX_PSI_INTERVAL


def _scheduler_time(value: Fraction | int | float, *, name: str) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (Fraction, int, float)):
        raise TypeError(f"{name} must be a Fraction, integer, or float")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
        return Fraction(str(value))
    return Fraction(value)


class ProgramTableScheduler:
    """Schedule PAT/PMT insertions without accumulating fractional drift.

    Poll this object on the same monotonic timeline used to schedule the output
    transport. The first poll emits immediately. Later polls emit at most one
    repetition; if the caller was late, skipped recommended-cadence slots and
    any violation of the mandatory ST 1402-02 interval remain observable on
    the returned :class:`ProgramTableEmission`.

    The default is the standard's recommended eight repetitions per second.
    Custom intervals must remain strictly below 250 ms because exactly four
    repetitions per second does not meet the "greater than 4" requirement.
    """

    def __init__(
        self,
        muxer: TransportMuxer,
        *,
        interval: Fraction | int | float = ST1402_RECOMMENDED_PSI_INTERVAL,
    ) -> None:
        if not isinstance(muxer, TransportMuxer):
            raise TypeError("muxer must be a TransportMuxer")
        cadence = _scheduler_time(interval, name="interval")
        if cadence <= 0:
            raise ValueError("interval must be positive")
        if cadence >= ST1402_MAX_PSI_INTERVAL:
            raise ValueError("interval must be less than 0.25 seconds for ST 1402-02")
        self._muxer = muxer
        self._interval = cadence
        self._next_due_at: Fraction | None = None
        self._observed_at: Fraction | None = None
        self._last_emitted_at: Fraction | None = None
        self._total_emissions = 0
        self._total_missed_repetitions = 0

    @property
    def interval(self) -> Fraction:
        return self._interval

    @property
    def next_due_at(self) -> Fraction | None:
        return self._next_due_at

    @property
    def total_emissions(self) -> int:
        return self._total_emissions

    @property
    def total_missed_repetitions(self) -> int:
        return self._total_missed_repetitions

    def reset(self) -> None:
        """Reset schedule metrics without rewinding mux continuity counters."""

        self._next_due_at = None
        self._observed_at = None
        self._last_emitted_at = None
        self._total_emissions = 0
        self._total_missed_repetitions = 0

    def poll(
        self,
        *,
        at: Fraction | int | float,
    ) -> ProgramTableEmission | None:
        """Emit one PAT/PMT pair when due at monotonic time ``at``."""

        timestamp = _scheduler_time(at, name="timestamp")
        if self._observed_at is not None and timestamp < self._observed_at:
            raise DecodeError("program-table scheduler timestamps must be monotonic")
        self._observed_at = timestamp

        if self._next_due_at is None:
            scheduled_at = timestamp
            missed = 0
            self._next_due_at = timestamp + self._interval
        elif timestamp < self._next_due_at:
            return None
        else:
            due_count = ((timestamp - self._next_due_at) // self._interval) + 1
            missed = due_count - 1
            scheduled_at = self._next_due_at + (missed * self._interval)
            self._next_due_at += due_count * self._interval

        emission = ProgramTableEmission(
            packets=self._muxer.program_tables(),
            scheduled_at=scheduled_at,
            emitted_at=timestamp,
            previous_emitted_at=self._last_emitted_at,
            missed_repetitions=missed,
        )
        self._last_emitted_at = timestamp
        self._total_emissions += 1
        self._total_missed_repetitions += missed
        return emission
