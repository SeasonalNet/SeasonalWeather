"""Strict typed SWWP/1 envelope and message payloads."""

from __future__ import annotations

import datetime as dt
import re
from enum import StrEnum
from typing import Any, ClassVar, Self

from ..capabilities.models import normalize_parameters
from ..diagnostics.codes import DiagnosticCode, DiagnosticCodeError
from ..jobs.contracts import AttemptOutcome
from ..jobs.policies import ExecutorClass, FailureCategory, JobType, QueueClass
from ..validation.modeling import (
    BaseModel,
    ConfigDict,
    Field,
    SerializeAsAny,
    field_validator,
    model_validator,
)
from .constants import PROTOCOL_NAME, PROTOCOL_VERSION, ProtocolErrorCategory, ReconcileDisposition

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")
_KEY_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")


def _utc(value: dt.datetime, name: str) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(dt.UTC)


def _identifier(value: str, name: str) -> str:
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"{name} must be a bounded opaque identifier")
    return value


def _key(value: str, name: str) -> str:
    if not _KEY_RE.fullmatch(value):
        raise ValueError(f"{name} must be a bounded declared key")
    return value


def _digest(value: str) -> str:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError("capability digest must use lowercase sha256")
    return value


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class Payload(WireModel):
    message_type: ClassVar[str]


