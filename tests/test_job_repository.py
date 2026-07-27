from __future__ import annotations

import asyncio
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from seasonalweather.commands.service import CommandStore
from seasonalweather.database.core import SeasonalDatabase
from seasonalweather.job_store import (
    AdmissionDisposition,
    CommandJobCoordinator,
    DurableJobService,
    JobDatabase,
    JobRepository,
    JobScheduler,
    JobStoreValidationError,
    ResultCommitReceipt,
    StaleJobMutationError,
)
from seasonalweather.jobs.contracts import AttemptOutcome, JobError, JobRecord, JobStatus
from seasonalweather.jobs.policies import FailureCategory, JobType
from seasonalweather.lifecycle import AdmissionClosedError, Lifecycle

NOW = dt.datetime(2026, 7, 24, 12, tzinfo=dt.UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += dt.timedelta(seconds=seconds)


def _runtime(tmp_path: Path) -> tuple[Clock, Lifecycle, JobRepository, DurableJobService]:
    clock = Clock()
    lifecycle = Lifecycle()
    lifecycle.mark_running()
    repository = JobRepository(
        JobDatabase(path=str(tmp_path / "jobs.sqlite3"), busy_timeout_ms=2000),
        payload_max_bytes=4096,
        result_max_bytes=4096,
        progress_retention=3,
        event_retention=10,
    )
    service = DurableJobService(
        repository,
        lifecycle,
        reconciliation_batch_size=20,
        clock=clock,
    )
    service.initialize()
    return clock, lifecycle, repository, service


def _admit_tts(
    service: DurableJobService,
    *,
    dedupe_key: str = "tts:forecast_0001",
    content_ref: str = "content_forecast_0001",
):
    return service.admit(
        job_type=JobType.TTS_SYNTHESIZE,
        payload={
            "content_ref": content_ref,
            "voice_profile_ref": "profile_default_0001",
            "output_format": "wav",
            "config_generation": 7,
        },
        dedupe_key=dedupe_key,
        config_generation=7,
    )


def _scheduler(
    repository: JobRepository,
    lifecycle: Lifecycle,
    clock: Clock,
) -> JobScheduler:
    return JobScheduler(
        repository,
        lifecycle,
        lease_seconds=30,
        acknowledgment_seconds=5,
        clock=clock,
    )


def test_fresh_and_repeat_initialization_enforces_separate_wal_database(
    tmp_path: Path,
) -> None:
    path = tmp_path / "jobs.sqlite3"
    database = JobDatabase(path=str(path), busy_timeout_ms=1234)

    database.initialize()
    database.initialize()

    assert path.is_file()
    assert database.settings() == {
        "initialized": True,
        "schema_version": 2,
        "expected_schema_version": 2,
        "journal_mode": "wal",
        "synchronous": 2,
        "foreign_keys": True,
        "busy_timeout_ms": 1234,
    }
    with database.connection() as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "jobs" in tables
    assert "job_attempts" in tables
    assert "api_commands" not in tables


def test_concurrent_equivalent_admission_has_one_authoritative_job(
    tmp_path: Path,
) -> None:
    _, _, repository, service = _runtime(tmp_path)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _: _admit_tts(service), range(2)))

    assert {result.disposition for result in results} == {
        AdmissionDisposition.CREATED,
        AdmissionDisposition.REUSED,
    }
    assert len({result.job.job_id for result in results}) == 1
    assert len(repository.list_jobs()) == 1


def test_coalesce_latest_supersedes_only_pending_work(tmp_path: Path) -> None:
    _, _, repository, service = _runtime(tmp_path)
    first = service.admit(
        job_type=JobType.CYCLE_REGENERATE,
        payload={
            "cycle_ref": "cycle_current_0001",
            "reason_code": "operator_request",
            "config_generation": 7,
        },
        dedupe_key="cycle:station_0001",
        config_generation=7,
    )
    second = service.admit(
        job_type=JobType.CYCLE_REGENERATE,
        payload={
            "cycle_ref": "cycle_current_0002",
            "reason_code": "source_change",
            "config_generation": 7,
        },
        dedupe_key="cycle:station_0001",
        config_generation=7,
    )

    assert second.disposition is AdmissionDisposition.SUPERSEDED
    assert second.related_job_id == first.job.job_id
    assert repository.get(first.job.job_id).status is JobStatus.SUPERSEDED
    assert repository.get(second.job.job_id).status is JobStatus.PENDING


