"""Stateful ST 1607 Segment and Amend hierarchy evaluation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from types import MappingProxyType
from typing import Any

from stanag4609.errors import DecodeError, LimitExceeded
from stanag4609.klv.model import KLVPacket
from stanag4609.st0102 import SecurityField, SecurityLocalSet
from stanag4609.st0601 import (
    DELETE,
    AmendLocalSet,
    DecodedField,
    FieldDecodingIssue,
    FieldDecodingMode,
    MetadataSubstreamID,
    SegmentLocalSet,
    SpecialValue,
    UASLocalSet,
    decode_uas_local_set,
)
from stanag4609.st0601_state import ReportOnChangeSnapshot, ReportOnChangeState
from stanag4609.st0902 import (
    MISMMSecurityContext,
    validate_mismms_current_state,
)
from stanag4609.st1204 import MIISCoreIdentifier
from stanag4609.st1601 import GeoRegistrationLocalSet
from stanag4609.st1602 import CompositeImagingLocalSet

MetadataSubstreamPath = tuple[MetadataSubstreamID, ...]


class MetadataBranchKind(str, Enum):
    """ST 1607 child operation represented by a metadata branch."""

    SEGMENT = "segment"
    AMEND = "amend"


@dataclass(frozen=True, slots=True)
class MetadataBranchSnapshot:
    """Current Report-on-Change state for one identified metadata branch."""

    path: MetadataSubstreamPath
    kind: MetadataBranchKind
    fields: tuple[DecodedField, ...]
    updated_tags: tuple[int, ...]
    cleared_tags: tuple[int, ...]
    expired_tags: tuple[int, ...]
    field_issues: tuple[FieldDecodingIssue, ...] = ()

    @property
    def parent_path(self) -> MetadataSubstreamPath:
        return self.path[:-1]

    @property
    def deleted_tags(self) -> tuple[int, ...]:
        return tuple(
            field.definition.tag for field in self.fields if field.value is DELETE
        )

    def getall(self, tag: int) -> tuple[DecodedField, ...]:
        return tuple(field for field in self.fields if field.definition.tag == tag)

    def get(self, tag: int) -> DecodedField | None:
        matches = self.getall(tag)
        if not matches:
            return None
        if len(matches) > 1:
            raise ValueError(f"tag {tag} occurs {len(matches)} times in branch state")
        return matches[0]

    def value(self, tag: int, default: Any = None) -> Any:
        field = self.get(tag)
        return default if field is None else field.value


@dataclass(frozen=True, slots=True)
class MetadataTreeSnapshot:
    """Receiver-visible root and identified ST 1607 branch hierarchy."""

    timestamp: datetime
    root: ReportOnChangeSnapshot
    branches: Mapping[MetadataSubstreamPath, MetadataBranchSnapshot]
    expired_paths: tuple[MetadataSubstreamPath, ...]

    def effective_fields(
        self, path: MetadataSubstreamPath = ()
    ) -> tuple[DecodedField, ...]:
        """Apply each Segment/Amend overlay along ``path`` to root state."""
        if path and path not in self.branches:
            raise KeyError(path)
        fields: dict[int, tuple[DecodedField, ...]] = {}
        for field in self.root.fields:
            if field.definition.tag not in {100, 101}:
                fields.setdefault(field.definition.tag, ())
                fields[field.definition.tag] += (field,)
        for depth in range(1, len(path) + 1):
            branch = self.branches[path[:depth]]
            grouped: dict[int, list[DecodedField]] = {}
            for field in branch.fields:
                grouped.setdefault(field.definition.tag, []).append(field)
            for tag, values in grouped.items():
                if any(field.value is DELETE for field in values):
                    fields.pop(tag, None)
                else:
                    fields[tag] = tuple(values)
        return tuple(field for tag in sorted(fields) for field in fields[tag])

    def effective_value(
        self, path: MetadataSubstreamPath, tag: int, default: Any = None
    ) -> Any:
        matches = tuple(
            field for field in self.effective_fields(path) if field.definition.tag == tag
        )
        if not matches:
            return default
        if len(matches) > 1:
            raise ValueError(f"tag {tag} occurs {len(matches)} times in effective state")
        return matches[0].value

    def effective_composite_timestamp(self, path: MetadataSubstreamPath) -> datetime:
        """Resolve ST 1602 Item 1, inheriting the parent timestamp when omitted."""
        try:
            branch = self.branches[path]
        except KeyError:
            raise KeyError(path) from None
        composite = branch.value(99)
        if not isinstance(composite, CompositeImagingLocalSet):
            raise ValueError(f"metadata substream {path!r} does not carry Item 99")
        if composite.timestamp is not None:
            return composite.timestamp
        parent_timestamp = self.effective_value(branch.parent_path, 2)
        if not isinstance(parent_timestamp, datetime):
            raise ValueError("composite parent has no typed Precision Time Stamp")
        return parent_timestamp

    def effective_geo_registration(
        self, path: MetadataSubstreamPath = ()
    ) -> GeoRegistrationLocalSet | None:
        """Resolve the ST 1601 result effective at an ST 1607 branch path.

        Parallel Amend Local Sets carry distinct geo-registration results under
        their Metadata Substream Identifier paths. Report-on-Change inheritance
        and deletion are applied before returning the typed Item 98 value.
        """
        value = self.effective_value(path, 98)
        if value is None:
            return None
        if not isinstance(value, GeoRegistrationLocalSet):
            raise ValueError(
                f"metadata substream {path!r} has no typed ST 1601 Item 98"
            )
        return value

    def effective_security(
        self, path: MetadataSubstreamPath = ()
    ) -> Mapping[int, SecurityField] | None:
        """Return root security with branch-specific country fields overlaid."""
        if path and path not in self.branches:
            raise KeyError(path)
        security: dict[int, SecurityField] | None = None
        root_value = self.root.value(48)
        if isinstance(root_value, SecurityLocalSet):
            security = {field.tag: field for field in root_value.fields}
        for depth in range(1, len(path) + 1):
            value = self.branches[path[:depth]].value(48)
            if value is DELETE:
                security = None
            elif isinstance(value, SecurityLocalSet):
                if security is None:
                    security = {}
                security.update((field.tag, field) for field in value.fields)
        return None if security is None else MappingProxyType(security)


@dataclass(frozen=True, slots=True)
class ST1607PolicyIssue:
    """One ST 1607 hierarchy policy violation."""

    code: str
    requirement: str
    message: str
    path: MetadataSubstreamPath
    tags: tuple[int, ...] = ()


def validate_st1602_composite(
    snapshot: MetadataTreeSnapshot,
) -> tuple[ST1607PolicyIssue, ...]:
    """Validate cross-segment ST 1602 composite-image requirements."""
    if not isinstance(snapshot, MetadataTreeSnapshot):
        raise TypeError("snapshot must be a MetadataTreeSnapshot")

    by_parent: dict[
        MetadataSubstreamPath,
        dict[int, list[MetadataSubstreamPath]],
    ] = {}
    for path, branch in snapshot.branches.items():
        if branch.kind is not MetadataBranchKind.SEGMENT:
            continue
        composite = branch.value(99)
        if isinstance(composite, CompositeImagingLocalSet):
            by_parent.setdefault(branch.parent_path, {}).setdefault(
                composite.z_order, []
            ).append(path)

    issues: list[ST1607PolicyIssue] = []
    composite_paths = sorted(
        (
            path
            for groups in by_parent.values()
            for paths in groups.values()
            for path in paths
        ),
        key=_path_sort_key,
    )
    for parent_path in sorted(by_parent, key=_path_sort_key):
        for z_order in sorted(by_parent[parent_path]):
            paths = sorted(by_parent[parent_path][z_order], key=_path_sort_key)
            if len(paths) < 2:
                continue
            for path in paths:
                issues.append(
                    ST1607PolicyIssue(
                        "duplicate_composite_z_order",
                        "ST 1602-04",
                        f"Z-order {z_order} is shared by sibling sub-images",
                        path,
                        (99,),
                    )
                )
    if len(composite_paths) > 1:
        for path in composite_paths:
            if isinstance(snapshot.branches[path].value(94), MIISCoreIdentifier):
                continue
            issues.append(
                ST1607PolicyIssue(
                    "missing_composite_sensor_miis",
                    "ST 1602.1-10",
                    "a multi-sensor composite child requires a direct "
                    "ST 1204 MIIS Core Identifier",
                    path,
                    (94,),
                )
            )
    return tuple(issues)


def validate_st1607_security(
    snapshot: MetadataTreeSnapshot,
) -> tuple[ST1607PolicyIssue, ...]:
    """Validate child security overrides required by ST 1607 Section 9."""
    if not isinstance(snapshot, MetadataTreeSnapshot):
        raise TypeError("snapshot must be a MetadataTreeSnapshot")
    issues: list[ST1607PolicyIssue] = []
    root_security = snapshot.root.value(48)
    branch_kinds = {branch.kind for branch in snapshot.branches.values()}
    if not isinstance(root_security, SecurityLocalSet):
        for kind, requirement in (
            (MetadataBranchKind.AMEND, "ST 1607-01"),
            (MetadataBranchKind.SEGMENT, "ST 1607-02"),
        ):
            if kind in branch_kinds:
                issues.append(
                    ST1607PolicyIssue(
                        "missing_root_security",
                        requirement,
                        "the root metadata set must carry the security metadata "
                        "that applies to every child and sub-child",
                        (),
                        (48,),
                    )
                )
    for path, branch in snapshot.branches.items():
        security = branch.value(48)
        if security is None:
            continue
        inheritance_requirement = (
            "ST 1607-01"
            if branch.kind is MetadataBranchKind.AMEND
            else "ST 1607-02"
        )
        country_requirement = (
            "ST 1607.2-09"
            if branch.kind is MetadataBranchKind.AMEND
            else "ST 1607-04"
        )
        if security is DELETE:
            issues.append(
                ST1607PolicyIssue(
                    "root_security_deleted",
                    inheritance_requirement,
                    "a child cannot remove the root security metadata that applies to it",
                    path,
                    (48,),
                )
            )
            continue
        if not isinstance(security, SecurityLocalSet):
            continue
        item_tags = {item.tag for item in security.local_set.items}
        present = {item.tag for item in security.local_set.items if item.value}
        missing = tuple(sorted({12, 13} - present))
        unexpected = tuple(sorted(item_tags - {12, 13}))
        if missing:
            issues.append(
                ST1607PolicyIssue(
                    "incomplete_child_country_security",
                    country_requirement,
                    "child-specific object-country security requires Items 12 and 13",
                    path,
                    missing,
                )
            )
        if unexpected:
            issues.append(
                ST1607PolicyIssue(
                    "unexpected_child_security_item",
                    country_requirement,
                    "a child Security Local Set may contain only Items 12 and 13",
                    path,
                    unexpected,
                )
            )
    return tuple(issues)


def validate_st1607_mismms(
    snapshot: MetadataTreeSnapshot,
    *,
    require_security: bool = True,
    require_miis: bool = True,
    security_context: MISMMSecurityContext | None = None,
) -> tuple[ST1607PolicyIssue, ...]:
    """Validate MISP/ST 0902 completeness at ST 1607-required tree levels."""
    if not isinstance(snapshot, MetadataTreeSnapshot):
        raise TypeError("snapshot must be a MetadataTreeSnapshot")
    checks: list[tuple[str, MetadataSubstreamPath]] = []
    if any(
        branch.kind is MetadataBranchKind.AMEND
        for branch in snapshot.branches.values()
    ):
        checks.append(("ST 1607-05", ()))
    segment_paths = tuple(
        path
        for path in snapshot.branches
        if any(
            snapshot.branches[path[:depth]].kind is MetadataBranchKind.SEGMENT
            for depth in range(1, len(path) + 1)
        )
    )
    segment_leaves = tuple(
        path
        for path in segment_paths
        if not any(
            len(other) > len(path) and other[: len(path)] == path
            for other in snapshot.branches
        )
    )
    checks.extend(("ST 1607-06", path) for path in segment_leaves)

    issues: list[ST1607PolicyIssue] = []
    for requirement, path in checks:
        profile_issues = validate_mismms_current_state(
            snapshot.effective_fields(path),
            require_security=require_security,
            require_miis=require_miis,
            security_context=security_context,
            effective_security=snapshot.effective_security(path),
        )
        issues.extend(
            ST1607PolicyIssue(
                f"mismms_{issue.code}",
                requirement,
                f"{issue.requirement}: {issue.message}",
                path,
                issue.tags,
            )
            for issue in profile_issues
        )
    return tuple(issues)


@dataclass(slots=True)
class _BranchState:
    kind: MetadataBranchKind
    fields: dict[int, tuple[DecodedField, ...]]
    field_seen: dict[int, datetime]
    observed_at: datetime

    def clone(self) -> _BranchState:
        return _BranchState(
            self.kind,
            dict(self.fields),
            dict(self.field_seen),
            self.observed_at,
        )


class MetadataTreeState:
    """Reconstruct and evaluate ST 1607 substreams carried by ST 0601."""

    def __init__(
        self,
        *,
        refresh_period: timedelta = timedelta(seconds=30),
        max_branches: int = 1024,
        max_fields_per_branch: int = 1024,
        field_decoding: FieldDecodingMode = FieldDecodingMode.PRESERVE,
    ) -> None:
        if not isinstance(refresh_period, timedelta):
            raise TypeError("refresh_period must be a timedelta")
        if refresh_period <= timedelta(0):
            raise ValueError("refresh_period must be positive")
        if refresh_period > timedelta(seconds=30):
            raise ValueError("ST 1607 refresh period cannot exceed 30 seconds")
        for name, value in (
            ("max_branches", max_branches),
            ("max_fields_per_branch", max_fields_per_branch),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(field_decoding, FieldDecodingMode):
            raise TypeError("field_decoding must be a FieldDecodingMode")
        self._refresh_period = refresh_period
        self._max_branches = max_branches
        self._max_fields_per_branch = max_fields_per_branch
        self._field_decoding = field_decoding
        self._root = ReportOnChangeState(
            refresh_period=refresh_period,
            field_decoding=field_decoding,
            max_items_per_tag=max(max_branches, max_fields_per_branch),
        )
        self._observed_at: datetime | None = None
        self._branches: dict[MetadataSubstreamPath, _BranchState] = {}

    @property
    def refresh_period(self) -> timedelta:
        return self._refresh_period

    @property
    def max_branches(self) -> int:
        return self._max_branches

    @property
    def max_fields_per_branch(self) -> int:
        return self._max_fields_per_branch

    @property
    def field_decoding(self) -> FieldDecodingMode:
        return self._field_decoding

    def reset(self) -> None:
        self._root.reset()
        self._observed_at = None
        self._branches.clear()

    def observe(
        self, packet: bytes | KLVPacket | UASLocalSet
    ) -> MetadataTreeSnapshot:
        uas = (
            packet
            if isinstance(packet, UASLocalSet)
            else decode_uas_local_set(packet, field_decoding=self._field_decoding)
        )
        timestamp = uas.value(2)
        if not isinstance(timestamp, datetime):
            raise DecodeError("ST 1607 tree state requires a typed timestamp")
        if self._observed_at is not None and timestamp < self._observed_at:
            raise DecodeError("ST 1607 tree-state timestamps must be monotonic")

        expired_paths = tuple(
            sorted(
                (
                    path
                    for path, branch in self._branches.items()
                    if timestamp - branch.observed_at > self._refresh_period
                ),
                key=_path_sort_key,
            )
        )
        candidate = {
            path: branch.clone()
            for path, branch in self._branches.items()
            if path not in expired_paths
        }
        updated: dict[MetadataSubstreamPath, set[int]] = {}
        cleared: dict[MetadataSubstreamPath, set[int]] = {}
        expired: dict[MetadataSubstreamPath, set[int]] = {}
        branch_issues: dict[MetadataSubstreamPath, tuple[FieldDecodingIssue, ...]] = {}
        for path, branch in candidate.items():
            expired[path] = {
                tag
                for tag, seen in branch.field_seen.items()
                if timestamp - seen > self._refresh_period
            }
            for tag in expired[path]:
                branch.fields.pop(tag, None)
                branch.field_seen.pop(tag, None)

        incoming: list[
            tuple[
                MetadataSubstreamPath,
                MetadataBranchKind,
                SegmentLocalSet | AmendLocalSet,
            ]
        ] = []
        for field in uas.getall(100):
            if isinstance(field.value, SegmentLocalSet):
                _walk_branch(field.value, (), MetadataBranchKind.SEGMENT, incoming)
        for field in uas.getall(101):
            if isinstance(field.value, AmendLocalSet):
                _walk_branch(field.value, (), MetadataBranchKind.AMEND, incoming)

        paths_by_id = {path[-1]: path for path in candidate}
        for path, kind, source in incoming:
            other_path = paths_by_id.get(path[-1])
            if other_path is not None and other_path != path:
                raise DecodeError("ST 1607 Metadata Substream ID changed parent lineage")
            branch_state = candidate.get(path)
            if branch_state is not None and branch_state.kind is not kind:
                raise DecodeError("ST 1607 Metadata Substream ID changed kind")
            if branch_state is None:
                branch_state = _BranchState(kind, {}, {}, timestamp)
                candidate[path] = branch_state
                paths_by_id[path[-1]] = path
            branch_state.observed_at = timestamp
            branch_issues[path] = source.issues
            grouped: dict[int, list[DecodedField]] = {}
            for field in source.fields:
                if field.definition.tag not in {100, 101, 143}:
                    grouped.setdefault(field.definition.tag, []).append(field)
            for tag, values in grouped.items():
                if kind is MetadataBranchKind.SEGMENT and any(
                    field.value is SpecialValue.UNKNOWN for field in values
                ):
                    branch_state.fields.pop(tag, None)
                    branch_state.field_seen.pop(tag, None)
                    cleared.setdefault(path, set()).add(tag)
                else:
                    branch_state.fields[tag] = tuple(values)
                    branch_state.field_seen[tag] = timestamp
                    updated.setdefault(path, set()).add(tag)
            if (
                sum(len(values) for values in branch_state.fields.values())
                > self._max_fields_per_branch
            ):
                raise LimitExceeded(
                    "ST 1607 branch exceeds configured field limit "
                    f"{self._max_fields_per_branch}"
                )
        if len(candidate) > self._max_branches:
            raise LimitExceeded(
                f"ST 1607 tree exceeds configured branch limit {self._max_branches}"
            )

        root = self._root.observe(uas)
        self._branches = candidate
        self._observed_at = timestamp
        snapshots = {
            path: MetadataBranchSnapshot(
                path,
                branch.kind,
                tuple(
                    field
                    for tag in sorted(branch.fields)
                    for field in branch.fields[tag]
                ),
                tuple(sorted(updated.get(path, set()))),
                tuple(sorted(cleared.get(path, set()))),
                tuple(sorted(expired.get(path, set()))),
                branch_issues.get(path, ()),
            )
            for path, branch in sorted(candidate.items(), key=lambda item: _path_sort_key(item[0]))
        }
        return MetadataTreeSnapshot(
            timestamp,
            root,
            MappingProxyType(snapshots),
            expired_paths,
        )


def _walk_branch(
    source: SegmentLocalSet | AmendLocalSet,
    parent_path: MetadataSubstreamPath,
    kind: MetadataBranchKind,
    result: list[
        tuple[
            MetadataSubstreamPath,
            MetadataBranchKind,
            SegmentLocalSet | AmendLocalSet,
        ]
    ],
) -> None:
    path = (*parent_path, source.substream_id)
    result.append((path, kind, source))
    for field in source.getall(100):
        if isinstance(field.value, SegmentLocalSet):
            _walk_branch(field.value, path, MetadataBranchKind.SEGMENT, result)
    for field in source.getall(101):
        if isinstance(field.value, AmendLocalSet):
            _walk_branch(field.value, path, MetadataBranchKind.AMEND, result)


def _path_sort_key(path: MetadataSubstreamPath) -> tuple[tuple[int, str], ...]:
    return tuple(
        (identifier.local_id, str(identifier.universal_id or "")) for identifier in path
    )