class VersionSupport(WireModel):
    swwp: tuple[int, ...] = Field(min_length=1, max_length=8)
    job_payloads: dict[JobType, tuple[int, ...]] = Field(default_factory=dict, max_length=16)
    job_results: dict[JobType, tuple[int, ...]] = Field(default_factory=dict, max_length=16)
    diagnostics: tuple[int, ...] = Field(min_length=1, max_length=8)
    capability_manifest: tuple[int, ...] = Field(min_length=1, max_length=8)
    configuration_schema: tuple[int, ...] = Field(min_length=1, max_length=8)

    @field_validator(
        "swwp",
        "diagnostics",
        "capability_manifest",
        "configuration_schema",
    )
    @classmethod
    def validate_versions(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(item < 1 or item > 255 for item in value):
            raise ValueError("schema versions must be between 1 and 255")
        if len(set(value)) != len(value):
            raise ValueError("schema versions must be unique")
        return tuple(sorted(value))

    @field_validator("job_payloads", "job_results")
    @classmethod
    def validate_job_versions(cls, value: dict[JobType, tuple[int, ...]]) -> dict[JobType, tuple[int, ...]]:
        normalized: dict[JobType, tuple[int, ...]] = {}
        for job_type, versions in value.items():
            if not versions or any(item < 1 or item > 255 for item in versions):
                raise ValueError("job schema versions must be non-empty and bounded")
            if len(set(versions)) != len(versions):
                raise ValueError("job schema versions must be unique")
            normalized[job_type] = tuple(sorted(versions))
        return dict(sorted(normalized.items(), key=lambda item: item[0].value))


class SelectedVersions(WireModel):
    swwp: int = Field(ge=1, le=255)
    job_payloads: dict[JobType, int] = Field(default_factory=dict, max_length=16)
    job_results: dict[JobType, int] = Field(default_factory=dict, max_length=16)
    diagnostics: int = Field(ge=1, le=255)
    capability_manifest: int = Field(ge=1, le=255)
    configuration_schema: int = Field(ge=1, le=255)


class CapabilityOperationalState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    DRAINING = "draining"
    DISABLED = "disabled"
    UNKNOWN = "unknown"


class CapabilityDependencyState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


CapabilityParameter = (
    str | int | float | bool | tuple[str, ...] | tuple[int, ...] | tuple[float, ...] | tuple[bool, ...]
)


class CapabilityRecordPayload(WireModel):
    name: str
    implemented: bool
    operational_state: CapabilityOperationalState
    accepting_new_jobs: bool
    total_capacity: int = Field(ge=0, le=128)
    reported_available: int = Field(ge=0, le=128)
    job_restrictions: tuple[str, ...] = Field(default_factory=tuple, max_length=16)
    parameters: dict[str, CapabilityParameter] = Field(default_factory=dict, max_length=16)
    validity_seconds: int = Field(ge=1, le=900)
    observed_at: dt.datetime
    published_at: dt.datetime
    dependency_health: dict[str, CapabilityDependencyState] = Field(
        default_factory=dict,
        max_length=8,
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _key(value, "capability name")

    @field_validator("observed_at", "published_at")
    @classmethod
    def validate_times(cls, value: dt.datetime, info: Any) -> dt.datetime:
        return _utc(value, info.field_name)

    @field_validator("job_restrictions")
    @classmethod
    def validate_restrictions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted({_key(item, "job restriction") for item in value}))

    @field_validator("parameters")
    @classmethod
    def validate_parameters(cls, value: dict[str, Any]) -> dict[str, Any]:
        return normalize_parameters(value)

    @field_validator("dependency_health")
    @classmethod
    def validate_dependencies(
        cls,
        value: dict[str, CapabilityDependencyState],
    ) -> dict[str, CapabilityDependencyState]:
        return dict(sorted((_key(key, "dependency name"), state) for key, state in value.items()))

    @model_validator(mode="after")
    def validate_capacity(self) -> Self:
        if self.reported_available > self.total_capacity:
            raise ValueError("available capability capacity exceeds total")
        inactive = {
            CapabilityOperationalState.UNAVAILABLE,
            CapabilityOperationalState.DRAINING,
            CapabilityOperationalState.DISABLED,
            CapabilityOperationalState.UNKNOWN,
        }
        if self.operational_state in inactive and self.accepting_new_jobs:
            raise ValueError("inactive capability cannot accept jobs")
        if not self.implemented and (self.accepting_new_jobs or self.total_capacity or self.reported_available):
            raise ValueError("unimplemented capability cannot report capacity")
        if self.observed_at > self.published_at:
            raise ValueError("capability observation cannot follow publication")
        return self


class CapabilityManifest(WireModel):
    schema_version: int = Field(ge=1, le=255)
    epoch: int = Field(ge=1)
    digest: str = Field(min_length=71, max_length=71)
    records: tuple[CapabilityRecordPayload, ...] = Field(max_length=64)

    @field_validator("digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _digest(value)

    @field_validator("records")
    @classmethod
    def validate_records(
        cls,
        value: tuple[CapabilityRecordPayload, ...],
    ) -> tuple[CapabilityRecordPayload, ...]:
        names = tuple(item.name for item in value)
        if names != tuple(sorted(set(names))):
            raise ValueError("capability records must be unique and sorted")
        return value

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(record.name for record in self.records)


class LeaseRef(WireModel):
    job_id: str
    lease_id: str
    attempt_id: str
    attempt: int = Field(ge=1, le=10)

    @field_validator("job_id", "lease_id", "attempt_id")
    @classmethod
    def validate_ids(cls, value: str, info: Any) -> str:
        return _identifier(value, info.field_name)


class Register(Payload):
    message_type = "register"
    worker_id: str
    worker_instance_id: str
    worker_epoch: int = Field(ge=1)
    software_version: str = Field(min_length=1, max_length=64)
    build_identity: str = Field(min_length=1, max_length=128)
    requested_queues: tuple[QueueClass, ...] = Field(min_length=1, max_length=8)
    requested_slots: int = Field(ge=1, le=128)
    capability_manifest: CapabilityManifest
    supported_versions: VersionSupport

    @field_validator("worker_id", "worker_instance_id")
    @classmethod
    def validate_ids(cls, value: str, info: Any) -> str:
        return _identifier(value, info.field_name)


class Registered(Payload):
    message_type = "registered"
    session_id: str
    controller_epoch: int = Field(ge=1)
    selected_subprotocol: str = Field(min_length=1, max_length=128)
    heartbeat_interval_seconds: int = Field(ge=1, le=300)
    heartbeat_timeout_seconds: int = Field(ge=2, le=900)
    lease_seconds: int = Field(ge=1, le=3600)
    assignment_ack_seconds: int = Field(ge=1, le=300)
    accepted_queues: tuple[QueueClass, ...] = Field(max_length=8)
    authorized_job_types: tuple[JobType, ...] = Field(max_length=16)
    authorized_capabilities: tuple[str, ...] = Field(max_length=64)
    selected_versions: SelectedVersions
    max_message_bytes: int = Field(ge=1024, le=16_777_216)
    max_active_assignments: int = Field(ge=1, le=128)
    effective_capabilities: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    capability_epoch: int = Field(ge=1)
    capability_digest: str = Field(min_length=71, max_length=71)
    qualification_required: bool = False

    @field_validator("session_id")
    @classmethod
    def validate_session(cls, value: str) -> str:
        return _identifier(value, "session_id")

    _capability_digest = field_validator("capability_digest")(_digest)


class RegistrationRejected(Payload):
    message_type = "registration_rejected"
    category: ProtocolErrorCategory
    summary: str = Field(min_length=1, max_length=256)
    supported_subprotocols: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    supported_swwp_versions: tuple[int, ...] = Field(default_factory=tuple, max_length=8)


class Heartbeat(Payload):
    message_type = "heartbeat"
    active_leases: tuple[LeaseRef, ...] = Field(default_factory=tuple, max_length=32)
    capability_epoch: int | None = Field(default=None, ge=1)
    capability_digest: str | None = Field(default=None, min_length=71, max_length=71)

    @model_validator(mode="after")
    def validate_capability_identity(self) -> Self:
        if (self.capability_epoch is None) != (self.capability_digest is None):
            raise ValueError("heartbeat capability epoch and digest must appear together")
        return self

    _capability_digest = field_validator("capability_digest")(
        lambda value: _digest(value) if value is not None else None
    )


class HeartbeatAck(Payload):
    message_type = "heartbeat_ack"
    renewed: tuple[LeaseRef, ...] = Field(default_factory=tuple, max_length=32)
    reconcile: tuple[LeaseRef, ...] = Field(default_factory=tuple, max_length=32)


class CapabilityUpdate(Payload):
    message_type = "capability_update"
    epoch: int = Field(ge=1)
    changed: tuple[CapabilityRecordPayload, ...] = Field(default_factory=tuple, max_length=64)
    removed: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    full_digest: str = Field(min_length=71, max_length=71)
    validity_seconds: int = Field(ge=1, le=900)

    @field_validator("removed")
    @classmethod
    def validate_removed(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted({_key(item, "removed capability") for item in value}))
        if normalized != value:
            raise ValueError("removed capabilities must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_changes(self) -> Self:
        changed = tuple(item.name for item in self.changed)
        if changed != tuple(sorted(set(changed))):
            raise ValueError("changed capabilities must be unique and sorted")
        if set(changed).intersection(self.removed):
            raise ValueError("capability cannot be changed and removed")
        return self

    _full_digest = field_validator("full_digest")(_digest)


class CapabilityRecoveryAction(StrEnum):
    NONE = "none"
    FULL_REPORT_REQUIRED = "full_report_required"
    STALE_IGNORED = "stale_ignored"


class CapabilityUpdateAck(Payload):
    message_type = "capability_update_ack"
    epoch: int = Field(ge=1)
    digest: str = Field(min_length=71, max_length=71)
    recovery_action: CapabilityRecoveryAction = CapabilityRecoveryAction.NONE

    _digest = field_validator("digest")(_digest)


class CapabilityProbe(Payload):
    message_type = "capability_probe"
    probe_id: str
    full: bool
    names: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    reason: str = Field(min_length=2, max_length=64)
    deadline_at: dt.datetime

    _probe_id = field_validator("probe_id")(lambda value: _identifier(value, "probe_id"))
    _reason = field_validator("reason")(lambda value: _key(value, "probe reason"))
    _deadline = field_validator("deadline_at")(lambda value: _utc(value, "deadline_at"))

    @model_validator(mode="after")
    def validate_mode(self) -> Self:
        if self.full == bool(self.names):
            raise ValueError("full probe has no targets; targeted probe requires targets")
        return self


class CapabilityReport(Payload):
    message_type = "capability_report"
    probe_id: str
    schema_version: int = Field(ge=1, le=255)
    epoch: int = Field(ge=1)
    records: tuple[CapabilityRecordPayload, ...] = Field(default_factory=tuple, max_length=64)
    full_digest: str = Field(min_length=71, max_length=71)
    validity_seconds: int = Field(ge=1, le=900)

    _probe_id = field_validator("probe_id")(lambda value: _identifier(value, "probe_id"))
    _full_digest = field_validator("full_digest")(_digest)

    @field_validator("records")
    @classmethod
    def validate_records(
        cls,
        value: tuple[CapabilityRecordPayload, ...],
    ) -> tuple[CapabilityRecordPayload, ...]:
        names = tuple(item.name for item in value)
        if names != tuple(sorted(set(names))):
            raise ValueError("capability report records must be unique and sorted")
        return value


class CapabilityRejectionCategory(StrEnum):
    CAPACITY_UNAVAILABLE = "capacity_unavailable"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    CAPABILITY_STALE = "capability_stale"
    PARAMETER_MISMATCH = "parameter_mismatch"
    SCHEMA_MISMATCH = "schema_mismatch"
    DRAINING = "draining"
    DISABLED = "disabled"


class JobAssignmentPayload(Payload):
    message_type = "job"
    lease: LeaseRef
    deadline_at: dt.datetime
    lease_expires_at: dt.datetime
    acknowledgment_deadline_at: dt.datetime
    job_type: JobType
    queue: QueueClass
    executor: ExecutorClass
    payload_schema_version: int = Field(ge=1, le=255)
    result_schema_version: int = Field(ge=1, le=255)
    configuration_generation: int | None = Field(default=None, ge=0)
    payload: dict[str, Any] = Field(max_length=32)
    capability_requirements: tuple[str, ...] = Field(default_factory=tuple, max_length=64)

    @field_validator("deadline_at", "lease_expires_at", "acknowledgment_deadline_at")
    @classmethod
    def validate_times(cls, value: dt.datetime, info: Any) -> dt.datetime:
        return _utc(value, info.field_name)

    @model_validator(mode="after")
    def validate_timing(self) -> Self:
        if not (self.acknowledgment_deadline_at <= self.lease_expires_at <= self.deadline_at):
            raise ValueError("assignment deadlines must be ordered")
        return self


class JobAccepted(Payload):
    message_type = "job_accepted"
    lease: LeaseRef


class JobRejected(Payload):
    message_type = "job_rejected"
    lease: LeaseRef
    category: CapabilityRejectionCategory
    summary: str = Field(min_length=1, max_length=256)
    capabilities: tuple[str, ...] = Field(default_factory=tuple, max_length=16)

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted({_key(item, "rejected capability") for item in value}))
        if normalized != value:
            raise ValueError("rejected capabilities must be unique and sorted")
        return value


class JobProgress(Payload):
    message_type = "job_progress"
    lease: LeaseRef
    stage: str = Field(min_length=2, max_length=64)
    reason: str | None = Field(default=None, max_length=64)
    numeric: dict[str, int | float] = Field(default_factory=dict, max_length=16)

    _stage = field_validator("stage")(lambda value: _key(value, "stage"))


class JobResult(Payload):
    message_type = "job_result"
    lease: LeaseRef
    result_schema_version: int = Field(ge=1, le=255)
    result: dict[str, Any] = Field(max_length=32)
    artifact_refs: tuple[str, ...] = Field(default_factory=tuple, max_length=16)
    completion_id: str

    _completion_id = field_validator("completion_id")(lambda value: _identifier(value, "completion_id"))

    @field_validator("artifact_refs")
    @classmethod
    def validate_artifacts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_identifier(item, "artifact_ref") for item in value)


class JobFailed(Payload):
    message_type = "job_failed"
    lease: LeaseRef
    outcome: AttemptOutcome
    category: FailureCategory
    error_code: str = Field(min_length=2, max_length=64)
    summary: str = Field(min_length=1, max_length=256)
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict, max_length=16)

    _error_code = field_validator("error_code")(lambda value: _key(value, "error_code"))

    @model_validator(mode="after")
    def validate_failure(self) -> Self:
        if self.outcome is AttemptOutcome.SUCCEEDED:
            raise ValueError("job_failed cannot report success")
        return self