def test_scheduler_leases_once_orders_work_and_fences_stale_updates(
    tmp_path: Path,
) -> None:
    clock, lifecycle, repository, service = _runtime(tmp_path)
    admitted = _admit_tts(service)
    scheduler = _scheduler(repository, lifecycle, clock)

    assignment = scheduler.assign(
        owner="worker_test_0001",
        capabilities={"tts.synthesis.v1"},
    )

    assert assignment is not None
    assert assignment.job.job_id == admitted.job.job_id
    assert assignment.attempt == 1
    assert (
        scheduler.assign(
            owner="worker_test_0002",
            capabilities={"tts.synthesis.v1"},
        )
        is None
    )
    with pytest.raises(StaleJobMutationError):
        repository.acknowledge(
            job_id=assignment.job.job_id,
            lease_id=assignment.lease_id,
            attempt_id=assignment.attempt_id,
            owner="worker_wrong_0001",
            at=clock(),
        )
    running = scheduler.acknowledge(assignment)
    assert running.status is JobStatus.RUNNING


def test_explicit_empty_candidate_filter_leases_nothing(tmp_path: Path) -> None:
    clock, lifecycle, repository, service = _runtime(tmp_path)
    admitted = _admit_tts(service)
    scheduler = _scheduler(repository, lifecycle, clock)

    assert (
        scheduler.assign(
            owner="worker_test_0001",
            capabilities={"tts.synthesis.v1"},
            candidate_job_ids=(),
        )
        is None
    )
    assert repository.get(admitted.job.job_id).status is JobStatus.PENDING

    assignment = scheduler.assign(
        owner="worker_test_0001",
        capabilities={"tts.synthesis.v1"},
        candidate_job_ids=None,
    )
    assert assignment is not None
    assert assignment.job.job_id == admitted.job.job_id


