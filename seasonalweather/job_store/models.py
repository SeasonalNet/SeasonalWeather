from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..jobs.contracts import JobRecord


class AdmissionDisposition(StrEnum):
    CREATED = "created"
    REUSED = "reused"
    COALESCED = "coalesced"
    SUPERSEDED = "superseded"
    CONFLICT = "conflict"
    REJECTED = "rejected"


@dataclass(frozen=True)
class DurableAdmission:
    disposition: AdmissionDisposition
    job: JobRecord
    related_job_id: str | None = None


@dataclass(frozen=True)
class JobAssignment:
    job: JobRecord
    lease_id: str
    attempt_id: str
    lease_owner: str
    attempt: int
    acknowledged_by: dt.datetime
    lease_expires_at: dt.datetime
    required_capabilities: tuple[str, ...]


@dataclass(frozen=True)
class ResultCommitReceipt:
    job_id: str
    attempt: int
    result_hash: str
    committed_at: dt.datetime
    idempotent_replay: bool


@dataclass(frozen=True)
class ArtifactPublicationReceipt:
    job_id: str
    attempt_id: str
    artifact_digest: str
    artifact_size_bytes: int
    artifact_class: str
    target_key: str
    prior_digest: str | None
    result_hash: str | None
    disposition: str
    metadata: dict[str, str | int | float | bool | None]
    prepared_at: dt.datetime | None
    promoted_at: dt.datetime | None
    committed_at: dt.datetime | None


@dataclass(frozen=True)
class ReconciliationSummary:
    inspected: int = 0
    expired_deadlines: int = 0
    cancelled_pending: int = 0
    released_unacknowledged: int = 0
    uncertain_attempts: int = 0
    command_sync_pending: int = 0


@dataclass(frozen=True)
class RepositoryHealth:
    enabled: bool
    initialized: bool
    schema_version: int
    wal: bool
    admission_open: bool
    queue_counts: dict[str, int]
    active_leases: int
    overdue_jobs: int
    cancellation_backlog: int
    reconciliation_required: int

    def details(self) -> dict[str, bool | int | str]:
        details: dict[str, bool | int | str] = {
            "initialized": self.initialized,
            "schema_version": self.schema_version,
            "wal": self.wal,
            "admission_open": self.admission_open,
            "active_leases": self.active_leases,
            "overdue_jobs": self.overdue_jobs,
            "cancellation_backlog": self.cancellation_backlog,
            "reconciliation_required": self.reconciliation_required,
        }
        for queue, count in sorted(self.queue_counts.items()):
            details[f"{queue}_pending"] = count
        return details


class StaleJobMutationError(RuntimeError):
    pass


class JobStoreConflictError(RuntimeError):
    pass


class JobStoreValidationError(ValueError):
    pass


JsonObject = dict[str, Any]