class Cancel(Payload):
    message_type = "cancel"
    lease: LeaseRef
    reason: str = Field(min_length=2, max_length=64)
    deadline_at: dt.datetime

    _reason = field_validator("reason")(lambda value: _key(value, "reason"))
    _deadline = field_validator("deadline_at")(lambda value: _utc(value, "deadline_at"))


class CancelAcknowledged(Payload):
    message_type = "cancel_acknowledged"
    lease: LeaseRef
    observed_at: dt.datetime

    _observed = field_validator("observed_at")(lambda value: _utc(value, "observed_at"))


class Drain(Payload):
    message_type = "drain"
    deadline_at: dt.datetime
    reason: str = Field(min_length=2, max_length=64)

    _deadline = field_validator("deadline_at")(lambda value: _utc(value, "deadline_at"))
    _reason = field_validator("reason")(lambda value: _key(value, "reason"))


class Drained(Payload):
    message_type = "drained"
    active: tuple[LeaseRef, ...] = Field(default_factory=tuple, max_length=32)
    unacknowledged_completions: tuple[str, ...] = Field(default_factory=tuple, max_length=32)


class ReconcileItem(WireModel):
    lease: LeaseRef
    prior_session_id: str | None = None
    accepted: bool = False
    cancellation_observed: bool = False
    completion_id: str | None = None
    result_schema_version: int | None = Field(default=None, ge=1, le=255)
    result: dict[str, Any] | None = Field(default=None, max_length=32)

    @field_validator("prior_session_id", "completion_id")
    @classmethod
    def validate_optional_ids(cls, value: str | None, info: Any) -> str | None:
        return _identifier(value, info.field_name) if value is not None else None


