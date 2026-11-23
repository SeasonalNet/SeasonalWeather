from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import shutil
import threading
import wave
from types import SimpleNamespace

import pytest

from seasonalweather.artifacts.composition import build_controller_artifact_composition
from seasonalweather.artifacts.models import ArtifactClass, ArtifactReference, ArtifactResult, MediaMetadata
from seasonalweather.configuration_reload.safe_point import (
    PUBLICATION,
    WORKER_RESULT,
    ActivityRegistry,
    SafePointCoordinator,
    SafePointTimeout,
)
from seasonalweather.job_store import DurableJobService, JobDatabase, JobRepository, JobScheduler
from seasonalweather.jobs.policies import JobType
from seasonalweather.jobs.worker_client import WorkerInputStore, WorkerSynthesisClient, WorkerSynthesisConfiguration
from seasonalweather.lifecycle import Lifecycle
from seasonalweather.swwp.adapter import JobStoreSwwpAdapter
from seasonalweather.swwp.codec import decode, encode
from seasonalweather.swwp.messages import Envelope, JobResult, LeaseRef
from seasonalweather.worker.handlers import HandlerContext, LocalTtsHandler

NOW = dt.datetime(2026, 7, 26, 12, tzinfo=dt.UTC)


@pytest.mark.skipif(
    not shutil.which("espeak-ng") or not shutil.which("ffmpeg"), reason="requires local eSpeak NG and ffmpeg"
)
def test_routine_worker_wav_commits_after_swwp_round_trip(tmp_path) -> None:
    lifecycle = Lifecycle()
    lifecycle.mark_running()
    repository = JobRepository(JobDatabase(path=str(tmp_path / "jobs.sqlite3"), busy_timeout_ms=1000))
    jobs = DurableJobService(repository, lifecycle, reconciliation_batch_size=20)
    _ = jobs.initialize()
    composition = build_controller_artifact_composition(
        SimpleNamespace(reload_activities=ActivityRegistry(), lifecycle=lifecycle, configuration_generation=0),
        repository,
        work_root=tmp_path,
        maximum_bytes=1024 * 1024,
    )
    inputs = composition.transport.paths.staging / "controller-inputs"
    content, profile = WorkerInputStore(inputs).put(
        text="This is a station identification test.",
        configuration=WorkerSynthesisConfiguration("espeak-ng", "en-us", 165, 1.0, 48000),
    )
    job_type, payload, dedupe_key = WorkerSynthesisClient._job_request(
        purpose="routine",
        content_ref=content,
        profile_ref=profile,
        generation=0,
        source_identity=None,
        event_identity=None,
        content_identity=None,
    )
    admitted = jobs.admit(job_type=job_type, payload=payload, dedupe_key=dedupe_key, config_generation=0)
    scheduler = JobScheduler(repository, lifecycle, lease_seconds=60, acknowledgment_seconds=10)
    adapter = JobStoreSwwpAdapter(scheduler, repository, artifact_results=composition.results)
    worker_id = "routine-worker"
    assignment = scheduler.assign(owner=worker_id, capabilities=("tts.synthesis.v1",))
    assert assignment is not None
    message = adapter.payload_for_assignment(assignment)
    _ = adapter.acknowledge(message.lease)
    synthesize = LocalTtsHandler(worker_id=worker_id, input_root=inputs).execute
    result = asyncio.run(synthesize(message, HandlerContext(asyncio.Event(), message.deadline_at)))
    job_result = JobResult(
        lease=message.lease,
        result_schema_version=message.result_schema_version,
        result=result.result,
        artifact_refs=result.artifact_refs,
        completion_id="completion-test",
    )
    envelope = Envelope(
        message_type=job_result.message_type,
        message_id="message-test",
        sent_at=dt.datetime.now(dt.UTC),
        payload=job_result,
    )
    received = decode(encode(envelope)).payload
    assert isinstance(received, JobResult)
    receipt = adapter.result(received)
    assert receipt.job_id == admitted.job.job_id
    artifact = repository.artifact_receipt(receipt.job_id, assignment.attempt_id)
    assert artifact is not None and artifact.disposition == "committed"
    assert (composition.transport.paths.active / artifact.target_key).is_file()