def test_concurrent_scheduler_connections_cannot_double_lease(tmp_path: Path) -> None:
    clock, lifecycle, repository, service = _runtime(tmp_path)
    _admit_tts(service)

    def assign(owner: str):
        scheduler = _scheduler(repository, lifecycle, clock)
        return scheduler.assign(
            owner=owner,
            capabilities={"tts.synthesis.v1"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        assignments = tuple(pool.map(assign, ("worker_test_0001", "worker_test_0002")))

    assert sum(item is not None for item in assignments) == 1


def test_scheduler_applies_priority_not_before_and_capability_filters(
    tmp_path: Path,
) -> None:
    clock, lifecycle, repository, service = _runtime(tmp_path)
    _admit_tts(service)
    cycle = service.admit(
        job_type=JobType.CYCLE_REGENERATE,
        payload={
            "cycle_ref": "cycle_current_0001",
            "reason_code": "operator_request",
            "config_generation": 7,
        },
        not_before=clock() + dt.timedelta(seconds=10),
        dedupe_key="cycle:station_0001",
        config_generation=7,
    )
    scheduler = _scheduler(repository, lifecycle, clock)

    first = scheduler.assign(
        owner="worker_test_0001",
        capabilities={"tts.synthesis.v1", "cycle.regenerate.v1"},
    )
    assert first is not None
    assert first.job.job_type is JobType.TTS_SYNTHESIZE

    clock.advance(10)
    second = scheduler.assign(
        owner="worker_test_0002",
        capabilities={"cycle.regenerate.v1"},
    )
    assert second is not None
    assert second.job.job_id == cycle.job.job_id


def test_progress_renewal_and_result_commit_are_bounded_and_idempotent(
    tmp_path: Path,
) -> None:
    clock, lifecycle, repository, service = _runtime(tmp_path)
    _admit_tts(service)
    scheduler = _scheduler(repository, lifecycle, clock)
    assignment = scheduler.assign(
        owner="worker_test_0001",
        capabilities={"tts.synthesis.v1"},
    )
    assert assignment is not None
    scheduler.acknowledge(assignment)
    for count in range(5):
        scheduler.progress(
            assignment,
            stage="synthesizing",
            numeric={"percent": count * 10},
        )
    clock.advance(2)
    renewed = scheduler.renew(assignment)
    assert renewed.lease_expires_at <= renewed.deadline_at

    payload = {
        "artifact_ref": "artifact_forecast_0001",
        "content_identity": "content_forecast_0001",
        "duration_seconds": 4.5,
    }
    receipt = scheduler.outcome(
        assignment,
        outcome=AttemptOutcome.SUCCEEDED,
        result_payload=payload,
    )
    replay = scheduler.outcome(
        assignment,
        outcome=AttemptOutcome.SUCCEEDED,
        result_payload=payload,
    )

    assert isinstance(receipt, ResultCommitReceipt)
    assert isinstance(replay, ResultCommitReceipt)
    assert receipt.idempotent_replay is False
    assert replay.idempotent_replay is True
    assert repository.get(assignment.job.job_id).status is JobStatus.SUCCEEDED
    with repository.database.connection() as conn:
        progress_count = conn.execute(
            "SELECT COUNT(*) FROM job_progress WHERE job_id = ?",
            (assignment.job.job_id,),
        ).fetchone()[0]
    assert progress_count == 3


def test_retry_attempts_are_monotonic_and_drain_closes_leasing(
    tmp_path: Path,
) -> None:
    clock, lifecycle, repository, service = _runtime(tmp_path)
    _admit_tts(service)
    scheduler = _scheduler(repository, lifecycle, clock)
    first = scheduler.assign(
        owner="worker_test_0001",
        capabilities={"tts.synthesis.v1"},
    )
    assert first is not None
    scheduler.acknowledge(first)
    pending = scheduler.outcome(
        first,
        outcome=AttemptOutcome.RETRYABLE_FAILURE,
        error=JobError(
            category=FailureCategory.DEPENDENCY_UNAVAILABLE,
            code="backend_unavailable",
            message="Synthesis backend is unavailable.",
        ),
    )
    assert isinstance(pending, JobRecord)
    assert pending.status is JobStatus.PENDING
    clock.now = pending.not_before
    second = scheduler.assign(
        owner="worker_test_0002",
        capabilities={"tts.synthesis.v1"},
    )
    assert second is not None
    assert second.attempt == 2
    assert second.attempt_id != first.attempt_id

    lifecycle.request_shutdown()
    with pytest.raises(AdmissionClosedError, match="admission is closed"):
        scheduler.assign(
            owner="worker_test_0003",
            capabilities={"tts.synthesis.v1"},
        )


def test_ack_timeout_and_reconciliation_are_restart_safe(tmp_path: Path) -> None:
    clock, lifecycle, repository, service = _runtime(tmp_path)
    admitted = _admit_tts(service)
    scheduler = _scheduler(repository, lifecycle, clock)
    assignment = scheduler.assign(
        owner="worker_test_0001",
        capabilities={"tts.synthesis.v1"},
    )
    assert assignment is not None
    clock.advance(6)

    first = service.reconcile()
    second = service.reconcile()

    assert first.released_unacknowledged == 1
    assert second.inspected == 0
    reopened = JobRepository(
        JobDatabase(
            path=repository.database.path,
            busy_timeout_ms=repository.database.busy_timeout_ms,
        )
    )
    reopened.initialize()
    recovered = reopened.get(admitted.job.job_id)
    assert recovered is not None
    assert recovered.status is JobStatus.PENDING
    assert recovered.attempt == 1


def test_restart_reconciliation_fences_prior_controller_running_lease(
    tmp_path: Path,
) -> None:
    clock, lifecycle, repository, service = _runtime(tmp_path)
    _admit_tts(service)
    scheduler = _scheduler(repository, lifecycle, clock)
    assignment = scheduler.assign(
        owner="worker_test_0001",
        capabilities={"tts.synthesis.v1"},
    )
    assert assignment is not None
    scheduler.acknowledge(assignment)

    restarted = JobRepository(
        JobDatabase(
            path=repository.database.path,
            busy_timeout_ms=repository.database.busy_timeout_ms,
        ),
        controller_id="controller_restarted_0001",
    )
    restarted.initialize()
    summary = restarted.reconcile(now=clock(), batch_size=20)

    assert summary.uncertain_attempts == 1
    recovered = restarted.get(assignment.job.job_id)
    assert recovered is not None
    assert recovered.status is JobStatus.FAILED
    assert recovered.error is not None
    assert recovered.error.code == "revalidation_required"


def test_shutdown_reconciliation_closes_active_lease_authority(
    tmp_path: Path,
) -> None:
    clock, lifecycle, repository, service = _runtime(tmp_path)
    _admit_tts(service)
    scheduler = _scheduler(repository, lifecycle, clock)
    assignment = scheduler.assign(
        owner="worker_test_0001",
        capabilities={"tts.synthesis.v1"},
    )
    assert assignment is not None
    scheduler.acknowledge(assignment)
    lifecycle.request_shutdown()

    summary = service.close()

    assert summary.uncertain_attempts == 1
    closed = repository.get(assignment.job.job_id)
    assert closed is not None
    assert closed.status is JobStatus.FAILED
    assert closed.error is not None
    assert closed.error.code == "revalidation_required"


def test_cancellation_result_race_fails_closed_and_can_acknowledge_cancel(
    tmp_path: Path,
) -> None:
    clock, lifecycle, repository, service = _runtime(tmp_path)
    _admit_tts(service)
    scheduler = _scheduler(repository, lifecycle, clock)
    assignment = scheduler.assign(
        owner="worker_test_0001",
        capabilities={"tts.synthesis.v1"},
    )
    assert assignment is not None
    scheduler.acknowledge(assignment)
    requested = repository.request_cancellation(
        assignment.job.job_id,
        at=clock(),
    )
    assert requested.cancel_requested is True

    with pytest.raises(StaleJobMutationError, match="cancel-requested"):
        scheduler.outcome(
            assignment,
            outcome=AttemptOutcome.SUCCEEDED,
            result_payload={
                "artifact_ref": "artifact_forecast_0001",
                "content_identity": "content_forecast_0001",
                "duration_seconds": 4.5,
            },
        )
    cancelled = scheduler.outcome(
        assignment,
        outcome=AttemptOutcome.CANCELLED,
        error=JobError(
            category=FailureCategory.CANCELLED,
            code="cancelled_by_controller",
            message="Controller cancellation was acknowledged.",
        ),
    )
    assert isinstance(cancelled, JobRecord)
    assert cancelled.status is JobStatus.CANCELLED


def test_payload_limits_and_prohibited_material_fail_closed(tmp_path: Path) -> None:
    clock, _, repository, _ = _runtime(tmp_path)
    valid = JobRecord(
        job_id="job_manual_0001",
        job_type=JobType.TTS_SYNTHESIZE,
        queue="routine",
        executor="routine_worker",
        priority=20,
        payload_schema_version=1,
        result_schema_version=1,
        payload={"token": "not-allowed"},
        created_at=clock(),
        not_before=clock(),
        deadline_at=clock() + dt.timedelta(minutes=1),
        max_attempts=2,
        config_generation=7,
        replay_policy="idempotent_all_fences",
    )

    with pytest.raises(JobStoreValidationError, match="prohibited"):
        repository.admit(valid, at=clock())


def test_command_aggregation_repairs_cross_database_crash_window(
    tmp_path: Path,
) -> None:
    clock, lifecycle, repository, service = _runtime(tmp_path)
    command_database = SeasonalDatabase(path=str(tmp_path / "operations.sqlite3"))
    command_store = CommandStore(
        database=command_database,
        lifecycle=lifecycle,
        clock=clock,
    )
    command, _ = asyncio.run(
        command_store.create_or_replay(
            command_type="cycle.rebuild",
            idempotency_key="p1-07-command-sync",
            actor="operator",
            payload={"reason": "test"},
        )
    )
    admitted = service.admit(
        job_type=JobType.ALERT_ARTIFACT_GENERATE,
        payload={
            "source_identity": "source_alert_0001",
            "event_identity": "event_alert_0001",
            "content_identity": "content_alert_0001",
            "content_ref": "content_alert_0001",
            "mode": "full",
            "config_generation": 7,
        },
        command_id=command.command_id,
        deadline_at=clock() + dt.timedelta(seconds=60),
        dedupe_key="alert:source_event_content_0001",
        config_generation=7,
    )
    scheduler = _scheduler(repository, lifecycle, clock)
    assignment = scheduler.assign(
        owner="worker_test_0001",
        capabilities={"tts.synthesis.v1", "audio.alert_artifact.v1"},
    )
    assert assignment is not None
    assert assignment.job.job_id == admitted.job.job_id
    scheduler.acknowledge(assignment)
    failed = scheduler.outcome(
        assignment,
        outcome=AttemptOutcome.PERMANENT_FAILURE,
        error=JobError(
            category=FailureCategory.INVALID_INPUT,
            code="artifact_invalid",
            message="Artifact contract validation failed.",
        ),
    )
    assert isinstance(failed, JobRecord)
    assert failed.status is JobStatus.FAILED

    coordinator = CommandJobCoordinator(repository, command_store)
    assert asyncio.run(coordinator.repair()) == 1
    repaired = asyncio.run(command_store.get(command.command_id))
    assert repaired.status.value == "failed"
    assert repository.jobs_pending_command_sync() == ()

    reopened_store = CommandStore(
        database=command_database,
        lifecycle=lifecycle,
        clock=clock,
    )
    assert asyncio.run(CommandJobCoordinator(repository, reopened_store).repair()) == 0
    assert asyncio.run(reopened_store.get(command.command_id)).status.value == "failed"


def test_artifact_publication_receipt_is_metadata_only_and_idempotent(tmp_path: Path) -> None:
    clock, _, repository, service = _runtime(tmp_path)
    admitted = _admit_tts(service)
    receipt = repository.record_artifact_receipt(
        job_id=admitted.job.job_id,
        attempt_id="attempt_00000001",
        artifact_digest="sha256:" + "a" * 64,
        artifact_size_bytes=12,
        artifact_class="blob",
        target_key="current.bin",
        disposition="rejected",
        metadata={"schema_version": 1},
        committed_at=None,
    )
    replay = repository.record_artifact_receipt(
        job_id=admitted.job.job_id,
        attempt_id="attempt_00000001",
        artifact_digest="sha256:" + "a" * 64,
        artifact_size_bytes=12,
        artifact_class="blob",
        target_key="current.bin",
        disposition="rejected",
        metadata={"schema_version": 1},
        committed_at=None,
    )
    assert replay == receipt