class Reconcile(Payload):
    message_type = "reconcile"
    prior_session_id: str | None = None
    prior_controller_epoch: int | None = Field(default=None, ge=1)
    items: tuple[ReconcileItem, ...] = Field(default_factory=tuple, max_length=64)

    @field_validator("prior_session_id")
    @classmethod
    def validate_prior_session(cls, value: str | None) -> str | None:
        return _identifier(value, "prior_session_id") if value is not None else None


class ReconcileDecision(WireModel):
    lease: LeaseRef
    disposition: ReconcileDisposition
    summary: str = Field(min_length=1, max_length=256)


class ReconcileResult(Payload):
    message_type = "reconcile_result"
    decisions: tuple[ReconcileDecision, ...] = Field(default_factory=tuple, max_length=64)


class ResultCommitted(Payload):
    message_type = "result_committed"
    lease: LeaseRef
    completion_id: str
    result_hash: str = Field(min_length=16, max_length=128)
    committed_at: dt.datetime

    _completion_id = field_validator("completion_id")(lambda value: _identifier(value, "completion_id"))
    _committed = field_validator("committed_at")(lambda value: _utc(value, "committed_at"))


class ProtocolErrorPayload(Payload):
    message_type = "protocol_error"
    category: ProtocolErrorCategory
    summary: str = Field(min_length=1, max_length=256)
    correlated_message_id: str | None = None
    fatal: bool

    @field_validator("correlated_message_id")
    @classmethod
    def validate_correlation(cls, value: str | None) -> str | None:
        return _identifier(value, "correlated_message_id") if value is not None else None


