"""Single controller-owned transactional configuration reload application service."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import replace
from typing import Any

from seasonalweather.commands.contracts import CommandRecord
from seasonalweather.commands.service import CommandStore, IdempotencyConflictError
from seasonalweather.configuration import build_runtime_config
from seasonalweather.configuration.compiler import CompiledConfiguration
from seasonalweather.database.configuration_reload import ReloadAdmissionConflictError, ReloadRepository
from seasonalweather.diagnostics.bindings import RELOAD_CODES, code_for_rule
from seasonalweather.validation.pipeline import verify_report_mapping

from .candidate_store import CandidateIntegrityError, CandidateStore
from .diff import build_reload_diff
from .models import (
    RELOAD_POLICY_VERSION,
    VALIDATION_REPORT_CLOCK_SKEW_SECONDS,
    ActiveGeneration,
    CandidateBinding,
    CandidateRecord,
    ReloadDiff,
    ReloadDisposition,
    ReloadOutcome,
    ReloadPhase,
    ReloadRequest,
    ReloadResult,
    WarningAcknowledgment,
    canonical_sha256,
)
from .report_freshness import ReportFreshness, ReportFreshnessError, require_report_fresh
from .resources import ResourcePreparer
from .safe_point import SafePointCoordinator, SafePointLease, SafePointTimeout
from .validation_job import ValidationJobRunner


class ReloadRejected(RuntimeError):
    def __init__(self, code: str, message: str, *, diagnostic_codes: tuple[str, ...] = ()) -> None:
        self.code = code
        self.safe_message = message
        self.diagnostic_codes = diagnostic_codes
        super().__init__(message)


class ReloadCancelled(ReloadRejected):
    def __init__(self) -> None:
        super().__init__("reload_cancelled", "Configuration reload was cancelled before commit.")


class PostCommitRecoveryRequired(RuntimeError):
    """Durable generation is authoritative but retirement/finalization remains."""


_SUCCESSFUL_COMMAND_OUTCOMES = frozenset(
    {
        ReloadOutcome.COMMITTED,
        ReloadOutcome.NOOP,
        ReloadOutcome.DRY_RUN,
        ReloadOutcome.RESTART_REQUIRED,
        ReloadOutcome.ACKNOWLEDGMENT_REQUIRED,
    }
)


class ConfigurationReloadService:
    """Owns candidate admission, validation, classification, and generation commit."""

    def __init__(
        self,
        *,
        config_path: str,
        candidate_store: CandidateStore,
        repository: ReloadRepository,
        command_store: CommandStore,
        validation_jobs: ValidationJobRunner,
        resource_preparer: ResourcePreparer,
        safe_points: SafePointCoordinator,
        active_configuration: Any,
        supervisor: Any | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        failure_injector: Callable[[str], None] = lambda _point: None,
        diagnostic_promoter: Callable[[str, str, BaseException], None] = lambda _code, _component, _error: None,
    ) -> None:
        self.config_path = str(config_path)
        self.candidates = candidate_store
        self.repository = repository
        self.command_store = command_store
        self.validation_jobs = validation_jobs
        self.resource_preparer = resource_preparer
        self.safe_points = safe_points
        self.supervisor = supervisor
        self._environ = environ
        self._clock = clock
        self._id_factory = id_factory
        self._failure_injector = failure_injector
        self._diagnostic_promoter = diagnostic_promoter
        self._commit_lock = asyncio.Lock()
        self._pending_retirements: dict[str, Any] = {}
        self._pending_rollbacks: dict[str, tuple[Any, ReloadOutcome, ReloadPhase]] = {}
        self._startup_retirement: Any | None = None
        active_candidate, active_compiled = self.candidates.capture(self.config_path)
        startup_candidate = active_candidate
        startup_compiled = active_compiled
        now = self._now()
        self.repository.initialize_active(active_candidate, configuration_generation=0, at=now)
        durable = self.repository.active()
        durable_identity = str(durable["candidate_identity_sha256"])
        self._startup_reconciliation_required = durable_identity != active_candidate.candidate_identity_sha256
        if self._startup_reconciliation_required:
            active_candidate = self.candidates.load(str(durable["candidate_reference"]))
            active_compiled = self.candidates.compile(active_candidate)
            active_configuration = build_runtime_config(active_compiled, environ=self._environ)
        self._active_candidate, self._active_compiled = active_candidate, active_compiled
        self._startup_candidate = startup_candidate
        self._startup_compiled = startup_compiled
        self._active = ActiveGeneration(
            generation=int(durable["generation"]),
            configuration=active_configuration,
            candidate_reference=active_candidate.reference,
            source_sha256=active_candidate.source_sha256,
            candidate_identity_sha256=active_candidate.candidate_identity_sha256,
            report_sha256=durable.get("report_sha256"),
            diff_sha256=durable.get("diff_sha256"),
            audit_reference=durable.get("audit_reference"),
        )

    @property
    def active(self) -> ActiveGeneration:
        return self._active

    async def admit(
        self,
        request: ReloadRequest,
        *,
        idempotency_key: str,
    ) -> tuple[CommandRecord, bool]:
        self._require_acknowledgment_actor(request)
        record, replayed, admitted_request = await self._admit_durable(request, idempotency_key=idempotency_key)
        if replayed:
            repaired = await self._repair_command_for_terminal_attempt(record)
            row = self.repository.get_by_command(record.command_id)
            if (
                row is not None
                and row.get("finished_at") is None
                and self.supervisor is not None
                and row.get("phase") == ReloadPhase.CANDIDATE_CAPTURED.value
            ):
                self.supervisor.create_task(
                    self.execute_command(record.command_id, admitted_request),
                    name=f"config_reload_{record.command_id}",
                    required=False,
                )
            return repaired, True
        if self.supervisor is None:
            raise ReloadRejected("reload_supervision_unavailable", "Reload execution supervision is unavailable.")
        self.supervisor.create_task(
            self.execute_command(record.command_id, admitted_request),
            name=f"config_reload_{record.command_id}",
            required=False,
        )
        return record, False

    async def _admit_durable(
        self,
        request: ReloadRequest,
        *,
        idempotency_key: str,
    ) -> tuple[CommandRecord, bool, ReloadRequest]:
        """Capture and journal the immutable candidate before asynchronous work."""

        journal = self.repository.get_admission(idempotency_key)
        if journal is not None:
            return await self._repair_admission_journal(journal, request, idempotency_key)

        candidate, _compiled = self.candidates.capture(request.source_path or self.config_path)
        admitted_request = replace(
            request,
            candidate=CandidateBinding.from_candidate(candidate),
            source_path=None,
        )
        old = self._active
        attempt_id = f"reload_{self._id_factory()[:24]}"
        try:
            journal, created = self.repository.begin_admission(
                idempotency_key=idempotency_key,
                attempt_id=attempt_id,
                actor=admitted_request.actor,
                reason=admitted_request.reason,
                request=admitted_request,
                candidate=candidate,
                old_generation=old.generation,
                old_candidate_reference=old.candidate_reference,
                old_source_sha256=old.source_sha256,
                old_candidate_identity_sha256=old.candidate_identity_sha256,
                at=self._now(),
            )
        except ReloadAdmissionConflictError as exc:
            raise IdempotencyConflictError(str(exc)) from exc
        if not created:
            return await self._repair_admission_journal(journal, request, idempotency_key)

        try:
            record, replayed = await self.command_store.create_or_replay(
                command_type="config.reload",
                idempotency_key=idempotency_key,
                actor=request.actor,
                payload=admitted_request.command_payload(),
                reason=request.reason,
            )
        except IdempotencyConflictError:
            self.repository.discard_admission(idempotency_key, attempt_id=attempt_id)
            raise
        self.repository.bind_admission_command(idempotency_key=idempotency_key, command_id=record.command_id)
        if replayed:
            existing_attempt = self.repository.get_by_command(record.command_id)
            if existing_attempt is not None:
                self.repository.complete_admission(
                    idempotency_key,
                    attempt_id=str(existing_attempt["attempt_id"]),
                    command_id=record.command_id,
                )
                return record, True, admitted_request
        if not replayed:
            self._failure_injector("after_durable_command_before_reload_attempt")
        self.repository.create_attempt(
            attempt_id=attempt_id,
            command_id=record.command_id,
            actor=admitted_request.actor,
            reason=admitted_request.reason,
            dry_run=admitted_request.dry_run,
            old_generation=old.generation,
            expected_generation=admitted_request.expected_generation,
            at=self._now(),
            candidate=candidate,
            request=admitted_request,
            idempotency_identity=canonical_sha256(
                {
                    "key_identity": hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest(),
                    "command_payload": admitted_request.command_payload(),
                }
            ),
            old_candidate_reference=old.candidate_reference,
            old_source_sha256=old.source_sha256,
            old_candidate_identity_sha256=old.candidate_identity_sha256,
        )
        self.repository.complete_admission(idempotency_key, attempt_id=attempt_id, command_id=record.command_id)
        return record, replayed, admitted_request

    async def _repair_admission_journal(
        self,
        journal: Mapping[str, object],
        request: ReloadRequest,
        idempotency_key: str,
    ) -> tuple[CommandRecord, bool, ReloadRequest]:
        stored_request = _request_from_attempt(journal)
        if (
            _request_without_candidate(request).command_payload()
            != _request_without_candidate(stored_request).command_payload()
        ):
            raise IdempotencyConflictError("idempotency key was reused with different reload material")
        self._verify_replayed_admission_candidate(journal, request)
        record, replayed = await self.command_store.create_or_replay(
            command_type="config.reload",
            idempotency_key=idempotency_key,
            actor=stored_request.actor,
            payload=stored_request.command_payload(),
            reason=stored_request.reason,
        )
        self.repository.bind_admission_command(idempotency_key=idempotency_key, command_id=record.command_id)
        attempt = self.repository.get_by_command(record.command_id)
        if attempt is None:
            candidate = self.candidates.load(str(journal["candidate_reference"]))
            self.repository.create_attempt(
                attempt_id=str(journal["attempt_id"]),
                command_id=record.command_id,
                actor=str(journal["actor"]),
                reason=str(journal["reason"]) if journal.get("reason") is not None else None,
                dry_run=stored_request.dry_run,
                old_generation=int(str(journal["old_generation"])),
                expected_generation=stored_request.expected_generation,
                at=_timestamp_or(journal.get("created_at"), self._now()),
                candidate=candidate,
                request=stored_request,
                idempotency_identity=canonical_sha256(
                    {
                        "key_identity": hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest(),
                        "command_payload": stored_request.command_payload(),
                    }
                ),
                old_candidate_reference=str(journal["old_candidate_reference"]),
                old_source_sha256=str(journal["old_source_sha256"]),
                old_candidate_identity_sha256=str(journal["old_candidate_identity_sha256"]),
            )
        self.repository.complete_admission(
            idempotency_key,
            attempt_id=str(journal["attempt_id"]),
            command_id=record.command_id,
        )
        return record, True if replayed or attempt is None else replayed, stored_request

    def _verify_replayed_admission_candidate(
        self,
        journal: Mapping[str, object],
        request: ReloadRequest,
    ) -> None:
        command_id = journal.get("command_id")
        if not command_id or self.repository.get_by_command(str(command_id)) is None:
            return
        if request.source_path is None:
            return
        current, _compiled = self.candidates.capture(request.source_path)
        stored = CandidateBinding.from_candidate(self.candidates.load(str(journal["candidate_reference"])))
        if (
            current.source_sha256 != stored.source_sha256
            or current.candidate_identity_sha256 != stored.candidate_identity_sha256
        ):
            raise IdempotencyConflictError("idempotency key was reused with different candidate material")

    async def execute_command(self, command_id: str, request: ReloadRequest | None = None) -> ReloadResult:
        row, request = self._bound_request(command_id, request)
        self._require_acknowledgment_actor(request)
        command = await self.command_store.get(command_id)
        prior_result = await self._prior_terminal_result(command_id, command)
        if prior_result is not None:
            return prior_result
        attempt_id = str(row["attempt_id"])
        old = self._active
        old_candidate = self._active_candidate
        old_compiled = self._active_compiled
        try:
            await self._require_not_cancelled(request, attempt_id)
            await self.command_store.mark_running(command_id)
            result = await self._execute(
                attempt_id,
                command_id,
                request,
                old,
                old_candidate,
                old_compiled,
            )
        except asyncio.CancelledError as exc:
            self._record_cancelled_audit(attempt_id, request, old, exc)
            await self._cancel_command_if_open(command_id)
            raise
        except ReloadCancelled as exc:
            self._record_cancelled_audit(attempt_id, request, old, exc)
            await self._cancel_command_if_open(command_id)
            raise
        except ReloadRejected as exc:
            self._record_failed_audit(attempt_id, request, old, exc)
            await self.command_store.mark_failed(
                command_id,
                {
                    "code": exc.code,
                    "message": exc.safe_message,
                    "details": {"diagnostic_codes": list(exc.diagnostic_codes)},
                },
            )
            raise
        except PostCommitRecoveryRequired:
            raise
        except BaseException as exc:
            self._record_failed_audit(attempt_id, request, old, exc)
            self._diagnostic_promoter(
                RELOAD_CODES["candidate_or_preparation_failed"],
                "configuration_reload.preparation",
                exc,
            )
            await self.command_store.mark_failed(
                command_id,
                {"code": "config_reload_failed", "message": "Configuration reload failed safely."},
            )
            raise ReloadRejected("config_reload_failed", "Configuration reload failed safely.") from exc
        attempt_row = self.repository.get_attempt(attempt_id)
        if attempt_row is not None and attempt_row.get("finished_at") is None:
            return result
        if result.outcome is ReloadOutcome.CANCELLED:
            await self.command_store.mark_cancelled(command_id)
        else:
            await self._finalize_command(command_id, result)
        return result

    def _bound_request(
        self,
        command_id: str,
        supplied: ReloadRequest | None,
    ) -> tuple[dict[str, Any], ReloadRequest]:
        row = self.repository.get_by_command(command_id)
        if row is None:
            raise ReloadRejected("reload_admission_incomplete", "Reload command has no captured candidate admission.")
        stored_request = _request_from_attempt(row)
        if supplied is not None and supplied.command_payload() != stored_request.command_payload():
            raise ReloadRejected(
                "reload_command_binding_mismatch", "Reload command payload differs from durable admission."
            )
        return row, stored_request

    async def _prior_terminal_result(
        self,
        command_id: str,
        command: CommandRecord,
    ) -> ReloadResult | None:
        prior = self.repository.get_by_command(command_id)
        if prior is None:
            return None
        if prior.get("finished_at") is None:
            if prior.get("phase") == ReloadPhase.CANDIDATE_CAPTURED.value:
                return None
            raise PostCommitRecoveryRequired("configuration reload attempt is still awaiting recovery")
        if not prior.get("audit_json"):
            raise ReloadRejected(
                "config_reload_evidence_incomplete",
                "Durable reload terminal evidence is incomplete.",
            )
        payload = json.loads(str(prior["audit_json"]))
        if _audit_has_result(payload):
            result = _result_from_audit(payload)
            if command.status.value not in {"succeeded", "failed", "cancelled"}:
                await self._finalize_command(command_id, result)
            return result
        if command.status.value not in {"succeeded", "failed", "cancelled"}:
            await self._finalize_command_from_row(prior)
        raise ReloadRejected(
            str(payload.get("failure_code") or "config_reload_failed"),
            "Durable reload evidence records a prior failed command.",
            diagnostic_codes=_string_tuple(payload.get("diagnostic_codes")),
        )

    def _record_failed_audit(
        self,
        attempt_id: str,
        request: ReloadRequest,
        old: ActiveGeneration,
        error: BaseException,
    ) -> None:
        rejected = error if isinstance(error, ReloadRejected) else None
        row = self.repository.get_attempt(attempt_id) or {}
        completed = self._now()
        created = _timestamp_or(row.get("created_at"), completed)
        rollback_error = getattr(error, "rollback_failure", None)
        self.repository.fail_attempt(
            attempt_id,
            phase=ReloadPhase.REJECTED,
            outcome=ReloadOutcome.FAILED.value,
            audit={
                "schema_version": 1,
                "attempt_id": attempt_id,
                "audit_reference": f"audit_{attempt_id.removeprefix('reload_')}",
                "outcome": ReloadOutcome.FAILED.value,
                "phase": ReloadPhase.REJECTED.value,
                "command_id": row.get("command_id"),
                "idempotency_identity": row.get("idempotency_identity"),
                "actor": request.actor,
                "auth_context": _safe_mapping(request.authorization_context),
                "reason": request.reason,
                "dry_run": request.dry_run,
                "old_generation": old.generation,
                "old_candidate_identity_sha256": old.candidate_identity_sha256,
                "candidate_reference": row.get("candidate_reference"),
                "candidate_source_sha256": row.get("source_sha256"),
                "candidate_byte_length": row.get("source_byte_length"),
                "candidate_source_manifest_sha256": row.get("source_manifest_sha256"),
                "candidate_sha256": row.get("candidate_sha256"),
                "candidate_identity_sha256": row.get("candidate_identity_sha256"),
                "failure_code": rejected.code if rejected else "config_reload_failed",
                "diagnostic_codes": list(rejected.diagnostic_codes) if rejected else [],
                "failure_evidence": self._failure_evidence(error, rollback_error),
                "rollback_state": "failed" if rollback_error else "not_required",
                "requested_at": created.isoformat(),
                "started_at": created.isoformat(),
                "completed_at": completed.isoformat(),
                "duration_seconds": round(max(0.0, (completed - created).total_seconds()), 6),
            },
            at=self._now(),
        )

    async def _execute(
        self,
        attempt_id: str,
        command_id: str,
        request: ReloadRequest,
        old: ActiveGeneration,
        old_candidate: CandidateRecord,
        old_compiled: CompiledConfiguration,
    ) -> ReloadResult:
        candidate, candidate_compiled = self._load_command_candidate(attempt_id, request, old)
        await self._require_not_cancelled(request, attempt_id)
        self.repository.transition(attempt_id, ReloadPhase.VALIDATION_QUEUED, at=self._now())
        self.repository.transition(attempt_id, ReloadPhase.VALIDATION_RUNNING, at=self._now())
        report_ref, report_sha256, report_mapping = await self._report(
            candidate,
            request.acknowledgment,
            command_id=command_id,
            generation=old.generation,
            preflight_required=not request.dry_run,
        )
        await self._require_not_cancelled(request, attempt_id)
        freshness = self._freshness(report_mapping)
        verification = verify_report_mapping(
            report_mapping,
            expected_candidate_sha256=candidate.candidate_sha256,
            expected_candidate_identity_sha256=candidate.candidate_identity_sha256,
            expected_report_sha256=report_sha256,
            current_active_generation=self._active.generation,
            require_fresh_generation=True,
        )
        if not verification.accepted:
            self.repository.transition(attempt_id, ReloadPhase.REJECTED, at=self._now(), finished_at=self._iso_now())
            raise ReloadRejected(
                "validation_report_rejected",
                "Validation report failed independent controller verification.",
                diagnostic_codes=(code_for_rule("validation.report_rejected"),),
            )
        self.repository.transition(
            attempt_id,
            ReloadPhase.REPORT_VERIFIED,
            at=self._now(),
            report_reference=report_ref,
            report_sha256=report_sha256,
        )
        await self._require_not_cancelled(request, attempt_id)
        summary = _mapping(report_mapping.get("summary"))
        if not bool(summary.get("valid")) or (not request.dry_run and not bool(summary.get("preflight_ready"))):
            self.repository.transition(attempt_id, ReloadPhase.REJECTED, at=self._now(), finished_at=self._iso_now())
            raise ReloadRejected("candidate_invalid", "Configuration candidate is not valid and ready for reload.")
        warning_ids, warning_paths = _warnings(report_mapping)
        diff = build_reload_diff(
            old_compiled,
            candidate_compiled,
            active_generation=old.generation,
            active_identity_sha256=old.candidate_identity_sha256,
            candidate_identity_sha256=candidate.candidate_identity_sha256,
            report_sha256=report_sha256,
            warning_paths=warning_paths,
            active_environment_inputs=old_candidate.environment_inputs,
            candidate_environment_inputs=candidate.environment_inputs,
        )
        self.repository.transition(
            attempt_id,
            ReloadPhase.CLASSIFIED,
            at=self._now(),
            diff_sha256=diff.digest,
            disposition=diff.disposition.value,
        )
        terminal = await self._report_only_result(
            attempt_id,
            request,
            candidate,
            report_sha256,
            diff,
            old,
            warning_ids,
            freshness,
            warning_acknowledgment_required=bool(summary.get("warning_acknowledgment_required")),
        )
        if terminal is not None:
            return terminal
        configuration = build_runtime_config(candidate_compiled, environ=self._environ)
        self.repository.transition(attempt_id, ReloadPhase.PREPARING, at=self._now())
        await self._require_not_cancelled(request, attempt_id)
        plan = await self.resource_preparer.prepare(
            configuration,
            diff=diff,
            expected_generation=old.generation,
            target_generation=old.generation + 1,
            candidate_identity_sha256=candidate.candidate_identity_sha256,
        )
        try:
            await self._require_not_cancelled(request, attempt_id)
            plan.validate_ready()
            self._verify_prepared_plan(plan, diff, candidate, old.generation)
        except asyncio.CancelledError as exc:
            rollback_error = self._rollback_for_failure(plan, exc, {"rollback": "not_required"})
            if rollback_error is not None:
                return self._rollback_recovery_result(
                    attempt_id,
                    request,
                    candidate,
                    report_sha256,
                    diff,
                    warning_ids,
                    plan,
                    {"safe_lease": None, "durable": False, "swapped": False, "rollback": "failed"},
                    ReloadOutcome.CANCELLED,
                    ReloadPhase.CANCELLED,
                    exc,
                    rollback_error,
                )
            raise
        except BaseException as exc:
            rollback_error = self._rollback_for_failure(plan, exc, {"rollback": "not_required"})
            if rollback_error is not None:
                return self._rollback_recovery_result(
                    attempt_id,
                    request,
                    candidate,
                    report_sha256,
                    diff,
                    warning_ids,
                    plan,
                    {"safe_lease": None, "durable": False, "swapped": False, "rollback": "failed"},
                    ReloadOutcome.FAILED,
                    ReloadPhase.ROLLED_BACK,
                    exc,
                    rollback_error,
                )
            raise
        return await self._commit(
            attempt_id,
            request,
            candidate,
            candidate_compiled,
            report_ref,
            report_sha256,
            report_mapping,
            diff,
            warning_ids,
            old,
            plan,
        )

    def _load_command_candidate(
        self,
        attempt_id: str,
        request: ReloadRequest,
        old: ActiveGeneration,
    ) -> tuple[CandidateRecord, CompiledConfiguration]:
        row = self.repository.get_attempt(attempt_id)
        if row is None or int(row["old_generation"]) != old.generation:
            raise ReloadRejected("stale_active_generation", "Active generation changed after reload admission.")
        if request.expected_generation is not None and request.expected_generation != old.generation:
            self.repository.transition(attempt_id, ReloadPhase.REJECTED, at=self._now(), finished_at=self._iso_now())
            raise ReloadRejected("stale_active_generation", "Expected active configuration generation is stale.")
        if request.candidate is None:
            raise ReloadRejected("reload_command_binding_missing", "Reload command has no immutable candidate binding.")
        candidate = self.candidates.load(request.candidate.reference)
        if not request.candidate.matches(candidate):
            raise ReloadRejected("reload_command_binding_mismatch", "Captured candidate differs from durable command.")
        return candidate, self.candidates.compile(candidate)

    async def _report_only_result(
        self,
        attempt_id: str,
        request: ReloadRequest,
        candidate: CandidateRecord,
        report_sha256: str,
        diff: ReloadDiff,
        old: ActiveGeneration,
        warning_ids: tuple[str, ...],
        freshness: ReportFreshness,
        *,
        warning_acknowledgment_required: bool,
    ) -> ReloadResult | None:
        if not diff.effective_change:
            await self._require_not_cancelled(request, attempt_id)
            return self._terminal_result(
                attempt_id,
                request,
                candidate,
                report_sha256,
                diff,
                ReloadOutcome.NOOP,
                ReloadPhase.COMPLETED,
                old.generation,
                warning_ids,
                "Candidate has no effective configuration changes.",
            )
        if diff.disposition is ReloadDisposition.RESTART_REQUIRED:
            await self._require_not_cancelled(request, attempt_id)
            return self._terminal_result(
                attempt_id,
                request,
                candidate,
                report_sha256,
                diff,
                ReloadOutcome.RESTART_REQUIRED,
                ReloadPhase.RESTART_REQUIRED,
                old.generation,
                warning_ids,
                "Candidate is valid but requires a process restart; nothing was applied.",
                diagnostic_codes=(RELOAD_CODES["operator_action_required"],),
            )
        acknowledged = self._acknowledged(
            request.acknowledgment,
            request.actor,
            candidate,
            report_sha256,
            old.generation,
            warning_ids,
            freshness,
        )
        if warning_ids and warning_acknowledgment_required and not acknowledged:
            await self._require_not_cancelled(request, attempt_id)
            return self._terminal_result(
                attempt_id,
                request,
                candidate,
                report_sha256,
                diff,
                ReloadOutcome.ACKNOWLEDGMENT_REQUIRED,
                ReloadPhase.AWAITING_ACKNOWLEDGMENT,
                old.generation,
                warning_ids,
                "Exact warning acknowledgment is required before commit.",
                diagnostic_codes=(RELOAD_CODES["operator_action_required"],),
                acknowledgment_challenge=self._acknowledgment_challenge(
                    request, candidate, report_sha256, old.generation, warning_ids, freshness
                ),
            )
        if request.dry_run:
            await self._require_not_cancelled(request, attempt_id)
            return self._terminal_result(
                attempt_id,
                request,
                candidate,
                report_sha256,
                diff,
                ReloadOutcome.DRY_RUN,
                ReloadPhase.COMPLETED,
                old.generation,
                warning_ids,
                "Dry run completed; no runtime state was changed.",
            )
        return None

    async def _report(
        self,
        candidate: CandidateRecord,
        acknowledgment: WarningAcknowledgment | None,
        *,
        command_id: str,
        generation: int,
        preflight_required: bool,
    ) -> tuple[str, str, dict[str, object]]:
        if acknowledgment is not None:
            report_ref = f"report_{acknowledgment.report_sha256[:40]}"
            try:
                mapping = self.candidates.load_report(candidate, report_ref, acknowledgment.report_sha256)
                require_report_fresh(mapping, now=self._now())
                return report_ref, acknowledgment.report_sha256, mapping
            except (CandidateIntegrityError, ReportFreshnessError):
                pass
        _job_id, result = await self.validation_jobs.execute(
            candidate,
            command_id=command_id,
            active_generation=generation,
            preflight_required=preflight_required,
        )
        report_ref = str(result["report_ref"])
        report_sha256 = str(result["report_sha256"])
        return report_ref, report_sha256, self.candidates.load_report(candidate, report_ref, report_sha256)

    def _freshness(self, report: Mapping[str, object]) -> ReportFreshness:
        try:
            return require_report_fresh(report, now=self._now())
        except ReportFreshnessError as exc:
            raise ReloadRejected(
                exc.code,
                "Validation report is outside the approved freshness window; fresh validation is required.",
                diagnostic_codes=(code_for_rule("validation.report_rejected"),),
            ) from exc

    async def _commit(
        self,
        attempt_id: str,
        request: ReloadRequest,
        candidate: CandidateRecord,
        candidate_compiled: CompiledConfiguration,
        report_ref: str,
        report_sha256: str,
        report_mapping: dict[str, object],
        diff: ReloadDiff,
        warning_ids: tuple[str, ...],
        old: ActiveGeneration,
        plan: Any,
    ) -> ReloadResult:
        progress: dict[str, Any] = {
            "swapped": False,
            "durable": False,
            "safe_lease": None,
            "activation": "not_started",
            "rollback": "not_required",
        }
        try:
            await self._serialized_commit(
                attempt_id,
                request,
                candidate,
                candidate_compiled,
                report_ref,
                report_sha256,
                report_mapping,
                diff,
                warning_ids,
                old,
                plan,
                progress,
            )
        except asyncio.CancelledError as exc:
            rollback_error = self._rollback_for_failure(plan, exc, progress)
            if rollback_error is not None:
                self._rollback_recovery_result(
                    attempt_id,
                    request,
                    candidate,
                    report_sha256,
                    diff,
                    warning_ids,
                    plan,
                    progress,
                    ReloadOutcome.CANCELLED,
                    ReloadPhase.CANCELLED,
                    exc,
                    rollback_error,
                )
                raise
            self._terminal_result(
                attempt_id,
                request,
                candidate,
                report_sha256,
                diff,
                ReloadOutcome.CANCELLED,
                ReloadPhase.CANCELLED,
                old.generation,
                warning_ids,
                "Configuration reload task was cancelled before commit.",
                diagnostic_codes=self._rollback_diagnostics(None, rollback_error),
                safe_point=self._successful_safe_point(progress),
                failure_evidence=self._failure_evidence(exc, rollback_error),
                lifecycle_states=progress,
            )
            raise
        except SafePointTimeout as exc:
            rollback_error = self._rollback_for_failure(plan, exc, progress)
            if rollback_error is not None:
                return self._rollback_recovery_result(
                    attempt_id,
                    request,
                    candidate,
                    report_sha256,
                    diff,
                    warning_ids,
                    plan,
                    progress,
                    ReloadOutcome.DEFERRED,
                    ReloadPhase.DEFERRED,
                    exc,
                    rollback_error,
                )
            self._diagnostic_promoter(exc.diagnostic_code, "configuration_reload.safe_point", exc)
            return self._terminal_result(
                attempt_id,
                request,
                candidate,
                report_sha256,
                diff,
                ReloadOutcome.DEFERRED,
                ReloadPhase.DEFERRED,
                old.generation,
                warning_ids,
                "Safe-point timeout deferred the reload; the old generation remains active.",
                diagnostic_codes=self._rollback_diagnostics(exc.diagnostic_code, rollback_error),
                safe_point={"outcome": "timed_out", **exc.snapshot.to_dict()},
                failure_evidence=self._failure_evidence(exc, rollback_error),
                lifecycle_states=progress,
            )
        except ReloadCancelled as exc:
            return self._handle_reload_cancelled(
                attempt_id,
                request,
                candidate,
                report_sha256,
                diff,
                warning_ids,
                old,
                plan,
                progress,
                exc,
            )
        except ReloadRejected as exc:
            rejected = self._handle_rejected_commit(
                attempt_id,
                request,
                candidate,
                report_sha256,
                diff,
                warning_ids,
                old,
                plan,
                progress,
                exc,
            )
            if rejected is not None:
                return rejected
            raise
        except BaseException as exc:
            return self._handle_unexpected_commit_failure(
                attempt_id,
                request,
                candidate,
                report_sha256,
                diff,
                warning_ids,
                old,
                plan,
                progress,
                exc,
            )
        finally:
            if progress["safe_lease"] is not None:
                progress["safe_lease"].release()
        return await self._retirement_result(
            attempt_id,
            request,
            candidate,
            report_sha256,
            diff,
            warning_ids,
            plan,
            progress,
        )

    def _handle_reload_cancelled(
        self,
        attempt_id: str,
        request: ReloadRequest,
        candidate: CandidateRecord,
        report_sha256: str,
        diff: ReloadDiff,
        warning_ids: tuple[str, ...],
        old: ActiveGeneration,
        plan: Any,
        progress: dict[str, Any],
        error: ReloadCancelled,
    ) -> ReloadResult:
        rollback_error = self._rollback_for_failure(plan, error, progress)
        if rollback_error is not None:
            return self._rollback_recovery_result(
                attempt_id,
                request,
                candidate,
                report_sha256,
                diff,
                warning_ids,
                plan,
                progress,
                ReloadOutcome.CANCELLED,
                ReloadPhase.CANCELLED,
                error,
                rollback_error,
            )
        return self._terminal_result(
            attempt_id,
            request,
            candidate,
            report_sha256,
            diff,
            ReloadOutcome.CANCELLED,
            ReloadPhase.CANCELLED,
            old.generation,
            warning_ids,
            "Configuration reload was cancelled before commit.",
            diagnostic_codes=self._rollback_diagnostics(None, rollback_error),
            safe_point=self._successful_safe_point(progress),
            failure_evidence=self._failure_evidence(error, rollback_error),
            lifecycle_states=progress,
        )

    def _handle_rejected_commit(
        self,
        attempt_id: str,
        request: ReloadRequest,
        candidate: CandidateRecord,
        report_sha256: str,
        diff: ReloadDiff,
        warning_ids: tuple[str, ...],
        old: ActiveGeneration,
        plan: Any,
        progress: dict[str, Any],
        error: ReloadRejected,
    ) -> ReloadResult | None:
        rollback_error = self._rollback_for_failure(plan, error, progress)
        if rollback_error is None:
            self.repository.transition(
                attempt_id,
                ReloadPhase.ROLLED_BACK,
                at=self._now(),
                finished_at=self._iso_now(),
            )
            return None
        return self._commit_failure_result(
            attempt_id,
            request,
            candidate,
            report_sha256,
            diff,
            warning_ids,
            old,
            plan,
            progress,
            error,
            rollback_error=rollback_error,
        )

    def _handle_unexpected_commit_failure(
        self,
        attempt_id: str,
        request: ReloadRequest,
        candidate: CandidateRecord,
        report_sha256: str,
        diff: ReloadDiff,
        warning_ids: tuple[str, ...],
        old: ActiveGeneration,
        plan: Any,
        progress: dict[str, Any],
        error: BaseException,
    ) -> ReloadResult:
        if progress["durable"]:
            self._pending_retirements[attempt_id] = plan
            progress["retirement"] = "pending"
            self._terminal_result(
                attempt_id,
                request,
                candidate,
                report_sha256,
                diff,
                ReloadOutcome.COMMITTED,
                ReloadPhase.COMMITTED,
                self._active.generation,
                warning_ids,
                "Generation is durable; resource retirement and command finalization remain pending.",
                diagnostic_codes=(RELOAD_CODES["reconciliation_required"],),
                retirement_pending=True,
                cleanup_state="pending",
                retirement_evidence={
                    "state": "pending",
                    "descriptor": self._retirement_descriptor(plan, old.generation),
                },
                finish=False,
            )
            self._diagnostic_promoter(
                RELOAD_CODES["reconciliation_required"],
                "configuration_reload.postcommit",
                error,
            )
            raise PostCommitRecoveryRequired("durable configuration commit requires postcommit recovery") from error
        return self._commit_failure_result(
            attempt_id,
            request,
            candidate,
            report_sha256,
            diff,
            warning_ids,
            old,
            plan,
            progress,
            error,
        )

    async def _serialized_commit(
        self,
        attempt_id: str,
        request: ReloadRequest,
        candidate: CandidateRecord,
        candidate_compiled: CompiledConfiguration,
        report_ref: str,
        report_sha256: str,
        report_mapping: dict[str, object],
        diff: ReloadDiff,
        warning_ids: tuple[str, ...],
        old: ActiveGeneration,
        plan: Any,
        progress: dict[str, Any],
    ) -> None:
        async with self._commit_lock:
            await self._require_not_cancelled(request, attempt_id)
            if self._active.generation != old.generation:
                raise ReloadRejected("stale_active_generation", "A newer configuration committed first.")
            self._verify_prepared_plan(plan, diff, candidate, old.generation)
            freshness = self._reverify(
                candidate,
                report_ref,
                report_sha256,
                report_mapping,
                diff,
                request,
                warning_ids,
                old,
                plan,
            )
            if plan.required_disposition is ReloadDisposition.QUIESCENT:
                self.repository.transition(attempt_id, ReloadPhase.AWAITING_SAFE_POINT, at=self._now())
                progress["safe_lease"] = await self.safe_points.acquire(
                    request.safe_point_timeout_seconds,
                    abort=lambda: self._require_not_cancelled(request, attempt_id),
                )
                self._final_volatile_fence(
                    candidate,
                    report_ref,
                    report_sha256,
                    diff,
                    request,
                    warning_ids,
                    old,
                    plan,
                    freshness,
                )
                command_row = self.repository.get_attempt(attempt_id)
                if command_row is not None and self.command_store.cancellation_requested(
                    str(command_row["command_id"])
                ):
                    raise ReloadCancelled()
            self._failure_injector("before_durable_intent")
            audit_reference = f"audit_{attempt_id.removeprefix('reload_')}"
            self.repository.record_intent(
                attempt_id,
                expected_generation=old.generation,
                intent={
                    "candidate_reference": candidate.reference,
                    "candidate_identity_sha256": candidate.candidate_identity_sha256,
                    "report_sha256": report_sha256,
                    "diff_sha256": diff.digest,
                },
                at=self._now(),
            )
            self._failure_injector("after_durable_intent")
            plan.activate(safe_point_acquired=progress["safe_lease"] is not None)
            progress["activation"] = "completed"
            progress["swapped"] = True
            self._failure_injector("after_reference_swap")
            generation = self.repository.complete_commit(
                attempt_id,
                expected_generation=old.generation,
                candidate=candidate,
                report_sha256=report_sha256,
                diff_sha256=diff.digest,
                audit_reference=audit_reference,
                retirement_descriptor=self._retirement_descriptor(plan, old.generation),
                at=self._now(),
            )
            progress["durable"] = True
            self._active = ActiveGeneration(
                generation=generation,
                configuration=plan.configuration,
                candidate_reference=candidate.reference,
                source_sha256=candidate.source_sha256,
                candidate_identity_sha256=candidate.candidate_identity_sha256,
                report_sha256=report_sha256,
                diff_sha256=diff.digest,
                audit_reference=audit_reference,
                resources=plan,
            )
            self._active_candidate = candidate
            self._active_compiled = candidate_compiled
            self._failure_injector("after_durable_completion")

    async def reconcile_startup(self) -> tuple[str, ...]:
        self.resource_preparer.synchronize_generation(self._active.generation)
        admission_repaired: list[str] = []
        for journal in self.repository.incomplete_admissions(limit=500):
            key = str(journal["idempotency_key"])
            stored_request = _request_from_attempt(journal)
            _record, _replayed, _request = await self._repair_admission_journal(
                journal,
                stored_request,
                key,
            )
            admission_repaired.append(str(journal["attempt_id"]))
        if self._startup_retirement is not None:
            await self._retire_startup_plan(self._startup_retirement)
        if self._startup_reconciliation_required:
            plan = await self.resource_preparer.prepare(
                self._active.configuration,
                diff=self._startup_reconciliation_diff(),
                expected_generation=self._active.generation,
                target_generation=self._active.generation,
                candidate_identity_sha256=self._active.candidate_identity_sha256,
            )
            plan.validate_ready()
            plan.activate(safe_point_acquired=True)
            self._active = ActiveGeneration(
                generation=self._active.generation,
                configuration=plan.configuration,
                candidate_reference=self._active.candidate_reference,
                source_sha256=self._active.source_sha256,
                candidate_identity_sha256=self._active.candidate_identity_sha256,
                report_sha256=self._active.report_sha256,
                diff_sha256=self._active.diff_sha256,
                audit_reference=self._active.audit_reference,
                resources=plan,
            )
            self._startup_reconciliation_required = False
            self._startup_retirement = plan
            await self._retire_startup_plan(plan)
        await self._retry_pending_retirements()
        await self._retry_pending_rollbacks()
        self._recover_durable_retirements()
        self._recover_durable_rollbacks()
        resumed: list[str] = []
        for row in self.repository.incomplete(limit=500):
            if row.get("phase") != ReloadPhase.CANDIDATE_CAPTURED.value or not row.get("request_json"):
                continue
            with suppress(ReloadRejected, PostCommitRecoveryRequired):
                await self.execute_command(str(row["command_id"]))
            resumed.append(str(row["attempt_id"]))
        reconciled = self.repository.reconcile_incomplete(
            at=self._now(),
            exclude_attempt_ids=frozenset((*self._pending_retirements, *self._pending_rollbacks)),
        )
        repaired: list[str] = list(dict.fromkeys((*admission_repaired, *resumed, *reconciled)))
        for row in self.repository.terminal_evidence_needing_command_finalization():
            if await self._repair_command_from_row(row):
                attempt_id = str(row["attempt_id"])
                if attempt_id not in repaired:
                    repaired.append(attempt_id)
        return tuple(repaired)

    def _recover_durable_retirements(self) -> None:
        """Use an explicit process-restart proof for process-local superseded resources."""
        for row in self.repository.incomplete(limit=500):
            evidence = self._durable_retirement_recovery(row)
            if evidence is None:
                continue
            audit, retirement_evidence = evidence
            attempt_id = str(row["attempt_id"])
            self.repository.transition(
                attempt_id,
                ReloadPhase.COMPLETED,
                at=self._now(),
                outcome=ReloadOutcome.COMMITTED.value,
                final_generation=row.get("final_generation"),
                retirement_evidence_json=json.dumps(
                    retirement_evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ),
                audit_json=json.dumps(audit, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
                finished_at=self._iso_now(),
            )

    def _durable_retirement_recovery(
        self,
        row: Mapping[str, object],
    ) -> tuple[dict[str, object], dict[str, object]] | None:
        phase = ReloadPhase(str(row["phase"]))
        if phase not in {ReloadPhase.COMMITTED, ReloadPhase.RETIRING}:
            return None
        if str(row["attempt_id"]) in self._pending_retirements:
            return None
        raw_descriptor = row.get("retirement_json")
        try:
            descriptor = json.loads(str(raw_descriptor)) if raw_descriptor else {}
        except json.JSONDecodeError:
            return None
        if not isinstance(descriptor, dict) or descriptor.get("resource_scope") != "controller_process_local":
            return None
        raw_audit = json.loads(str(row["audit_json"])) if row.get("audit_json") else {}
        audit = raw_audit if isinstance(raw_audit, dict) else {}
        retirement_evidence: dict[str, object] = {
            "proof": "process_restart_superseded_resource_gone",
            "descriptor": descriptor,
        }
        self._apply_durable_retirement_evidence(audit, retirement_evidence)
        return audit, retirement_evidence

    @staticmethod
    def _apply_durable_retirement_evidence(
        audit: dict[str, object],
        retirement_evidence: dict[str, object],
    ) -> None:
        audit.update(
            {
                "phase": ReloadPhase.COMPLETED.value,
                "outcome": ReloadOutcome.COMMITTED.value,
                "retirement_pending": False,
                "cleanup_state": "completed",
                "retirement_evidence": retirement_evidence,
            }
        )
        lifecycle = audit.get("lifecycle_states")
        if isinstance(lifecycle, dict):
            lifecycle = dict(lifecycle)
            lifecycle["retirement"] = "completed"
            lifecycle["reconciliation"] = "not_required"
            audit["lifecycle_states"] = lifecycle
        codes = audit.get("diagnostic_codes")
        if isinstance(codes, list):
            audit["diagnostic_codes"] = [
                code
                for code in codes
                if code
                not in {
                    RELOAD_CODES["retirement_pending"],
                    RELOAD_CODES["reconciliation_required"],
                }
            ]

    async def _retry_pending_rollbacks(self) -> None:
        for attempt_id, (plan, primary_outcome, primary_phase) in tuple(self._pending_rollbacks.items()):
            try:
                plan.rollback()
            except BaseException as exc:
                self._diagnostic_promoter(
                    RELOAD_CODES["reconciliation_required"],
                    "configuration_reload.rollback_retry",
                    exc,
                )
                continue
            self._complete_rollback_recovery(attempt_id, primary_outcome, primary_phase, "owned_rollback_retry")
            del self._pending_rollbacks[attempt_id]

    def _recover_durable_rollbacks(self) -> None:
        for row in self.repository.incomplete(limit=500):
            if str(row["attempt_id"]) in self._pending_rollbacks:
                continue
            if ReloadPhase(str(row["phase"])) is not ReloadPhase.RECONCILIATION_REQUIRED:
                continue
            try:
                recovery = json.loads(str(row.get("recovery_json") or "{}"))
            except json.JSONDecodeError:
                continue
            if not isinstance(recovery, dict) or recovery.get("kind") != "rollback":
                continue
            primary_outcome = ReloadOutcome(str(recovery.get("primary_outcome", ReloadOutcome.ROLLED_BACK.value)))
            primary_phase = ReloadPhase(str(recovery.get("primary_phase", ReloadPhase.ROLLED_BACK.value)))
            self._complete_rollback_recovery(
                str(row["attempt_id"]),
                primary_outcome,
                primary_phase,
                "process_restart_rebuilt_old_generation",
            )

    def _complete_rollback_recovery(
        self,
        attempt_id: str,
        primary_outcome: ReloadOutcome,
        primary_phase: ReloadPhase,
        proof: str,
    ) -> None:
        row = self.repository.get_attempt(attempt_id)
        if row is None:
            return
        audit = json.loads(str(row["audit_json"])) if row.get("audit_json") else {}
        if not isinstance(audit, dict):
            audit = {}
        audit.update(
            {
                "outcome": primary_outcome.value,
                "phase": primary_phase.value,
                "cleanup_state": "completed",
                "recovery_evidence": {"kind": "rollback", "proof": proof},
            }
        )
        lifecycle = audit.get("lifecycle_states")
        if isinstance(lifecycle, dict):
            lifecycle = dict(lifecycle)
            lifecycle["rollback"] = "completed"
            lifecycle["reconciliation"] = "completed"
            audit["lifecycle_states"] = lifecycle
        codes = audit.get("diagnostic_codes")
        if isinstance(codes, list):
            audit["diagnostic_codes"] = [code for code in codes if code != RELOAD_CODES["reconciliation_required"]]
        self.repository.transition(
            attempt_id,
            primary_phase,
            at=self._now(),
            outcome=primary_outcome.value,
            audit_json=json.dumps(audit, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
            recovery_json=json.dumps(
                {"kind": "rollback", "state": "completed", "proof": proof},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ),
            finished_at=self._iso_now(),
        )

    async def _retire_startup_plan(self, plan: Any) -> None:
        try:
            await plan.retire()
        except BaseException as exc:
            self._diagnostic_promoter(
                RELOAD_CODES["retirement_pending"],
                "configuration_reload.startup_retirement",
                exc,
            )
            raise
        if self._startup_retirement is plan:
            self._startup_retirement = None

    async def _retry_pending_retirements(self) -> None:
        for attempt_id, plan in tuple(self._pending_retirements.items()):
            try:
                await plan.retire()
            except BaseException as exc:
                self._diagnostic_promoter(
                    RELOAD_CODES["retirement_pending"],
                    "configuration_reload.retirement_retry",
                    exc,
                )
                continue
            row = self.repository.get_attempt(attempt_id)
            if row is not None:
                audit = json.loads(str(row["audit_json"])) if row.get("audit_json") else {}
                if not isinstance(audit, dict):
                    audit = {}
                audit.update(
                    {
                        "outcome": ReloadOutcome.COMMITTED.value,
                        "phase": ReloadPhase.COMPLETED.value,
                        "retirement_pending": False,
                        "cleanup_state": "completed",
                        "retirement_evidence": {"proof": "owned_retirement_operation_completed"},
                        "completed_at": self._iso_now(),
                    }
                )
                lifecycle = audit.get("lifecycle_states")
                if isinstance(lifecycle, dict):
                    lifecycle = dict(lifecycle)
                    lifecycle["retirement"] = "completed"
                    lifecycle["reconciliation"] = "not_required"
                    audit["lifecycle_states"] = lifecycle
                codes = audit.get("diagnostic_codes")
                if isinstance(codes, list):
                    audit["diagnostic_codes"] = [
                        code
                        for code in codes
                        if code
                        not in {
                            RELOAD_CODES["retirement_pending"],
                            RELOAD_CODES["reconciliation_required"],
                        }
                    ]
                self.repository.transition(
                    attempt_id,
                    ReloadPhase.COMPLETED,
                    at=self._now(),
                    outcome=ReloadOutcome.COMMITTED.value,
                    final_generation=row.get("final_generation"),
                    retirement_evidence_json=json.dumps(
                        audit["retirement_evidence"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
                    ),
                    audit_json=json.dumps(audit, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
                    finished_at=self._iso_now(),
                )
            del self._pending_retirements[attempt_id]

    def _startup_reconciliation_diff(self) -> ReloadDiff:
        report_sha256 = self._active.report_sha256 or ("0" * 64)
        return build_reload_diff(
            self._startup_compiled,
            self._active_compiled,
            active_generation=self._active.generation,
            active_identity_sha256=self._startup_candidate.candidate_identity_sha256,
            candidate_identity_sha256=self._active_candidate.candidate_identity_sha256,
            report_sha256=report_sha256,
            active_environment_inputs=self._startup_candidate.environment_inputs,
            candidate_environment_inputs=self._active_candidate.environment_inputs,
        )

    async def _reconcile_command(self, attempt_id: str) -> None:
        row = self.repository.get_attempt(attempt_id)
        if row is None:
            return
        command_id = str(row["command_id"])
        command = await self.command_store.get(command_id)
        if command.status.value in {"succeeded", "failed", "cancelled"}:
            return
        await self._finalize_command_from_row(row)

    async def _repair_command_for_terminal_attempt(self, command: CommandRecord) -> CommandRecord:
        row = self.repository.get_by_command(command.command_id)
        if row is None or row.get("finished_at") is None or not row.get("audit_json") or not row.get("outcome"):
            return command
        if command.status.value in {"succeeded", "failed", "cancelled"}:
            return command
        await self._finalize_command_from_row(row)
        return await self.command_store.get(command.command_id)

    async def _repair_command_from_row(self, row: dict[str, Any]) -> bool:
        if row.get("finished_at") is None:
            return False
        command = await self.command_store.get(str(row["command_id"]))
        if command.status.value in {"succeeded", "failed", "cancelled"}:
            return False
        await self._finalize_command_from_row(row)
        return True

    async def _finalize_command_from_row(self, row: dict[str, Any]) -> None:
        command_id = str(row["command_id"])
        outcome = ReloadOutcome(str(row.get("outcome") or ReloadOutcome.FAILED.value))
        audit = json.loads(str(row["audit_json"])) if row.get("audit_json") else {}
        if outcome is ReloadOutcome.CANCELLED:
            await self.command_store.mark_cancelled(command_id)
            return
        if outcome in _SUCCESSFUL_COMMAND_OUTCOMES:
            if _audit_has_result(audit):
                result = _result_from_audit(audit)
                await self.command_store.mark_succeeded(command_id, result.command_result())
                return
            await self.command_store.mark_succeeded(
                command_id,
                {
                    "outcome": outcome.value,
                    "final_generation": row.get("final_generation"),
                    "attempt_id": str(row["attempt_id"]),
                },
            )
            return
        if outcome is ReloadOutcome.RECONCILIATION_REQUIRED:
            error = RuntimeError("configuration reload requires reconciliation")
            self._diagnostic_promoter(
                RELOAD_CODES["reconciliation_required"],
                "configuration_reload.reconcile",
                error,
            )
        await self.command_store.mark_failed(
            command_id,
            {
                "code": str(audit.get("failure_code") or f"config_reload_{outcome.value}"),
                "message": "Durable configuration reload did not complete successfully.",
                "details": {"outcome": outcome.value, "diagnostic_codes": audit.get("diagnostic_codes", [])},
            },
        )

    async def _finalize_command(self, command_id: str, result: ReloadResult) -> None:
        row = self.repository.get_by_command(command_id)
        if row is not None and row.get("finished_at") is None:
            return
        if result.outcome is ReloadOutcome.CANCELLED:
            await self.command_store.mark_cancelled(command_id)
        elif result.outcome in _SUCCESSFUL_COMMAND_OUTCOMES:
            await self.command_store.mark_succeeded(command_id, result.command_result())
        else:
            await self.command_store.mark_failed(
                command_id,
                {
                    "code": f"config_reload_{result.outcome.value}",
                    "message": result.message or "Configuration reload did not complete successfully.",
                    "details": {
                        "outcome": result.outcome.value,
                        "diagnostic_codes": list(result.diagnostic_codes),
                    },
                },
            )

    def _commit_failure_result(
        self,
        attempt_id: str,
        request: ReloadRequest,
        candidate: CandidateRecord,
        report_sha256: str,
        diff: ReloadDiff,
        warning_ids: tuple[str, ...],
        old: ActiveGeneration,
        plan: Any,
        progress: dict[str, Any],
        error: BaseException,
        rollback_error: BaseException | None = None,
    ) -> ReloadResult:
        rollback_error, rollback_failed, phase, outcome, diagnostics = self._commit_failure_state(
            plan, progress, error, rollback_error
        )
        generation = self._active.generation if progress["durable"] else old.generation
        self._diagnostic_promoter(diagnostics[0], "configuration_reload.commit", error)
        if rollback_failed:
            self._pending_rollbacks[attempt_id] = (plan, ReloadOutcome.ROLLED_BACK, ReloadPhase.ROLLED_BACK)
            phase, outcome = ReloadPhase.RECONCILIATION_REQUIRED, ReloadOutcome.FAILED
        return self._terminal_result(
            attempt_id,
            request,
            candidate,
            report_sha256,
            diff,
            outcome,
            phase,
            generation,
            warning_ids,
            "Commit failure preserved bounded recovery evidence.",
            diagnostic_codes=diagnostics,
            safe_point=self._successful_safe_point(progress),
            failure_evidence=self._failure_evidence(error, rollback_error),
            lifecycle_states=progress,
            cleanup_state="pending" if rollback_failed else "not_required",
            recovery_evidence=(
                {
                    "kind": "rollback",
                    "state": "pending",
                    "primary_outcome": ReloadOutcome.ROLLED_BACK.value,
                    "primary_phase": ReloadPhase.ROLLED_BACK.value,
                }
                if rollback_failed
                else None
            ),
            finish=not rollback_failed,
        )

    def _commit_failure_state(
        self,
        plan: Any,
        progress: dict[str, Any],
        error: BaseException,
        rollback_error: BaseException | None,
    ) -> tuple[BaseException | None, bool, ReloadPhase, ReloadOutcome, tuple[str, ...]]:
        if rollback_error is None and not progress["durable"]:
            rollback_error = self._rollback_preserving_primary(plan, error)
        rollback_failed = rollback_error is not None
        progress["rollback"] = (
            "failed" if rollback_failed else ("completed" if not progress["durable"] else "not_required")
        )
        phase, outcome = _failure_disposition(progress, rollback_failed)
        recovery = rollback_failed or phase is not ReloadPhase.ROLLED_BACK
        diagnostics = (
            (RELOAD_CODES["reconciliation_required"],)
            if recovery
            else (RELOAD_CODES["candidate_or_preparation_failed"],)
        )
        return rollback_error, rollback_failed, phase, outcome, diagnostics

    def _rollback_recovery_result(
        self,
        attempt_id: str,
        request: ReloadRequest,
        candidate: CandidateRecord,
        report_sha256: str,
        diff: ReloadDiff,
        warning_ids: tuple[str, ...],
        plan: Any,
        progress: dict[str, Any],
        primary_outcome: ReloadOutcome,
        primary_phase: ReloadPhase,
        primary: BaseException,
        rollback_error: BaseException,
    ) -> ReloadResult:
        self._pending_rollbacks[attempt_id] = (plan, primary_outcome, primary_phase)
        self._diagnostic_promoter(
            RELOAD_CODES["reconciliation_required"],
            "configuration_reload.rollback",
            rollback_error,
        )
        return self._terminal_result(
            attempt_id,
            request,
            candidate,
            report_sha256,
            diff,
            primary_outcome,
            ReloadPhase.RECONCILIATION_REQUIRED,
            self._active.generation if progress["durable"] else diff.active_generation,
            warning_ids,
            "Reload cleanup remains pending after rollback failure.",
            diagnostic_codes=(RELOAD_CODES["reconciliation_required"],),
            safe_point=self._successful_safe_point(progress),
            failure_evidence=self._failure_evidence(primary, rollback_error),
            lifecycle_states=progress,
            cleanup_state="pending",
            recovery_evidence={
                "kind": "rollback",
                "state": "pending",
                "primary_outcome": primary_outcome.value,
                "primary_phase": primary_phase.value,
            },
            finish=False,
        )

    @staticmethod
    def _retirement_descriptor(plan: Any, old_generation: int) -> dict[str, object]:
        descriptor = getattr(plan, "retirement_descriptor", None)
        if callable(descriptor):
            value = descriptor()
            if isinstance(value, Mapping):
                return _safe_mapping(value)
        return {
            "schema_version": 1,
            "resource_kind": type(plan).__name__[:96],
            "resource_scope": "controller_process_local",
            "cleanup": "retire_superseded_prepared_resources",
            "old_generation": old_generation,
            "proof_on_restart": "superseded_process_resource_gone",
        }

    async def _retirement_result(
        self,
        attempt_id: str,
        request: ReloadRequest,
        candidate: CandidateRecord,
        report_sha256: str,
        diff: ReloadDiff,
        warning_ids: tuple[str, ...],
        plan: Any,
        progress: dict[str, Any],
    ) -> ReloadResult:
        self.repository.transition(attempt_id, ReloadPhase.RETIRING, at=self._now())
        progress["retirement"] = "running"
        retirement_pending = False
        retirement_diagnostics: tuple[str, ...] = ()
        try:
            await plan.retire()
        except BaseException as exc:
            retirement_pending = True
            progress["retirement"] = "pending"
            retirement_diagnostics = (RELOAD_CODES["retirement_pending"],)
            self._diagnostic_promoter(
                retirement_diagnostics[0],
                "configuration_reload.retirement",
                exc,
            )
            self._pending_retirements[attempt_id] = plan
        else:
            progress["retirement"] = "completed"
        return self._terminal_result(
            attempt_id,
            request,
            candidate,
            report_sha256,
            diff,
            ReloadOutcome.COMMITTED,
            ReloadPhase.RETIRING if retirement_pending else ReloadPhase.COMPLETED,
            self._active.generation,
            warning_ids,
            "Configuration generation committed atomically.",
            diagnostic_codes=retirement_diagnostics,
            retirement_pending=retirement_pending,
            cleanup_state="pending" if retirement_pending else "completed",
            retirement_evidence={
                "state": "pending" if retirement_pending else "completed",
                "descriptor": self._retirement_descriptor(plan, diff.active_generation),
            },
            safe_point=self._successful_safe_point(progress),
            lifecycle_states=progress,
            finish=not retirement_pending,
        )

    @staticmethod
    def _successful_safe_point(progress: Mapping[str, object]) -> dict[str, object]:
        lease = progress.get("safe_lease")
        if lease is None:
            return {"outcome": "not_required", "blockers": [], "waited_seconds": 0.0}
        if not isinstance(lease, SafePointLease):
            raise RuntimeError("reload safe-point progress contains an invalid lease")
        return {"outcome": "acquired", **lease.snapshot.to_dict()}

    def _reverify(
        self,
        candidate: CandidateRecord,
        report_ref: str,
        report_sha256: str,
        report_mapping: dict[str, object],
        diff: ReloadDiff,
        request: ReloadRequest,
        warning_ids: tuple[str, ...],
        old: ActiveGeneration,
        plan: Any,
    ) -> ReportFreshness:
        final_compiled = self.candidates.compile(candidate)
        admitted = self.candidates.load_report(candidate, report_ref, report_sha256)
        final_diff = build_reload_diff(
            self._active_compiled,
            final_compiled,
            active_generation=self._active.generation,
            active_identity_sha256=self._active_candidate.candidate_identity_sha256,
            candidate_identity_sha256=candidate.candidate_identity_sha256,
            report_sha256=report_sha256,
            warning_paths=_warnings(admitted)[1],
            active_environment_inputs=self._active_candidate.environment_inputs,
            candidate_environment_inputs=candidate.environment_inputs,
        )
        if (
            admitted != report_mapping
            or diff.policy_version != 1
            or diff.active_generation != self._active.generation
            or final_diff != diff
        ):
            raise ReloadRejected("reload_fence_mismatch", "Reload policy, report, diff, or generation fence is stale.")
        verification = verify_report_mapping(
            admitted,
            expected_candidate_sha256=candidate.candidate_sha256,
            expected_candidate_identity_sha256=candidate.candidate_identity_sha256,
            expected_report_sha256=report_sha256,
            current_active_generation=self._active.generation,
            require_fresh_generation=True,
        )
        if not verification.accepted or old.candidate_identity_sha256 != diff.active_identity_sha256:
            raise ReloadRejected("reload_fence_mismatch", "Candidate or active identity changed before commit.")
        freshness = self._freshness(admitted)
        self._verify_prepared_plan(plan, diff, candidate, old.generation)
        if warning_ids and not self._acknowledged(
            request.acknowledgment,
            request.actor,
            candidate,
            report_sha256,
            self._active.generation,
            warning_ids,
            freshness,
        ):
            raise ReloadRejected("warning_acknowledgment_stale", "Warning acknowledgment became stale before commit.")
        return freshness

    def _final_volatile_fence(
        self,
        candidate: CandidateRecord,
        report_ref: str,
        report_sha256: str,
        diff: ReloadDiff,
        request: ReloadRequest,
        warning_ids: tuple[str, ...],
        old: ActiveGeneration,
        plan: Any,
        freshness: ReportFreshness,
    ) -> None:
        if self._volatile_generation_mismatch(old) or self._volatile_admission_mismatch(
            candidate, report_ref, report_sha256, diff, request, old
        ):
            raise ReloadRejected("reload_fence_mismatch", "Reload identity or generation fence is stale.")
        self._verify_volatile_report_fence(
            candidate,
            report_ref,
            report_sha256,
            diff,
            request,
            warning_ids,
            old,
            plan,
            freshness,
        )

    def _volatile_generation_mismatch(self, old: ActiveGeneration) -> bool:
        return (
            self._active.generation != old.generation
            or self._active.candidate_reference != old.candidate_reference
            or self._active.candidate_identity_sha256 != old.candidate_identity_sha256
            or self._active_candidate.reference != old.candidate_reference
            or self._active_candidate.candidate_identity_sha256 != old.candidate_identity_sha256
        )

    @staticmethod
    def _volatile_admission_mismatch(
        candidate: CandidateRecord,
        report_ref: str,
        report_sha256: str,
        diff: ReloadDiff,
        request: ReloadRequest,
        old: ActiveGeneration,
    ) -> bool:
        return (
            diff.active_generation != old.generation
            or diff.active_identity_sha256 != old.candidate_identity_sha256
            or diff.candidate_identity_sha256 != candidate.candidate_identity_sha256
            or diff.report_sha256 != report_sha256
            or diff.policy_version != RELOAD_POLICY_VERSION
            or report_ref != f"report_{report_sha256[:40]}"
            or request.candidate is None
            or not request.candidate.matches(candidate)
        )

    def _verify_volatile_report_fence(
        self,
        candidate: CandidateRecord,
        report_ref: str,
        report_sha256: str,
        diff: ReloadDiff,
        request: ReloadRequest,
        warning_ids: tuple[str, ...],
        old: ActiveGeneration,
        plan: Any,
        freshness: ReportFreshness,
    ) -> None:
        self.candidates.verify_commit_artifacts(candidate, report_ref, report_sha256)
        now = self._now()
        earliest = freshness.completed_at - dt.timedelta(seconds=VALIDATION_REPORT_CLOCK_SKEW_SECONDS)
        if not earliest <= now <= freshness.expires_at:
            raise ReloadRejected(
                "validation_report_expired",
                "Validation report is outside the approved freshness window; fresh validation is required.",
            )
        current_environment = tuple(item.to_dict() for item in self.candidates.environment_identities())
        if current_environment != candidate.environment_inputs:
            raise CandidateIntegrityError("complete candidate inputs changed after capture")
        self._verify_prepared_plan(plan, diff, candidate, old.generation)
        if warning_ids and not self._acknowledged(
            request.acknowledgment,
            request.actor,
            candidate,
            report_sha256,
            self._active.generation,
            warning_ids,
            freshness,
        ):
            raise ReloadRejected("warning_acknowledgment_stale", "Warning acknowledgment became stale before commit.")

    @staticmethod
    def _verify_prepared_plan(
        plan: Any,
        diff: ReloadDiff,
        candidate: CandidateRecord,
        expected_generation: int,
    ) -> None:
        if (
            plan.expected_generation != expected_generation
            or plan.target_generation != expected_generation + 1
            or plan.candidate_identity_sha256 != candidate.candidate_identity_sha256
            or plan.diff_sha256 != diff.digest
            or plan.required_disposition is not diff.disposition
        ):
            raise ReloadRejected(
                "reload_plan_fence_mismatch",
                "Prepared resource plan is stale or requires a different reload disposition.",
            )

    async def _require_not_cancelled(self, request: ReloadRequest, attempt_id: str) -> None:
        del request
        row = self.repository.get_attempt(attempt_id)
        if row is None:
            raise ReloadCancelled()
        command = await self.command_store.get(str(row["command_id"]))
        if command.cancel_requested_at is not None:
            raise ReloadCancelled()

    @staticmethod
    def _acknowledged(
        acknowledgment: WarningAcknowledgment | None,
        actor: str,
        candidate: CandidateRecord,
        report_sha256: str,
        generation: int,
        warning_ids: tuple[str, ...],
        freshness: ReportFreshness,
    ) -> bool:
        if acknowledgment is None:
            return False
        actual = (
            acknowledgment.actor,
            acknowledgment.candidate_sha256,
            acknowledgment.candidate_identity_sha256,
            acknowledgment.report_sha256,
            acknowledgment.active_generation,
            acknowledgment.warning_identities,
            acknowledgment.validator_completed_at.astimezone(dt.UTC),
            acknowledgment.expires_at.astimezone(dt.UTC),
        )
        expected = (
            actor,
            candidate.candidate_sha256,
            candidate.candidate_identity_sha256,
            report_sha256,
            generation,
            warning_ids,
            freshness.completed_at,
            freshness.expires_at,
        )
        acknowledged_at = acknowledgment.acknowledged_at.astimezone(dt.UTC)
        earliest = freshness.completed_at - dt.timedelta(seconds=VALIDATION_REPORT_CLOCK_SKEW_SECONDS)
        return actual == expected and earliest <= acknowledged_at <= freshness.expires_at

    @staticmethod
    def _require_acknowledgment_actor(request: ReloadRequest) -> None:
        if request.acknowledgment is not None and request.acknowledgment.actor != request.actor:
            raise ReloadRejected(
                "warning_acknowledgment_actor_mismatch",
                "Warning acknowledgment actor does not match the reload principal.",
            )

    def _acknowledgment_challenge(
        self,
        request: ReloadRequest,
        candidate: CandidateRecord,
        report_sha256: str,
        generation: int,
        warning_ids: tuple[str, ...],
        freshness: ReportFreshness,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "actor": request.actor,
            "candidate_sha256": candidate.candidate_sha256,
            "candidate_identity_sha256": candidate.candidate_identity_sha256,
            "report_sha256": report_sha256,
            "active_generation": generation,
            "warning_identities": list(warning_ids),
            "acknowledged_at": self._now().isoformat(),
            **freshness.challenge_fields(),
        }

    def _validator_stamp_identity(self, candidate: CandidateRecord, report_sha256: str) -> str | None:
        report_ref = f"report_{report_sha256[:40]}"
        try:
            report = self.candidates.load_report(candidate, report_ref, report_sha256)
        except CandidateIntegrityError:
            return None
        stamp = report.get("validator_stamp")
        return canonical_sha256(stamp) if isinstance(stamp, Mapping) else None

    @staticmethod
    def _audit_lifecycle_states(
        values: Mapping[str, object] | None,
        retirement_pending: bool,
    ) -> dict[str, object]:
        raw = values or {}
        return {
            "preparation": str(raw.get("preparation", "completed" if values else "not_required")),
            "activation": str(raw.get("activation", "not_required")),
            "rollback": str(raw.get("rollback", "not_required")),
            "retirement": "pending" if retirement_pending else str(raw.get("retirement", "not_required")),
            "reconciliation": str(raw.get("reconciliation", "not_required")),
        }

    def _rollback_preserving_primary(self, plan: Any, primary: BaseException) -> BaseException | None:
        try:
            plan.rollback()
        except BaseException as rollback_error:
            primary.add_note(f"rollback failure retained: {type(rollback_error).__name__}")
            primary.__dict__["rollback_failure"] = rollback_error
            self._diagnostic_promoter(
                RELOAD_CODES["reconciliation_required"],
                "configuration_reload.rollback",
                rollback_error,
            )
            return rollback_error
        return None

    def _rollback_for_failure(
        self,
        plan: Any,
        primary: BaseException,
        progress: dict[str, Any],
    ) -> BaseException | None:
        rollback = self._rollback_preserving_primary(plan, primary)
        progress["rollback"] = "failed" if rollback is not None else "completed"
        return rollback

    @staticmethod
    def _rollback_diagnostics(primary_code: str | None, rollback: BaseException | None) -> tuple[str, ...]:
        codes = (primary_code,) if primary_code is not None else ()
        if rollback is not None:
            return (*codes, RELOAD_CODES["reconciliation_required"])
        return codes

    @staticmethod
    def _failure_evidence(
        primary: BaseException,
        rollback: BaseException | None,
    ) -> dict[str, object]:
        primary_code = primary.code if isinstance(primary, ReloadRejected) else "internal_failure"
        return {
            "primary": {"code": primary_code, "type": type(primary).__name__[:96]},
            "rollback": (
                {"code": "rollback_failed", "type": type(rollback).__name__[:96]} if rollback is not None else None
            ),
        }

    def _record_cancelled_audit(
        self,
        attempt_id: str,
        request: ReloadRequest,
        old: ActiveGeneration,
        error: BaseException,
    ) -> None:
        row = self.repository.get_attempt(attempt_id)
        if row is None:
            row = {}
        elif row.get("finished_at") is not None:
            return
        elif row.get("recovery_json"):
            audit = json.loads(str(row.get("audit_json") or "{}"))
            if isinstance(audit, dict):
                audit["failure_evidence"] = self._failure_evidence(error, getattr(error, "rollback_failure", None))
                self.repository.transition(
                    attempt_id,
                    ReloadPhase.RECONCILIATION_REQUIRED,
                    at=self._now(),
                    audit_json=json.dumps(audit, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
                )
            return
        completed = self._now()
        created = _timestamp_or(row.get("created_at"), completed)
        self.repository.fail_attempt(
            attempt_id,
            phase=ReloadPhase.CANCELLED,
            outcome=ReloadOutcome.CANCELLED.value,
            audit={
                "schema_version": 1,
                "attempt_id": attempt_id,
                "audit_reference": f"audit_{attempt_id.removeprefix('reload_')}",
                "outcome": ReloadOutcome.CANCELLED.value,
                "phase": ReloadPhase.CANCELLED.value,
                "command_id": row.get("command_id"),
                "idempotency_identity": row.get("idempotency_identity"),
                "actor": request.actor,
                "auth_context": _safe_mapping(request.authorization_context),
                "reason": request.reason,
                "old_generation": old.generation,
                "old_candidate_identity_sha256": old.candidate_identity_sha256,
                "candidate_reference": row.get("candidate_reference"),
                "candidate_source_sha256": row.get("source_sha256"),
                "candidate_byte_length": row.get("source_byte_length"),
                "candidate_source_manifest_sha256": row.get("source_manifest_sha256"),
                "candidate_sha256": row.get("candidate_sha256"),
                "candidate_identity_sha256": row.get("candidate_identity_sha256"),
                "failure_evidence": self._failure_evidence(error, None),
                "requested_at": created.isoformat(),
                "started_at": created.isoformat(),
                "completed_at": completed.isoformat(),
                "duration_seconds": round(max(0.0, (completed - created).total_seconds()), 6),
            },
            at=completed,
        )

    async def _cancel_command_if_open(self, command_id: str) -> None:
        row = self.repository.get_by_command(command_id)
        if row is not None and row.get("finished_at") is None and row.get("recovery_json"):
            return
        command = await self.command_store.get(command_id)
        if command.status.value not in {"succeeded", "failed", "cancelled", "expired", "superseded"}:
            await self.command_store.mark_cancelled(command_id)

    def _terminal_result(
        self,
        attempt_id: str,
        request: ReloadRequest,
        candidate: CandidateRecord,
        report_sha256: str,
        diff: ReloadDiff,
        outcome: ReloadOutcome,
        phase: ReloadPhase,
        final_generation: int,
        warning_ids: tuple[str, ...],
        message: str,
        *,
        diagnostic_codes: tuple[str, ...] = (),
        retirement_pending: bool = False,
        cleanup_state: str = "not_required",
        safe_point: dict[str, object] | None = None,
        acknowledgment_challenge: Mapping[str, object] | None = None,
        failure_evidence: Mapping[str, object] | None = None,
        lifecycle_states: Mapping[str, object] | None = None,
        recovery_evidence: Mapping[str, object] | None = None,
        retirement_evidence: Mapping[str, object] | None = None,
        finish: bool = True,
    ) -> ReloadResult:
        audit_reference = f"audit_{attempt_id.removeprefix('reload_')}"
        result = ReloadResult(
            attempt_id=attempt_id,
            audit_reference=audit_reference,
            outcome=outcome,
            phase=phase,
            disposition=diff.disposition,
            old_generation=diff.active_generation,
            final_generation=final_generation,
            candidate_reference=candidate.reference,
            candidate_sha256=candidate.candidate_sha256,
            candidate_identity_sha256=candidate.candidate_identity_sha256,
            report_sha256=report_sha256,
            diff_sha256=diff.digest,
            changed_paths=diff.grouped_paths(),
            warning_identities=warning_ids,
            diagnostic_codes=diagnostic_codes,
            acknowledgment_challenge=acknowledgment_challenge,
            retirement_pending=retirement_pending,
            cleanup_state=cleanup_state,
            message=message,
        )
        audit = self._build_terminal_audit(
            attempt_id,
            result,
            request,
            candidate,
            report_sha256,
            diff,
            diagnostic_codes=diagnostic_codes,
            retirement_pending=retirement_pending,
            cleanup_state=cleanup_state,
            safe_point=safe_point,
            failure_evidence=failure_evidence,
            lifecycle_states=lifecycle_states,
            recovery_evidence=recovery_evidence,
            retirement_evidence=retirement_evidence,
            finish=finish,
        )
        self._persist_terminal_audit(
            attempt_id,
            phase,
            outcome,
            final_generation,
            audit,
            recovery_evidence=recovery_evidence,
            finish=finish,
        )
        return result

    def _build_terminal_audit(
        self,
        attempt_id: str,
        result: ReloadResult,
        request: ReloadRequest,
        candidate: CandidateRecord,
        report_sha256: str,
        diff: ReloadDiff,
        *,
        diagnostic_codes: tuple[str, ...],
        retirement_pending: bool,
        cleanup_state: str,
        safe_point: dict[str, object] | None,
        failure_evidence: Mapping[str, object] | None,
        lifecycle_states: Mapping[str, object] | None,
        recovery_evidence: Mapping[str, object] | None,
        retirement_evidence: Mapping[str, object] | None,
        finish: bool,
    ) -> dict[str, object]:
        row = self.repository.get_attempt(attempt_id) or {}
        completed = self._now()
        created = _timestamp_or(row.get("created_at"), completed)
        audit = result.to_dict() | {
            "command_id": row.get("command_id"),
            "idempotency_identity": row.get("idempotency_identity"),
            "actor": request.actor,
            "auth_context": _safe_mapping(request.authorization_context),
            "reason": request.reason,
            "dry_run": request.dry_run,
            "expected_generation": request.expected_generation,
            "old_candidate_reference": row.get("old_candidate_reference"),
            "old_source_sha256": row.get("old_source_sha256"),
            "old_candidate_identity_sha256": row.get("old_candidate_identity_sha256") or diff.active_identity_sha256,
            "candidate_source_sha256": candidate.source_sha256,
            "candidate_byte_length": candidate.byte_length,
            "candidate_source_manifest_sha256": (
                request.candidate.source_manifest_sha256 if request.candidate is not None else None
            ),
            "validator_stamp_identity": self._validator_stamp_identity(candidate, report_sha256),
            "reload_policy_version": diff.policy_version,
            "safe_point": safe_point or {"outcome": "not_required", "blockers": [], "waited_seconds": 0.0},
            "acknowledgment": request.acknowledgment.to_dict() if request.acknowledgment else None,
            "failure_evidence": _safe_mapping(failure_evidence or {}),
            "lifecycle_states": self._audit_lifecycle_states(lifecycle_states, retirement_pending),
            "cleanup_state": cleanup_state,
            "recovery_evidence": _safe_mapping(recovery_evidence or {}),
            "retirement_evidence": _safe_mapping(retirement_evidence or {}),
            "requested_at": created.isoformat(),
            "started_at": created.isoformat(),
            "duration_seconds": round(max(0.0, (completed - created).total_seconds()), 6),
        }
        audit["completed_at" if finish else "recovery_pending_at"] = completed.isoformat()
        return audit

    def _persist_terminal_audit(
        self,
        attempt_id: str,
        phase: ReloadPhase,
        outcome: ReloadOutcome,
        final_generation: int,
        audit: Mapping[str, object],
        *,
        recovery_evidence: Mapping[str, object] | None,
        finish: bool,
    ) -> None:
        transition_fields: dict[str, object] = {
            "outcome": outcome.value,
            "final_generation": final_generation,
            "audit_json": json.dumps(audit, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
        }
        if finish:
            transition_fields["finished_at"] = self._iso_now()
        if recovery_evidence is not None:
            transition_fields["recovery_json"] = json.dumps(
                recovery_evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
        self.repository.transition(attempt_id, phase, at=self._now(), **transition_fields)

    def _now(self) -> dt.datetime:
        return self._clock().astimezone(dt.UTC)

    def _iso_now(self) -> str:
        return self._now().isoformat()


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ReloadRejected("validation_report_malformed", "Validation report summary is malformed.")
    return value


def _warnings(report: dict[str, object]) -> tuple[tuple[str, ...], frozenset[str]]:
    issues = report.get("issues")
    if not isinstance(issues, list):
        raise ReloadRejected("validation_report_malformed", "Validation report issue list is malformed.")
    identities: list[str] = []
    paths: set[str] = set()
    for issue in issues:
        if not isinstance(issue, dict) or issue.get("severity") != "warning":
            continue
        path = issue.get("path")
        path_value = str(path.get("value")) if isinstance(path, dict) and path.get("value") is not None else "root"
        raw = f"{issue.get('rule_id')}|{issue.get('code')}|{path_value}"
        identities.append(f"warning:{hashlib.sha256(raw.encode()).hexdigest()[:24]}")
        if path_value.startswith("/"):
            paths.add(path_value)
    return tuple(sorted(set(identities))), frozenset(paths)


def _result_from_audit(payload: dict[str, object]) -> ReloadResult:
    return ReloadResult(
        attempt_id=str(payload["attempt_id"]),
        audit_reference=str(payload["audit_reference"]),
        outcome=ReloadOutcome(str(payload["outcome"])),
        phase=ReloadPhase(str(payload["phase"])),
        disposition=ReloadDisposition(str(payload["disposition"])),
        old_generation=_integer(payload["old_generation"]),
        final_generation=_integer(payload["final_generation"]),
        candidate_reference=str(payload["candidate_reference"]),
        candidate_sha256=str(payload["candidate_sha256"]),
        candidate_identity_sha256=str(payload["candidate_identity_sha256"]),
        report_sha256=str(payload["report_sha256"]),
        diff_sha256=str(payload["diff_sha256"]),
        changed_paths={str(key): _string_list(value) for key, value in _mapping(payload["changed_paths"]).items()},
        warning_identities=_string_tuple(payload.get("warning_identities")),
        diagnostic_codes=_string_tuple(payload.get("diagnostic_codes")),
        acknowledgment_challenge=(
            _mapping(payload["acknowledgment_challenge"])
            if payload.get("acknowledgment_challenge") is not None
            else None
        ),
        retirement_pending=bool(payload.get("retirement_pending")),
        cleanup_state=str(payload.get("cleanup_state") or "not_required"),
        message=str(payload.get("message") or ""),
    )


def _audit_has_result(payload: dict[str, object]) -> bool:
    required = {
        "attempt_id",
        "audit_reference",
        "outcome",
        "phase",
        "disposition",
        "old_generation",
        "final_generation",
        "candidate_reference",
        "candidate_sha256",
        "candidate_identity_sha256",
        "report_sha256",
        "diff_sha256",
        "changed_paths",
    }
    return required.issubset(payload)


def _integer(value: object) -> int:
    if not isinstance(value, int):
        raise ReloadRejected("reload_audit_malformed", "Reload audit generation is malformed.")
    return value


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ReloadRejected("reload_audit_malformed", "Reload audit path list is malformed.")
    return list(value)


def _string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list | tuple) or any(not isinstance(item, str) for item in value):
        raise ReloadRejected("reload_audit_malformed", "Reload audit identity list is malformed.")
    return tuple(value)


def _request_from_attempt(row: Mapping[str, object]) -> ReloadRequest:
    raw = row.get("request_json")
    if not isinstance(raw, str):
        raise ReloadRejected("reload_command_binding_missing", "Reload admission has no durable request contract.")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReloadRejected(
            "reload_command_binding_malformed", "Reload durable request contract is malformed."
        ) from exc
    if not isinstance(payload, dict):
        raise ReloadRejected("reload_command_binding_malformed", "Reload durable request contract is malformed.")
    candidate = _candidate_binding_from_payload(payload.get("candidate"))
    acknowledgment = _acknowledgment_from_payload(payload.get("acknowledgment"))
    auth_context = payload.get("auth_context")
    if not isinstance(auth_context, dict):
        raise ReloadRejected("reload_command_binding_malformed", "Reload authorization context is malformed.")
    try:
        return ReloadRequest(
            actor=str(row["actor"]),
            reason=str(payload["reason"]) if payload.get("reason") is not None else None,
            dry_run=bool(payload["dry_run"]),
            expected_generation=_optional_integer(payload.get("expected_generation")),
            safe_point_timeout_seconds=float(payload["safe_point_timeout_seconds"]),
            acknowledgment=acknowledgment,
            candidate=candidate,
            authorization_context=auth_context,
            schema_version=_integer(payload["schema_version"]),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise ReloadRejected(
            "reload_command_binding_malformed", "Reload durable request contract is malformed."
        ) from exc


def _request_without_candidate(request: ReloadRequest) -> ReloadRequest:
    return replace(request, candidate=None, source_path=None)


def _candidate_binding_from_payload(raw: object) -> CandidateBinding:
    if not isinstance(raw, dict):
        raise ReloadRejected("reload_command_binding_missing", "Reload durable candidate binding is missing.")
    return CandidateBinding(
        reference=str(raw.get("reference")),
        source_sha256=str(raw.get("source_sha256")),
        byte_length=_integer(raw.get("byte_length")),
        source_manifest_sha256=str(raw.get("source_manifest_sha256")),
        candidate_sha256=str(raw.get("candidate_sha256")),
        candidate_identity_sha256=str(raw.get("candidate_identity_sha256")),
    )


def _acknowledgment_from_payload(raw: object) -> WarningAcknowledgment | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ReloadRejected("reload_command_binding_malformed", "Reload acknowledgment binding is malformed.")
    try:
        return WarningAcknowledgment(
            actor=str(raw["actor"]),
            candidate_sha256=str(raw["candidate_sha256"]),
            candidate_identity_sha256=str(raw["candidate_identity_sha256"]),
            report_sha256=str(raw["report_sha256"]),
            active_generation=_integer(raw["active_generation"]),
            warning_identities=_string_tuple(raw["warning_identities"]),
            acknowledged_at=dt.datetime.fromisoformat(str(raw["acknowledged_at"])),
            validator_completed_at=dt.datetime.fromisoformat(str(raw["validator_completed_at"])),
            expires_at=dt.datetime.fromisoformat(str(raw["expires_at"])),
            maximum_age_seconds=_integer(raw["maximum_age_seconds"]),
            clock_skew_seconds=_integer(raw["clock_skew_seconds"]),
            schema_version=_integer(raw["schema_version"]),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise ReloadRejected(
            "reload_command_binding_malformed",
            "Reload acknowledgment binding is malformed.",
        ) from exc


def _optional_integer(value: object) -> int | None:
    return None if value is None else _integer(value)


def _failure_disposition(
    progress: Mapping[str, object],
    rollback_failed: bool,
) -> tuple[ReloadPhase, ReloadOutcome]:
    if rollback_failed or (progress["swapped"] and not progress["durable"]):
        return ReloadPhase.RECONCILIATION_REQUIRED, ReloadOutcome.RECONCILIATION_REQUIRED
    if progress["durable"]:
        return ReloadPhase.COMMITTED, ReloadOutcome.COMMITTED
    return ReloadPhase.ROLLED_BACK, ReloadOutcome.ROLLED_BACK


def _timestamp_or(value: object, fallback: dt.datetime) -> dt.datetime:
    if not isinstance(value, str):
        return fallback
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return fallback
    return parsed.astimezone(dt.UTC)


def _safe_mapping(value: Mapping[str, object], *, depth: int = 0) -> dict[str, object]:
    if depth > 3:
        return {}
    safe: dict[str, object] = {}
    for raw_key, raw_value in sorted(value.items())[:32]:
        key = str(raw_key)[:64]
        if isinstance(raw_value, bool | int | float) or raw_value is None:
            safe[key] = raw_value
        elif isinstance(raw_value, str):
            safe[key] = raw_value[:160]
        elif isinstance(raw_value, Mapping):
            safe[key] = _safe_mapping(raw_value, depth=depth + 1)
        elif isinstance(raw_value, tuple | list):
            safe[key] = [
                _safe_mapping(item, depth=depth + 1) if isinstance(item, Mapping) else str(item)[:128]
                for item in raw_value[:32]
            ]
    return safe
