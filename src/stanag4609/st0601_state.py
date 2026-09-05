"""Stateful MISB ST 0601 Report-on-Change reconstruction."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any

from stanag4609.errors import DecodeError, LimitExceeded
from stanag4609.klv.model import KLVPacket
from stanag4609.st0601 import (
    ActivePayloads,
    ActiveWavelengthList,
    ControlCommand,
    ControlCommandVerificationList,
    DecodedField,
    FieldDecodingIssue,
    FieldDecodingMode,
    PayloadList,
    PayloadRecord,
    ResolvedUASField,
    SpecialValue,
    ST0601Semantic,
    UASLocalSet,
    WavelengthRecord,
    WavelengthsList,
    WaypointList,
    WaypointRecord,
    WeaponsStores,
    WeaponStore,
    decode_uas_local_set,
    effective_uas_fields,
    misp_timestamp_to_utc,
    resolve_preferred_uas_field,
)

_MANDATORY_PACKET_TAGS = frozenset({1, 2, 65})


@dataclass(frozen=True, slots=True)
class ReportOnChangeSnapshot:
    """One reconstructed receiver view after applying an ST 0601 packet."""

    timestamp: datetime
    fields: tuple[DecodedField, ...]
    updated_tags: tuple[int, ...]
    cleared_tags: tuple[int, ...]
    expired_tags: tuple[int, ...]
    issues: tuple[FieldDecodingIssue, ...] = ()

    def getall(self, tag: int) -> tuple[DecodedField, ...]:
        return tuple(field for field in self.fields if field.definition.tag == tag)

    def get(self, tag: int) -> DecodedField | None:
        matches = self.getall(tag)
        if not matches:
            return None
        if len(matches) > 1:
            raise ValueError(f"tag {tag} occurs {len(matches)} times in receiver state")
        return matches[0]

    def value(self, tag: int, default: Any = None) -> Any:
        field = self.get(tag)
        return default if field is None else field.value

    def utc_timestamp(
        self,
        *,
        leap_seconds: int | None = None,
        correction_offset: int | None = None,
    ) -> datetime:
        """Convert the current MISP timestamp with Items 136/137 or overrides."""

        selected_leaps = self.value(136) if leap_seconds is None else leap_seconds
        if selected_leaps is None:
            raise ValueError(
                "leap_seconds is required when ST 0601 Item 136 is absent"
            )
        selected_correction = (
            self.value(137, 0) if correction_offset is None else correction_offset
        )
        return misp_timestamp_to_utc(
            self.timestamp,
            leap_seconds=selected_leaps,
            correction_offset=selected_correction,
        )

    @property
    def effective_fields(self) -> tuple[DecodedField, ...]:
        """Current fields after preferred-representation rules are applied."""

        return effective_uas_fields(self.fields)

    def preferred_field(
        self, semantic: ST0601Semantic | str
    ) -> ResolvedUASField | None:
        """Resolve a current logical value to its preferred representation."""

        return resolve_preferred_uas_field(self.fields, semantic)


class ReportOnChangeState:
    """Reconstruct the receiver-visible state of an ST 0601 packet stream.

    Positive-length values remain current until replaced, cleared by a
    zero-length item, or absent for more than the configured refresh period.
    The standard's three mandatory packet fields are always taken from the
    current packet and are not subject to Report-on-Change expiry.

    Multiple instances of one tag are retained and replaced as an atomic group.
    Standards-specific distributed-list and Segment/Amend lifecycle semantics
    remain the responsibility of their dedicated evaluators.
    """

    def __init__(
        self,
        *,
        refresh_period: timedelta = timedelta(seconds=30),
        field_decoding: FieldDecodingMode = FieldDecodingMode.PRESERVE,
        max_items_per_tag: int = 1024,
    ) -> None:
        if not isinstance(refresh_period, timedelta):
            raise TypeError("refresh_period must be a timedelta")
        if refresh_period <= timedelta(0):
            raise ValueError("refresh_period must be positive")
        if refresh_period > timedelta(seconds=30):
            raise ValueError("ST 0107.4-18 limits the refresh period to 30 seconds")
        if not isinstance(field_decoding, FieldDecodingMode):
            raise TypeError("field_decoding must be a FieldDecodingMode")
        if (
            isinstance(max_items_per_tag, bool)
            or not isinstance(max_items_per_tag, int)
            or max_items_per_tag < 1
        ):
            raise ValueError("max_items_per_tag must be a positive integer")
        self._refresh_period = refresh_period
        self._field_decoding = field_decoding
        self._max_items_per_tag = max_items_per_tag
        self._observed_at: datetime | None = None
        self._fields: dict[int, tuple[DecodedField, ...]] = {}
        self._last_seen: dict[int, datetime] = {}

    @property
    def refresh_period(self) -> timedelta:
        return self._refresh_period

    @property
    def field_decoding(self) -> FieldDecodingMode:
        return self._field_decoding

    @property
    def max_items_per_tag(self) -> int:
        return self._max_items_per_tag

    @property
    def last_seen(self) -> Mapping[int, datetime]:
        """Return an immutable snapshot of positive-length update times."""
        return MappingProxyType(dict(self._last_seen))

    def reset(self) -> None:
        """Discard receiver state and accept a new stream timeline."""
        self._observed_at = None
        self._fields.clear()
        self._last_seen.clear()

    def observe(
        self, packet: bytes | KLVPacket | UASLocalSet
    ) -> ReportOnChangeSnapshot:
        """Apply one packet and return the resulting receiver-visible snapshot."""
        uas = (
            packet
            if isinstance(packet, UASLocalSet)
            else decode_uas_local_set(packet, field_decoding=self._field_decoding)
        )
        timestamp = uas.value(2)
        if not isinstance(timestamp, datetime):
            raise DecodeError("ST 0601 Report-on-Change requires a typed timestamp")
        if self._observed_at is not None and timestamp < self._observed_at:
            raise DecodeError("ST 0601 Report-on-Change timestamps must be monotonic")

        fields_by_tag: dict[int, list[DecodedField]] = {}
        for field in uas.fields:
            tag_fields = fields_by_tag.setdefault(field.definition.tag, [])
            tag_fields.append(field)
            if len(tag_fields) > self._max_items_per_tag:
                raise LimitExceeded(
                    f"ST 0601 tag {field.definition.tag} exceeds configured receiver-state "
                    f"limit {self._max_items_per_tag}"
                )

        expired_tags = tuple(
            sorted(
                tag
                for tag, last_seen in self._last_seen.items()
                if tag not in _MANDATORY_PACKET_TAGS
                and timestamp - last_seen > self._refresh_period
            )
        )
        for tag in expired_tags:
            self._fields.pop(tag, None)
            del self._last_seen[tag]
        self._observed_at = timestamp

        cleared_tags: list[int] = []
        updated_tags: list[int] = []
        for tag, fields in fields_by_tag.items():
            raw_items = uas.local_set.getall(tag)
            if any(not item.value for item in raw_items):
                self._fields.pop(tag, None)
                self._last_seen.pop(tag, None)
                cleared_tags.append(tag)
                continue
            self._fields[tag] = tuple(fields)
            self._last_seen[tag] = timestamp
            updated_tags.append(tag)

        current_fields = tuple(
            field
            for tag in sorted(self._fields)
            for field in self._fields[tag]
        )
        return ReportOnChangeSnapshot(
            timestamp,
            current_fields,
            tuple(sorted(updated_tags)),
            tuple(sorted(cleared_tags)),
            expired_tags,
            uas.issues,
        )


@dataclass(frozen=True, slots=True)
class ControlCommandIssue:
    """One ST 0601 Item 115/116 command-lifecycle violation."""

    code: str
    message: str
    command_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class ControlCommandSnapshot:
    """Current issued-command and acknowledgement receiver state."""

    timestamp: datetime
    outstanding_commands: Mapping[int, ControlCommand]
    issued_at: Mapping[int, datetime]
    acknowledged_ids: tuple[int, ...]
    updated_ids: tuple[int, ...]
    newly_acknowledged_ids: tuple[int, ...]
    issues: tuple[ControlCommandIssue, ...] = ()
    field_issues: tuple[FieldDecodingIssue, ...] = ()


class ControlCommandState:
    """Validate the cross-packet lifecycle of ST 0601 Items 115 and 116.

    A newly issued command ID must increase beyond every earlier command ID.
    Repetitions retain the original command text and effective issue time. An
    Item 116 acknowledgement closes its referenced command, after which Item
    115 must not repeat it. History is intentionally bounded so hostile or
    indefinitely long streams cannot grow receiver memory without limit.
    """

    def __init__(
        self,
        *,
        max_command_history: int = 65_536,
        field_decoding: FieldDecodingMode = FieldDecodingMode.PRESERVE,
    ) -> None:
        if (
            isinstance(max_command_history, bool)
            or not isinstance(max_command_history, int)
            or max_command_history < 1
        ):
            raise ValueError("max_command_history must be a positive integer")
        if not isinstance(field_decoding, FieldDecodingMode):
            raise TypeError("field_decoding must be a FieldDecodingMode")
        self._max_command_history = max_command_history
        self._field_decoding = field_decoding
        self._observed_at: datetime | None = None
        self._history: dict[int, ControlCommand] = {}
        self._issued_at: dict[int, datetime] = {}
        self._outstanding_ids: set[int] = set()
        self._acknowledged_ids: set[int] = set()
        self._highest_command_id: int | None = None

    @property
    def max_command_history(self) -> int:
        return self._max_command_history

    @property
    def field_decoding(self) -> FieldDecodingMode:
        return self._field_decoding

    def reset(self) -> None:
        """Discard command history and accept a new stream timeline."""
        self._observed_at = None
        self._history.clear()
        self._issued_at.clear()
        self._outstanding_ids.clear()
        self._acknowledged_ids.clear()
        self._highest_command_id = None

    def observe(
        self, packet: bytes | KLVPacket | UASLocalSet
    ) -> ControlCommandSnapshot:
        """Apply one packet and return the resulting command lifecycle."""
        uas = (
            packet
            if isinstance(packet, UASLocalSet)
            else decode_uas_local_set(packet, field_decoding=self._field_decoding)
        )
        timestamp = uas.value(2)
        if not isinstance(timestamp, datetime):
            raise DecodeError("ST 0601 command state requires a typed timestamp")
        if self._observed_at is not None and timestamp < self._observed_at:
            raise DecodeError("ST 0601 command-state timestamps must be monotonic")

        commands = tuple(
            field.value
            for field in uas.getall(115)
            if isinstance(field.value, ControlCommand)
        )
        new_ids = {
            command.command_id
            for command in commands
            if command.command_id not in self._history
        }
        if len(self._history) + len(new_ids) > self._max_command_history:
            raise LimitExceeded(
                "ST 0601 command history exceeds configured limit "
                f"{self._max_command_history}"
            )

        history = dict(self._history)
        issued_at = dict(self._issued_at)
        outstanding_ids = set(self._outstanding_ids)
        acknowledged_ids = set(self._acknowledged_ids)
        highest_command_id = self._highest_command_id
        issues: list[ControlCommandIssue] = []
        updated_ids: set[int] = set()
        seen_in_packet: set[int] = set()

        for command in commands:
            command_id = command.command_id
            if command_id in seen_in_packet:
                issues.append(
                    ControlCommandIssue(
                        "duplicate_command_in_packet",
                        "ST 0601 Item 115 repeats the same Command ID more than once "
                        "in one Local Set",
                        (command_id,),
                    )
                )
                continue
            seen_in_packet.add(command_id)
            updated_ids.add(command_id)

            if command_id in history:
                if command_id in acknowledged_ids:
                    issues.append(
                        ControlCommandIssue(
                            "command_after_acknowledgement",
                            "ST 0601 Item 115 must stop repeating a Command ID after "
                            "Item 116 acknowledges it",
                            (command_id,),
                        )
                    )
                    continue
                original = history[command_id]
                if command.command != original.command:
                    issues.append(
                        ControlCommandIssue(
                            "command_changed",
                            "a repeated ST 0601 Item 115 Command ID changed its "
                            "Command String",
                            (command_id,),
                        )
                    )
                if (
                    command.command_time is not None
                    and command.command_time != issued_at[command_id]
                ):
                    issues.append(
                        ControlCommandIssue(
                            "command_time_changed",
                            "a repeated ST 0601 Item 115 Command Time does not match "
                            "the command's original effective issue time",
                            (command_id,),
                        )
                    )
                continue

            if highest_command_id is not None and command_id <= highest_command_id:
                issues.append(
                    ControlCommandIssue(
                        "non_increasing_command_id",
                        "new ST 0601 Item 115 Command IDs must be increasing and unique",
                        (command_id,),
                    )
                )
            history[command_id] = command
            issued_at[command_id] = command.command_time or timestamp
            outstanding_ids.add(command_id)
            highest_command_id = (
                command_id
                if highest_command_id is None
                else max(highest_command_id, command_id)
            )

        acknowledgement = uas.value(116)
        newly_acknowledged_ids: set[int] = set()
        if isinstance(acknowledgement, ControlCommandVerificationList):
            counts: dict[int, int] = {}
            for command_id in acknowledgement.command_ids:
                counts[command_id] = counts.get(command_id, 0) + 1
            duplicates = tuple(
                sorted(command_id for command_id, count in counts.items() if count > 1)
            )
            if duplicates:
                issues.append(
                    ControlCommandIssue(
                        "duplicate_acknowledgement",
                        "ST 0601 Item 116 contains duplicate Command IDs",
                        duplicates,
                    )
                )
            unknown = tuple(
                sorted(command_id for command_id in counts if command_id not in history)
            )
            if unknown:
                issues.append(
                    ControlCommandIssue(
                        "unknown_acknowledgement",
                        "ST 0601 Item 116 acknowledges Command IDs not observed in "
                        "Item 115",
                        unknown,
                    )
                )
            for command_id in counts:
                if command_id in history and command_id not in acknowledged_ids:
                    acknowledged_ids.add(command_id)
                    newly_acknowledged_ids.add(command_id)
                    outstanding_ids.discard(command_id)

        self._observed_at = timestamp
        self._history = history
        self._issued_at = issued_at
        self._outstanding_ids = outstanding_ids
        self._acknowledged_ids = acknowledged_ids
        self._highest_command_id = highest_command_id

        return ControlCommandSnapshot(
            timestamp,
            MappingProxyType(
                {
                    command_id: history[command_id]
                    for command_id in sorted(outstanding_ids)
                }
            ),
            MappingProxyType(dict(sorted(issued_at.items()))),
            tuple(sorted(acknowledged_ids)),
            tuple(sorted(updated_ids)),
            tuple(sorted(newly_acknowledged_ids)),
            tuple(issues),
            uas.issues,
        )


@dataclass(frozen=True, slots=True)
class WavelengthTableIssue:
    """One cross-packet ST 0601 wavelength-table violation."""

    code: str
    message: str
    wavelength_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class WavelengthTableSnapshot:
    """Current custom table and active wavelength selection."""

    timestamp: datetime
    custom_records: Mapping[int, WavelengthRecord]
    active_ids: tuple[int, ...] | None
    updated_ids: tuple[int, ...]
    expired_ids: tuple[int, ...]
    cleared_custom_table: bool
    active_expired: bool
    active_cleared: bool
    issues: tuple[WavelengthTableIssue, ...] = ()
    field_issues: tuple[FieldDecodingIssue, ...] = ()

    @property
    def known_ids(self) -> tuple[int, ...]:
        """Return predefined IDs 0..6 followed by current custom IDs."""
        return tuple(range(7)) + tuple(self.custom_records)


class WavelengthTableState:
    """Reconstruct distributed ST 0601 Items 121/128 receiver state.

    Custom wavelength records are keyed by their IDs and expire independently.
    Active IDs are checked against predefined IDs 0..6 and custom records
    defined within the current refresh window.
    """

    def __init__(
        self,
        *,
        refresh_period: timedelta = timedelta(seconds=30),
        max_custom_records: int = 4096,
        field_decoding: FieldDecodingMode = FieldDecodingMode.PRESERVE,
    ) -> None:
        if not isinstance(refresh_period, timedelta):
            raise TypeError("refresh_period must be a timedelta")
        if refresh_period <= timedelta(0):
            raise ValueError("refresh_period must be positive")
        if refresh_period > timedelta(seconds=30):
            raise ValueError("ST 0601 Item 128 refresh period cannot exceed 30 seconds")
        if (
            isinstance(max_custom_records, bool)
            or not isinstance(max_custom_records, int)
            or max_custom_records < 1
        ):
            raise ValueError("max_custom_records must be a positive integer")
        if not isinstance(field_decoding, FieldDecodingMode):
            raise TypeError("field_decoding must be a FieldDecodingMode")
        self._refresh_period = refresh_period
        self._max_custom_records = max_custom_records
        self._field_decoding = field_decoding
        self._observed_at: datetime | None = None
        self._records: dict[int, WavelengthRecord] = {}
        self._record_seen: dict[int, datetime] = {}
        self._active_ids: tuple[int, ...] | None = None
        self._active_seen: datetime | None = None

    @property
    def refresh_period(self) -> timedelta:
        return self._refresh_period

    @property
    def max_custom_records(self) -> int:
        return self._max_custom_records

    @property
    def field_decoding(self) -> FieldDecodingMode:
        return self._field_decoding

    def reset(self) -> None:
        """Clear all custom definitions, active IDs, and stream time."""
        self._observed_at = None
        self._records.clear()
        self._record_seen.clear()
        self._active_ids = None
        self._active_seen = None

    def observe(
        self, packet: bytes | KLVPacket | UASLocalSet
    ) -> WavelengthTableSnapshot:
        """Apply one ST 0601 packet and validate its current wavelength references."""
        uas = (
            packet
            if isinstance(packet, UASLocalSet)
            else decode_uas_local_set(packet, field_decoding=self._field_decoding)
        )
        timestamp = uas.value(2)
        if not isinstance(timestamp, datetime):
            raise DecodeError("ST 0601 wavelength state requires a typed timestamp")
        if self._observed_at is not None and timestamp < self._observed_at:
            raise DecodeError("ST 0601 wavelength-state timestamps must be monotonic")

        expired_ids = tuple(
            sorted(
                identifier
                for identifier, last_seen in self._record_seen.items()
                if timestamp - last_seen > self._refresh_period
            )
        )
        wavelengths = uas.value(128)
        incoming_records = (
            wavelengths.records if isinstance(wavelengths, WavelengthsList) else ()
        )
        cleared_custom_table = wavelengths is SpecialValue.UNKNOWN
        retained_ids = set() if cleared_custom_table else set(self._records) - set(expired_ids)
        candidate_ids = retained_ids | {record.wavelength_id for record in incoming_records}
        if len(candidate_ids) > self._max_custom_records:
            raise LimitExceeded(
                "ST 0601 custom wavelength table exceeds configured limit "
                f"{self._max_custom_records}"
            )

        active_expired = (
            self._active_seen is not None
            and timestamp - self._active_seen > self._refresh_period
        )
        for identifier in expired_ids:
            self._records.pop(identifier, None)
            del self._record_seen[identifier]
        if cleared_custom_table:
            self._records.clear()
            self._record_seen.clear()
        for record in incoming_records:
            self._records[record.wavelength_id] = record
            self._record_seen[record.wavelength_id] = timestamp

        if active_expired:
            self._active_ids = None
            self._active_seen = None
        active = uas.value(121)
        active_cleared = active is SpecialValue.UNKNOWN
        if isinstance(active, ActiveWavelengthList):
            self._active_ids = active.wavelength_ids
            self._active_seen = timestamp
        elif active_cleared:
            self._active_ids = None
            self._active_seen = None
        self._observed_at = timestamp

        issues: list[WavelengthTableIssue] = []
        ids_by_name: dict[str, list[int]] = {}
        for identifier, record in self._records.items():
            ids_by_name.setdefault(record.name, []).append(identifier)
        duplicate_name_ids = tuple(
            sorted(
                identifier
                for identifiers in ids_by_name.values()
                if len(identifiers) > 1
                for identifier in identifiers
            )
        )
        if duplicate_name_ids:
            issues.append(
                WavelengthTableIssue(
                    "duplicate_custom_name",
                    "ST 0601 Item 128 custom Wavelength Names must be unique",
                    duplicate_name_ids,
                )
            )
        if self._active_ids is not None:
            reserved = tuple(identifier for identifier in self._active_ids if 7 <= identifier <= 20)
            undefined = tuple(
                identifier
                for identifier in self._active_ids
                if identifier >= 21 and identifier not in self._records
            )
            if reserved:
                issues.append(
                    WavelengthTableIssue(
                        "reserved_active_id",
                        "ST 0601 Item 121 references reserved Wavelength IDs 7 through 20",
                        reserved,
                    )
                )
            if undefined:
                issues.append(
                    WavelengthTableIssue(
                        "undefined_active_id",
                        "ST 0601.17-37 requires custom active Wavelength IDs to be defined "
                        "within the last 30 seconds",
                        undefined,
                    )
                )

        return WavelengthTableSnapshot(
            timestamp,
            MappingProxyType(dict(sorted(self._records.items()))),
            self._active_ids,
            tuple(sorted(record.wavelength_id for record in incoming_records)),
            expired_ids,
            cleared_custom_table,
            active_expired,
            active_cleared,
            tuple(issues),
            uas.issues,
        )


@dataclass(frozen=True, slots=True)
class PayloadTableIssue:
    """One cross-packet ST 0601 payload-table violation."""

    code: str
    message: str
    payload_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class PayloadTableSnapshot:
    """Current distributed payload table and active payload selection."""

    timestamp: datetime
    total_count: int | None
    records: Mapping[int, PayloadRecord]
    active_ids: frozenset[int] | None
    updated_ids: tuple[int, ...]
    expired_ids: tuple[int, ...]
    table_restarted: bool
    table_cleared: bool
    table_expired: bool
    active_cleared: bool
    active_expired: bool
    issues: tuple[PayloadTableIssue, ...] = ()
    field_issues: tuple[FieldDecodingIssue, ...] = ()

    @property
    def complete(self) -> bool:
        """Whether every sequential ID declared by Payload Count is current."""
        return self.total_count is not None and tuple(self.records) == tuple(
            range(self.total_count)
        )

    @property
    def missing_ids(self) -> tuple[int, ...]:
        """Return declared sequential IDs absent from the current table."""
        if self.total_count is None:
            return ()
        return tuple(
            identifier
            for identifier in range(self.total_count)
            if identifier not in self.records
        )


class PayloadTableState:
    """Reconstruct distributed ST 0601 Items 138/139 receiver state."""

    def __init__(
        self,
        *,
        refresh_period: timedelta = timedelta(seconds=30),
        max_payload_records: int = 4096,
        field_decoding: FieldDecodingMode = FieldDecodingMode.PRESERVE,
    ) -> None:
        if not isinstance(refresh_period, timedelta):
            raise TypeError("refresh_period must be a timedelta")
        if refresh_period <= timedelta(0):
            raise ValueError("refresh_period must be positive")
        if refresh_period > timedelta(seconds=30):
            raise ValueError("ST 0601 Item 138 refresh period cannot exceed 30 seconds")
        if (
            isinstance(max_payload_records, bool)
            or not isinstance(max_payload_records, int)
            or max_payload_records < 1
        ):
            raise ValueError("max_payload_records must be a positive integer")
        if not isinstance(field_decoding, FieldDecodingMode):
            raise TypeError("field_decoding must be a FieldDecodingMode")
        self._refresh_period = refresh_period
        self._max_payload_records = max_payload_records
        self._field_decoding = field_decoding
        self._observed_at: datetime | None = None
        self._total_count: int | None = None
        self._count_seen: datetime | None = None
        self._records: dict[int, PayloadRecord] = {}
        self._record_seen: dict[int, datetime] = {}
        self._active_ids: frozenset[int] | None = None
        self._active_seen: datetime | None = None

    @property
    def refresh_period(self) -> timedelta:
        return self._refresh_period

    @property
    def max_payload_records(self) -> int:
        return self._max_payload_records

    @property
    def field_decoding(self) -> FieldDecodingMode:
        return self._field_decoding

    def reset(self) -> None:
        """Clear the payload table, active selection, and stream time."""
        self._observed_at = None
        self._total_count = None
        self._count_seen = None
        self._records.clear()
        self._record_seen.clear()
        self._active_ids = None
        self._active_seen = None

    def observe(
        self, packet: bytes | KLVPacket | UASLocalSet
    ) -> PayloadTableSnapshot:
        """Apply one packet and validate Item 139 against current Item 138 records."""
        uas = (
            packet
            if isinstance(packet, UASLocalSet)
            else decode_uas_local_set(packet, field_decoding=self._field_decoding)
        )
        timestamp = uas.value(2)
        if not isinstance(timestamp, datetime):
            raise DecodeError("ST 0601 payload state requires a typed timestamp")
        if self._observed_at is not None and timestamp < self._observed_at:
            raise DecodeError("ST 0601 payload-state timestamps must be monotonic")

        expired_ids = tuple(
            sorted(
                identifier
                for identifier, last_seen in self._record_seen.items()
                if timestamp - last_seen > self._refresh_period
            )
        )
        table_expired = (
            self._count_seen is not None
            and timestamp - self._count_seen > self._refresh_period
        )
        payloads = uas.value(138)
        incoming_records = payloads.records if isinstance(payloads, PayloadList) else ()
        incoming_count = payloads.total_count if isinstance(payloads, PayloadList) else None
        if incoming_count is not None and incoming_count > self._max_payload_records:
            raise LimitExceeded(
                f"ST 0601 Payload Count exceeds configured limit {self._max_payload_records}"
            )
        table_cleared = payloads is SpecialValue.UNKNOWN
        previous_count = None if table_expired or table_cleared else self._total_count
        table_restarted = (
            previous_count is not None
            and incoming_count is not None
            and incoming_count != previous_count
        )
        retained_ids = (
            set()
            if table_expired or table_cleared or table_restarted
            else set(self._records) - set(expired_ids)
        )
        candidate_ids = retained_ids | {record.payload_id for record in incoming_records}
        if len(candidate_ids) > self._max_payload_records:
            raise LimitExceeded(
                f"ST 0601 payload table exceeds configured limit {self._max_payload_records}"
            )

        active = uas.value(139)
        if isinstance(active, ActivePayloads) and (
            len(active.payload_ids) > self._max_payload_records
            or any(identifier >= self._max_payload_records for identifier in active.payload_ids)
        ):
            raise LimitExceeded(
                "ST 0601 Active Payload ID exceeds configured payload-table limit "
                f"{self._max_payload_records}"
            )
        active_expired = (
            self._active_seen is not None
            and timestamp - self._active_seen > self._refresh_period
        )

        if table_expired or table_cleared or table_restarted:
            self._records.clear()
            self._record_seen.clear()
            self._total_count = None
            self._count_seen = None
        else:
            for identifier in expired_ids:
                self._records.pop(identifier, None)
                del self._record_seen[identifier]
        if incoming_count is not None:
            self._total_count = incoming_count
            self._count_seen = timestamp
            for record in incoming_records:
                self._records[record.payload_id] = record
                self._record_seen[record.payload_id] = timestamp

        if active_expired:
            self._active_ids = None
            self._active_seen = None
        active_cleared = active is SpecialValue.UNKNOWN
        if isinstance(active, ActivePayloads):
            self._active_ids = active.payload_ids
            self._active_seen = timestamp
        elif active_cleared:
            self._active_ids = None
            self._active_seen = None
        self._observed_at = timestamp

        issues: list[PayloadTableIssue] = []
        if table_restarted:
            issues.append(
                PayloadTableIssue(
                    "payload_count_changed",
                    "ST 0601 Item 138 Payload Count changed within one current table; "
                    "receiver state started a new table generation",
                )
            )
        if self._active_ids is not None:
            undefined = tuple(sorted(self._active_ids - self._records.keys()))
            if undefined:
                issues.append(
                    PayloadTableIssue(
                        "undefined_active_payload",
                        "ST 0601.17-39 requires active Payload IDs to be defined within "
                        "the last 30 seconds",
                        undefined,
                    )
                )

        return PayloadTableSnapshot(
            timestamp,
            self._total_count,
            MappingProxyType(dict(sorted(self._records.items()))),
            self._active_ids,
            tuple(sorted(record.payload_id for record in incoming_records)),
            expired_ids,
            table_restarted,
            table_cleared,
            table_expired,
            active_cleared,
            active_expired,
            tuple(issues),
            uas.issues,
        )


@dataclass(frozen=True, slots=True)
class WeaponsStoresSnapshot:
    """Current receiver view of the distributed Item 140 stores list."""

    timestamp: datetime
    records: Mapping[tuple[int, int, int, int], WeaponStore]
    updated_addresses: tuple[tuple[int, int, int, int], ...]
    expired_addresses: tuple[tuple[int, int, int, int], ...]
    cleared: bool
    field_issues: tuple[FieldDecodingIssue, ...] = ()


class WeaponsStoresState:
    """Reconstruct distributed ST 0601 Item 140 receiver state.

    A weapon record's four-part physical address is its identity. Records sent
    in separate packets merge into one receiver list, updates replace the
    record at the same address, and each address expires independently.
    """

    def __init__(
        self,
        *,
        refresh_period: timedelta = timedelta(seconds=30),
        max_weapon_records: int = 4096,
        field_decoding: FieldDecodingMode = FieldDecodingMode.PRESERVE,
    ) -> None:
        if not isinstance(refresh_period, timedelta):
            raise TypeError("refresh_period must be a timedelta")
        if refresh_period <= timedelta(0):
            raise ValueError("refresh_period must be positive")
        if refresh_period > timedelta(seconds=30):
            raise ValueError("ST 0601 Item 140 refresh period cannot exceed 30 seconds")
        if (
            isinstance(max_weapon_records, bool)
            or not isinstance(max_weapon_records, int)
            or max_weapon_records < 1
        ):
            raise ValueError("max_weapon_records must be a positive integer")
        if not isinstance(field_decoding, FieldDecodingMode):
            raise TypeError("field_decoding must be a FieldDecodingMode")
        self._refresh_period = refresh_period
        self._max_weapon_records = max_weapon_records
        self._field_decoding = field_decoding
        self._observed_at: datetime | None = None
        self._records: dict[tuple[int, int, int, int], WeaponStore] = {}
        self._record_seen: dict[tuple[int, int, int, int], datetime] = {}

    @property
    def refresh_period(self) -> timedelta:
        return self._refresh_period

    @property
    def max_weapon_records(self) -> int:
        return self._max_weapon_records

    @property
    def field_decoding(self) -> FieldDecodingMode:
        return self._field_decoding

    def reset(self) -> None:
        """Clear all weapon records and accept a new stream timeline."""
        self._observed_at = None
        self._records.clear()
        self._record_seen.clear()

    def observe(
        self, packet: bytes | KLVPacket | UASLocalSet
    ) -> WeaponsStoresSnapshot:
        """Apply one packet and return the reconstructed Weapons Stores list."""
        uas = (
            packet
            if isinstance(packet, UASLocalSet)
            else decode_uas_local_set(packet, field_decoding=self._field_decoding)
        )
        timestamp = uas.value(2)
        if not isinstance(timestamp, datetime):
            raise DecodeError("ST 0601 weapon-store state requires a typed timestamp")
        if self._observed_at is not None and timestamp < self._observed_at:
            raise DecodeError("ST 0601 weapon-store timestamps must be monotonic")

        expired_addresses = tuple(
            sorted(
                address
                for address, last_seen in self._record_seen.items()
                if timestamp - last_seen > self._refresh_period
            )
        )
        stores = uas.value(140)
        incoming_records = stores.records if isinstance(stores, WeaponsStores) else ()
        cleared = stores is SpecialValue.UNKNOWN
        retained_addresses = (
            set()
            if cleared
            else set(self._records) - set(expired_addresses)
        )
        candidate_addresses = retained_addresses | {
            record.address for record in incoming_records
        }
        if len(candidate_addresses) > self._max_weapon_records:
            raise LimitExceeded(
                "ST 0601 weapon-store table exceeds configured limit "
                f"{self._max_weapon_records}"
            )

        for address in expired_addresses:
            self._records.pop(address, None)
            del self._record_seen[address]
        if cleared:
            self._records.clear()
            self._record_seen.clear()
        for record in incoming_records:
            self._records[record.address] = record
            self._record_seen[record.address] = timestamp
        self._observed_at = timestamp

        return WeaponsStoresSnapshot(
            timestamp,
            MappingProxyType(dict(sorted(self._records.items()))),
            tuple(sorted(record.address for record in incoming_records)),
            expired_addresses,
            cleared,
            uas.issues,
        )


@dataclass(frozen=True, slots=True)
class WaypointListIssue:
    """One cross-packet ST 0601 waypoint-list violation."""

    code: str
    message: str
    waypoint_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class WaypointListSnapshot:
    """Current distributed Item 141 waypoint list and derived order views."""

    timestamp: datetime
    records: Mapping[int, WaypointRecord]
    updated_ids: tuple[int, ...]
    expired_ids: tuple[int, ...]
    cleared: bool
    order_conflicts: Mapping[int, tuple[int, ...]]
    issues: tuple[WaypointListIssue, ...] = ()
    field_issues: tuple[FieldDecodingIssue, ...] = ()

    @property
    def current_ids(self) -> tuple[int, ...]:
        """Return all current IDs; distributed reordering may expose more than one."""
        return tuple(
            identifier
            for identifier, record in self.records.items()
            if record.prosecution_order == 0
        )

    @property
    def planned_ids(self) -> tuple[int, ...]:
        """Return planned IDs ordered by prosecution order and then ID."""
        return tuple(
            record.waypoint_id
            for record in sorted(
                (
                    record
                    for record in self.records.values()
                    if 0 < record.prosecution_order < 32767
                ),
                key=lambda record: (record.prosecution_order, record.waypoint_id),
            )
        )

    @property
    def historical_ids(self) -> tuple[int, ...]:
        """Return historical IDs with the most recently assigned order first."""
        return tuple(
            record.waypoint_id
            for record in sorted(
                (
                    record
                    for record in self.records.values()
                    if record.prosecution_order < 0
                ),
                key=lambda record: (record.prosecution_order, record.waypoint_id),
            )
        )

    @property
    def cancelled_ids(self) -> tuple[int, ...]:
        """Return IDs carrying the repeatable 0x7FFF cancellation marker."""
        return tuple(
            identifier
            for identifier, record in self.records.items()
            if record.prosecution_order == 32767
        )


class WaypointListState:
    """Reconstruct distributed ST 0601 Item 141 receiver state by Waypoint ID."""

    def __init__(
        self,
        *,
        refresh_period: timedelta = timedelta(seconds=30),
        max_waypoint_records: int = 4096,
        field_decoding: FieldDecodingMode = FieldDecodingMode.PRESERVE,
    ) -> None:
        if not isinstance(refresh_period, timedelta):
            raise TypeError("refresh_period must be a timedelta")
        if refresh_period <= timedelta(0):
            raise ValueError("refresh_period must be positive")
        if refresh_period > timedelta(seconds=30):
            raise ValueError("ST 0601 Item 141 refresh period cannot exceed 30 seconds")
        if (
            isinstance(max_waypoint_records, bool)
            or not isinstance(max_waypoint_records, int)
            or max_waypoint_records < 1
        ):
            raise ValueError("max_waypoint_records must be a positive integer")
        if not isinstance(field_decoding, FieldDecodingMode):
            raise TypeError("field_decoding must be a FieldDecodingMode")
        self._refresh_period = refresh_period
        self._max_waypoint_records = max_waypoint_records
        self._field_decoding = field_decoding
        self._observed_at: datetime | None = None
        self._records: dict[int, WaypointRecord] = {}
        self._record_seen: dict[int, datetime] = {}

    @property
    def refresh_period(self) -> timedelta:
        return self._refresh_period

    @property
    def max_waypoint_records(self) -> int:
        return self._max_waypoint_records

    @property
    def field_decoding(self) -> FieldDecodingMode:
        return self._field_decoding

    def reset(self) -> None:
        """Clear all waypoints and accept a new stream timeline."""
        self._observed_at = None
        self._records.clear()
        self._record_seen.clear()

    def observe(
        self, packet: bytes | KLVPacket | UASLocalSet
    ) -> WaypointListSnapshot:
        """Apply one packet and return the reconstructed Waypoint List."""
        uas = (
            packet
            if isinstance(packet, UASLocalSet)
            else decode_uas_local_set(packet, field_decoding=self._field_decoding)
        )
        timestamp = uas.value(2)
        if not isinstance(timestamp, datetime):
            raise DecodeError("ST 0601 waypoint state requires a typed timestamp")
        if self._observed_at is not None and timestamp < self._observed_at:
            raise DecodeError("ST 0601 waypoint-state timestamps must be monotonic")

        expired_ids = tuple(
            sorted(
                identifier
                for identifier, last_seen in self._record_seen.items()
                if timestamp - last_seen > self._refresh_period
            )
        )
        waypoints = uas.value(141)
        incoming_records = waypoints.records if isinstance(waypoints, WaypointList) else ()
        cleared = waypoints is SpecialValue.UNKNOWN
        candidate_records = (
            {}
            if cleared
            else {
                identifier: record
                for identifier, record in self._records.items()
                if identifier not in expired_ids
            }
        )
        previous_records = dict(candidate_records)
        candidate_records.update(
            (record.waypoint_id, record) for record in incoming_records
        )
        if len(candidate_records) > self._max_waypoint_records:
            raise LimitExceeded(
                "ST 0601 waypoint list exceeds configured limit "
                f"{self._max_waypoint_records}"
            )

        newly_historical = tuple(
            record
            for record in incoming_records
            if record.prosecution_order < 0
            and (
                record.waypoint_id not in previous_records
                or previous_records[record.waypoint_id].prosecution_order >= 0
            )
        )
        new_historical_ids = {record.waypoint_id for record in newly_historical}
        prior_historical_orders = tuple(
            record.prosecution_order
            for identifier, record in candidate_records.items()
            if record.prosecution_order < 0 and identifier not in new_historical_ids
        )
        historical_violations = (
            tuple(
                sorted(
                    record.waypoint_id
                    for record in newly_historical
                    if record.prosecution_order >= min(prior_historical_orders)
                )
            )
            if prior_historical_orders
            else ()
        )

        self._records = candidate_records
        if cleared:
            self._record_seen.clear()
        else:
            for identifier in expired_ids:
                self._record_seen.pop(identifier, None)
            for record in incoming_records:
                self._record_seen[record.waypoint_id] = timestamp
        self._observed_at = timestamp

        ids_by_order: dict[int, list[int]] = {}
        for identifier, record in self._records.items():
            if record.prosecution_order != 32767:
                ids_by_order.setdefault(record.prosecution_order, []).append(identifier)
        conflicts = {
            order: tuple(sorted(identifiers))
            for order, identifiers in sorted(ids_by_order.items())
            if len(identifiers) > 1
        }
        issues = (
            (
                WaypointListIssue(
                    "historical_order_not_decreasing",
                    "ST 0601.17-40 requires each new historical waypoint to use an "
                    "order below the most-negative current historical order",
                    historical_violations,
                ),
            )
            if historical_violations
            else ()
        )
        return WaypointListSnapshot(
            timestamp,
            MappingProxyType(dict(sorted(self._records.items()))),
            tuple(sorted(record.waypoint_id for record in incoming_records)),
            expired_ids,
            cleared,
            MappingProxyType(conflicts),
            issues,
            uas.issues,
        )