class DiagnosticTransition(StrEnum):
    ACTIVE = "active"
    RESOLVED = "resolved"


class DiagnosticFrame(WireModel):
    filename: str = Field(min_length=1, max_length=512)
    line: int = Field(ge=1, le=10_000_000)
    function: str = Field(min_length=1, max_length=256)
    source: str = Field(default="", max_length=512)


class DiagnosticEvidence(WireModel):
    exception_type: str = Field(min_length=1, max_length=256)
    message: str = Field(default="", max_length=1024)
    notes: tuple[str, ...] = Field(default_factory=tuple, max_length=16)
    frames: tuple[DiagnosticFrame, ...] = Field(default_factory=tuple, max_length=128)


class WorkerDiagnostic(Payload):
    message_type = "diagnostic"
    envelope_schema_version: int = Field(ge=1, le=255)
    diagnostic_schema_version: int = Field(ge=1, le=255)
    catalog_version: int = Field(ge=1, le=2_147_483_647)
    diagnostic_id: str
    code: str = Field(min_length=7, max_length=32)
    short_message: str = Field(min_length=1, max_length=512)
    component: str = Field(min_length=1, max_length=64)
    transition: DiagnosticTransition = DiagnosticTransition.ACTIVE
    controller_occurrence_id: str | None = None
    reason_code: str | None = Field(default=None, max_length=64)
    capability: str | None = Field(default=None, max_length=64)
    evidence: DiagnosticEvidence | None = None
    retryable_hint: bool | None = None
    fatal_hint: bool | None = None

    _diagnostic_id = field_validator("diagnostic_id")(lambda value: _identifier(value, "diagnostic_id"))

    @field_validator("controller_occurrence_id")
    @classmethod
    def validate_occurrence_id(cls, value: str | None) -> str | None:
        return _identifier(value, "controller_occurrence_id") if value is not None else None

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        try:
            DiagnosticCode.parse(value)
        except DiagnosticCodeError as exc:
            raise ValueError("worker diagnostic code syntax is invalid") from exc
        return value

    @model_validator(mode="after")
    def validate_transition(self) -> Self:
        if self.transition is DiagnosticTransition.RESOLVED and self.controller_occurrence_id is None:
            raise ValueError("worker resolution requires a controller occurrence identity")
        if self.transition is DiagnosticTransition.ACTIVE and self.controller_occurrence_id is not None:
            raise ValueError("worker activation cannot choose a controller occurrence identity")
        return self


