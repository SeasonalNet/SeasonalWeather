"""Pure worker/job capability qualification."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from ..jobs.policies import CapabilityRequirement, JobType
from .models import (
    CapabilityRecord,
    CompatibilityState,
    OperationalState,
    ParameterValue,
)


class QualificationReason(StrEnum):
    QUALIFIED = "qualified"
    NOT_IMPLEMENTED = "not_implemented"
    UNAUTHORIZED = "unauthorized"
    INCOMPATIBLE = "incompatible"
    UNHEALTHY = "unhealthy"
    DEGRADED_NOT_ACCEPTING = "degraded_not_accepting"
    UNAVAILABLE = "unavailable"
    DRAINING = "draining"
    DISABLED = "disabled"
    UNKNOWN_OR_STALE = "unknown_or_stale"
    PARAMETER_MISMATCH = "parameter_mismatch"
    SCHEMA_MISMATCH = "schema_mismatch"
    NO_CAPACITY = "no_capacity"
    SESSION_UNAVAILABLE = "session_unavailable"
    PROBE_REQUIRED = "probe_required"


@dataclass(frozen=True)
class QualificationResult:
    qualified: bool
    reason: QualificationReason
    worker_id: str
    worker_instance_id: str
    capability_names: tuple[str, ...]
    epoch: int | None
    digest: str | None
    effective_capacity: int
    evidence: tuple[str, ...]
    snapshot_token: str


@dataclass(frozen=True)
class WorkerQualificationView:
    worker_id: str
    worker_instance_id: str
    session_id: str
    epoch: int
    digest: str
    records: tuple[CapabilityRecord, ...]
    authorized_capabilities: frozenset[str]
    authorized_job_types: frozenset[JobType]
    payload_versions: Mapping[JobType, int]
    result_versions: Mapping[JobType, int]
    effective_capacity: Mapping[str, int]
    trusted: bool = True
    connected: bool = True
    probe_required: bool = False


def _token(view: WorkerQualificationView, names: tuple[str, ...]) -> str:
    raw = json.dumps(
        {
            "digest": view.digest,
            "epoch": view.epoch,
            "instance": view.worker_instance_id,
            "names": names,
            "session": view.session_id,
            "worker": view.worker_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"q:{hashlib.sha256(raw).hexdigest()[:32]}"


def _result(
    view: WorkerQualificationView,
    *,
    reason: QualificationReason,
    names: tuple[str, ...],
    capacity: int = 0,
    evidence: tuple[str, ...] = (),
) -> QualificationResult:
    return QualificationResult(
        qualified=reason is QualificationReason.QUALIFIED,
        reason=reason,
        worker_id=view.worker_id,
        worker_instance_id=view.worker_instance_id,
        capability_names=names,
        epoch=view.epoch,
        digest=view.digest,
        effective_capacity=max(0, capacity),
        evidence=evidence[:8],
        snapshot_token=_token(view, names),
    )


def _parameter_matches(reported: ParameterValue, required: str | int | bool) -> bool:
    if isinstance(reported, tuple):
        return required in reported
    return reported == required


def _state_reason(record: CapabilityRecord) -> QualificationReason | None:
    if not record.implemented:
        return QualificationReason.NOT_IMPLEMENTED
    compatibility_reasons = {
        CompatibilityState.INCOMPATIBLE: QualificationReason.INCOMPATIBLE,
        CompatibilityState.UNKNOWN: QualificationReason.SCHEMA_MISMATCH,
    }
    if reason := compatibility_reasons.get(record.compatibility):
        return reason
    state_reasons = {
        OperationalState.UNKNOWN: QualificationReason.UNKNOWN_OR_STALE,
        OperationalState.UNAVAILABLE: QualificationReason.UNAVAILABLE,
        OperationalState.DRAINING: QualificationReason.DRAINING,
        OperationalState.DISABLED: QualificationReason.DISABLED,
    }
    if reason := state_reasons.get(record.operational_state):
        return reason
    if record.operational_state is OperationalState.DEGRADED and not record.accepting_new_jobs:
        return QualificationReason.DEGRADED_NOT_ACCEPTING
    if not record.accepting_new_jobs:
        return QualificationReason.UNHEALTHY
    return None


def _view_reason(
    view: WorkerQualificationView,
    job_type: JobType,
    payload_schema_version: int,
    result_schema_version: int,
) -> QualificationReason | None:
    if not view.connected:
        return QualificationReason.SESSION_UNAVAILABLE
    if view.probe_required:
        return QualificationReason.PROBE_REQUIRED
    if not view.trusted:
        return QualificationReason.UNKNOWN_OR_STALE
    if job_type not in view.authorized_job_types:
        return QualificationReason.UNAUTHORIZED
    versions_match = (
        view.payload_versions.get(job_type) == payload_schema_version
        and view.result_versions.get(job_type) == result_schema_version
    )
    return None if versions_match else QualificationReason.SCHEMA_MISMATCH


def _requirement_reason(
    view: WorkerQualificationView,
    record: CapabilityRecord | None,
    requirement: CapabilityRequirement,
    job_type: JobType,
) -> tuple[QualificationReason | None, tuple[str, ...]]:
    if requirement.name not in view.authorized_capabilities:
        return QualificationReason.UNAUTHORIZED, ()
    if record is None:
        return QualificationReason.NOT_IMPLEMENTED, ()
    if reason := _state_reason(record):
        return reason, ()
    if record.job_restrictions and job_type.value not in record.job_restrictions:
        return QualificationReason.PARAMETER_MISMATCH, ()
    mismatches = _parameter_mismatches(record, requirement)
    if mismatches:
        return QualificationReason.PARAMETER_MISMATCH, mismatches[:1]
    if view.effective_capacity.get(requirement.name, 0) <= 0:
        return QualificationReason.NO_CAPACITY, ()
    return None, ()


def _parameter_mismatches(
    record: CapabilityRecord,
    requirement: CapabilityRequirement,
) -> tuple[str, ...]:
    return tuple(
        key
        for key, required in requirement.parameters.items()
        if (reported := record.parameters.get(key)) is None or not _parameter_matches(reported, required)
    )


def qualify(
    view: WorkerQualificationView,
    *,
    job_type: JobType,
    payload_schema_version: int,
    result_schema_version: int,
    requirements: tuple[CapabilityRequirement, ...],
) -> QualificationResult:
    """Evaluate one job against one immutable worker view without side effects."""

    names = tuple(sorted({item.name for item in requirements if item.required}))
    reason = _view_reason(view, job_type, payload_schema_version, result_schema_version)
    if reason:
        return _result(view, reason=reason, names=names)

    records = {item.name: item for item in view.records}
    required_capacity: list[int] = []
    evidence: list[str] = []
    for requirement in requirements:
        if not requirement.required:
            continue
        record = records.get(requirement.name)
        reason, mismatch = _requirement_reason(view, record, requirement, job_type)
        if reason:
            return _result(view, reason=reason, names=names, evidence=mismatch)
        capacity = view.effective_capacity.get(requirement.name, 0)
        required_capacity.append(capacity)
        evidence.append(requirement.name)
    capacity = min(required_capacity) if required_capacity else 0
    return _result(
        view,
        reason=QualificationReason.QUALIFIED,
        names=names,
        capacity=capacity,
        evidence=tuple(evidence),
    )


def compatible_records(
    records: tuple[CapabilityRecord, ...],
    *,
    allowed_names: frozenset[str],
) -> tuple[CapabilityRecord, ...]:
    """Apply controller ownership to worker-reported implementation records."""

    normalized: list[CapabilityRecord] = []
    for record in records:
        compatibility = (
            CompatibilityState.COMPATIBLE if record.name in allowed_names else CompatibilityState.INCOMPATIBLE
        )
        normalized.append(record.model_copy(update={"compatibility": compatibility}))
    return tuple(sorted(normalized, key=lambda item: item.name))
