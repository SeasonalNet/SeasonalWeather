from __future__ import annotations

import datetime as dt
import hashlib
import wave

import pytest

from seasonalweather.artifacts.integration import ArtifactResultCoordinator, CurrentArtifactAuthority
from seasonalweather.artifacts.models import ArtifactClass, ArtifactReference, ArtifactResult, MediaMetadata
from seasonalweather.artifacts.promotion import PromotionService
from seasonalweather.artifacts.service import ArtifactService
from seasonalweather.artifacts.staging import StagingService
from seasonalweather.job_store import DurableJobService, JobDatabase, JobRepository, JobScheduler
from seasonalweather.jobs.policies import JobType
from seasonalweather.lifecycle import Lifecycle
from seasonalweather.swwp.adapter import JobStoreSwwpAdapter
from seasonalweather.swwp.messages import JobResult, LeaseRef

NOW = dt.datetime(2026, 7, 26, 12, tzinfo=dt.UTC)


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
    staging_dir = tmp_path / "staging" / "worker_00000001"
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

    def fail_after_result_commit(stage: str) -> None:
        if stage == "after_result_commit" and injected["armed"]:
            injected["armed"] = False
            raise RuntimeError("lost result acknowledgment")

    artifact_service = ArtifactService(
        StagingService(tmp_path / "staging", tmp_path / "blobs", maximum_bytes=8192),
        PromotionService(tmp_path / "active", maximum_bytes=8192),
        repository,
        clock=lambda: NOW,
        failure_injector=fail_after_result_commit,
    )
    coordinator = ArtifactResultCoordinator(
        artifact_service,
        authority=lambda _: CurrentArtifactAuthority(7, None, None, "content_00000001"),
        target_policy=lambda _: "current.wav",
    )
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
    with pytest.raises(RuntimeError, match="lost result acknowledgment"):
        adapter.result(message)
    assert repository.get(admitted.job.job_id).status.value == "succeeded"
    assert (
        repository.artifact_receipt(admitted.job.job_id, assignment.attempt_id).disposition == "reconciliation_required"
    )
    assert (tmp_path / "active" / "current.wav").read_bytes() == data
    receipt = adapter.result(message)
    assert repository.artifact_receipt(admitted.job.job_id, assignment.attempt_id).disposition == "committed"
    assert adapter.result(message).result_hash == receipt.result_hash
