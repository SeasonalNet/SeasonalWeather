from __future__ import annotations

import datetime as dt
from pathlib import Path

from seasonalweather.capabilities.manifest import CapabilityManifest
from seasonalweather.capabilities.models import (
    CapabilityRecord,
    CompatibilityState,
    OperationalState,
)
from seasonalweather.capabilities.registry import CapabilityRegistry
from seasonalweather.capabilities.service import (
    CapabilitySchedulerService,
    declared_capability_names,
)
from seasonalweather.job_store import (
    DurableJobService,
    JobDatabase,
    JobRepository,
    JobScheduler,
)
from seasonalweather.jobs.contracts import JobStatus
from seasonalweather.jobs.policies import ExecutorClass, JobType, QueueClass
from seasonalweather.lifecycle import Lifecycle
from seasonalweather.swwp.adapter import JobStoreSwwpAdapter
from seasonalweather.swwp.messages import JobResult
from tests.support.swwp_simulation import DeterministicIds

NOW = dt.datetime(2026, 7, 24, 12, tzinfo=dt.UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += dt.timedelta(seconds=seconds)


def capability_record(*, available: int = 1) -> CapabilityRecord:
    return CapabilityRecord(
        name="tts.synthesis.v1",
        implemented=True,
        compatibility=CompatibilityState.UNKNOWN,
        operational_state=OperationalState.HEALTHY,
        accepting_new_jobs=True,
        total_capacity=1,
        reported_available=available,
        parameters={"format": "wav"},
        validity_seconds=60,
        observed_at=NOW,
        published_at=NOW,
    )


def runtime(tmp_path: Path):
    clock = Clock()
    lifecycle = Lifecycle()
    lifecycle.mark_running()
    repository = JobRepository(
        JobDatabase(
            path=str(tmp_path / "jobs.sqlite3"),
            busy_timeout_ms=2000,
        ),
        payload_max_bytes=4096,
        result_max_bytes=4096,
    )
    jobs = DurableJobService(
        repository,
        lifecycle,
        reconciliation_batch_size=20,
        clock=clock,
    )
    jobs.initialize()
    scheduler = JobScheduler(
        repository,
        lifecycle,
        lease_seconds=30,
        acknowledgment_seconds=5,
        clock=clock,
    )
    adapter = JobStoreSwwpAdapter(scheduler, repository)
    registry = CapabilityRegistry(allowed_capabilities=declared_capability_names())
    service = CapabilitySchedulerService(
        registry,
        adapter,
        clock=clock,
        id_factory=DeterministicIds(),
    )
    manifest = CapabilityManifest.create(epoch=1, records=(capability_record(),))
    service.register(
        worker_id="worker_00000001",
        worker_instance_id="instance_00000001",
        session_id="session_00000001",
        manifest=manifest,
        authorized_capabilities=frozenset({"tts.synthesis.v1"}),
        authorized_job_types=frozenset({JobType.TTS_SYNTHESIZE}),
        payload_versions={JobType.TTS_SYNTHESIZE: 1},
        result_versions={JobType.TTS_SYNTHESIZE: 1},
    )
    return clock, repository, jobs, registry, service, manifest


def admit_tts(jobs: DurableJobService, suffix: str):
    return jobs.admit(
        job_type=JobType.TTS_SYNTHESIZE,
        payload={
            "content_ref": f"content_{suffix}",
            "voice_profile_ref": "profile_default_0001",
            "output_format": "wav",
            "config_generation": 7,
        },
        dedupe_key=f"tts:{suffix}",
        config_generation=7,
    )


def test_qualification_and_reservation_precede_durable_lease(tmp_path: Path) -> None:
    _, repository, jobs, registry, service, _ = runtime(tmp_path)
    admitted = admit_tts(jobs, "forecast_0001")

    assignment = service.acquire(
        owner="worker_00000001",
        queues=(QueueClass.ROUTINE,),
        executors=(ExecutorClass.ROUTINE_WORKER,),
        capabilities=("unauthorized.input.is.ignored",),
    )

    assert assignment is not None
    assert assignment.lease.job_id == admitted.job.job_id
    assert repository.get(admitted.job.job_id).status is JobStatus.LEASED
    snapshot = registry.snapshot("worker_00000001", NOW)
    assert snapshot is not None
    assert snapshot.pending_reservations == 1
    assert snapshot.effective_capacity["tts.synthesis.v1"] == 0
    service.acknowledge(assignment.lease)
    assert repository.get(admitted.job.job_id).status is JobStatus.RUNNING
    assert registry.snapshot("worker_00000001", NOW).active_assignments == 1


def test_last_race_rejection_reconciles_and_requires_targeted_probe(
    tmp_path: Path,
) -> None:
    _, repository, jobs, registry, service, manifest = runtime(tmp_path)
    admitted = admit_tts(jobs, "forecast_0002")
    assignment = service.acquire(
        owner="worker_00000001",
        queues=(QueueClass.ROUTINE,),
        executors=(ExecutorClass.ROUTINE_WORKER,),
        capabilities=(),
    )
    assert assignment is not None

    service.reject_unacknowledged(
        assignment.lease,
        category="capability_unavailable",
        capability_names=("tts.synthesis.v1",),
    )
    rejected = repository.get(admitted.job.job_id)
    snapshot = registry.snapshot("worker_00000001", NOW)

    assert rejected is not None
    assert rejected.status is JobStatus.PENDING
    assert rejected.error is None
    assert snapshot is not None
    assert snapshot.probe_required is True
    assert snapshot.records[0].operational_state is OperationalState.UNKNOWN
    assert (
        service.acquire(
            owner="worker_00000001",
            queues=(QueueClass.ROUTINE,),
            executors=(ExecutorClass.ROUTINE_WORKER,),
            capabilities=(),
        )
        is None
    )

    probe = service.take_probes("worker_00000001")[0]
    assert probe.target_names == ("tts.synthesis.v1",)
    service.report(
        "worker_00000001",
        session_id="session_00000001",
        worker_instance_id="instance_00000001",
        probe_id=probe.probe_id,
        schema_version=1,
        epoch=2,
        records=manifest.records,
        full_digest=manifest.digest,
        validity_seconds=60,
    )
    restored = registry.snapshot("worker_00000001", NOW)
    assert restored is not None
    assert restored.probe_required is False
    assert restored.records[0].operational_state is OperationalState.HEALTHY


def test_deterministic_worker_order_prefers_capacity_then_identity(
    tmp_path: Path,
) -> None:
    _, _, jobs, registry, service, _ = runtime(tmp_path)
    admitted = admit_tts(jobs, "forecast_0003")
    second_manifest = CapabilityManifest.create(
        epoch=1,
        records=(capability_record().model_copy(update={"total_capacity": 2, "reported_available": 2}),),
    )
    service.register(
        worker_id="worker_00000002",
        worker_instance_id="instance_00000002",
        session_id="session_00000002",
        manifest=second_manifest,
        authorized_capabilities=frozenset({"tts.synthesis.v1"}),
        authorized_job_types=frozenset({JobType.TTS_SYNTHESIZE}),
        payload_versions={JobType.TTS_SYNTHESIZE: 1},
        result_versions={JobType.TTS_SYNTHESIZE: 1},
    )

    qualified = service.qualified_workers(job=admitted.job)

    assert [item.worker_id for item in qualified] == [
        "worker_00000002",
        "worker_00000001",
    ]
    assert registry.snapshot("worker_00000002", NOW).effective_capacity["tts.synthesis.v1"] == 2


def test_durable_result_releases_active_capacity_exactly_once(tmp_path: Path) -> None:
    _, _, jobs, registry, service, _ = runtime(tmp_path)
    admit_tts(jobs, "forecast_0004")
    assignment = service.acquire(
        owner="worker_00000001",
        queues=(QueueClass.ROUTINE,),
        executors=(ExecutorClass.ROUTINE_WORKER,),
        capabilities=(),
    )
    assert assignment is not None
    service.acknowledge(assignment.lease)
    result = JobResult(
        lease=assignment.lease,
        result_schema_version=1,
        result={
            "artifact_ref": "artifact_forecast_0004",
            "content_identity": "content_forecast_0004",
            "duration_seconds": 1.0,
        },
        completion_id="completion_forecast_0004",
    )
    first = service.result(result)
    assert registry.snapshot("worker_00000001", NOW).active_assignments == 0
    second = service.result(result)
    assert second.result_hash == first.result_hash
    assert registry.snapshot("worker_00000001", NOW).active_assignments == 0