class WorkerDiagnosticAck(Payload):
    message_type = "diagnostic_ack"
    diagnostic_id: str
    accepted: bool
    controller_occurrence_id: str | None = None
    compatibility: bool = False
    summary: str = Field(min_length=1, max_length=256)

    _diagnostic_id = field_validator("diagnostic_id")(lambda value: _identifier(value, "diagnostic_id"))

    @field_validator("controller_occurrence_id")
    @classmethod
    def validate_occurrence_id(cls, value: str | None) -> str | None:
        return _identifier(value, "controller_occurrence_id") if value is not None else None


PAYLOAD_TYPES: tuple[type[Payload], ...] = (
    Register,
    Registered,
    RegistrationRejected,
    Heartbeat,
    HeartbeatAck,
    CapabilityUpdate,
    CapabilityUpdateAck,
    CapabilityProbe,
    CapabilityReport,
    JobAssignmentPayload,
    JobAccepted,
    JobRejected,
    JobProgress,
    JobResult,
    JobFailed,
    Cancel,
    CancelAcknowledged,
    Drain,
    Drained,
    Reconcile,
    ReconcileResult,
    ResultCommitted,
    ProtocolErrorPayload,
    WorkerDiagnostic,
    WorkerDiagnosticAck,
)
PAYLOAD_BY_TYPE = {model.message_type: model for model in PAYLOAD_TYPES}


class Envelope(WireModel):
    protocol: str = PROTOCOL_NAME
    protocol_version: int = PROTOCOL_VERSION
    message_type: str = Field(min_length=2, max_length=64)
    message_id: str
    sent_at: dt.datetime
    session_id: str | None = None
    worker_id: str | None = None
    worker_instance_id: str | None = None
    controller_epoch: int | None = Field(default=None, ge=1)
    worker_epoch: int | None = Field(default=None, ge=1)
    payload: SerializeAsAny[Payload]

    @field_validator("message_id", "session_id", "worker_id", "worker_instance_id")
    @classmethod
    def validate_ids(cls, value: str | None, info: Any) -> str | None:
        return _identifier(value, info.field_name) if value is not None else None

    @field_validator("sent_at")
    @classmethod
    def validate_sent_at(cls, value: dt.datetime) -> dt.datetime:
        return _utc(value, "sent_at")

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.protocol != PROTOCOL_NAME:
            raise ValueError("unsupported protocol identity")
        if self.protocol_version != PROTOCOL_VERSION:
            raise ValueError("unsupported SWWP wire version")
        if self.message_type != self.payload.message_type:
            raise ValueError("envelope message_type does not match payload")
        return self
