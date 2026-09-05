"""Program Specific Information assembly and PAT/PMT decoding."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
from types import MappingProxyType

from stanag4609.errors import DecodeError, TruncatedData
from stanag4609.klv.checksum import mpeg2_crc32 as mpeg2_crc32
from stanag4609.transport.mpegts import TransportPacket

ST1402_MAX_PSI_INTERVAL = Fraction(1, 4)
ST1402_RECOMMENDED_PSI_INTERVAL = Fraction(1, 8)


@dataclass(frozen=True, slots=True)
class ProgramAssociation:
    program_number: int
    program_map_pid: int


@dataclass(frozen=True, slots=True)
class ProgramAssociationTable:
    transport_stream_id: int
    version_number: int
    current_next_indicator: bool
    section_number: int
    last_section_number: int
    programs: tuple[ProgramAssociation, ...]
    raw: bytes


@dataclass(frozen=True, slots=True)
class Descriptor:
    tag: int
    data: bytes

    @property
    def raw(self) -> bytes:
        return bytes((self.tag, len(self.data))) + self.data


@dataclass(frozen=True, slots=True)
class ElementaryStreamInfo:
    stream_type: int
    elementary_pid: int
    descriptors: tuple[Descriptor, ...]


@dataclass(frozen=True, slots=True)
class ProgramMapTable:
    program_number: int
    version_number: int
    current_next_indicator: bool
    section_number: int
    last_section_number: int
    pcr_pid: int
    descriptors: tuple[Descriptor, ...]
    streams: tuple[ElementaryStreamInfo, ...]
    raw: bytes


@dataclass(frozen=True, slots=True)
class PSICadenceIssue:
    """One ST 1402-02 PAT/PMT recurrence violation."""

    code: str
    table: str
    program_number: int | None
    previous_at: Fraction
    observed_at: Fraction
    maximum_interval: Fraction
    elapsed: Fraction
    message: str


class KLVCarriage(Enum):
    SYNCHRONOUS = "synchronous"
    ASYNCHRONOUS = "asynchronous"


def _cadence_time(value: Fraction | int | float, *, name: str) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (Fraction, int, float)):
        raise TypeError(f"{name} must be a Fraction, integer, or float")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
        return Fraction(str(value))
    return Fraction(value)


class PSICadenceValidator:
    """Track the strict ST 1402-02 recurrence of current PAT and PMT tables.

    ``at`` values are seconds on any one monotonic timeline. Exact PCR-derived
    :class:`~fractions.Fraction` values and ordinary monotonic-clock floats are
    both accepted. Call :meth:`start` when the beginning of the monitored
    program is known; otherwise the first observation establishes the baseline.

    ST 1402-02 says tables occur *more than* four times per second, so an
    interval exactly equal to 250 ms is a violation. The 125 ms recommendation
    is exposed as configuration but is not reported as a conformance failure.
    """

    def __init__(
        self,
        *,
        maximum_interval: Fraction | int | float = ST1402_MAX_PSI_INTERVAL,
        recommended_interval: Fraction | int | float = ST1402_RECOMMENDED_PSI_INTERVAL,
    ) -> None:
        maximum = _cadence_time(maximum_interval, name="maximum_interval")
        recommended = _cadence_time(
            recommended_interval,
            name="recommended_interval",
        )
        if maximum <= 0 or recommended <= 0:
            raise ValueError("cadence intervals must be positive")
        if recommended > maximum:
            raise ValueError("recommended_interval must not exceed maximum_interval")
        self._maximum_interval = maximum
        self._recommended_interval = recommended
        self._started_at: Fraction | None = None
        self._observed_at: Fraction | None = None
        self._last_pat_at: Fraction | None = None
        self._expected_since: dict[int, Fraction] = {}
        self._last_pmt_at: dict[int, Fraction] = {}
        self._pat_cycle_key: tuple[int, int, int] | None = None
        self._pat_sections: dict[int, ProgramAssociationTable] = {}

    @property
    def maximum_interval(self) -> Fraction:
        return self._maximum_interval

    @property
    def recommended_interval(self) -> Fraction:
        return self._recommended_interval

    @property
    def expected_programs(self) -> tuple[int, ...]:
        """Return current PAT program numbers in deterministic order."""

        return tuple(sorted(self._expected_since))

    @property
    def last_pat_at(self) -> Fraction | None:
        return self._last_pat_at

    @property
    def last_pmt_at(self) -> Mapping[int, Fraction]:
        """Return an immutable snapshot of PMT observations by program."""

        return MappingProxyType(dict(self._last_pmt_at))

    def reset(self) -> None:
        """Forget all timing and program-discovery state."""

        self._started_at = None
        self._observed_at = None
        self._last_pat_at = None
        self._expected_since.clear()
        self._last_pmt_at.clear()
        self._pat_cycle_key = None
        self._pat_sections.clear()

    def start(self, *, at: Fraction | int | float) -> None:
        """Establish a known program-start baseline before the first PAT."""

        if self._observed_at is not None:
            raise DecodeError("PSI cadence monitoring has already started")
        timestamp = _cadence_time(at, name="timestamp")
        self._started_at = timestamp
        self._observed_at = timestamp

    def observe_pat(
        self,
        table: ProgramAssociationTable,
        *,
        at: Fraction | int | float,
    ) -> tuple[PSICadenceIssue, ...]:
        """Observe one CRC-validated PAT and return any PAT interval issue."""

        if not isinstance(table, ProgramAssociationTable):
            raise TypeError("table must be a ProgramAssociationTable")
        timestamp = self._advance(at)
        if not table.current_next_indicator:
            return ()
        self._validate_section_numbers(table.section_number, table.last_section_number)
        cycle_key = (
            table.transport_stream_id,
            table.version_number,
            table.last_section_number,
        )
        if cycle_key != self._pat_cycle_key:
            self._pat_cycle_key = cycle_key
            self._pat_sections.clear()
        self._pat_sections[table.section_number] = table
        if len(self._pat_sections) != table.last_section_number + 1:
            return ()

        previous_programs = set(self._expected_since)
        current_programs = {
            program.program_number
            for section in self._pat_sections.values()
            for program in section.programs
        }
        issue = self._pat_issue(timestamp)
        for program_number in previous_programs - current_programs:
            self._expected_since.pop(program_number, None)
            self._last_pmt_at.pop(program_number, None)
        for program_number in current_programs - previous_programs:
            self._expected_since[program_number] = timestamp
        self._last_pat_at = timestamp
        self._pat_cycle_key = None
        self._pat_sections.clear()
        return () if issue is None else (issue,)

    def observe_pmt(
        self,
        table: ProgramMapTable,
        *,
        at: Fraction | int | float,
    ) -> tuple[PSICadenceIssue, ...]:
        """Observe one CRC-validated PMT and return its program interval issue."""

        if not isinstance(table, ProgramMapTable):
            raise TypeError("table must be a ProgramMapTable")
        timestamp = self._advance(at)
        if (
            not table.current_next_indicator
            or table.program_number not in self._expected_since
        ):
            return ()
        if table.section_number != 0 or table.last_section_number != 0:
            raise DecodeError("PMT section_number and last_section_number must both be zero")
        issue = self._pmt_issue(table.program_number, timestamp)
        self._last_pmt_at[table.program_number] = timestamp
        return () if issue is None else (issue,)

    def check(self, *, at: Fraction | int | float) -> tuple[PSICadenceIssue, ...]:
        """Return the PAT and active-program PMT violations at ``at``."""

        timestamp = self._advance(at)
        issues: list[PSICadenceIssue] = []
        pat_issue = self._pat_issue(timestamp)
        if pat_issue is not None:
            issues.append(pat_issue)
        for program_number in sorted(self._expected_since):
            pmt_issue = self._pmt_issue(program_number, timestamp)
            if pmt_issue is not None:
                issues.append(pmt_issue)
        return tuple(issues)

    def _advance(self, at: Fraction | int | float) -> Fraction:
        timestamp = _cadence_time(at, name="timestamp")
        if self._observed_at is not None and timestamp < self._observed_at:
            raise DecodeError("PSI cadence timestamps must be monotonic")
        self._observed_at = timestamp
        if self._started_at is None:
            self._started_at = timestamp
        return timestamp

    @staticmethod
    def _validate_section_numbers(section_number: int, last_section_number: int) -> None:
        if not 0 <= section_number <= last_section_number <= 0xFF:
            raise DecodeError("PSI section_number exceeds last_section_number")

    def _pat_issue(self, timestamp: Fraction) -> PSICadenceIssue | None:
        baseline = self._last_pat_at if self._last_pat_at is not None else self._started_at
        assert baseline is not None
        return self._issue(
            table="PAT",
            program_number=None,
            previous_at=baseline,
            observed_at=timestamp,
            missing=self._last_pat_at is None,
        )

    def _pmt_issue(
        self,
        program_number: int,
        timestamp: Fraction,
    ) -> PSICadenceIssue | None:
        previous = self._last_pmt_at.get(program_number)
        return self._issue(
            table="PMT",
            program_number=program_number,
            previous_at=(
                previous if previous is not None else self._expected_since[program_number]
            ),
            observed_at=timestamp,
            missing=previous is None,
        )

    def _issue(
        self,
        *,
        table: str,
        program_number: int | None,
        previous_at: Fraction,
        observed_at: Fraction,
        missing: bool,
    ) -> PSICadenceIssue | None:
        elapsed = observed_at - previous_at
        if elapsed < self._maximum_interval:
            return None
        identity = table if program_number is None else f"{table} for program {program_number}"
        return PSICadenceIssue(
            code="missing" if missing else "interval",
            table=table,
            program_number=program_number,
            previous_at=previous_at,
            observed_at=observed_at,
            maximum_interval=self._maximum_interval,
            elapsed=elapsed,
            message=(
                f"ST 1402-02 requires {identity} more than 4 times per second; "
                f"observed interval is {float(elapsed):g} seconds"
            ),
        )


def _validate_section(section: bytes, *, table_id: int, name: str) -> None:
    if len(section) < 3:
        raise TruncatedData(f"{name} section is shorter than its 3-byte header")
    if section[0] != table_id:
        raise DecodeError(f"expected {name} table_id 0x{table_id:02X}")
    if not section[1] & 0x80:
        raise DecodeError(f"{name} section_syntax_indicator is not set")
    if section[1] & 0x40:
        raise DecodeError(f"{name} fixed zero bit after section_syntax_indicator is set")
    if section[1] & 0x30 != 0x30:
        raise DecodeError(f"{name} section header reserved bits are not all set")
    section_length = ((section[1] & 0x0F) << 8) | section[2]
    if section_length > 1021:
        raise DecodeError(f"{name} section_length exceeds 1021 bytes")
    expected_length = 3 + section_length
    if len(section) < expected_length:
        raise TruncatedData(
            f"{name} declares {expected_length} bytes, observed {len(section)}"
        )
    if len(section) != expected_length:
        raise DecodeError(f"{name} has trailing bytes after its declared section")
    if mpeg2_crc32(section) != 0:
        raise DecodeError(f"{name} MPEG-2 CRC-32 mismatch")


def _table_common(section: bytes) -> tuple[int, int, bool, int, int]:
    if section[5] & 0xC0 != 0xC0:
        raise DecodeError("PSI table header reserved bits are not all set")
    section_number = section[6]
    last_section_number = section[7]
    if section_number > last_section_number:
        raise DecodeError("PSI section_number exceeds last_section_number")
    return (
        int.from_bytes(section[3:5], "big"),
        (section[5] >> 1) & 0x1F,
        bool(section[5] & 0x01),
        section_number,
        last_section_number,
    )


def parse_pat(section: bytes) -> ProgramAssociationTable:
    """Decode one complete Program Association Table section."""
    _validate_section(section, table_id=0x00, name="PAT")
    if len(section) < 12:
        raise DecodeError("PAT is too short")
    entries = section[8:-4]
    if len(entries) % 4:
        raise DecodeError("PAT program loop is not a multiple of four bytes")
    programs: list[ProgramAssociation] = []
    seen_programs: set[int] = set()
    for index in range(0, len(entries), 4):
        program_number = int.from_bytes(entries[index : index + 2], "big")
        if program_number in seen_programs:
            raise DecodeError(f"PAT program_number {program_number} occurs twice")
        seen_programs.add(program_number)
        if entries[index + 2] & 0xE0 != 0xE0:
            raise DecodeError("PAT program PID reserved bits are not all set")
        if program_number != 0:
            programs.append(
                ProgramAssociation(
                    program_number,
                    ((entries[index + 2] & 0x1F) << 8) | entries[index + 3],
                )
            )
    table_id_extension, version, current, section_number, last_section = _table_common(
        section
    )
    return ProgramAssociationTable(
        table_id_extension,
        version,
        current,
        section_number,
        last_section,
        tuple(programs),
        section,
    )


def _parse_descriptors(data: bytes) -> tuple[Descriptor, ...]:
    descriptors: list[Descriptor] = []
    cursor = 0
    while cursor < len(data):
        if len(data) - cursor < 2:
            raise TruncatedData("descriptor loop ends inside a descriptor header")
        length = data[cursor + 1]
        end = cursor + 2 + length
        if end > len(data):
            raise TruncatedData("descriptor overruns its descriptor loop")
        descriptors.append(Descriptor(data[cursor], data[cursor + 2 : end]))
        cursor = end
    return tuple(descriptors)


def parse_pmt(section: bytes) -> ProgramMapTable:
    """Decode one complete Program Map Table section."""
    _validate_section(section, table_id=0x02, name="PMT")
    if len(section) < 16:
        raise DecodeError("PMT is too short")
    if section[8] & 0xE0 != 0xE0:
        raise DecodeError("PMT PCR_PID reserved bits are not all set")
    if section[10] & 0xF0 != 0xF0:
        raise DecodeError("PMT program_info_length reserved bits are not all set")
    pcr_pid = ((section[8] & 0x1F) << 8) | section[9]
    program_info_length = ((section[10] & 0x0F) << 8) | section[11]
    program_info_end = 12 + program_info_length
    loop_end = len(section) - 4
    if program_info_end > loop_end:
        raise TruncatedData("PMT program descriptors overrun the section")
    program_descriptors = _parse_descriptors(section[12:program_info_end])

    streams: list[ElementaryStreamInfo] = []
    cursor = program_info_end
    while cursor < loop_end:
        if loop_end - cursor < 5:
            raise TruncatedData("PMT ends inside an elementary stream header")
        if section[cursor + 1] & 0xE0 != 0xE0:
            raise DecodeError("PMT elementary_PID reserved bits are not all set")
        if section[cursor + 3] & 0xF0 != 0xF0:
            raise DecodeError("PMT ES_info_length reserved bits are not all set")
        elementary_pid = ((section[cursor + 1] & 0x1F) << 8) | section[cursor + 2]
        info_length = ((section[cursor + 3] & 0x0F) << 8) | section[cursor + 4]
        info_end = cursor + 5 + info_length
        if info_end > loop_end:
            raise TruncatedData("PMT elementary descriptors overrun the section")
        streams.append(
            ElementaryStreamInfo(
                section[cursor],
                elementary_pid,
                _parse_descriptors(section[cursor + 5 : info_end]),
            )
        )
        cursor = info_end

    program_number, version, current, section_number, last_section = _table_common(section)
    if section_number != 0 or last_section != 0:
        raise DecodeError("PMT section_number and last_section_number must both be zero")
    return ProgramMapTable(
        program_number,
        version,
        current,
        section_number,
        last_section,
        pcr_pid,
        program_descriptors,
        tuple(streams),
        section,
    )


def _has_registration(descriptors: tuple[Descriptor, ...], identifier: bytes) -> bool:
    return any(
        descriptor.tag == 0x05 and descriptor.data[:4] == identifier
        for descriptor in descriptors
    )


def _metadata_format_identifier(descriptor: Descriptor) -> bytes | None:
    if descriptor.tag != 0x26 or len(descriptor.data) < 3:
        return None
    cursor = 2
    application_format = int.from_bytes(descriptor.data[:2], "big")
    if application_format == 0xFFFF:
        cursor += 4
    if len(descriptor.data) <= cursor:
        return None
    metadata_format = descriptor.data[cursor]
    cursor += 1
    if metadata_format != 0xFF or len(descriptor.data) < cursor + 4:
        return None
    return descriptor.data[cursor : cursor + 4]


def find_klv_streams(pmt: ProgramMapTable) -> tuple[tuple[int, KLVCarriage], ...]:
    """Return PMT elementary PIDs explicitly registered as KLVA metadata."""
    found: list[tuple[int, KLVCarriage]] = []
    for stream in pmt.streams:
        carriage = klv_carriage(stream)
        if carriage is not None:
            found.append((stream.elementary_pid, carriage))
    return tuple(found)


def klv_carriage(stream: ElementaryStreamInfo) -> KLVCarriage | None:
    """Classify one explicitly signalled ST 1402 KLVA elementary stream."""
    if stream.stream_type == 0x06 and _has_registration(stream.descriptors, b"KLVA"):
        return KLVCarriage.ASYNCHRONOUS
    if stream.stream_type == 0x15 and any(
        _metadata_format_identifier(descriptor) == b"KLVA"
        for descriptor in stream.descriptors
    ):
        return KLVCarriage.SYNCHRONOUS
    return None


class PSISectionAssembler:
    """Reconstruct PSI sections spanning arbitrary packets for one PID."""

    def __init__(self, *, pid: int, max_section_length: int = 1021) -> None:
        if not 0 <= pid <= 0x1FFF:
            raise ValueError("PID must fit in 13 bits")
        if not 3 <= max_section_length <= 4096:
            raise ValueError("max_section_length must be between 3 and 4096")
        self.pid = pid
        self.max_section_length = max_section_length
        self._buffer = bytearray()
        self._synchronized = False

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, packet: TransportPacket) -> list[bytes]:
        if packet.pid != self.pid:
            raise ValueError(f"expected PID {self.pid}, observed PID {packet.pid}")
        if not packet.has_payload or not packet.payload:
            return []
        payload = packet.payload
        sections: list[bytes] = []
        if packet.payload_unit_start:
            pointer = payload[0]
            if pointer > len(payload) - 1:
                raise DecodeError("PSI pointer_field exceeds packet payload")
            continuation_end = 1 + pointer
            if self._buffer:
                sections.extend(self._consume(payload[1:continuation_end]))
                if self._buffer:
                    raise TruncatedData("new PSI section starts before previous section ended")
            self._synchronized = True
            sections.extend(self._consume(payload[continuation_end:]))
        elif self._synchronized:
            sections.extend(self._consume(payload))
        return sections

    def finish(self) -> list[bytes]:
        if self._buffer:
            raise TruncatedData(
                f"PSI stream ended with {len(self._buffer)} incomplete section byte(s)"
            )
        return []

    def _consume(self, data: bytes) -> list[bytes]:
        self._buffer.extend(data)
        sections: list[bytes] = []
        while self._buffer:
            if self._buffer[0] == 0xFF:
                if any(byte != 0xFF for byte in self._buffer):
                    raise DecodeError("PSI payload contains a non-0xFF byte after stuffing begins")
                self._buffer.clear()
                break
            if len(self._buffer) < 3:
                break
            section_length = ((self._buffer[1] & 0x0F) << 8) | self._buffer[2]
            total = 3 + section_length
            if section_length > self.max_section_length:
                raise DecodeError(
                    f"PSI section_length {section_length} exceeds configured limit "
                    f"{self.max_section_length}"
                )
            if len(self._buffer) < total:
                break
            sections.append(bytes(self._buffer[:total]))
            del self._buffer[:total]
        return sections
