"""MISB ST 0902.8 minimum-metadata stream profile validation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from types import MappingProxyType

from stanag4609.errors import DecodeError
from stanag4609.klv.model import KLVPacket
from stanag4609.st0102 import SecurityClassification, SecurityLocalSet
from stanag4609.st0601 import (
    DecodedField,
    FieldDecodingIssue,
    FieldDecodingMode,
    UASLocalSet,
    decode_uas_local_set,
)


@dataclass(frozen=True, slots=True)
class MISMMSValidationIssue:
    """One ST 0902.8 profile violation observed in a metadata stream."""

    code: str
    requirement: str
    message: str
    tags: tuple[int, ...]


class MISMMSPopulationStatus(str, Enum):
    """End-of-observation population state for one ST 0902 requirement."""

    CURRENT = "current"
    MISSING = "missing"
    OVERDUE = "overdue"


@dataclass(frozen=True, slots=True)
class MISMMSRequirementCoverage:
    """Population timing for one selected ST 0902 minimum-item group."""

    requirement: str
    tags: tuple[int, ...]
    status: MISMMSPopulationStatus
    last_seen: datetime | None
    age_seconds: float | None
    parent_tag: int | None = None

    @property
    def tag_paths(self) -> tuple[tuple[int, ...], ...]:
        """Return each root or nested Local Set path satisfying the group."""

        if self.parent_tag is None:
            return tuple((tag,) for tag in self.tags)
        return tuple((self.parent_tag, tag) for tag in self.tags)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible coverage entry."""

        return {
            "requirement": self.requirement,
            "tags": list(self.tags),
            "status": self.status.value,
            "last_seen": None if self.last_seen is None else self.last_seen.isoformat(),
            "age_seconds": self.age_seconds,
            "tag_paths": [list(path) for path in self.tag_paths],
        }


