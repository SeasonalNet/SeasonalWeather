from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Iterable
from typing import Any, cast

from ..jobs.contracts import (
    TERMINAL_JOB_STATUSES,
    AttemptOutcome,
    JobError,
    JobRecord,
    JobResult,
    JobStatus,
    resolve_attempt,
    start_job,
)
from ..jobs.policies import (
    DedupeMode,
    ExecutorClass,
    FailureCategory,
    JobPriority,
    QueueClass,
)
from ..jobs.registry import policy_for
from .core import JobDatabase
from .models import (
    AdmissionDisposition,
    DurableAdmission,
    JobAssignment,
    JobStoreConflictError,
    JobStoreValidationError,
    ReconciliationSummary,
    RepositoryHealth,
    ResultCommitReceipt,
    StaleJobMutationError,
)

_KEY_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_SECRET_KEYS = {
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
    "traceback",
    "exception",
}
_TERMINAL = tuple(status.value for status in TERMINAL_JOB_STATUSES)


def _iso(value: dt.datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise JobStoreValidationError("timestamps must be timezone-aware")
    return value.astimezone(dt.UTC).isoformat()


def _time(value: str | None) -> dt.datetime | None:
    if value is None:
        return None
    parsed = dt.datetime.fromisoformat(value)
    return parsed.astimezone(dt.UTC)


def _required_time(value: str | None) -> dt.datetime:
    parsed = _time(value)
    if parsed is None:
        raise JobStoreValidationError("required repository timestamp is absent")
    return parsed


def _json_bytes(value: Any, *, limit: int, name: str) -> tuple[str, str]:
    _validate_json(value, path=name)
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    raw = encoded.encode("utf-8")
    if len(raw) > limit:
        raise JobStoreValidationError(f"{name} exceeds configured byte limit")
    return encoded, hashlib.sha256(raw).hexdigest()


def _validate_json(value: Any, *, path: str, depth: int = 0) -> None:
    if depth > 6:
        raise JobStoreValidationError(f"{path} exceeds nesting limit")
    if value is None or isinstance(value, bool | int | float):
        return
    if isinstance(value, str):
        _validate_json_string(value, path)
        return
    if isinstance(value, list | tuple):
        _validate_json_sequence(value, path=path, depth=depth)
        return
    if isinstance(value, dict):
        _validate_json_mapping(value, path=path, depth=depth)
        return
    raise JobStoreValidationError(f"{path} must contain JSON data only")


def _validate_json_string(value: str, path: str) -> None:
    if len(value) > 1024:
        raise JobStoreValidationError(f"{path} contains overlong text")
    lowered = value.lower()
    prohibited = (
        "authorization:" in lowered
        or "bearer " in lowered
        or "seasonalclient " in lowered
        or value.startswith(("/", "\\\\"))
    )
    if prohibited:
        raise JobStoreValidationError(f"{path} contains prohibited data")


def _validate_json_sequence(
    value: list[Any] | tuple[Any, ...],
    *,
    path: str,
    depth: int,
) -> None:
    if len(value) > 64:
        raise JobStoreValidationError(f"{path} contains too many items")
    for item in value:
        _validate_json(item, path=f"{path}[]", depth=depth + 1)


def _validate_json_mapping(
    value: dict[Any, Any],
    *,
    path: str,
    depth: int,
) -> None:
    if len(value) > 64:
        raise JobStoreValidationError(f"{path} contains too many keys")
    for raw_key, item in value.items():
        key = str(raw_key).strip().lower()
        if key in _SECRET_KEYS or key.endswith(("_path", "_token", "_secret")):
            raise JobStoreValidationError(f"{path} contains prohibited data")
        _validate_json(item, path=f"{path}.{key}", depth=depth + 1)


class JobRepository:
    """Synchronous controller-owned durable state machine.

    Each public mutation owns exactly one short SQLite transaction and uses
    state/version or lease/attempt predicates for its write.
    """

    def __init__(
        self,
        database: JobDatabase,
        *,
        payload_max_bytes: int = 65_536,
        result_max_bytes: int = 65_536,
        progress_retention: int = 100,
        event_retention: int = 500,
        controller_id: str | None = None,
    ) -> None:
        self.database = database
        self.payload_max_bytes = int(payload_max_bytes)
        self.result_max_bytes = int(result_max_bytes)
        self.progress_retention = int(progress_retention)
        self.event_retention = int(event_retention)
        self.controller_id = controller_id or f"controller_{uuid.uuid4().hex}"

    def initialize(self) -> None:
        self.database.initialize()

    def get(self, job_id: str) -> JobRecord | None:
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return self._job(row) if row is not None else None

    def list_jobs(
        self,
        *,
        statuses: Iterable[JobStatus] | None = None,
        limit: int = 100,
    ) -> tuple[JobRecord, ...]:
        bounded = max(1, min(int(limit), 500))
        values = frozenset(status.value for status in statuses or ())
        with self.database.connection() as conn:
            rows = conn.execute("SELECT * FROM jobs ORDER BY created_at, job_id LIMIT 500").fetchall()
        selected = (row for row in rows if row["status"] in values) if values else iter(rows)
        return tuple(self._job(row) for row in selected)[:bounded]

    def admit(self, job: JobRecord, *, at: dt.datetime) -> DurableAdmission:
        payload_json, _ = _json_bytes(
            job.payload,
            limit=self.payload_max_bytes,
            name="payload",
        )
        policy = policy_for(job.job_type)
        if job.status is not JobStatus.PENDING or job.attempt != 0:
            raise JobStoreValidationError("only fresh pending job specifications may be admitted")
        if job.queue is not policy.queue or job.executor is not policy.executor:
            raise JobStoreValidationError("job queue/executor differs from authoritative policy")
        now = _iso(at)
        with self.database.transaction() as conn:
            existing = self._active_dedupe(conn, job)
            if existing is not None:
                decision = self._dedupe(conn, existing, job, payload_json, now)
                if decision is not None:
                    return decision
            self._insert_job(conn, job, payload_json)
            self._event(conn, job.job_id, "admitted", now)
        return DurableAdmission(AdmissionDisposition.CREATED, job)

    def acquire_next(
        self,
        *,
        owner: str,
        now: dt.datetime,
        lease_seconds: int,
        acknowledgment_seconds: int,
        queues: Iterable[QueueClass] | None = None,
        executors: Iterable[ExecutorClass] | None = None,
        capabilities: Iterable[str] = (),
        candidate_job_ids: Iterable[str] | None = None,
    ) -> JobAssignment | None:
        owner = self._bounded_identifier(owner, "lease owner")
        queue_values = {item.value for item in queues or QueueClass}
        executor_values = {item.value for item in executors or ExecutorClass}
        available = frozenset(capabilities)
        candidates = None if candidate_job_ids is None else frozenset(candidate_job_ids)
        now_iso = _iso(now)
        with self.database.transaction() as conn:
            rows = conn.execute(
                """
                SELECT * FROM jobs
                 WHERE status = 'pending'
                   AND cancel_requested = 0
                   AND not_before <= ?
                   AND deadline_at > ?
                 ORDER BY priority ASC, not_before ASC, created_at ASC, job_id ASC
                 LIMIT 128
                """,
                (now_iso, now_iso),
            ).fetchall()
            row = self._select_eligible_assignment(
                rows,
                queue_values=queue_values,
                executor_values=executor_values,
                available=available,
                candidate_job_ids=candidates,
            )
            if row is None:
                return None
            job = self._job(row)
            attempt = job.attempt + 1
            lease_id = f"lease_{uuid.uuid4().hex}"
            attempt_id = f"attempt_{uuid.uuid4().hex}"
            lease_expires = min(
                now + dt.timedelta(seconds=lease_seconds),
                job.deadline_at,
            )
            ack_deadline = min(
                now + dt.timedelta(seconds=acknowledgment_seconds),
                lease_expires,
            )
            cursor = conn.execute(
                """
                UPDATE jobs
                   SET status = 'leased', attempt = ?, attempt_id = ?,
                       lease_id = ?, lease_owner = ?, ack_deadline_at = ?,
                       lease_expires_at = ?, lease_controller_id = ?,
                       version = version + 1
                 WHERE job_id = ? AND status = 'pending' AND version = ?
                   AND cancel_requested = 0 AND deadline_at > ?
                """,
                (
                    attempt,
                    attempt_id,
                    lease_id,
                    owner,
                    _iso(ack_deadline),
                    _iso(lease_expires),
                    self.controller_id,
                    job.job_id,
                    int(row["version"]),
                    now_iso,
                ),
            )
            self._changed(cursor, "job was concurrently leased")
            conn.execute(
                """
                INSERT INTO job_attempts (
                    job_id, attempt, attempt_id, lease_id, lease_owner,
                    acquired_at, ack_deadline_at, lease_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    attempt,
                    attempt_id,
                    lease_id,
                    owner,
                    now_iso,
                    _iso(ack_deadline),
                    _iso(lease_expires),
                ),
            )
            self._lease_event(
                conn,
                job.job_id,
                attempt,
                lease_id,
                "acquired",
                now_iso,
            )
            leased_row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (job.job_id,),
            ).fetchone()
        leased = self._job(leased_row)
        return JobAssignment(
            job=leased,
            lease_id=lease_id,
            attempt_id=attempt_id,
            lease_owner=owner,
            attempt=attempt,
            acknowledged_by=ack_deadline,
            lease_expires_at=lease_expires,
            required_capabilities=tuple(item.name for item in policy_for(leased.job_type).capabilities),
        )

    def acknowledge(
        self,
        *,
        job_id: str,
        lease_id: str,
        attempt_id: str,
        owner: str,
        at: dt.datetime,
    ) -> JobRecord:
        with self.database.transaction() as conn:
            row = self._current_lease(
                conn,
                job_id,
                lease_id,
                attempt_id,
                owner,
            )
            if row["status"] != JobStatus.LEASED.value or _required_time(row["ack_deadline_at"]) <= at:
                raise StaleJobMutationError("assignment acknowledgment is stale")
            started = start_job(
                self._job(row),
                attempt_id=attempt_id,
                lease_owner=owner,
                at=at,
            )
            cursor = conn.execute(
                """
                UPDATE jobs
                   SET status = 'running', started_at = ?, version = version + 1
                 WHERE job_id = ? AND version = ? AND status = 'leased'
                   AND lease_id = ? AND attempt_id = ? AND lease_owner = ?
                """,
                (
                    _iso(at),
                    job_id,
                    int(row["version"]),
                    lease_id,
                    attempt_id,
                    owner,
                ),
            )
            self._changed(cursor, "assignment acknowledgment lost its lease")
            conn.execute(
                """
                UPDATE job_attempts
                   SET acknowledged_at = ?, started_at = ?
                 WHERE job_id = ? AND attempt = ? AND lease_id = ?
                """,
                (_iso(at), _iso(at), job_id, started.attempt, lease_id),
            )
            self._lease_event(
                conn,
                job_id,
                started.attempt,
                lease_id,
                "acknowledged",
                _iso(at),
            )
        return started

    def reject_unacknowledged(
        self,
        *,
        job_id: str,
        lease_id: str,
        attempt_id: str,
        owner: str,
        at: dt.datetime,
    ) -> JobRecord:
        """Release a current pre-acceptance lease without a handler outcome."""

        now_iso = _iso(at)
        with self.database.transaction() as conn:
            row = self._current_lease(
                conn,
                job_id,
                lease_id,
                attempt_id,
                owner,
            )
            if row["status"] != JobStatus.LEASED.value:
                raise StaleJobMutationError("only an unacknowledged lease may be rejected")
            self._release_unacknowledged(conn, row, now_iso)
            updated = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return self._job(updated)

    def renew(
        self,
        *,
        job_id: str,
        lease_id: str,
        attempt_id: str,
        owner: str,
        at: dt.datetime,
        lease_seconds: int,
    ) -> JobRecord:
        with self.database.transaction() as conn:
            row = self._current_lease(
                conn,
                job_id,
                lease_id,
                attempt_id,
                owner,
            )
            if row["status"] not in {"leased", "running"}:
                raise StaleJobMutationError("only an active lease may renew")
            if _required_time(row["lease_expires_at"]) <= at:
                raise StaleJobMutationError("expired lease cannot renew")
            expires = min(
                at + dt.timedelta(seconds=lease_seconds),
                self._job(row).deadline_at,
            )
            if expires <= at:
                raise StaleJobMutationError("job deadline prevents renewal")
            cursor = conn.execute(
                """
                UPDATE jobs
                   SET lease_expires_at = ?, version = version + 1
                 WHERE job_id = ? AND version = ? AND lease_id = ?
                   AND attempt_id = ? AND lease_owner = ?
                   AND status IN ('leased', 'running')
                """,
                (
                    _iso(expires),
                    job_id,
                    int(row["version"]),
                    lease_id,
                    attempt_id,
                    owner,
                ),
            )
            self._changed(cursor, "lease renewal was stale")
            conn.execute(
                """
                UPDATE job_attempts SET lease_expires_at = ?
                 WHERE job_id = ? AND attempt = ? AND lease_id = ?
                """,
                (_iso(expires), job_id, int(row["attempt"]), lease_id),
            )
            self._lease_event(
                conn,
                job_id,
                int(row["attempt"]),
                lease_id,
                "renewed",
                _iso(at),
            )
            updated = dict(row)
            updated["lease_expires_at"] = _iso(expires)
        return self._job(updated)

    def append_progress(
        self,
        *,
        job_id: str,
        lease_id: str,
        attempt_id: str,
        owner: str,
        stage: str,
        reason: str | None,
        numeric: dict[str, int | float],
        at: dt.datetime,
    ) -> None:
        if not _KEY_RE.fullmatch(stage) or (reason is not None and not _KEY_RE.fullmatch(reason)):
            raise JobStoreValidationError("progress stage/reason must be bounded keys")
        if len(numeric) > 16 or any(
            not _KEY_RE.fullmatch(key) or isinstance(value, bool) for key, value in numeric.items()
        ):
            raise JobStoreValidationError("progress numeric data is invalid")
        numeric_json, _ = _json_bytes(numeric, limit=2048, name="progress")
        with self.database.transaction() as conn:
            row = self._current_lease(
                conn,
                job_id,
                lease_id,
                attempt_id,
                owner,
            )
            if row["status"] != "running" or _required_time(row["lease_expires_at"]) <= at:
                raise StaleJobMutationError("progress requires a current running lease")
            conn.execute(
                """
                INSERT INTO job_progress (
                    job_id, attempt, lease_id, stage, reason, numeric_json,
                    occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    int(row["attempt"]),
                    lease_id,
                    stage,
                    reason,
                    numeric_json,
                    _iso(at),
                ),
            )
            conn.execute(
                """
                DELETE FROM job_progress
                 WHERE job_id = ? AND progress_id NOT IN (
                    SELECT progress_id FROM job_progress
                     WHERE job_id = ? ORDER BY progress_id DESC LIMIT ?
                 )
                """,
                (job_id, job_id, self.progress_retention),
            )

    def request_cancellation(self, job_id: str, *, at: dt.datetime) -> JobRecord:
        now = _iso(at)
        with self.database.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            job = self._job(row)
            if job.status in TERMINAL_JOB_STATUSES:
                return job
            if job.status is JobStatus.PENDING:
                cursor = conn.execute(
                    """
                    UPDATE jobs
                       SET status = 'cancelled', cancel_requested = 1,
                           finished_at = ?, version = version + 1,
                           command_sync_pending = CASE
                             WHEN command_id IS NULL THEN 0 ELSE 1 END
                     WHERE job_id = ? AND version = ? AND status = 'pending'
                    """,
                    (now, job_id, int(row["version"])),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE jobs
                       SET cancel_requested = 1, version = version + 1
                     WHERE job_id = ? AND version = ?
                       AND status IN ('leased', 'running')
                    """,
                    (job_id, int(row["version"])),
                )
            self._changed(cursor, "cancellation request raced with a transition")
            self._event(conn, job_id, "cancellation_requested", now)
            updated = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return self._job(updated)

    def record_outcome(
        self,
        *,
        job_id: str,
        lease_id: str,
        attempt_id: str,
        owner: str,
        outcome: AttemptOutcome,
        at: dt.datetime,
        result_payload: dict[str, Any] | None = None,
        error: JobError | None = None,
        replay_permitted: bool = False,
    ) -> JobRecord | ResultCommitReceipt:
        with self.database.transaction() as conn:
            stored = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if stored is None:
                raise KeyError(job_id)
            stored_job = self._job(stored)
            result_json, result_hash, typed_result = self._validated_result(
                stored_job,
                outcome=outcome,
                result_payload=result_payload,
            )
            committed = conn.execute(
                "SELECT * FROM job_result_commits WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if committed is not None:
                return self._replayed_commit(
                    committed,
                    outcome=outcome,
                    result_hash=result_hash,
                )
            row = self._current_lease(
                conn,
                job_id,
                lease_id,
                attempt_id,
                owner,
            )
            job = self._job(row)
            self._validate_outcome_fences(job, row, outcome=outcome, at=at)
            resolved = resolve_attempt(
                job,
                attempt_id=attempt_id,
                lease_owner=owner,
                outcome=outcome,
                at=at,
                retry_policy=policy_for(job.job_type).retry,
                result=typed_result,
                error=error,
                replay_permitted=replay_permitted,
            )
            error_json = self._serialized_error(resolved)
            self._persist_outcome(
                conn,
                row=row,
                job=job,
                resolved=resolved,
                outcome=outcome,
                at=at,
                lease_id=lease_id,
                attempt_id=attempt_id,
                owner=owner,
                result_json=result_json,
                result_hash=result_hash,
                error_json=error_json,
            )
            if outcome is AttemptOutcome.SUCCEEDED:
                return self._commit_result(
                    conn,
                    job=job,
                    lease_id=lease_id,
                    at=at,
                    result_json=result_json,
                    result_hash=result_hash,
                )
        return resolved

    def _validated_result(
        self,
        job: JobRecord,
        *,
        outcome: AttemptOutcome,
        result_payload: dict[str, Any] | None,
    ) -> tuple[str | None, str | None, JobResult | None]:
        if outcome is not AttemptOutcome.SUCCEEDED:
            return None, None, None
        if result_payload is None:
            raise JobStoreValidationError("successful outcome requires a result")
        policy = policy_for(job.job_type)
        normalized = policy.result_schema.model_validate(result_payload).model_dump(mode="json")
        result_json, result_hash = _json_bytes(
            normalized,
            limit=self.result_max_bytes,
            name="result",
        )
        return result_json, result_hash, self._contract_result(normalized)

    @staticmethod
    def _replayed_commit(
        committed: sqlite3.Row,
        *,
        outcome: AttemptOutcome,
        result_hash: str | None,
    ) -> ResultCommitReceipt:
        if outcome is not AttemptOutcome.SUCCEEDED or committed["result_hash"] != result_hash:
            raise JobStoreConflictError("job already has a different terminal result")
        return ResultCommitReceipt(
            str(committed["job_id"]),
            int(committed["attempt"]),
            str(committed["result_hash"]),
            _required_time(committed["committed_at"]),
            True,
        )

    @staticmethod
    def _validate_outcome_fences(
        job: JobRecord,
        row: sqlite3.Row,
        *,
        outcome: AttemptOutcome,
        at: dt.datetime,
    ) -> None:
        if at >= job.deadline_at or _required_time(row["lease_expires_at"]) <= at:
            raise StaleJobMutationError("result arrived after its lease or deadline")
        if job.cancel_requested and outcome is AttemptOutcome.SUCCEEDED:
            raise StaleJobMutationError("cancel-requested work cannot commit a successful result")

    def _serialized_error(self, resolved: JobRecord) -> str | None:
        if resolved.error is None:
            return None
        return _json_bytes(
            resolved.error.model_dump(mode="json"),
            limit=self.result_max_bytes,
            name="error",
        )[0]

    def _persist_outcome(
        self,
        conn: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        job: JobRecord,
        resolved: JobRecord,
        outcome: AttemptOutcome,
        at: dt.datetime,
        lease_id: str,
        attempt_id: str,
        owner: str,
        result_json: str | None,
        result_hash: str | None,
        error_json: str | None,
    ) -> None:
        cursor = conn.execute(
            """
            UPDATE jobs
               SET status = ?, not_before = ?, started_at = ?,
                   finished_at = ?, attempt_id = ?, lease_id = ?,
                   lease_owner = ?, ack_deadline_at = ?,
                   lease_expires_at = ?, lease_controller_id = ?,
                   result_json = ?, error_json = ?,
                   result_hash = ?, version = version + 1,
                   command_sync_pending = CASE
                     WHEN ? IS NULL OR ? = 'pending' THEN 0 ELSE 1 END
             WHERE job_id = ? AND version = ? AND lease_id = ?
               AND attempt_id = ? AND lease_owner = ?
               AND status IN ('leased', 'running')
            """,
            (
                resolved.status.value,
                _iso(resolved.not_before),
                _iso(resolved.started_at) if resolved.started_at else None,
                _iso(resolved.finished_at) if resolved.finished_at else None,
                resolved.attempt_id,
                None,
                resolved.lease_owner,
                None,
                _iso(resolved.lease_expires_at) if resolved.lease_expires_at else None,
                None,
                result_json,
                error_json,
                result_hash,
                job.command_id,
                resolved.status.value,
                job.job_id,
                int(row["version"]),
                lease_id,
                attempt_id,
                owner,
            ),
        )
        self._changed(cursor, "attempt outcome was stale")
        conn.execute(
            """
            UPDATE job_attempts
               SET finished_at = ?, outcome = ?, error_json = ?
             WHERE job_id = ? AND attempt = ? AND lease_id = ?
            """,
            (
                _iso(at),
                outcome.value,
                error_json,
                job.job_id,
                job.attempt,
                lease_id,
            ),
        )
        self._lease_event(
            conn,
            job.job_id,
            job.attempt,
            lease_id,
            "released",
            _iso(at),
            {"outcome": outcome.value},
        )
        self._event(
            conn,
            job.job_id,
            f"attempt_{outcome.value}",
            _iso(at),
        )

    @staticmethod
    def _commit_result(
        conn: sqlite3.Connection,
        *,
        job: JobRecord,
        lease_id: str,
        at: dt.datetime,
        result_json: str | None,
        result_hash: str | None,
    ) -> ResultCommitReceipt:
        if result_json is None or result_hash is None:
            raise JobStoreValidationError("successful result normalization was incomplete")
        conn.execute(
            """
            INSERT INTO job_result_commits (
                job_id, attempt, lease_id, result_schema_version,
                result_json, result_hash, committed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.job_id,
                job.attempt,
                lease_id,
                job.result_schema_version,
                result_json,
                result_hash,
                _iso(at),
            ),
        )
        return ResultCommitReceipt(
            job.job_id,
            job.attempt,
            result_hash,
            at.astimezone(dt.UTC),
            False,
        )

    def reconcile(
        self,
        *,
        now: dt.datetime,
        batch_size: int,
    ) -> ReconciliationSummary:
        now_iso = _iso(now)
        counts = {
            "inspected": 0,
            "expired_deadlines": 0,
            "cancelled_pending": 0,
            "released_unacknowledged": 0,
            "uncertain_attempts": 0,
            "command_sync_pending": 0,
        }
        with self.database.transaction() as conn:
            rows = conn.execute(
                """
                SELECT * FROM jobs
                 WHERE status NOT IN ('succeeded','failed','cancelled','expired','superseded')
                   AND (
                       deadline_at <= ? OR cancel_requested = 1
                       OR (status = 'leased' AND ack_deadline_at <= ?)
                       OR (status = 'running' AND lease_expires_at <= ?)
                       OR (status IN ('leased','running')
                           AND lease_controller_id <> ?)
                   )
                 ORDER BY created_at, job_id LIMIT ?
                """,
                (
                    now_iso,
                    now_iso,
                    now_iso,
                    self.controller_id,
                    max(1, min(batch_size, 1000)),
                ),
            ).fetchall()
            for row in rows:
                counts["inspected"] += 1
                changed = self._reconcile_one(conn, row, now_iso)
                if changed is not None:
                    counts[changed] += 1
            counts["command_sync_pending"] = int(
                conn.execute("SELECT COUNT(*) FROM jobs WHERE command_sync_pending = 1").fetchone()[0]
            )
        return ReconciliationSummary(**counts)

    def _reconcile_one(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        now_iso: str,
    ) -> str | None:
        if row["deadline_at"] <= now_iso:
            self._reconcile_terminal(
                conn,
                row,
                status="expired",
                outcome="timed_out",
                event="deadline_expired",
                at=now_iso,
            )
            return "expired_deadlines"
        if row["status"] == "pending" and row["cancel_requested"]:
            self._reconcile_terminal(
                conn,
                row,
                status="cancelled",
                outcome="cancelled",
                event="pending_cancelled",
                at=now_iso,
            )
            return "cancelled_pending"
        if row["status"] == "leased":
            self._release_unacknowledged(conn, row, now_iso)
            return "released_unacknowledged"
        if row["status"] == "running":
            self._reconcile_terminal(
                conn,
                row,
                status="failed",
                outcome="lost",
                event="revalidation_required",
                at=now_iso,
                error=JobError(
                    category=FailureCategory.SIDE_EFFECT_UNCERTAIN,
                    code="revalidation_required",
                    message="Expired running attempt requires controller revalidation.",
                ),
            )
            return "uncertain_attempts"
        return None

    def reconcile_for_shutdown(
        self,
        *,
        now: dt.datetime,
        batch_size: int,
    ) -> ReconciliationSummary:
        """Close current lease authority without claiming execution outcome."""
        now_iso = _iso(now)
        released = 0
        uncertain = 0
        inspected = 0
        with self.database.transaction() as conn:
            rows = conn.execute(
                """
                SELECT * FROM jobs WHERE status IN ('leased','running')
                 ORDER BY created_at, job_id LIMIT ?
                """,
                (max(1, min(batch_size, 1000)),),
            ).fetchall()
            for row in rows:
                inspected += 1
                if row["status"] == "leased":
                    self._release_unacknowledged(conn, row, now_iso)
                    released += 1
                else:
                    self._reconcile_terminal(
                        conn,
                        row,
                        status="failed",
                        outcome="lost",
                        event="revalidation_required",
                        at=now_iso,
                        error=JobError(
                            category=FailureCategory.SIDE_EFFECT_UNCERTAIN,
                            code="revalidation_required",
                            message="Shutdown interrupted an attempt; controller revalidation is required.",
                        ),
                    )
                    uncertain += 1
            command_sync = int(conn.execute("SELECT COUNT(*) FROM jobs WHERE command_sync_pending = 1").fetchone()[0])
        return ReconciliationSummary(
            inspected=inspected,
            released_unacknowledged=released,
            uncertain_attempts=uncertain,
            command_sync_pending=command_sync,
        )

    def jobs_pending_command_sync(self, *, limit: int = 100) -> tuple[JobRecord, ...]:
        with self.database.connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM jobs WHERE command_sync_pending = 1
                 ORDER BY finished_at, job_id LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
        return tuple(self._job(row) for row in rows)

    def mark_command_synced(self, job_id: str, *, expected_status: JobStatus) -> None:
        with self.database.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs SET command_sync_pending = 0, version = version + 1
                 WHERE job_id = ? AND status = ? AND command_sync_pending = 1
                """,
                (job_id, expected_status.value),
            )
            self._changed(cursor, "command aggregation synchronization was stale")

    def health(self, *, now: dt.datetime, admission_open: bool) -> RepositoryHealth:
        settings = self.database.settings()
        now_iso = _iso(now)
        with self.database.connection() as conn:
            queue_counts = {
                str(row["queue"]): int(row["count"])
                for row in conn.execute(
                    """
                    SELECT queue, COUNT(*) AS count FROM jobs
                     WHERE status = 'pending' GROUP BY queue
                    """
                )
            }
            active = int(conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('leased','running')").fetchone()[0])
            overdue = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM jobs
                     WHERE status NOT IN ('succeeded','failed','cancelled','expired','superseded')
                       AND deadline_at <= ?
                    """,
                    (now_iso,),
                ).fetchone()[0]
            )
            cancellations = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM jobs
                     WHERE cancel_requested = 1
                       AND status IN ('leased','running')
                    """
                ).fetchone()[0]
            )
            reconcile = overdue + int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM jobs
                     WHERE command_sync_pending = 1
                        OR (status = 'leased' AND ack_deadline_at <= ?)
                        OR (status = 'running' AND lease_expires_at <= ?)
                        OR (status IN ('leased','running')
                            AND lease_controller_id <> ?)
                    """,
                    (now_iso, now_iso, self.controller_id),
                ).fetchone()[0]
            )
        return RepositoryHealth(
            enabled=True,
            initialized=bool(settings["initialized"]),
            schema_version=int(settings["schema_version"]),
            wal=settings["journal_mode"] == "wal",
            admission_open=admission_open,
            queue_counts=queue_counts,
            active_leases=active,
            overdue_jobs=overdue,
            cancellation_backlog=cancellations,
            reconciliation_required=reconcile,
        )

    def _active_dedupe(
        self,
        conn: sqlite3.Connection,
        job: JobRecord,
    ) -> sqlite3.Row | None:
        if job.dedupe_key is None:
            return None
        return cast(
            sqlite3.Row | None,
            conn.execute(
                """
            SELECT * FROM jobs
             WHERE job_type = ? AND dedupe_key = ?
               AND status IN ('pending','leased','running')
            """,
                (job.job_type.value, job.dedupe_key),
            ).fetchone(),
        )

    def _dedupe(
        self,
        conn: sqlite3.Connection,
        existing: sqlite3.Row,
        job: JobRecord,
        payload_json: str,
        now: str,
    ) -> DurableAdmission | None:
        policy = policy_for(job.job_type).dedupe
        current = self._job(existing)
        if policy.mode is DedupeMode.NONE:
            return None
        if existing["payload_json"] == payload_json:
            disposition = (
                AdmissionDisposition.REUSED if policy.mode is DedupeMode.EXACT else AdmissionDisposition.COALESCED
            )
            self._event(conn, current.job_id, disposition.value, now)
            return DurableAdmission(disposition, current, current.job_id)
        if policy.mode is DedupeMode.EXACT:
            self._event(conn, current.job_id, "dedupe_conflict", now)
            return DurableAdmission(
                AdmissionDisposition.CONFLICT,
                current,
                current.job_id,
            )
        if existing["status"] != JobStatus.PENDING.value or not policy.supersedes_older:
            self._event(conn, current.job_id, "coalesced", now)
            return DurableAdmission(
                AdmissionDisposition.COALESCED,
                current,
                current.job_id,
            )
        cursor = conn.execute(
            """
            UPDATE jobs
               SET status = 'superseded', finished_at = ?, version = version + 1,
                   command_sync_pending = CASE
                     WHEN command_id IS NULL THEN 0 ELSE 1 END
             WHERE job_id = ? AND version = ? AND status = 'pending'
            """,
            (now, current.job_id, int(existing["version"])),
        )
        self._changed(cursor, "dedupe supersession was stale")
        self._insert_job(conn, job, payload_json)
        conn.execute(
            """
            INSERT INTO job_relationships (
                job_id, related_job_id, relation, created_at
            ) VALUES (?, ?, 'supersedes', ?)
            """,
            (job.job_id, current.job_id, now),
        )
        self._event(conn, current.job_id, "superseded", now)
        self._event(conn, job.job_id, "admitted", now)
        return DurableAdmission(
            AdmissionDisposition.SUPERSEDED,
            job,
            current.job_id,
        )

    @staticmethod
    def _insert_job(
        conn: sqlite3.Connection,
        job: JobRecord,
        payload_json: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO jobs (
                job_id, command_id, job_type, queue, executor, priority,
                status, payload_schema_version, result_schema_version,
                payload_json, created_at, not_before, deadline_at,
                started_at, finished_at, attempt, max_attempts, dedupe_key,
                config_generation, replay_policy, cancel_requested
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.job_id,
                job.command_id,
                job.job_type.value,
                job.queue.value,
                job.executor.value,
                int(job.priority),
                job.status.value,
                job.payload_schema_version,
                job.result_schema_version,
                payload_json,
                _iso(job.created_at),
                _iso(job.not_before),
                _iso(job.deadline_at),
                _iso(job.started_at) if job.started_at else None,
                _iso(job.finished_at) if job.finished_at else None,
                job.attempt,
                job.max_attempts,
                job.dedupe_key,
                job.config_generation,
                job.replay_policy.value,
                int(job.cancel_requested),
            ),
        )

    @staticmethod
    def _changed(cursor: sqlite3.Cursor, message: str) -> None:
        if cursor.rowcount != 1:
            raise StaleJobMutationError(message)

    def _current_lease(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        lease_id: str,
        attempt_id: str,
        owner: str,
    ) -> sqlite3.Row:
        row = cast(
            sqlite3.Row | None,
            conn.execute(
                """
            SELECT * FROM jobs WHERE job_id = ? AND lease_id = ?
             AND attempt_id = ? AND lease_owner = ?
             AND lease_controller_id = ?
            """,
                (job_id, lease_id, attempt_id, owner, self.controller_id),
            ).fetchone(),
        )
        if row is None:
            raise StaleJobMutationError("lease, owner, or attempt is stale")
        return row

    @staticmethod
    def _bounded_identifier(value: str, name: str) -> str:
        value = str(value).strip()
        if not 3 <= len(value) <= 128 or any(not (char.isalnum() or char in "_.:-") for char in value):
            raise JobStoreValidationError(f"{name} is not a bounded identifier")
        return value

    @staticmethod
    def _capabilities_satisfied(
        row: sqlite3.Row,
        available: frozenset[str],
    ) -> bool:
        required = {capability.name for capability in policy_for(row["job_type"]).capabilities if capability.required}
        return required <= available

    def _select_eligible_assignment(
        self,
        rows: Iterable[sqlite3.Row],
        *,
        queue_values: set[str],
        executor_values: set[str],
        available: frozenset[str],
        candidate_job_ids: frozenset[str] | None,
    ) -> sqlite3.Row | None:
        return next(
            (
                row
                for row in rows
                if row["queue"] in queue_values
                and row["executor"] in executor_values
                and (candidate_job_ids is None or row["job_id"] in candidate_job_ids)
                and self._capabilities_satisfied(row, available)
            ),
            None,
        )

    def _event(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        event_type: str,
        occurred_at: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        data_json, _ = _json_bytes(data or {}, limit=4096, name="event")
        conn.execute(
            """
            INSERT INTO job_events(job_id, event_type, occurred_at, data_json)
            VALUES (?, ?, ?, ?)
            """,
            (job_id, event_type, occurred_at, data_json),
        )
        conn.execute(
            """
            DELETE FROM job_events
             WHERE job_id = ? AND event_id NOT IN (
                SELECT event_id FROM job_events WHERE job_id = ?
                 ORDER BY event_id DESC LIMIT ?
             )
            """,
            (job_id, job_id, self.event_retention),
        )

    def _lease_event(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        attempt: int,
        lease_id: str,
        event_type: str,
        occurred_at: str,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        evidence_json = json.dumps(
            evidence or {},
            sort_keys=True,
            separators=(",", ":"),
        )
        conn.execute(
            """
            INSERT INTO job_lease_events (
                job_id, attempt, lease_id, event_type, occurred_at, evidence_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (job_id, attempt, lease_id, event_type, occurred_at, evidence_json),
        )
        conn.execute(
            """
            DELETE FROM job_lease_events
             WHERE job_id = ? AND event_id NOT IN (
                SELECT event_id FROM job_lease_events WHERE job_id = ?
                 ORDER BY event_id DESC LIMIT ?
             )
            """,
            (job_id, job_id, self.event_retention),
        )

    def _release_unacknowledged(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        at: str,
    ) -> None:
        exhausted = int(row["attempt"]) >= int(row["max_attempts"])
        status = "failed" if exhausted or row["cancel_requested"] else "pending"
        error = (
            JobError(
                category=FailureCategory.TIMED_OUT,
                code="assignment_unacknowledged",
                message="Assignment acknowledgment deadline expired.",
            )
            if status == "failed"
            else None
        )
        error_json = json.dumps(error.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) if error else None
        cursor = conn.execute(
            """
            UPDATE jobs
               SET status = ?, finished_at = ?, attempt_id = NULL,
                   lease_id = NULL, lease_owner = NULL, ack_deadline_at = NULL,
                   lease_expires_at = NULL, lease_controller_id = NULL,
                   error_json = ?, version = version + 1,
                   command_sync_pending = CASE
                     WHEN ? = 'pending' OR command_id IS NULL THEN 0 ELSE 1 END
             WHERE job_id = ? AND version = ? AND status = 'leased'
            """,
            (
                status,
                at if status != "pending" else None,
                error_json,
                status,
                row["job_id"],
                int(row["version"]),
            ),
        )
        self._changed(cursor, "unacknowledged lease reconciliation was stale")
        conn.execute(
            """
            UPDATE job_attempts SET finished_at = ?, outcome = 'timed_out',
                   error_json = ?
             WHERE job_id = ? AND attempt = ?
            """,
            (at, error_json, row["job_id"], int(row["attempt"])),
        )
        self._lease_event(
            conn,
            row["job_id"],
            int(row["attempt"]),
            row["lease_id"],
            "ack_timeout",
            at,
        )

    def _reconcile_terminal(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        status: str,
        outcome: str,
        event: str,
        at: str,
        error: JobError | None = None,
    ) -> None:
        error_json = (
            json.dumps(error.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
            if error
            else row["error_json"]
        )
        cursor = conn.execute(
            """
            UPDATE jobs
               SET status = ?, finished_at = ?, error_json = ?,
                   attempt_id = NULL, lease_id = NULL, lease_owner = NULL,
                   lease_controller_id = NULL, ack_deadline_at = NULL,
                   lease_expires_at = NULL,
                   version = version + 1,
                   command_sync_pending = CASE
                     WHEN command_id IS NULL THEN 0 ELSE 1 END
             WHERE job_id = ? AND version = ?
               AND status NOT IN ('succeeded','failed','cancelled','expired','superseded')
            """,
            (status, at, error_json, row["job_id"], int(row["version"])),
        )
        self._changed(cursor, "reconciliation transition was stale")
        if row["attempt"]:
            conn.execute(
                """
                UPDATE job_attempts SET finished_at = ?, outcome = ?,
                       error_json = ?
                 WHERE job_id = ? AND attempt = ? AND finished_at IS NULL
                """,
                (at, outcome, error_json, row["job_id"], int(row["attempt"])),
            )
        self._event(conn, row["job_id"], event, at)

    @staticmethod
    def _contract_result(payload: dict[str, Any]) -> JobResult:
        result_ref = next(
            (str(value) for key, value in payload.items() if key.endswith("_ref") and isinstance(value, str)),
            None,
        )
        if result_ref is None:
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[
                :24
            ]
            result_ref = f"result_{digest}"
        metadata = {
            key: value
            for key, value in payload.items()
            if isinstance(value, str | int | float | bool) and not key.endswith("_ref")
        }
        return JobResult(
            code="completed",
            result_ref=result_ref,
            metadata=dict(list(sorted(metadata.items()))[:16]),
        )

    @staticmethod
    def _job(row: sqlite3.Row | dict[str, Any]) -> JobRecord:
        result_raw = json.loads(row["result_json"]) if row["result_json"] else None
        error_raw = json.loads(row["error_json"]) if row["error_json"] else None
        result = (
            JobRepository._contract_result(result_raw)
            if result_raw is not None and row["status"] == "succeeded"
            else None
        )
        return JobRecord(
            job_id=row["job_id"],
            command_id=row["command_id"],
            job_type=row["job_type"],
            queue=row["queue"],
            executor=row["executor"],
            status=row["status"],
            priority=JobPriority(int(row["priority"])),
            payload_schema_version=int(row["payload_schema_version"]),
            result_schema_version=int(row["result_schema_version"]),
            payload=json.loads(row["payload_json"]),
            created_at=_required_time(row["created_at"]),
            not_before=_required_time(row["not_before"]),
            deadline_at=_required_time(row["deadline_at"]),
            started_at=_time(row["started_at"]),
            finished_at=_time(row["finished_at"]),
            attempt=int(row["attempt"]),
            attempt_id=row["attempt_id"],
            max_attempts=int(row["max_attempts"]),
            dedupe_key=row["dedupe_key"],
            config_generation=row["config_generation"],
            replay_policy=row["replay_policy"],
            cancel_requested=bool(row["cancel_requested"]),
            lease_owner=row["lease_owner"],
            lease_expires_at=_time(row["lease_expires_at"]),
            result=result,
            error=JobError.model_validate(error_raw) if error_raw else None,
        )