def test_swwp_artifact_result_promotes_then_commits_and_replays(tmp_path) -> None:
    lifecycle = Lifecycle()
    lifecycle.mark_running()
    repository = JobRepository(JobDatabase(path=str(tmp_path / "jobs.sqlite3"), busy_timeout_ms=1000))
    jobs = DurableJobService(repository, lifecycle, reconciliation_batch_size=20, clock=lambda: NOW)
    jobs.initialize()
    admitted = jobs.admit(
        job_type=JobType.TTS_SYNTHESIZE,
        payload={
            "content_ref": "content_00000001",
            "voice_profile_ref": "profile_00000001",
            "output_format": "wav",
            "config_generation": 7,
        },
        dedupe_key="tts:content_00000001",
        config_generation=7,
    )
    scheduler = JobScheduler(repository, lifecycle, lease_seconds=60, acknowledgment_seconds=10, clock=lambda: NOW)
    staging_dir = tmp_path / "worker-artifacts" / "staging" / "worker_00000001"
    staging_dir.mkdir(parents=True)
    staged = staging_dir / "result.wav"
    with wave.open(str(staged), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b"\0\0" * 800)
    data = staged.read_bytes()
    reference = ArtifactReference(
        artifact_class=ArtifactClass.WAV,
        staging_namespace="worker_00000001",
        staging_token="result.wav",
        claimed_sha256=f"sha256:{hashlib.sha256(data).hexdigest()}",
        claimed_size_bytes=len(data),
        media=MediaMetadata(
            media_type="audio/wav",
            encoding="pcm_s",
            sample_width_bytes=2,
            sample_rate_hz=8000,
            channels=1,
            frame_count=800,
            duration_seconds=0.1,
        ),
    )
    injected = {"armed": True}
    activities = ActivityRegistry()
    observed_activities: list[frozenset[str]] = []
    promotion_entered = threading.Event()
    release_promotion = threading.Event()

    def fail_after_result_commit(stage: str) -> None:
        if stage == "after_replace" and injected["armed"]:
            promotion_entered.set()
            assert release_promotion.wait(timeout=1.0)
        if stage == "after_result_commit" and injected["armed"]:
            observed_activities.append(frozenset(activities.blockers()))
            injected["armed"] = False
            raise RuntimeError("lost result acknowledgment")

    orch = SimpleNamespace(
        reload_activities=activities,
        lifecycle=lifecycle,
        configuration_generation=7,
    )
    composition = build_controller_artifact_composition(
        orch,
        repository,
        work_root=tmp_path,
        maximum_bytes=8192,
    )
    assert composition.activities is activities
    artifact_service = composition.service
    artifact_service._clock = lambda: NOW
    artifact_service._failure_injector = fail_after_result_commit
    coordinator = composition.results
    adapter = JobStoreSwwpAdapter(scheduler, repository, artifact_results=coordinator)
    assignment = scheduler.assign(owner="worker_00000001", capabilities=("tts.synthesis.v1",))
    assert assignment is not None
    lease = adapter.payload_for_assignment(assignment).lease
    adapter.acknowledge(lease)
    result = ArtifactResult(
        job_id=admitted.job.job_id,
        job_type=JobType.TTS_SYNTHESIZE.value,
        lease_id=assignment.lease_id,
        attempt_id=assignment.attempt_id,
        result_schema_version=1,
        configuration_generation=7,
        content_identity="content_00000001",
        artifact=reference,
        completed_at=NOW,
    )
    message = JobResult(
        lease=LeaseRef(
            job_id=assignment.job.job_id,
            lease_id=assignment.lease_id,
            attempt_id=assignment.attempt_id,
            attempt=assignment.attempt,
        ),
        result_schema_version=1,
        result=result.model_dump(mode="json"),
        completion_id="completion_00000001",
    )
    orch.configuration_generation = 8
    with pytest.raises(ValueError, match="stale_configuration_generation"):
        adapter.result(message)
    orch.configuration_generation = 7
    failure: list[BaseException] = []

    def promote() -> None:
        try:
            adapter.result(message)
        except BaseException as exc:
            failure.append(exc)

    thread = threading.Thread(target=promote, daemon=True)
    thread.start()
    assert promotion_entered.wait(timeout=1.0)
    with pytest.raises(SafePointTimeout) as blocked:
        asyncio.run(SafePointCoordinator(activities, poll_interval_seconds=0.001).acquire(0.01))
    assert blocked.value.snapshot.blockers == (PUBLICATION, WORKER_RESULT)
    release_promotion.set()
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    assert len(failure) == 1 and isinstance(failure[0], RuntimeError)
    assert observed_activities == [frozenset({PUBLICATION, WORKER_RESULT})]
    job = repository.get(admitted.job.job_id)
    assert job is not None and job.status.value == "succeeded"
    pending = repository.artifact_receipt(admitted.job.job_id, assignment.attempt_id)
    assert pending is not None and pending.disposition == "reconciliation_required"
    active_files = tuple((tmp_path / "worker-artifacts" / "active").iterdir())
    assert len(active_files) == 1 and active_files[0].read_bytes() == data
    restarted = build_controller_artifact_composition(orch, repository, work_root=tmp_path, maximum_bytes=8192)
    assert restarted.service.reconcile() == 0
    recovered = repository.artifact_receipt(admitted.job.job_id, assignment.attempt_id)
    assert recovered is not None and recovered.disposition == "committed"
    receipt = adapter.result(message)
    assert receipt.result_hash == recovered.result_hash
    assert adapter.result(message).result_hash == receipt.result_hash

    async def new_result_waits_for_commit_gate() -> None:
        lease = await SafePointCoordinator(activities, poll_interval_seconds=0.001).acquire(0.2)
        completed = threading.Event()

        def replay_result() -> None:
            _ = adapter.result(message)
            completed.set()

        replay = threading.Thread(target=replay_result, daemon=True)
        replay.start()
        await asyncio.sleep(0.01)
        assert not completed.is_set()
        lease.release()
        replay.join(timeout=1.0)
        assert completed.is_set() and not replay.is_alive()

    asyncio.run(new_result_waits_for_commit_gate())