@dataclass(frozen=True, slots=True)
class MISMMSecurityContext:
    """Declare which context-dependent ST 0102 markings apply to a stream.

    ST 0902 Table 1 includes Security Local Set Items 4, 5, and 6, while
    ST 0102 makes each mandatory only when the corresponding marking applies.
    The default therefore requires the six unconditional Security sub-items
    without inventing mission-specific security policy.
    """

    sci_shi: bool = False
    caveats: bool = False
    releasing_instructions: bool = False
    expected_classification: SecurityClassification | None = None
    expected_classifying_country: str | None = None
    expected_sci_shi: str | None = None
    expected_caveats: str | None = None
    required_releasing_countries: frozenset[str] = frozenset()
    required_object_countries: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, bool)
            for value in (self.sci_shi, self.caveats, self.releasing_instructions)
        ):
            raise TypeError("MISMMS security-context fields must be booleans")
        if self.expected_classification is not None and not isinstance(
            self.expected_classification, SecurityClassification
        ):
            raise TypeError("expected_classification must be a SecurityClassification or None")
        for name in ("expected_sci_shi", "expected_caveats"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise TypeError(f"{name} must be a non-empty string or None")
        if self.expected_classifying_country is not None:
            _validate_policy_country(
                self.expected_classifying_country,
                name="expected_classifying_country",
            )
        for name in ("required_releasing_countries", "required_object_countries"):
            values = getattr(self, name)
            if not isinstance(values, frozenset) or not all(
                isinstance(value, str) and value for value in values
            ):
                raise TypeError(f"{name} must be a frozenset of non-empty strings")
            for value in values:
                _validate_policy_country(value, name=name)
        if self.expected_sci_shi is not None and (
            not self.expected_sci_shi.endswith("//")
            or any(not entry for entry in self.expected_sci_shi[:-2].split("/"))
        ):
            raise ValueError("expected_sci_shi must use ST 0102 slash framing")
        if self.expected_caveats is not None and not _is_printable_ascii(
            self.expected_caveats
        ):
            raise ValueError("expected_caveats must be printable ASCII")

    @property
    def required_tags(self) -> tuple[int, ...]:
        """Return applicable context-dependent ST 0102 Local Set tags."""
        return tuple(sorted({
            tag
            for tag, required in (
                (4, self.sci_shi),
                (4, self.expected_sci_shi is not None),
                (5, self.caveats),
                (5, self.expected_caveats is not None),
                (6, self.releasing_instructions),
                (6, bool(self.required_releasing_countries)),
            )
            if required
        }))

    @property
    def has_policy(self) -> bool:
        """Return whether any caller-supplied marking constraint is active."""

        return any(
            (
                self.required_tags,
                self.expected_classification is not None,
                self.expected_classifying_country is not None,
                bool(self.required_object_countries),
            )
        )


def _is_printable_ascii(value: str) -> bool:
    return value.isascii() and all(" " <= character <= "~" for character in value)


def _validate_policy_country(value: object, *, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise TypeError(f"{name} must contain non-empty strings")
    if (
        not _is_printable_ascii(value)
        or value != value.upper()
        or any(character.isspace() or character in "/;" for character in value)
    ):
        raise ValueError(
            f"{name} country codes must be uppercase ASCII without separators"
        )


@dataclass(frozen=True, slots=True)
class _RequirementGroup:
    name: str
    tags: tuple[int, ...]
    parent_tag: int | None = None


_BASE_REQUIREMENTS = (
    _RequirementGroup("checksum", (1,)),
    _RequirementGroup("precision_timestamp", (2,)),
    _RequirementGroup("mission_id", (3,)),
    _RequirementGroup("platform_heading", (5,)),
    _RequirementGroup("platform_pitch", (6, 90)),
    _RequirementGroup("platform_roll", (7, 91)),
    _RequirementGroup("platform_designation", (10,)),
    _RequirementGroup("image_source_sensor", (11,)),
    _RequirementGroup("image_coordinate_system", (12,)),
    _RequirementGroup("sensor_latitude", (13,)),
    _RequirementGroup("sensor_longitude", (14,)),
    _RequirementGroup("sensor_altitude", (15, 75, 104)),
    _RequirementGroup("sensor_horizontal_fov", (16,)),
    _RequirementGroup("sensor_vertical_fov", (17,)),
    _RequirementGroup("sensor_relative_azimuth", (18,)),
    _RequirementGroup("sensor_relative_elevation", (19,)),
    _RequirementGroup("sensor_relative_roll", (20,)),
    _RequirementGroup("slant_range", (21,)),
    _RequirementGroup("target_width", (22, 96)),
    _RequirementGroup("frame_center_latitude", (23,)),
    _RequirementGroup("frame_center_longitude", (24,)),
    _RequirementGroup("frame_center_elevation", (25, 78)),
    _RequirementGroup("uas_local_set_version", (65,)),
)

_SECURITY_CORE_TAGS = (1, 2, 3, 12, 13, 22)

_SECURITY_REQUIREMENTS = {
    1: _RequirementGroup("security_classification", (1,), 48),
    2: _RequirementGroup("security_country_coding_method", (2,), 48),
    3: _RequirementGroup("security_classifying_country", (3,), 48),
    4: _RequirementGroup("security_sci_shi_information", (4,), 48),
    5: _RequirementGroup("security_caveats", (5,), 48),
    6: _RequirementGroup("security_releasing_instructions", (6,), 48),
    12: _RequirementGroup("security_object_country_coding_method", (12,), 48),
    13: _RequirementGroup("security_object_country_codes", (13,), 48),
    22: _RequirementGroup("security_metadata_version", (22,), 48),
}


def _security_value(fields: Mapping[int, object], tag: int) -> object | None:
    value = fields.get(tag)
    return getattr(value, "value", value)


def _security_policy_issues(
    fields: Mapping[int, object],
    context: MISMMSecurityContext,
) -> tuple[MISMMSValidationIssue, ...]:
    issues: list[MISMMSValidationIssue] = []

    def mismatch(requirement: str, tag: int, message: str) -> None:
        issues.append(
            MISMMSValidationIssue(
                "security_policy",
                requirement,
                message,
                (tag,),
            )
        )

    classification = _security_value(fields, 1)
    if (
        classification is not None
        and context.expected_classification is not None
        and classification != context.expected_classification
    ):
        mismatch(
            "security_classification",
            1,
            "ST 0102 Security Classification does not match caller-supplied "
            f"policy: expected {context.expected_classification.name}",
        )

    classifying_country = _security_value(fields, 3)
    if (
        isinstance(classifying_country, str)
        and context.expected_classifying_country is not None
        and classifying_country.removeprefix("//")
        != context.expected_classifying_country
    ):
        mismatch(
            "security_classifying_country",
            3,
            "ST 0102 Classifying Country does not match caller-supplied policy: "
            f"expected {context.expected_classifying_country}",
        )

    sci_shi = _security_value(fields, 4)
    if (
        isinstance(sci_shi, str)
        and context.expected_sci_shi is not None
        and sci_shi != context.expected_sci_shi
    ):
        mismatch(
            "security_sci_shi_information",
            4,
            "ST 0102 SCI/SHI Information does not match caller-supplied policy",
        )

    caveats = _security_value(fields, 5)
    if (
        isinstance(caveats, str)
        and context.expected_caveats is not None
        and caveats != context.expected_caveats
    ):
        mismatch(
            "security_caveats",
            5,
            "ST 0102 Caveats do not match caller-supplied policy",
        )

    releasing = _security_value(fields, 6)
    if isinstance(releasing, str) and not context.required_releasing_countries.issubset(
        releasing.split(" ")
    ):
        missing = sorted(context.required_releasing_countries - set(releasing.split(" ")))
        mismatch(
            "security_releasing_instructions",
            6,
            "ST 0102 Releasing Instructions omit caller-required countries: "
            + ", ".join(missing),
        )

    object_countries = _security_value(fields, 13)
    if isinstance(
        object_countries, str
    ) and not context.required_object_countries.issubset(object_countries.split(";")):
        missing = sorted(
            context.required_object_countries - set(object_countries.split(";"))
        )
        mismatch(
            "security_object_country_codes",
            13,
            "ST 0102 Object Country Codes omit caller-required countries: "
            + ", ".join(missing),
        )
    return tuple(issues)


def _selected_requirement_groups(
    *,
    require_security: bool,
    require_miis: bool,
    security_context: MISMMSecurityContext,
) -> tuple[_RequirementGroup, ...]:
    selected = list(_BASE_REQUIREMENTS)
    if require_security:
        selected.extend(_SECURITY_REQUIREMENTS[tag] for tag in _SECURITY_CORE_TAGS)
        selected.extend(
            _SECURITY_REQUIREMENTS[tag] for tag in security_context.required_tags
        )
    if require_miis:
        selected.append(_RequirementGroup("miis_core_identifier", (94,)))
    return tuple(selected)


def validate_mismms_current_state(
    fields: Iterable[DecodedField] | UASLocalSet,
    *,
    field_issues: Iterable[FieldDecodingIssue] = (),
    require_security: bool = True,
    require_miis: bool = True,
    security_context: MISMMSecurityContext | None = None,
    effective_security: Mapping[int, object] | None = None,
) -> tuple[MISMMSValidationIssue, ...]:
    """Validate a reconstructed current ST 0902 minimum-metadata view.

    A :class:`UASLocalSet` carries its preserve-mode diagnostics automatically.
    When passing a reconstructed field iterable, also pass the corresponding
    receiver-state ``field_issues``. Malformed known fields are intentionally
    absent from the decoded field collection; retaining their diagnostics
    distinguishes invalid population from an item that was never populated.
    """
    if not isinstance(require_security, bool) or not isinstance(require_miis, bool):
        raise TypeError("require_security and require_miis must be booleans")
    if security_context is None:
        security_context = MISMMSecurityContext()
    if not isinstance(security_context, MISMMSecurityContext):
        raise TypeError("security_context must be a MISMMSecurityContext")
    if not require_security and security_context.has_policy:
        raise ValueError("security_context cannot require fields when require_security=False")
    if isinstance(fields, UASLocalSet):
        current = fields.fields
        source_field_issues = fields.issues
    else:
        current = tuple(fields)
        source_field_issues = ()
    current_field_issues = (*source_field_issues, *tuple(field_issues))
    if not all(
        isinstance(issue, FieldDecodingIssue) for issue in current_field_issues
    ):
        raise TypeError("field_issues must contain FieldDecodingIssue values")
    selected = list(_BASE_REQUIREMENTS)
    if require_security:
        selected.append(_RequirementGroup("security", (48,)))
    if require_miis:
        selected.append(_RequirementGroup("miis_core_identifier", (94,)))
    by_tag = {
        tag: tuple(field for field in current if field.definition.tag == tag)
        for tag in {tag for requirement in selected for tag in requirement.tags}
    }
    issues: list[MISMMSValidationIssue] = []
    for requirement in selected:
        invalid_tags = tuple(
            sorted(
                {
                    issue.tag
                    for issue in current_field_issues
                    if issue.tag in requirement.tags
                }
            )
        )
        if invalid_tags:
            issues.append(
                MISMMSValidationIssue(
                    "invalid_field",
                    requirement.name,
                    "malformed ST 0601 field cannot meet the "
                    f"{requirement.name} reporting requirement",
                    invalid_tags,
                )
            )
        elif not any(
            field.raw for tag in requirement.tags for field in by_tag[tag]
        ):
            issues.append(
                MISMMSValidationIssue(
                    "missing",
                    requirement.name,
                    f"ST 0902.3-03 requires the {requirement.name} item to be populated",
                    requirement.tags,
                )
            )
    if any(field.raw for field in by_tag.get(75, ())) and any(
        field.raw for field in by_tag.get(104, ())
    ):
        issues.append(
            MISMMSValidationIssue(
                "exclusive_or",
                "sensor_altitude",
                "ST 0902.8 Note 1 makes Items 75 and 104 mutually exclusive",
                (75, 104),
            )
        )
    if require_security and any(field.raw for field in by_tag.get(48, ())):
        if effective_security is None:
            security_value = next(
                (
                    field.value
                    for field in by_tag.get(48, ())
                    if isinstance(field.value, SecurityLocalSet)
                ),
                None,
            )
            if isinstance(security_value, SecurityLocalSet):
                effective_security = {
                    field.tag: field for field in security_value.fields
                }
        missing_security = tuple(
            tag
            for tag in _SECURITY_CORE_TAGS + security_context.required_tags
            if effective_security is None
            or tag not in effective_security
            or not getattr(effective_security[tag], "raw", b"")
        )
        if missing_security:
            issues.append(
                MISMMSValidationIssue(
                    "security_subitem",
                    "security",
                    "ST 0902.8 Table 1 Security sub-items are missing or Unknown",
                    missing_security,
                )
            )
        if effective_security is not None:
            issues.extend(_security_policy_issues(effective_security, security_context))
    return tuple(issues)


class MISMMSValidator:
    """Validate ST 0902.8 minimum-item reporting across a timed packet stream.

    Required fields may be spread across packets. A requirement becomes overdue
    only after more than the configured interval has elapsed, matching the
    standard's inclusive 30-second reporting window.
    """

    def __init__(
        self,
        *,
        require_security: bool = True,
        require_miis: bool = True,
        security_context: MISMMSecurityContext | None = None,
        maximum_interval: timedelta = timedelta(seconds=30),
        field_decoding: FieldDecodingMode = FieldDecodingMode.PRESERVE,
    ) -> None:
        if not isinstance(require_security, bool) or not isinstance(require_miis, bool):
            raise TypeError("require_security and require_miis must be booleans")
        if security_context is None:
            security_context = MISMMSecurityContext()
        if not isinstance(security_context, MISMMSecurityContext):
            raise TypeError("security_context must be a MISMMSecurityContext")
        if not require_security and security_context.has_policy:
            raise ValueError("security_context cannot require fields when require_security=False")
        if not isinstance(maximum_interval, timedelta):
            raise TypeError("maximum_interval must be a timedelta")
        if maximum_interval <= timedelta(0):
            raise ValueError("maximum_interval must be positive")
        if not isinstance(field_decoding, FieldDecodingMode):
            raise TypeError("field_decoding must be a FieldDecodingMode")
        self._requirements = _selected_requirement_groups(
            require_security=require_security,
            require_miis=require_miis,
            security_context=security_context,
        )
        self._security_context = security_context
        self._maximum_interval = maximum_interval
        self._field_decoding = field_decoding
        self._started_at: datetime | None = None
        self._observed_at: datetime | None = None
        self._last_seen: dict[str, datetime] = {}
        self._sensor_altitude_seen: dict[int, datetime] = {}

    @property
    def last_seen(self) -> Mapping[str, datetime]:
        """Return an immutable snapshot of last valid observations by requirement."""
        return MappingProxyType(dict(self._last_seen))

    @property
    def maximum_interval(self) -> timedelta:
        return self._maximum_interval

    @property
    def security_context(self) -> MISMMSecurityContext:
        return self._security_context

    @property
    def field_decoding(self) -> FieldDecodingMode:
        return self._field_decoding

    def coverage(
        self,
        at: datetime | None = None,
    ) -> tuple[MISMMSRequirementCoverage, ...]:
        """Return the selected profile's population state at one stream time.

        This is an end-state inventory. Historical violations remain available
        from :meth:`observe` and :meth:`finish` even if a later value is current.
        """

        if at is not None and not isinstance(at, datetime):
            raise TypeError("coverage time must be a datetime")
        reference = self._observed_at if at is None else at
        if (
            reference is not None
            and self._observed_at is not None
            and reference < self._observed_at
        ):
            raise DecodeError("coverage time cannot be before the last observation")
        coverage: list[MISMMSRequirementCoverage] = []
        for requirement in self._requirements:
            last_seen = self._last_seen.get(requirement.name)
            age = None if last_seen is None or reference is None else reference - last_seen
            if last_seen is None:
                status = MISMMSPopulationStatus.MISSING
            elif age is not None and age > self._maximum_interval:
                status = MISMMSPopulationStatus.OVERDUE
            else:
                status = MISMMSPopulationStatus.CURRENT
            coverage.append(
                MISMMSRequirementCoverage(
                    requirement.name,
                    requirement.tags,
                    status,
                    last_seen,
                    None if age is None else age.total_seconds(),
                    requirement.parent_tag,
                )
            )
        return tuple(coverage)

    def reset(self) -> None:
        """Reset all stream state so the validator can observe a new stream."""
        self._started_at = None
        self._observed_at = None
        self._last_seen.clear()
        self._sensor_altitude_seen.clear()

    def observe(
        self, packet: bytes | KLVPacket | UASLocalSet
    ) -> tuple[MISMMSValidationIssue, ...]:
        """Observe one packet and return profile issues at its timestamp.

        A supplied :class:`UASLocalSet` is decoded again from its retained wire
        packet.  This prevents a value originally decoded with relaxed packet
        checks from bypassing the ST 0107/ST 0601 conformance required by
        ST 0902.3-01 and ST 0902.3-03.
        """
        source = packet.packet if isinstance(packet, UASLocalSet) else packet
        uas = decode_uas_local_set(source, field_decoding=self._field_decoding)
        timestamp = uas.value(2)
        if not isinstance(timestamp, datetime):
            raise DecodeError("ST 0902 packet requires a typed Precision Time Stamp")
        if self._observed_at is not None and timestamp < self._observed_at:
            raise DecodeError("ST 0902 packet timestamps must be monotonic")
        if self._started_at is None:
            self._started_at = timestamp
        self._observed_at = timestamp

        issues: list[MISMMSValidationIssue] = []
        invalid_tags = {issue.tag for issue in uas.issues}
        security_sets = tuple(
            field.value
            for field in uas.getall(48)
            if isinstance(field.value, SecurityLocalSet)
        )
        if any(not item.value for item in uas.local_set.getall(48)):
            issues.append(
                MISMMSValidationIssue(
                    "zero_length",
                    "security",
                    "ST 0902.8-05 forbids using a zero-length Security Local Set "
                    "to meet nested minimum reporting requirements",
                    (48,),
                )
            )
        for security in security_sets:
            issues.extend(
                _security_policy_issues(
                    {field.tag: field for field in security.fields},
                    self._security_context,
                )
            )
        for requirement in self._requirements:
            if requirement.parent_tag is None:
                items = tuple(
                    item
                    for tag in requirement.tags
                    for item in uas.local_set.getall(tag)
                )
                invalid_requirement_tags = tuple(
                    sorted(set(requirement.tags) & invalid_tags)
                )
            else:
                items = tuple(
                    item
                    for security in security_sets
                    for tag in requirement.tags
                    for item in security.local_set.getall(tag)
                )
                invalid_requirement_tags = (
                    requirement.tags if requirement.parent_tag in invalid_tags else ()
                )
            populated_tags = tuple(
                sorted(
                    {
                        item.tag
                        for item in items
                        if item.value
                        and (
                            requirement.parent_tag is not None
                            or item.tag not in invalid_tags
                        )
                    }
                )
            )
            if invalid_requirement_tags:
                location = (
                    ""
                    if requirement.parent_tag is None
                    else f" in ST 0601 Item {requirement.parent_tag}"
                )
                issues.append(
                    MISMMSValidationIssue(
                        "invalid_field",
                        requirement.name,
                        "malformed field"
                        f"{location} cannot meet the {requirement.name} reporting requirement",
                        invalid_requirement_tags,
                    )
                )
            if not populated_tags and any(not item.value for item in items):
                zli_tags = tuple(sorted({item.tag for item in items if not item.value}))
                issues.append(
                    MISMMSValidationIssue(
                        "zero_length",
                        requirement.name,
                        "ST 0902.8-05 forbids using a zero-length item to meet "
                        f"the {requirement.name} reporting requirement",
                        zli_tags,
                    )
                )

            previous = self._last_seen.get(requirement.name, self._started_at)
            assert previous is not None
            if timestamp - previous > self._maximum_interval:
                issues.append(
                    MISMMSValidationIssue(
                        "overdue",
                        requirement.name,
                        f"ST 0902.3-04 reporting interval exceeded "
                        f"{self._maximum_interval.total_seconds():g} seconds",
                        requirement.tags,
                    )
                )
            if populated_tags:
                self._last_seen[requirement.name] = timestamp

        for tag in (75, 104):
            items = uas.local_set.getall(tag)
            if tag not in invalid_tags and any(item.value for item in items):
                self._sensor_altitude_seen[tag] = timestamp
            elif any(not item.value for item in items):
                self._sensor_altitude_seen.pop(tag, None)
        self._sensor_altitude_seen = {
            tag: seen
            for tag, seen in self._sensor_altitude_seen.items()
            if timestamp - seen <= self._maximum_interval
        }
        if self._sensor_altitude_seen.keys() == {75, 104}:
            issues.append(
                MISMMSValidationIssue(
                    "exclusive_or",
                    "sensor_altitude",
                    "ST 0902.8 Note 1 makes current Items 75 and 104 mutually exclusive",
                    (75, 104),
                )
            )
        return tuple(issues)

    def finish(self, at: datetime | None = None) -> tuple[MISMMSValidationIssue, ...]:
        """Finalize a finite stream and report missing or trailing-overdue items.

        ``observe`` reports cadence violations as packets arrive. ``finish`` also
        identifies requirements that never appeared, which cannot otherwise be
        diagnosed for recordings shorter than the reporting interval. Supplying
        ``at`` accounts for elapsed time after the final observed packet.
        """
        if at is not None and not isinstance(at, datetime):
            raise TypeError("at must be a datetime or None")
        if self._observed_at is not None:
            if at is None:
                at = self._observed_at
            elif at < self._observed_at:
                raise DecodeError("finish time cannot be before the last observation")

        issues: list[MISMMSValidationIssue] = []
        for requirement in self._requirements:
            previous = self._last_seen.get(requirement.name)
            if previous is None:
                issues.append(
                    MISMMSValidationIssue(
                        "missing",
                        requirement.name,
                        f"ST 0902.3-03 requires the {requirement.name} item to be populated",
                        requirement.tags,
                    )
                )
            elif at is not None and at - previous > self._maximum_interval:
                issues.append(
                    MISMMSValidationIssue(
                        "overdue",
                        requirement.name,
                        f"ST 0902.3-04 reporting interval exceeded "
                        f"{self._maximum_interval.total_seconds():g} seconds at stream end",
                        requirement.tags,
                    )
                )
        return tuple(issues)
