"""Bounded receiver state for MISB ST 0903.6 target lifecycles."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

from stanag4609.errors import LimitExceeded
from stanag4609.st0903 import DetectionStatus, VMTILocalSet


@dataclass(frozen=True, slots=True)
class VMTILifecycleIssue:
    """One cross-packet target-lifecycle diagnostic."""

    code: str
    message: str
    target_id: int
    previous_status: DetectionStatus | None = None
    current_status: DetectionStatus | None = None


@dataclass(frozen=True, slots=True)
class VTargetLifecycle:
    """Receiver-visible history summary for one target identifier."""

    target_id: int
    first_seen_generation: int
    last_seen_generation: int
    observations: int
    first_timestamp: datetime | None
    last_timestamp: datetime | None
    status: DetectionStatus | None
    retired: bool


@dataclass(frozen=True, slots=True)
class VMTILifecycleSnapshot:
    """Immutable result after observing one VMTI Local Set."""

    generation: int
    targets: Mapping[int, VTargetLifecycle]
    current_target_ids: tuple[int, ...]
    retired_target_ids: frozenset[int]
    issues: tuple[VMTILifecycleIssue, ...]


_ACTIVE = frozenset({DetectionStatus.ACTIVE_MOVING, DetectionStatus.ACTIVE_STOPPED})
_ALLOWED_TRANSITIONS: dict[DetectionStatus, frozenset[DetectionStatus]] = {
    DetectionStatus.ACTIVE_MOVING: frozenset(
        {
            DetectionStatus.ACTIVE_MOVING,
            DetectionStatus.ACTIVE_STOPPED,
            DetectionStatus.ACTIVE_COASTING,
            DetectionStatus.INACTIVE,
            DetectionStatus.DROPPED,
        }
    ),
    DetectionStatus.ACTIVE_STOPPED: frozenset(
        {
            DetectionStatus.ACTIVE_MOVING,
            DetectionStatus.ACTIVE_STOPPED,
            DetectionStatus.ACTIVE_COASTING,
            DetectionStatus.INACTIVE,
            DetectionStatus.DROPPED,
        }
    ),
    DetectionStatus.ACTIVE_COASTING: frozenset(
        {
            DetectionStatus.ACTIVE_MOVING,
            DetectionStatus.ACTIVE_STOPPED,
            DetectionStatus.ACTIVE_COASTING,
            DetectionStatus.INACTIVE,
            DetectionStatus.DROPPED,
        }
    ),
    DetectionStatus.INACTIVE: frozenset(
        {
            DetectionStatus.ACTIVE_MOVING,
            DetectionStatus.ACTIVE_STOPPED,
            DetectionStatus.INACTIVE,
            DetectionStatus.DROPPED,
        }
    ),
    DetectionStatus.DROPPED: frozenset({DetectionStatus.DROPPED}),
}


class VMTILifecycleState:
    """Validate target identity and state transitions across ST 0903.6 packets.

    Use one instance per VMTI process/sensor stream. Missing targets are not
    removed because a VMTI packet may report only a subset of the producer's
    target list. A target observed as Dropped remains retired for the lifetime
    of this state so reuse of its identifier can be diagnosed.
    """

    __slots__ = ("_generation", "_targets", "assume_stream_start", "max_target_ids")

    def __init__(
        self,
        *,
        max_target_ids: int = 100_000,
        assume_stream_start: bool = False,
    ) -> None:
        if (
            isinstance(max_target_ids, bool)
            or not isinstance(max_target_ids, int)
            or max_target_ids < 1
        ):
            raise ValueError("max_target_ids must be a positive integer")
        if not isinstance(assume_stream_start, bool):
            raise TypeError("assume_stream_start must be boolean")
        self.max_target_ids = max_target_ids
        self.assume_stream_start = assume_stream_start
        self._generation = 0
        self._targets: dict[int, VTargetLifecycle] = {}

    @property
    def generation(self) -> int:
        return self._generation

    def reset(self) -> None:
        """Forget all observed identities and lifecycle history."""

        self._generation = 0
        self._targets.clear()

    def snapshot(self) -> VMTILifecycleSnapshot:
        """Return the current state without consuming another packet."""

        return self._snapshot((), ())

    def observe(self, vmti: VMTILocalSet) -> VMTILifecycleSnapshot:
        """Atomically observe one decoded VMTI Local Set."""

        if not isinstance(vmti, VMTILocalSet):
            raise TypeError("vmti must be VMTILocalSet")
        current_ids = tuple(target.target_id for target in vmti.targets)
        new_ids = set(current_ids) - self._targets.keys()
        if len(self._targets) + len(new_ids) > self.max_target_ids:
            raise LimitExceeded(
                f"VMTI lifecycle state exceeds {self.max_target_ids} target identifiers"
            )

        generation = self._generation + 1
        timestamp_value = vmti.value(2)
        timestamp = timestamp_value if isinstance(timestamp_value, datetime) else None
        issues: list[VMTILifecycleIssue] = []
        updates: dict[int, VTargetLifecycle] = {}
        for target in vmti.targets:
            target_id = target.target_id
            status_value = target.value(23)
            status = status_value if isinstance(status_value, DetectionStatus) else None
            previous = self._targets.get(target_id)
            if status is None:
                issues.append(
                    VMTILifecycleIssue(
                        "missing_detection_status",
                        f"target {target_id} has no ST 0903 detectionStatus Item 23",
                        target_id,
                        None if previous is None else previous.status,
                    )
                )
            if previous is None:
                if self.assume_stream_start and status is not None and status not in _ACTIVE:
                    issues.append(
                        VMTILifecycleIssue(
                            "invalid_initial_status",
                            f"new target {target_id} starts in {status.name}, "
                            "expected an active state",
                            target_id,
                            current_status=status,
                        )
                    )
                updates[target_id] = VTargetLifecycle(
                    target_id,
                    generation,
                    generation,
                    1,
                    timestamp,
                    timestamp,
                    status,
                    status is DetectionStatus.DROPPED,
                )
                continue

            if previous.retired:
                issues.append(
                    VMTILifecycleIssue(
                        "retired_target_id_reused",
                        f"target {target_id} was Dropped and its identifier was reused",
                        target_id,
                        previous.status,
                        status,
                    )
                )
            elif (
                previous.status is not None
                and status is not None
                and status not in _ALLOWED_TRANSITIONS[previous.status]
            ):
                issues.append(
                    VMTILifecycleIssue(
                        "invalid_status_transition",
                        f"target {target_id} transitions from {previous.status.name} "
                        f"to {status.name}",
                        target_id,
                        previous.status,
                        status,
                    )
                )
            updates[target_id] = VTargetLifecycle(
                target_id,
                previous.first_seen_generation,
                generation,
                previous.observations + 1,
                previous.first_timestamp,
                timestamp,
                status,
                previous.retired or status is DetectionStatus.DROPPED,
            )

        self._targets.update(updates)
        self._generation = generation
        return self._snapshot(current_ids, tuple(issues))

    def _snapshot(
        self,
        current_target_ids: tuple[int, ...],
        issues: tuple[VMTILifecycleIssue, ...],
    ) -> VMTILifecycleSnapshot:
        targets = dict(self._targets)
        return VMTILifecycleSnapshot(
            self._generation,
            MappingProxyType(targets),
            current_target_ids,
            frozenset(
                target_id for target_id, target in targets.items() if target.retired
            ),
            issues,
        )
