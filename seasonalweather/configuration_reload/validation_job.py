"""Embedded P1-15 handler executed through the accepted durable job authority."""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Callable
from typing import Any

from seasonalweather.job_store import (
    DurableJobService,
    JobStoreConflictError,
    JobStoreValidationError,
    StaleJobMutationError,
)
from seasonalweather.jobs.contracts import AttemptOutcome, JobError
from seasonalweather.jobs.policies import ExecutorClass, FailureCategory, JobType, QueueClass
from seasonalweather.validation.pipeline import (
    EnvironmentInputIdentity,
    ValidationContext,
    ValidationPolicy,
    validate_compiled,
)
from seasonalweather.validation.probe_factory import configured_preflight_probes

from .candidate_store import CandidateIntegrityError, CandidateStore
from .models import CandidateRecord

_DEFAULT_POLICY = ValidationPolicy(warning_acknowledgment_required=True)


class ValidationJobExecutionError(RuntimeError):
    def __init__(self, code: str, category: FailureCategory, message: str) -> None:
        self.code = code
        self.category = category
        super().__init__(message)


class ValidationJobRunner:
    OWNER = "controller.config-validator"

    def __init__(
        self,
        store: CandidateStore,
        job_service: DurableJobService | None,
        *,
        preflight_executor: Any = None,
        validation_policy: ValidationPolicy = _DEFAULT_POLICY,
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC).replace(microsecond=0),
    ) -> None:
        self.store = store
        self.job_service = job_service
        self.preflight_executor = preflight_executor
        self.validation_policy = validation_policy
        self.clock = clock

    async def execute(
        self,
        candidate: CandidateRecord,
        *,
        command_id: str,
        active_generation: int,
        preflight_required: bool,
    ) -> tuple[str | None, dict[str, object]]:
        job_id, assignment = self._admit(
            candidate,
            command_id=command_id,
            active_generation=active_generation,
            preflight_required=preflight_required,
        )
        try:
            result = await self._run_attempt(
                candidate,
                assignment,
                active_generation=active_generation,
                preflight_required=preflight_required,
            )
        except asyncio.CancelledError:
            self._record_cancellation(assignment)
            raise
        except Exception as exc:
            self._raise_classified_failure(assignment, exc)
        return job_id, result

    def _admit(
        self,
        candidate: CandidateRecord,
        *,
        command_id: str,
        active_generation: int,
        preflight_required: bool,
    ) -> tuple[str | None, Any]:
        payload = {
            "candidate_ref": candidate.reference,
            "candidate_sha256": candidate.candidate_sha256,
            "candidate_identity_sha256": candidate.candidate_identity_sha256,
            "current_generation": active_generation,
            "preflight_required": preflight_required,
        }
        job_id: str | None = None
        assignment = None
        if self.job_service is not None:
            admission = self.job_service.admit(
                job_type=JobType.CONFIG_VALIDATE,
                payload=payload,
                command_id=command_id,
                dedupe_key=f"config:{candidate.candidate_identity_sha256}:{active_generation}:{command_id}",
                config_generation=active_generation,
            )
            job_id = admission.job.job_id
            now = self.job_service.clock()
            assignment = self.job_service.repository.acquire_next(
                owner=self.OWNER,
                now=now,
                lease_seconds=650,
                acknowledgment_seconds=10,
                queues=(QueueClass.CONTROL,),
                executors=(ExecutorClass.CONTROLLER,),
                candidate_job_ids=(job_id,),
            )
            if assignment is None:
                existing = self.job_service.repository.get(job_id)
                if existing is None or existing.result is None:
                    raise RuntimeError("validation job could not be acquired or replayed")
                raise RuntimeError("validation job result artifact must be admitted by its owning attempt")
            self.job_service.repository.acknowledge(
                job_id=job_id,
                lease_id=assignment.lease_id,
                attempt_id=assignment.attempt_id,
                owner=assignment.lease_owner,
                at=self.job_service.clock(),
            )
        return job_id, assignment

    async def _run_attempt(
        self,
        candidate: CandidateRecord,
        assignment: Any,
        *,
        active_generation: int,
        preflight_required: bool,
    ) -> dict[str, object]:
        async with asyncio.timeout(600):
            result = await self._handle(
                candidate,
                active_generation=active_generation,
                preflight_required=preflight_required,
            )
        if assignment is not None and self.job_service is not None:
            self.job_service.repository.record_outcome(
                job_id=assignment.job.job_id,
                lease_id=assignment.lease_id,
                attempt_id=assignment.attempt_id,
                owner=assignment.lease_owner,
                outcome=AttemptOutcome.SUCCEEDED,
                at=self.job_service.clock(),
                result_payload=result,
            )
        return result

    def _raise_classified_failure(self, assignment: Any, exc: Exception) -> None:
        code, category, outcome, audit_message, raised_message = self._failure_details(exc)
        if outcome is not None:
            self._record_failure(
                assignment,
                outcome=outcome,
                category=category,
                code=code,
                message=audit_message,
            )
        raise ValidationJobExecutionError(code, category, raised_message) from exc

    @staticmethod
    def _failure_details(
        exc: Exception,
    ) -> tuple[str, FailureCategory, AttemptOutcome | None, str, str]:
        if isinstance(exc, TimeoutError):
            return (
                "config_validation_timed_out",
                FailureCategory.TIMED_OUT,
                AttemptOutcome.TIMED_OUT,
                "Configuration validation exceeded its bounded attempt timeout.",
                "configuration validation job timed out",
            )
        if isinstance(exc, StaleJobMutationError):
            return (
                "config_validation_lease_lost",
                FailureCategory.SIDE_EFFECT_UNCERTAIN,
                None,
                "",
                "configuration validation lost its durable lease",
            )
        if isinstance(exc, JobStoreConflictError):
            return (
                "config_validation_result_conflict",
                FailureCategory.SIDE_EFFECT_UNCERTAIN,
                None,
                "",
                "configuration validation result conflicts with durable evidence",
            )
        if isinstance(exc, CandidateIntegrityError | JobStoreValidationError | ValueError):
            return (
                "config_validation_invalid_candidate",
                FailureCategory.INVALID_INPUT,
                AttemptOutcome.PERMANENT_FAILURE,
                "Configuration validation rejected invalid candidate input.",
                "configuration validation rejected invalid candidate input",
            )
        return (
            "config_validation_dependency_failed",
            FailureCategory.DEPENDENCY_UNAVAILABLE,
            AttemptOutcome.PERMANENT_FAILURE,
            "Configuration validation dependency failed.",
            "configuration validation dependency failed",
        )

    def _record_cancellation(self, assignment: Any) -> None:
        if assignment is None or self.job_service is None:
            return
        try:
            self.job_service.repository.request_cancellation(assignment.job.job_id, at=self.job_service.clock())
            self._record_failure(
                assignment,
                outcome=AttemptOutcome.CANCELLED,
                category=FailureCategory.CANCELLED,
                code="config_validation_cancelled",
                message="Configuration validation was cancelled.",
            )
        except Exception:
            # Durable reconciliation owns an attempt whose lease vanished while
            # cancellation was being delivered. Cancellation itself must pass through.
            return

    def _record_failure(
        self,
        assignment: Any,
        *,
        outcome: AttemptOutcome,
        category: FailureCategory,
        code: str,
        message: str,
    ) -> None:
        if assignment is None or self.job_service is None:
            return
        self.job_service.repository.record_outcome(
            job_id=assignment.job.job_id,
            lease_id=assignment.lease_id,
            attempt_id=assignment.attempt_id,
            owner=assignment.lease_owner,
            outcome=outcome,
            at=self.job_service.clock(),
            error=JobError(category=category, code=code, message=message),
        )

    async def _handle(
        self,
        candidate: CandidateRecord,
        *,
        active_generation: int,
        preflight_required: bool,
    ) -> dict[str, object]:
        compiled = self.store.compile(candidate)
        environment = tuple(
            EnvironmentInputIdentity(
                variable=str(item["variable"]),
                present=bool(item["present"]),
                opaque_change_identity=(
                    str(item["opaque_change_identity"]) if item.get("opaque_change_identity") is not None else None
                ),
            )
            for item in candidate.environment_inputs
        )
        context = ValidationContext(
            active_configuration_generation=active_generation,
            preflight_enabled=preflight_required,
            preflight_probes=configured_preflight_probes(compiled) if preflight_required else (),
            preflight_executor=self.preflight_executor,
            environment_inputs=environment,
            policy=self.validation_policy,
            clock=self.clock,
        )
        report = await validate_compiled(compiled, context=context)
        mapping = report.to_dict()
        report_ref, report_sha256 = self.store.store_report(candidate, mapping)
        return {
            "candidate_ref": candidate.reference,
            "candidate_sha256": candidate.candidate_sha256,
            "candidate_identity_sha256": candidate.candidate_identity_sha256,
            "report_ref": report_ref,
            "report_sha256": report_sha256,
            "valid": report.decision.valid,
            "preflight_ready": report.decision.preflight_ready,
            "issue_codes": tuple(sorted({issue.code for issue in report.issues})),
        }
