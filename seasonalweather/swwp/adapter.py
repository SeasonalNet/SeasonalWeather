"""Narrow controller adapter from SWWP sessions to P1-07 durable ports."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from ..job_store import JobRepository, JobScheduler
from ..job_store.models import JobAssignment, ResultCommitReceipt
from ..jobs.contracts import AttemptOutcome, JobError, JobRecord, JobStatus
from ..jobs.policies import ExecutorClass, JobType, QueueClass
from .constants import ReconcileDisposition
from .messages import (
    JobAssignmentPayload,
    JobFailed,
    JobProgress,
    JobResult,
    LeaseRef,
    ReconcileDecision,
    ReconcileItem,
)


class DurableSwwpPort(Protocol):
    def acquire(
        self,
        *,
        owner: str,
        queues: tuple[QueueClass, ...],
        executors: tuple[ExecutorClass, ...],
        capabilities: tuple[str, ...],
    ) -> JobAssignmentPayload | None: ...

    def acknowledge(self, lease: LeaseRef) -> JobRecord: ...

    def renew(self, lease: LeaseRef) -> JobRecord: ...

    def progress(self, progress: JobProgress) -> None: ...

    def result(self, result: JobResult) -> ResultCommitReceipt: ...

    def failure(self, failure: JobFailed) -> JobRecord | ResultCommitReceipt: ...

    def request_cancellation(self, job_id: str) -> JobRecord: ...

    def reconcile(self, item: ReconcileItem) -> ReconcileDecision: ...

    def reconcile_repository(self) -> None: ...

    def reject_unacknowledged(self, lease: LeaseRef) -> JobRecord: ...


class ArtifactResultPort(Protocol):
    def supports(self, job_type: JobType) -> bool: ...

    def accept(
        self,
        assignment: JobAssignment,
        message: JobResult,
        commit: Callable[[dict[str, object]], ResultCommitReceipt],
    ) -> ResultCommitReceipt: ...


@dataclass
class JobStoreSwwpAdapter:
    """The only SWWP owner allowed to import the concrete P1-07 boundary."""

    scheduler: JobScheduler
    repository: JobRepository
    artifact_results: ArtifactResultPort | None = None

    def __post_init__(self) -> None:
        self._assignments: dict[tuple[str, str, str, int], JobAssignment] = {}

    @staticmethod
    def _key(lease: LeaseRef) -> tuple[str, str, str, int]:
        return (lease.job_id, lease.lease_id, lease.attempt_id, lease.attempt)

    def acquire(
        self,
        *,
        owner: str,
        queues: tuple[QueueClass, ...],
        executors: tuple[ExecutorClass, ...],
        capabilities: tuple[str, ...],
    ) -> JobAssignmentPayload | None:
        assignment = self.scheduler.assign(
            owner=owner,
            queues=queues,
            executors=executors,
            capabilities=capabilities,
        )
        if assignment is None:
            return None
        return self.payload_for_assignment(assignment)

    def acquire_job(
        self,
        *,
        owner: str,
        queues: tuple[QueueClass, ...],
        executors: tuple[ExecutorClass, ...],
        capabilities: tuple[str, ...],
        job_id: str,
    ) -> JobAssignmentPayload | None:
        assignment = self.scheduler.assign(
            owner=owner,
            queues=queues,
            executors=executors,
            capabilities=capabilities,
            candidate_job_ids=(job_id,),
        )
        return self.payload_for_assignment(assignment) if assignment is not None else None

    def payload_for_assignment(
        self,
        assignment: JobAssignment,
    ) -> JobAssignmentPayload:
        lease = LeaseRef(
            job_id=assignment.job.job_id,
            lease_id=assignment.lease_id,
            attempt_id=assignment.attempt_id,
            attempt=assignment.attempt,
        )
        self._assignments[self._key(lease)] = assignment
        return JobAssignmentPayload(
            lease=lease,
            deadline_at=assignment.job.deadline_at,
            lease_expires_at=assignment.lease_expires_at,
            acknowledgment_deadline_at=assignment.acknowledged_by,
            job_type=assignment.job.job_type,
            queue=assignment.job.queue,
            executor=assignment.job.executor,
            payload_schema_version=assignment.job.payload_schema_version,
            result_schema_version=assignment.job.result_schema_version,
            configuration_generation=assignment.job.config_generation,
            payload=assignment.job.payload,
            capability_requirements=assignment.required_capabilities,
        )

    def _assignment(self, lease: LeaseRef) -> JobAssignment:
        try:
            return self._assignments[self._key(lease)]
        except KeyError as exc:
            raise KeyError("unknown session-local assignment") from exc

    def acknowledge(self, lease: LeaseRef) -> JobRecord:
        return self.scheduler.acknowledge(self._assignment(lease))

    def reject_unacknowledged(self, lease: LeaseRef) -> JobRecord:
        assignment = self._assignment(lease)
        result = self.scheduler.reject_unacknowledged(assignment)
        self._assignments.pop(self._key(lease), None)
        return result

    def renew(self, lease: LeaseRef) -> JobRecord:
        return self.scheduler.renew(self._assignment(lease))

    def progress(self, progress: JobProgress) -> None:
        self.scheduler.progress(
            self._assignment(progress.lease),
            stage=progress.stage,
            reason=progress.reason,
            numeric=progress.numeric,
        )

    def result(self, result: JobResult) -> ResultCommitReceipt:
        assignment = self._assignment(result.lease)
        if result.result_schema_version != assignment.job.result_schema_version:
            raise ValueError("result schema version differs from durable assignment")
        if self.artifact_results is not None and self.artifact_results.supports(assignment.job.job_type):
            return self.artifact_results.accept(
                assignment,
                result,
                lambda payload: self._commit_success(assignment, payload),
            )
        return self._commit_success(assignment, result.result)

    def _commit_success(
        self,
        assignment: JobAssignment,
        payload: dict[str, object],
    ) -> ResultCommitReceipt:
        receipt = self.scheduler.outcome(
            assignment,
            outcome=AttemptOutcome.SUCCEEDED,
            result_payload=payload,
        )
        if not isinstance(receipt, ResultCommitReceipt):
            raise RuntimeError("successful result did not produce durable commit receipt")
        return receipt

    def failure(self, failure: JobFailed) -> JobRecord | ResultCommitReceipt:
        error = JobError(
            category=failure.category,
            code=failure.error_code,
            message=failure.summary,
        )
        return self.scheduler.outcome(
            self._assignment(failure.lease),
            outcome=failure.outcome,
            error=error,
        )

    def request_cancellation(self, job_id: str) -> JobRecord:
        return self.repository.request_cancellation(job_id, at=self.scheduler.clock())

    def reconcile(self, item: ReconcileItem) -> ReconcileDecision:
        job = self.repository.get(item.lease.job_id)
        if job is None:
            disposition = ReconcileDisposition.UNKNOWN
            summary = "durable job is unknown"
        else:
            disposition, summary = self._reconcile_known(job, item)
        return ReconcileDecision(lease=item.lease, disposition=disposition, summary=summary)

    @staticmethod
    def _reconcile_known(job: JobRecord, item: ReconcileItem) -> tuple[ReconcileDisposition, str]:
        terminal = {JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.EXPIRED, JobStatus.SUPERSEDED}
        if job.status is JobStatus.SUCCEEDED:
            return ReconcileDisposition.ALREADY_COMMITTED, "durable result is already committed"
        if job.status in terminal:
            return ReconcileDisposition.DISCARD_STALE, "durable job is terminal"
        if job.cancel_requested:
            return ReconcileDisposition.CANCEL, "durable cancellation is pending"
        if job.status is JobStatus.RUNNING:
            return JobStoreSwwpAdapter._running_disposition(job, item)
        if job.status is JobStatus.LEASED:
            return JobStoreSwwpAdapter._leased_disposition(job, item)
        return ReconcileDisposition.DISCARD_STALE, "worker memory cannot recreate pending work"

    @staticmethod
    def _same_attempt(job: JobRecord, item: ReconcileItem) -> bool:
        return job.attempt_id == item.lease.attempt_id and job.attempt == item.lease.attempt

    @staticmethod
    def _running_disposition(job: JobRecord, item: ReconcileItem) -> tuple[ReconcileDisposition, str]:
        if JobStoreSwwpAdapter._same_attempt(job, item):
            if item.completion_id is not None:
                return ReconcileDisposition.RESEND_RESULT, "unacknowledged completion must be resent"
            return ReconcileDisposition.RESUME, "durable running attempt matches"
        return ReconcileDisposition.REVALIDATION_REQUIRED, "attempt requires revalidation"

    @staticmethod
    def _leased_disposition(job: JobRecord, item: ReconcileItem) -> tuple[ReconcileDisposition, str]:
        if JobStoreSwwpAdapter._same_attempt(job, item):
            return ReconcileDisposition.RENEW, "durable lease matches"
        return ReconcileDisposition.DISCARD_STALE, "lease is stale"

    def reconcile_repository(self) -> None:
        self.repository.reconcile(now=self.scheduler.clock(), batch_size=100)


def remote_executors(job_types: tuple[JobType, ...]) -> tuple[ExecutorClass, ...]:
    executors: set[ExecutorClass] = set()
    if any(
        job_type.value.startswith("routine.") or job_type is JobType.ALERT_ARTIFACT_GENERATE for job_type in job_types
    ):
        executors.add(ExecutorClass.ROUTINE_WORKER)
    if JobType.MAINTENANCE_RECONCILE in job_types:
        executors.add(ExecutorClass.MAINTENANCE_WORKER)
    return tuple(sorted(executors, key=lambda item: item.value))
