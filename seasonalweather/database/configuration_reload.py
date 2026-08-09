"""Short-transaction durable journal for configuration reload attempts."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from seasonalweather.configuration_reload.models import CandidateRecord, ReloadPhase, ReloadRequest
from seasonalweather.diagnostics.bindings import RELOAD_CODES, code_for_rule

from .core import SeasonalDatabase

_ALLOWED: dict[ReloadPhase, frozenset[ReloadPhase]] = {
    ReloadPhase.REQUESTED: frozenset({ReloadPhase.CANDIDATE_CAPTURED, ReloadPhase.REJECTED, ReloadPhase.CANCELLED}),
    ReloadPhase.CANDIDATE_CAPTURED: frozenset(
        {ReloadPhase.VALIDATION_QUEUED, ReloadPhase.REJECTED, ReloadPhase.CANCELLED}
    ),
    ReloadPhase.VALIDATION_QUEUED: frozenset(
        {ReloadPhase.VALIDATION_RUNNING, ReloadPhase.REJECTED, ReloadPhase.CANCELLED}
    ),
    ReloadPhase.VALIDATION_RUNNING: frozenset(
        {ReloadPhase.REPORT_VERIFIED, ReloadPhase.REJECTED, ReloadPhase.CANCELLED}
    ),
    ReloadPhase.REPORT_VERIFIED: frozenset({ReloadPhase.CLASSIFIED, ReloadPhase.REJECTED}),
    ReloadPhase.CLASSIFIED: frozenset(
        {
            ReloadPhase.AWAITING_ACKNOWLEDGMENT,
            ReloadPhase.RESTART_REQUIRED,
            ReloadPhase.PREPARING,
            ReloadPhase.COMPLETED,
            ReloadPhase.REJECTED,
        }
    ),
    ReloadPhase.PREPARING: frozenset(
        {
            ReloadPhase.AWAITING_SAFE_POINT,
            ReloadPhase.COMMITTING,
            ReloadPhase.ROLLED_BACK,
            ReloadPhase.RECONCILIATION_REQUIRED,
            ReloadPhase.CANCELLED,
        }
    ),
    ReloadPhase.AWAITING_SAFE_POINT: frozenset(
        {
            ReloadPhase.COMMITTING,
            ReloadPhase.DEFERRED,
            ReloadPhase.ROLLED_BACK,
            ReloadPhase.RECONCILIATION_REQUIRED,
            ReloadPhase.CANCELLED,
        }
    ),
    ReloadPhase.COMMITTING: frozenset(
        {ReloadPhase.COMMITTED, ReloadPhase.ROLLED_BACK, ReloadPhase.RECONCILIATION_REQUIRED}
    ),
    ReloadPhase.COMMITTED: frozenset({ReloadPhase.RETIRING, ReloadPhase.COMPLETED}),
    ReloadPhase.RETIRING: frozenset({ReloadPhase.COMPLETED, ReloadPhase.RECONCILIATION_REQUIRED}),
    ReloadPhase.RECONCILIATION_REQUIRED: frozenset(
        {
            ReloadPhase.RECONCILIATION_REQUIRED,
            ReloadPhase.ROLLED_BACK,
            ReloadPhase.DEFERRED,
            ReloadPhase.CANCELLED,
            ReloadPhase.COMPLETED,
        }
    ),
}


class ReloadRepositoryError(RuntimeError):
    diagnostic_code = RELOAD_CODES["candidate_or_preparation_failed"]


class StaleReloadError(ReloadRepositoryError):
    diagnostic_code = code_for_rule("validation.report_rejected")


class ReloadAdmissionConflictError(ReloadRepositoryError):
    """The durable admission journal contains different same-key material."""


class ReloadRepository:
    def __init__(self, database: SeasonalDatabase) -> None:
        self.database = database

    @staticmethod
    def _iso(value: dt.datetime) -> str:
        return value.astimezone(dt.UTC).isoformat()

    def initialize_active(self, candidate: CandidateRecord, *, configuration_generation: int, at: dt.datetime) -> None:
        with self.database.transaction() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO configuration_reload_active (
                    singleton, generation, candidate_reference, source_sha256,
                    candidate_identity_sha256, updated_at
                ) VALUES (1, ?, ?, ?, ?, ?)
                """,
                (
                    configuration_generation,
                    candidate.reference,
                    candidate.source_sha256,
                    candidate.candidate_identity_sha256,
                    self._iso(at),
                ),
            )

    def active(self) -> dict[str, Any]:
        with self.database.connect() as conn:
            row = conn.execute("SELECT * FROM configuration_reload_active WHERE singleton = 1").fetchone()
        if row is None:
            raise ReloadRepositoryError("active configuration generation is not initialized")
        return dict(row)

    def create_attempt(
        self,
        *,
        attempt_id: str,
        command_id: str,
        actor: str,
        reason: str | None,
        dry_run: bool,
        old_generation: int,
        expected_generation: int | None,
        at: dt.datetime,
        candidate: CandidateRecord | None = None,
        request: ReloadRequest | None = None,
        idempotency_identity: str | None = None,
        old_candidate_reference: str | None = None,
        old_source_sha256: str | None = None,
        old_candidate_identity_sha256: str | None = None,
    ) -> None:
        phase, candidate_values, request_json = _candidate_admission(candidate, request)
        with self.database.transaction() as conn:
            conn.execute(
                """
                INSERT INTO configuration_reload_attempts (
                    attempt_id, command_id, phase, actor, reason, dry_run,
                    old_generation, expected_generation, candidate_reference,
                    source_sha256, source_byte_length, source_manifest_sha256, candidate_sha256,
                    candidate_identity_sha256, request_json, idempotency_identity,
                    old_candidate_reference, old_source_sha256, old_candidate_identity_sha256,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    command_id,
                    phase.value,
                    actor,
                    reason,
                    int(dry_run),
                    old_generation,
                    expected_generation,
                    *candidate_values,
                    request_json,
                    idempotency_identity,
                    old_candidate_reference,
                    old_source_sha256,
                    old_candidate_identity_sha256,
                    self._iso(at),
                    self._iso(at),
                ),
            )

    def begin_admission(
        self,
        *,
        idempotency_key: str,
        attempt_id: str,
        actor: str,
        reason: str | None,
        request: ReloadRequest,
        candidate: CandidateRecord,
        old_generation: int,
        old_candidate_reference: str,
        old_source_sha256: str,
        old_candidate_identity_sha256: str,
        at: dt.datetime,
    ) -> tuple[dict[str, Any], bool]:
        request_json = _bounded_json(request.command_payload())
        values = (
            idempotency_key,
            attempt_id,
            actor,
            reason,
            request_json,
            candidate.reference,
            candidate.source_sha256,
            candidate.byte_length,
            request.candidate.source_manifest_sha256 if request.candidate is not None else None,
            candidate.candidate_sha256,
            candidate.candidate_identity_sha256,
            old_generation,
            old_candidate_reference,
            old_source_sha256,
            old_candidate_identity_sha256,
            self._iso(at),
            self._iso(at),
        )
        with self.database.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM configuration_reload_admissions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is not None:
                existing = dict(row)
                if existing["request_json"] != request_json:
                    raise ReloadAdmissionConflictError("reload admission key was reused with different material")
                return existing, False
            conn.execute(
                """
                INSERT INTO configuration_reload_admissions (
                    idempotency_key, attempt_id, actor, reason, request_json,
                    candidate_reference, source_sha256, source_byte_length,
                    source_manifest_sha256, candidate_sha256, candidate_identity_sha256,
                    old_generation, old_candidate_reference, old_source_sha256,
                    old_candidate_identity_sha256, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            row = conn.execute(
                "SELECT * FROM configuration_reload_admissions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            raise ReloadRepositoryError("reload admission journal was not persisted")
        return dict(row), True

    def get_admission(self, idempotency_key: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM configuration_reload_admissions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return dict(row) if row is not None else None

    def incomplete_admissions(self, *, limit: int = 100) -> tuple[dict[str, Any], ...]:
        with self.database.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM configuration_reload_admissions
                 ORDER BY created_at, attempt_id LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def complete_admission(self, idempotency_key: str, *, attempt_id: str, command_id: str) -> None:
        with self.database.transaction() as conn:
            conn.execute(
                """
                DELETE FROM configuration_reload_admissions
                 WHERE idempotency_key = ? AND attempt_id = ?
                   AND (command_id IS NULL OR command_id = ?)
                """,
                (idempotency_key, attempt_id, command_id),
            )

    def discard_admission(self, idempotency_key: str, *, attempt_id: str) -> None:
        with self.database.transaction() as conn:
            conn.execute(
                "DELETE FROM configuration_reload_admissions WHERE idempotency_key = ? AND attempt_id = ?",
                (idempotency_key, attempt_id),
            )

    def bind_admission_command(self, *, idempotency_key: str, command_id: str) -> None:
        with self.database.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE configuration_reload_admissions
                   SET command_id = ?, updated_at = updated_at
                 WHERE idempotency_key = ? AND (command_id IS NULL OR command_id = ?)
                """,
                (command_id, idempotency_key, command_id),
            )
            if cursor.rowcount != 1:
                raise ReloadRepositoryError("reload admission command binding changed")

    def transition(self, attempt_id: str, target: ReloadPhase, *, at: dt.datetime, **fields: object) -> None:
        if "audit_json" in fields:
            fields["audit_json"] = _validate_audit_json(fields["audit_json"])
        with self.database.transaction() as conn:
            row = conn.execute(
                "SELECT phase FROM configuration_reload_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise KeyError(attempt_id)
            current = ReloadPhase(str(row["phase"]))
            if target is not current and target not in _ALLOWED.get(current, frozenset()):
                raise ReloadRepositoryError(f"illegal reload transition {current.value} -> {target.value}")
            allowed = {
                "candidate_reference",
                "source_sha256",
                "candidate_sha256",
                "candidate_identity_sha256",
                "report_reference",
                "report_sha256",
                "diff_sha256",
                "disposition",
                "outcome",
                "final_generation",
                "intent_json",
                "audit_json",
                "retirement_json",
                "retirement_evidence_json",
                "recovery_json",
                "finished_at",
            }
            if set(fields) - allowed:
                raise ValueError("unsupported reload journal field")
            ordered = (
                "candidate_reference",
                "source_sha256",
                "candidate_sha256",
                "candidate_identity_sha256",
                "report_reference",
                "report_sha256",
                "diff_sha256",
                "disposition",
                "outcome",
                "final_generation",
                "intent_json",
                "audit_json",
                "retirement_json",
                "retirement_evidence_json",
                "recovery_json",
                "finished_at",
            )
            values: list[object] = [target.value, self._iso(at)]
            for name in ordered:
                values.extend((int(name in fields), fields.get(name)))
            values.append(attempt_id)
            conn.execute(
                """
                UPDATE configuration_reload_attempts
                   SET phase = ?, updated_at = ?,
                       candidate_reference = CASE WHEN ? THEN ? ELSE candidate_reference END,
                       source_sha256 = CASE WHEN ? THEN ? ELSE source_sha256 END,
                       candidate_sha256 = CASE WHEN ? THEN ? ELSE candidate_sha256 END,
                       candidate_identity_sha256 = CASE WHEN ? THEN ? ELSE candidate_identity_sha256 END,
                       report_reference = CASE WHEN ? THEN ? ELSE report_reference END,
                       report_sha256 = CASE WHEN ? THEN ? ELSE report_sha256 END,
                       diff_sha256 = CASE WHEN ? THEN ? ELSE diff_sha256 END,
                       disposition = CASE WHEN ? THEN ? ELSE disposition END,
                       outcome = CASE WHEN ? THEN ? ELSE outcome END,
                       final_generation = CASE WHEN ? THEN ? ELSE final_generation END,
                       intent_json = CASE WHEN ? THEN ? ELSE intent_json END,
                       audit_json = CASE WHEN ? THEN ? ELSE audit_json END,
                       retirement_json = CASE WHEN ? THEN ? ELSE retirement_json END,
                       retirement_evidence_json = CASE WHEN ? THEN ? ELSE retirement_evidence_json END,
                       recovery_json = CASE WHEN ? THEN ? ELSE recovery_json END,
                       finished_at = CASE WHEN ? THEN ? ELSE finished_at END
                 WHERE attempt_id = ?
                """,
                values,
            )

    def record_intent(
        self,
        attempt_id: str,
        *,
        expected_generation: int,
        intent: dict[str, object],
        at: dt.datetime,
    ) -> None:
        encoded = _bounded_json(intent)
        with self.database.transaction() as conn:
            active = conn.execute("SELECT generation FROM configuration_reload_active WHERE singleton = 1").fetchone()
            if active is None or int(active["generation"]) != expected_generation:
                raise StaleReloadError("active generation changed before durable reload intent")
            row = conn.execute(
                "SELECT phase FROM configuration_reload_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if row is None or ReloadPhase(str(row["phase"])) not in {
                ReloadPhase.PREPARING,
                ReloadPhase.AWAITING_SAFE_POINT,
            }:
                raise ReloadRepositoryError("reload intent is not at the commit boundary")
            conn.execute(
                """
                UPDATE configuration_reload_attempts
                   SET phase = ?, intent_json = ?, updated_at = ?
                 WHERE attempt_id = ?
                """,
                (ReloadPhase.COMMITTING.value, encoded, self._iso(at), attempt_id),
            )

    def complete_commit(
        self,
        attempt_id: str,
        *,
        expected_generation: int,
        candidate: CandidateRecord,
        report_sha256: str,
        diff_sha256: str,
        audit_reference: str,
        at: dt.datetime,
        retirement_descriptor: dict[str, object] | None = None,
    ) -> int:
        generation = expected_generation + 1
        with self.database.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE configuration_reload_active
                   SET generation = ?, candidate_reference = ?, source_sha256 = ?,
                       candidate_identity_sha256 = ?, report_sha256 = ?, diff_sha256 = ?,
                       audit_reference = ?, updated_at = ?
                 WHERE singleton = 1 AND generation = ?
                """,
                (
                    generation,
                    candidate.reference,
                    candidate.source_sha256,
                    candidate.candidate_identity_sha256,
                    report_sha256,
                    diff_sha256,
                    audit_reference,
                    self._iso(at),
                    expected_generation,
                ),
            )
            if cursor.rowcount != 1:
                raise StaleReloadError("active generation changed during durable commit")
            attempt_cursor = conn.execute(
                """
                UPDATE configuration_reload_attempts
                   SET phase = ?, final_generation = ?, retirement_json = ?, updated_at = ?
                 WHERE attempt_id = ? AND phase = ?
                """,
                (
                    ReloadPhase.COMMITTED.value,
                    generation,
                    _bounded_json(retirement_descriptor) if retirement_descriptor is not None else None,
                    self._iso(at),
                    attempt_id,
                    ReloadPhase.COMMITTING.value,
                ),
            )
            if attempt_cursor.rowcount != 1:
                raise StaleReloadError("committing reload attempt changed during durable commit")
        return generation

    def get_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM configuration_reload_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_by_command(self, command_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM configuration_reload_attempts WHERE command_id = ?",
                (command_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def incomplete(self, *, limit: int = 100) -> tuple[dict[str, Any], ...]:
        with self.database.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM configuration_reload_attempts
                 WHERE finished_at IS NULL
                 ORDER BY created_at, attempt_id LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def terminal_evidence_needing_command_finalization(
        self,
        *,
        limit: int = 500,
    ) -> tuple[dict[str, Any], ...]:
        """Select only terminal reloads whose generic command is not terminal."""

        with self.database.connect() as conn:
            rows = conn.execute(
                """
                SELECT reload.* FROM configuration_reload_attempts AS reload
                  JOIN api_commands AS command ON command.command_id = reload.command_id
                 WHERE reload.finished_at IS NOT NULL
                   AND reload.audit_json IS NOT NULL AND reload.outcome IS NOT NULL
                   AND command.status NOT IN ('succeeded', 'failed', 'cancelled', 'expired', 'superseded')
                 ORDER BY reload.updated_at, reload.attempt_id LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def reconcile_incomplete(
        self,
        *,
        at: dt.datetime,
        limit: int = 100,
        exclude_attempt_ids: frozenset[str] = frozenset(),
    ) -> tuple[str, ...]:
        reconciled: list[str] = []
        for row in self.incomplete(limit=limit):
            attempt_id = str(row["attempt_id"])
            if attempt_id in exclude_attempt_ids:
                continue
            phase = ReloadPhase(str(row["phase"]))
            if phase is ReloadPhase.RECONCILIATION_REQUIRED and row.get("recovery_json"):
                continue
            committed = phase in {ReloadPhase.COMMITTED, ReloadPhase.RETIRING}
            if phase is ReloadPhase.COMMITTING or committed:
                target, outcome = ReloadPhase.RECONCILIATION_REQUIRED, "reconciliation_required"
            else:
                target, outcome = ReloadPhase.CANCELLED, "cancelled"
            audit = _bounded_json(
                {
                    "schema_version": 1,
                    "attempt_id": attempt_id,
                    "audit_reference": f"audit_{attempt_id.removeprefix('reload_')}",
                    "outcome": outcome,
                    "phase": target.value,
                    "old_generation": int(row["old_generation"]),
                    "final_generation": row.get("final_generation"),
                    "candidate_reference": row.get("candidate_reference"),
                    "candidate_sha256": row.get("candidate_sha256"),
                    "candidate_identity_sha256": row.get("candidate_identity_sha256"),
                    "report_sha256": row.get("report_sha256"),
                    "diff_sha256": row.get("diff_sha256"),
                    "diagnostic_codes": [RELOAD_CODES["reconciliation_required"]]
                    if target is ReloadPhase.RECONCILIATION_REQUIRED
                    else [],
                    "retirement_pending": phase in {ReloadPhase.COMMITTED, ReloadPhase.RETIRING},
                    "recovery_evidence": "durable retirement proof is required",
                    "reconciled_at": self._iso(at),
                }
            )
            with self.database.transaction() as conn:
                conn.execute(
                    """
                    UPDATE configuration_reload_attempts
                       SET phase = ?, outcome = ?, audit_json = ?,
                           finished_at = ?, updated_at = ?
                     WHERE attempt_id = ? AND finished_at IS NULL
                    """,
                    (target.value, outcome, audit, self._iso(at), self._iso(at), attempt_id),
                )
            reconciled.append(attempt_id)
        return tuple(reconciled)

    def fail_attempt(
        self,
        attempt_id: str,
        *,
        phase: ReloadPhase,
        outcome: str,
        audit: dict[str, object],
        at: dt.datetime,
    ) -> None:
        if phase not in {ReloadPhase.REJECTED, ReloadPhase.CANCELLED, ReloadPhase.RECONCILIATION_REQUIRED}:
            raise ValueError("unsupported reload failure phase")
        encoded = _bounded_json(audit)
        with self.database.transaction() as conn:
            conn.execute(
                """
                UPDATE configuration_reload_attempts
                   SET phase = CASE WHEN finished_at IS NULL THEN ? ELSE phase END,
                       outcome = COALESCE(outcome, ?), audit_json = ?,
                       finished_at = COALESCE(finished_at, ?), updated_at = ?
                 WHERE attempt_id = ?
                """,
                (phase.value, outcome, encoded, self._iso(at), self._iso(at), attempt_id),
            )


def _candidate_admission(
    candidate: CandidateRecord | None,
    request: ReloadRequest | None,
) -> tuple[ReloadPhase, tuple[object, ...], str | None]:
    if candidate is None and request is None:
        return ReloadPhase.REQUESTED, (None,) * 6, None
    if candidate is None or request is None or request.candidate is None:
        raise ValueError("candidate and durable request must be journaled together")
    return (
        ReloadPhase.CANDIDATE_CAPTURED,
        (
            candidate.reference,
            candidate.source_sha256,
            candidate.byte_length,
            request.candidate.source_manifest_sha256,
            candidate.candidate_sha256,
            candidate.candidate_identity_sha256,
        ),
        _bounded_json(request.command_payload()),
    )


def _bounded_json(value: dict[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if len(encoded.encode()) > 262_144:
        raise ReloadRepositoryError("reload journal artifact exceeds its bound")
    lowered = encoded.lower()
    if any(term in lowered for term in ("authorization", "bearer ", "raw_source", "secret_value")):
        raise ReloadRepositoryError("reload journal artifact contains prohibited material")
    return encoded


def _validate_audit_json(value: object) -> str:
    if not isinstance(value, str):
        raise ReloadRepositoryError("reload audit must be canonical JSON text")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ReloadRepositoryError("reload audit is malformed") from exc
    if not isinstance(decoded, dict):
        raise ReloadRepositoryError("reload audit must be a JSON object")
    return _bounded_json(decoded)
