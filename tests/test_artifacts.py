from __future__ import annotations

import datetime as dt
import asyncio
import hashlib
import os
import stat
import threading
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from seasonalweather.artifacts.fencing import ExpectedResultFence, FenceDecision, evaluate_fence
from seasonalweather.artifacts.hashing import ContentIdentity
from seasonalweather.artifacts.media import validate_wav
from seasonalweather.artifacts.models import ArtifactClass, ArtifactReference, ArtifactResult, MediaMetadata
from seasonalweather.artifacts.promotion import PromotionService
from seasonalweather.artifacts.service import AcceptanceState, ArtifactService
from seasonalweather.artifacts.staging import StagingService
from seasonalweather.jobs.contracts import JobStatus
from seasonalweather.jobs.policies import ConfigFence, ReplayPolicy
from seasonalweather.configuration_reload.safe_point import (
    ActivityRegistry,
    PUBLICATION,
    SafePointCoordinator,
    SafePointTimeout,
)

NOW = dt.datetime(2026, 7, 26, 12, tzinfo=dt.UTC)


class Journal:
    def __init__(self) -> None:
        self.receipts = {}
        self.jobs = {}
        self.results = {}

    def artifact_receipt(self, job_id, attempt_id):
        return self.receipts.get((job_id, attempt_id))

    def record_artifact_receipt(self, **values):
        from seasonalweather.job_store.models import ArtifactPublicationReceipt

        key = (values["job_id"], values["attempt_id"])
        prior = self.receipts.get(key)
        receipt = ArtifactPublicationReceipt(
            job_id=values["job_id"],
            attempt_id=values["attempt_id"],
            artifact_digest=values["artifact_digest"],
            artifact_size_bytes=values["artifact_size_bytes"],
            artifact_class=values["artifact_class"],
            target_key=values["target_key"],
            prior_digest=values.get("prior_digest") or (prior.prior_digest if prior else None),
            result_hash=values.get("result_hash") or (prior.result_hash if prior else None),
            disposition=values["disposition"],
            metadata=values["metadata"],
            prepared_at=values.get("prepared_at") or (prior.prepared_at if prior else None),
            promoted_at=values.get("promoted_at") or (prior.promoted_at if prior else None),
            committed_at=values.get("committed_at") or (prior.committed_at if prior else None),
        )
        self.receipts[key] = receipt
        return receipt

    def pending_artifact_receipts(self, *, limit=256):
        return tuple(item for item in self.receipts.values() if item.disposition != "committed")[:limit]

    def get(self, job_id):
        return self.jobs.get(job_id)

    def result_commit_receipt(self, job_id):
        return self.results.get(job_id)


def _reference(path: Path, *, artifact_class: ArtifactClass = ArtifactClass.BLOB) -> ArtifactReference:
    data = path.read_bytes()
    return ArtifactReference(
        artifact_class=artifact_class,
        staging_namespace="worker_0001",
        staging_token=path.name,
        claimed_sha256=f"sha256:{hashlib.sha256(data).hexdigest()}",
        claimed_size_bytes=len(data),
        media=MediaMetadata(
            media_type="audio/wav" if artifact_class is ArtifactClass.WAV else "application/octet-stream"
        ),
    )


def _result(reference: ArtifactReference) -> ArtifactResult:
    return ArtifactResult(
        job_id="job_0123456789",
        job_type="routine.tts.synthesize",
        lease_id="lease_0123456789",
        attempt_id="attempt_0123456789",
        result_schema_version=1,
        configuration_generation=3,
        content_identity="content_0123456789",
        artifact=reference,
        completed_at=NOW,
    )


def _fence(result: ArtifactResult) -> ExpectedResultFence:
    return ExpectedResultFence(
        job_id=result.job_id,
        job_type=result.job_type,
        lease_id=result.lease_id,
        attempt_id=result.attempt_id,
        result_schema_version=1,
        deadline_at=NOW + dt.timedelta(minutes=1),
        replay_policy=ReplayPolicy.IDEMPOTENT_FENCED,
        artifact_class=result.artifact.artifact_class,
        generation_policy=ConfigFence.REQUIRED,
        configuration_generation=3,
        content_identity=result.content_identity,
    )


def test_reference_rejects_paths_unknown_fields_and_noncanonical_digest(tmp_path: Path) -> None:
    path = tmp_path / "result.wav"
    path.write_bytes(b"x")
    values = _reference(path).model_dump()
    for token in ("../result.wav", "/result.wav", "result\\wav", "a//b"):
        with pytest.raises(ValidationError):
            ArtifactReference(**(values | {"staging_token": token}))
    with pytest.raises(ValidationError):
        ArtifactReference(**(values | {"claimed_sha256": "sha256:ABC"}))
    with pytest.raises(ValidationError):
        ArtifactReference(**(values | {"unexpected": "x"}))


def test_fence_rejects_stale_lease_deadline_and_identity(tmp_path: Path) -> None:
    path = tmp_path / "result.wav"
    path.write_bytes(b"artifact")
    result = _result(_reference(path))
    fence = _fence(result)
    assert evaluate_fence(result, fence, now=NOW) is FenceDecision.CURRENT
    assert (
        evaluate_fence(result.model_copy(update={"lease_id": "lease_stale_123"}), fence, now=NOW)
        is FenceDecision.STALE_LEASE
    )
    assert evaluate_fence(result, fence, now=fence.deadline_at) is FenceDecision.EXPIRED_DEADLINE
    assert (
        evaluate_fence(result.model_copy(update={"content_identity": "content_other_123"}), fence, now=NOW)
        is FenceDecision.CONTENT_IDENTITY_MISMATCH
    )


def test_fence_covers_generation_schema_attempt_source_event_and_supersession(tmp_path: Path) -> None:
    path = tmp_path / "result.bin"
    path.write_bytes(b"artifact")
    result = _result(_reference(path)).model_copy(
        update={"source_identity": "source_0001", "event_identity": "event_0001"}
    )

    def fence(**updates):
        values = {
            "job_id": result.job_id,
            "job_type": result.job_type,
            "lease_id": result.lease_id,
            "attempt_id": result.attempt_id,
            "result_schema_version": 1,
            "deadline_at": NOW + dt.timedelta(seconds=1),
            "replay_policy": ReplayPolicy.REVALIDATE,
            "artifact_class": ArtifactClass.BLOB,
            "generation_policy": ConfigFence.REQUIRED,
            "configuration_generation": 3,
            "current_configuration_generation": 3,
            "source_required": True,
            "event_required": True,
            "content_required": True,
            "source_identity": "source_0001",
            "event_identity": "event_0001",
            "content_identity": result.content_identity,
        }
        return ExpectedResultFence(**(values | updates))

    cases = (
        (result.model_copy(update={"attempt_id": "attempt_stale_0001"}), fence(), FenceDecision.STALE_ATTEMPT),
        (
            result.model_copy(update={"result_schema_version": 2}),
            fence(),
            FenceDecision.RESULT_SCHEMA_MISMATCH,
        ),
        (
            result.model_copy(update={"source_identity": "source_0002"}),
            fence(),
            FenceDecision.STALE_SOURCE_IDENTITY,
        ),
        (
            result.model_copy(update={"event_identity": "event_0002"}),
            fence(),
            FenceDecision.STALE_EVENT_OR_PRODUCT_IDENTITY,
        ),
        (result, fence(current_configuration_generation=4), FenceDecision.STALE_CONFIGURATION_GENERATION),
        (
            result,
            fence(current_configuration_generation=None),
            FenceDecision.REVALIDATION_REQUIRED,
        ),
        (result, fence(source_identity=None), FenceDecision.REVALIDATION_REQUIRED),
        (result, fence(superseded=True), FenceDecision.SUPERSEDED),
    )
    for candidate, expected, decision in cases:
        assert evaluate_fence(candidate, expected, now=NOW) is decision
    assert (
        evaluate_fence(result, fence(current_configuration_generation=4, generation_compatible=True), now=NOW)
        is FenceDecision.CURRENT
    )
    optional = fence(
        generation_policy=ConfigFence.OPTIONAL,
        configuration_generation=None,
        current_configuration_generation=None,
        source_required=False,
        event_required=False,
        content_required=False,
    )
    assert (
        evaluate_fence(result.model_copy(update={"configuration_generation": None}), optional, now=NOW)
        is FenceDecision.CURRENT
    )
    not_applicable = fence(
        generation_policy=ConfigFence.NOT_APPLICABLE,
        configuration_generation=None,
        current_configuration_generation=None,
        source_required=False,
        event_required=False,
        content_required=False,
    )
    assert (
        evaluate_fence(result.model_copy(update={"configuration_generation": None}), not_applicable, now=NOW)
        is FenceDecision.CURRENT
    )
    assert evaluate_fence(result, not_applicable, now=NOW) is FenceDecision.POLICY_VIOLATION
    with pytest.raises(AttributeError, match="immutable"):
        fence().superseded = True


def test_controller_claims_hashes_and_promotes_blob(tmp_path: Path) -> None:
    staging = tmp_path / "staging" / "worker_0001"
    staging.mkdir(parents=True)
    source = staging / "result.bin"
    source.write_bytes(b"artifact bytes")
    result = _result(_reference(source))
    service = ArtifactService(
        StagingService(tmp_path / "staging", tmp_path / "blobs", maximum_bytes=1024),
        PromotionService(tmp_path / "active", maximum_bytes=1024),
        Journal(),
        clock=lambda: NOW,
    )

    def commit():
        return SimpleNamespace(committed_at=NOW, result_hash="sha256:" + "b" * 64)

    receipt = service.accept(result, _fence(result), target_key="current.bin", commit_result=commit)
    assert receipt.state is AcceptanceState.DURABLY_COMMITTED
    assert (tmp_path / "active" / "current.bin").read_bytes() == b"artifact bytes"
    assert (
        service.accept(result, _fence(result), target_key="current.bin", commit_result=commit).digest == receipt.digest
    )


def test_artifact_promotion_participates_in_reload_gate_and_new_work_waits(tmp_path: Path) -> None:
    registry = ActivityRegistry()
    coordinator = SafePointCoordinator(registry, poll_interval_seconds=0.001)
    entered_commit = threading.Event()
    release_commit = threading.Event()
    namespace = tmp_path / "staging" / "worker_0001"
    namespace.mkdir(parents=True)
    source = namespace / "result.bin"
    source.write_bytes(b"artifact race")
    result = _result(_reference(source))
    service = ArtifactService(
        StagingService(tmp_path / "staging", tmp_path / "blobs", maximum_bytes=1024),
        PromotionService(tmp_path / "active", maximum_bytes=1024),
        Journal(),
        clock=lambda: NOW,
        activity_context=lambda: registry.activity(PUBLICATION),
    )

    def commit():
        entered_commit.set()
        assert release_commit.wait(timeout=1.0)
        return SimpleNamespace(committed_at=NOW, result_hash="sha256:" + "f" * 64)

    promotion = threading.Thread(
        target=lambda: service.accept(result, _fence(result), target_key="current.bin", commit_result=commit),
        daemon=True,
    )
    promotion.start()
    assert entered_commit.wait(timeout=1.0)
    with pytest.raises(SafePointTimeout) as raised:
        asyncio.run(coordinator.acquire(0.01))
    assert raised.value.snapshot.blockers == (PUBLICATION,)
    release_commit.set()
    promotion.join(timeout=1.0)
    assert not promotion.is_alive()

    second_source = namespace / "second.bin"
    second_source.write_bytes(b"second artifact")
    second = _result(_reference(second_source)).model_copy(
        update={"job_id": "job_1123456789", "lease_id": "lease_1123456789", "attempt_id": "attempt_1123456789"}
    )
    second_done = threading.Event()

    async def held_gate_scenario() -> None:
        lease = await coordinator.acquire(0.2)
        thread = threading.Thread(
            target=lambda: (
                service.accept(
                    second,
                    _fence(second),
                    target_key="second.bin",
                    commit_result=lambda: SimpleNamespace(
                        committed_at=NOW,
                        result_hash="sha256:" + "e" * 64,
                    ),
                ),
                second_done.set(),
            ),
            daemon=True,
        )
        thread.start()
        await asyncio.sleep(0.01)
        assert not second_done.is_set()
        lease.release()
        thread.join(timeout=1.0)
        assert second_done.is_set() and not thread.is_alive()

    asyncio.run(held_gate_scenario())


def test_old_generation_artifact_result_stays_fenced_after_reload_commit(tmp_path: Path) -> None:
    namespace = tmp_path / "staging" / "worker_0001"
    namespace.mkdir(parents=True)
    source = namespace / "result.bin"
    source.write_bytes(b"old generation")
    result = _result(_reference(source))
    stale_fence = ExpectedResultFence(
        job_id=result.job_id,
        job_type=result.job_type,
        lease_id=result.lease_id,
        attempt_id=result.attempt_id,
        result_schema_version=1,
        deadline_at=NOW + dt.timedelta(minutes=1),
        replay_policy=ReplayPolicy.IDEMPOTENT_FENCED,
        artifact_class=result.artifact.artifact_class,
        generation_policy=ConfigFence.REQUIRED,
        configuration_generation=3,
        current_configuration_generation=4,
        content_identity=result.content_identity,
    )
    service = ArtifactService(
        StagingService(tmp_path / "staging", tmp_path / "blobs", maximum_bytes=1024),
        PromotionService(tmp_path / "active", maximum_bytes=1024),
        Journal(),
        clock=lambda: NOW,
    )

    assert evaluate_fence(result, stale_fence, now=NOW) is FenceDecision.STALE_CONFIGURATION_GENERATION
    with pytest.raises(ValueError, match="stale_configuration_generation"):
        service.accept(
            result,
            stale_fence,
            target_key="current.bin",
            commit_result=lambda: SimpleNamespace(committed_at=NOW, result_hash="sha256:" + "d" * 64),
        )
    assert source.exists()


def test_wav_claimed_metadata_is_controller_validated(tmp_path: Path) -> None:
    staging = tmp_path / "staging" / "worker_0001"
    staging.mkdir(parents=True)
    source = staging / "result.wav"
    with wave.open(str(source), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b"\0\0" * 800)
    reference = _reference(source, artifact_class=ArtifactClass.WAV)
    result = _result(reference)
    service = ArtifactService(
        StagingService(tmp_path / "staging", tmp_path / "blobs", maximum_bytes=8192),
        PromotionService(tmp_path / "active", maximum_bytes=8192),
        Journal(),
        clock=lambda: NOW,
    )

    def commit():
        return SimpleNamespace(committed_at=NOW, result_hash="sha256:" + "c" * 64)

    assert (
        service.accept(result, _fence(result), target_key="current.wav", commit_result=commit).state
        is AcceptanceState.DURABLY_COMMITTED
    )


def test_post_replace_crash_retries_without_worker_bytes(tmp_path: Path) -> None:
    staging = tmp_path / "staging" / "worker_0001"
    staging.mkdir(parents=True)
    source = staging / "result.bin"
    source.write_bytes(b"crash recoverable bytes")
    result = _result(_reference(source))
    journal = Journal()
    injected = {"armed": True}

    def fail(stage: str) -> None:
        if stage == "after_replace" and injected["armed"]:
            injected["armed"] = False
            raise RuntimeError("injected crash")

    service = ArtifactService(
        StagingService(tmp_path / "staging", tmp_path / "blobs", maximum_bytes=1024),
        PromotionService(tmp_path / "active", maximum_bytes=1024),
        journal,
        clock=lambda: NOW,
        failure_injector=fail,
    )

    def commit():
        return SimpleNamespace(committed_at=NOW, result_hash="sha256:" + "d" * 64)

    with pytest.raises(RuntimeError, match="injected crash"):
        service.accept(result, _fence(result), target_key="current.bin", commit_result=commit)
    assert not source.exists()
    receipt = service.accept(result, _fence(result), target_key="current.bin", commit_result=commit)
    assert receipt.state is AcceptanceState.DURABLY_COMMITTED


def test_wav_rejects_truncated_data_and_every_claimed_media_field(tmp_path: Path) -> None:
    path = tmp_path / "media.wav"
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b"\0\0" * 80)
    valid = MediaMetadata(
        media_type="audio/wav",
        encoding="pcm_s",
        sample_width_bytes=2,
        sample_rate_hz=8000,
        channels=1,
        frame_count=80,
        duration_seconds=0.01,
    )
    assert validate_wav(path, valid).duration_seconds == 0.01
    for field, value in (
        ("encoding", "pcm_u"),
        ("sample_width_bytes", 1),
        ("sample_rate_hz", 16000),
        ("channels", 2),
        ("frame_count", 81),
        ("duration_seconds", 0.02),
    ):
        with pytest.raises(ValueError, match="claimed WAV metadata"):
            validate_wav(path, valid.model_copy(update={field: value}))
    path.write_bytes(path.read_bytes()[:-20])
    with pytest.raises(ValueError, match="truncated|invalid"):
        validate_wav(path, valid)


def test_staging_rejects_symlinks_fifo_hardlinks_and_oversize(tmp_path: Path) -> None:
    root = tmp_path / "staging"
    namespace = root / "worker_0001"
    namespace.mkdir(parents=True)
    service = StagingService(root, tmp_path / "blobs", maximum_bytes=8)

    outside = tmp_path / "outside"
    outside.write_bytes(b"safe")
    link = namespace / "link.bin"
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="opened safely"):
        service.claim(_reference(link))

    nested = tmp_path / "nested"
    nested.mkdir()
    nested_file = nested / "result.bin"
    nested_file.write_bytes(b"safe")
    (namespace / "linked-parent").symlink_to(nested, target_is_directory=True)
    linked_parent_reference = _reference(nested_file).model_copy(update={"staging_token": "linked-parent/result.bin"})
    with pytest.raises(ValueError, match="unsafe component"):
        service.claim(linked_parent_reference)

    hard = namespace / "hard.bin"
    os.link(outside, hard)
    with pytest.raises(ValueError, match="regular file"):
        service.claim(_reference(hard))

    fifo = namespace / "fifo"
    os.mkfifo(fifo)
    fifo_reference = ArtifactReference(
        artifact_class=ArtifactClass.BLOB,
        staging_namespace="worker_0001",
        staging_token="fifo",
        claimed_sha256="sha256:" + "0" * 64,
        claimed_size_bytes=1,
        media=MediaMetadata(media_type="application/octet-stream"),
    )
    with pytest.raises(ValueError, match="regular file"):
        service.claim(fifo_reference)

    large = namespace / "large.bin"
    large.write_bytes(b"x" * 9)
    with pytest.raises(ValueError, match="unsafe file metadata"):
        service.claim(_reference(large))


def test_claim_detects_mutation_through_a_preexisting_writer(tmp_path: Path, monkeypatch) -> None:
    namespace = tmp_path / "staging" / "worker_0001"
    namespace.mkdir(parents=True)
    source = namespace / "result.bin"
    source.write_bytes(b"stable")
    reference = _reference(source)
    writer = os.open(source, os.O_RDWR)
    service = StagingService(tmp_path / "staging", tmp_path / "blobs", maximum_bytes=1024)
    original = service._copy_descriptor

    def mutate(descriptor):
        claimed = original(descriptor)
        os.pwrite(writer, b"!", 6)
        os.fsync(writer)
        return claimed

    monkeypatch.setattr(service, "_copy_descriptor", mutate)
    try:
        with pytest.raises(ValueError, match="changed while being copied"):
            service.claim(reference)
    finally:
        os.close(writer)


def test_content_addressing_deduplicates_rejects_corruption_and_cleans_bounded(
    tmp_path: Path,
) -> None:
    namespace = tmp_path / "staging" / "worker_0001"
    namespace.mkdir(parents=True)
    service = StagingService(tmp_path / "staging", tmp_path / "blobs", maximum_bytes=1024)
    first = namespace / "first.bin"
    first.write_bytes(b"same")
    reference = _reference(first)
    blob = service.import_claimed(service.claim(reference))
    assert stat.S_IMODE(blob.stat().st_mode) == 0o440

    second = namespace / "second.bin"
    second.write_bytes(b"same")
    duplicate = reference.model_copy(update={"staging_token": "second.bin"})
    assert service.import_claimed(service.claim(duplicate)) == blob

    blob.chmod(0o640)
    blob.write_bytes(b"evil")
    third = namespace / "third.bin"
    third.write_bytes(b"same")
    conflicting = reference.model_copy(update={"staging_token": "third.bin"})
    with pytest.raises(RuntimeError, match="collision|corruption"):
        service.import_claimed(service.claim(conflicting))

    service.pending_root.mkdir(exist_ok=True)
    for index in range(4):
        (service.pending_root / f".claim-{index}").write_bytes(b"x")
    assert service.cleanup_pending(maximum_files=2) == 2
    assert service.pending_count() == 2


def test_missing_pending_directory_is_an_empty_bounded_state(tmp_path: Path) -> None:
    service = StagingService(tmp_path / "staging", tmp_path / "blobs", maximum_bytes=1024)
    identity = ContentIdentity("sha256:" + hashlib.sha256(b"missing").hexdigest(), 7)

    assert service.pending_count() == 0
    assert service.cleanup_pending() == 0
    assert service.recover_claim(identity) is None


def test_promotion_confines_targets_rejects_links_and_serializes(tmp_path: Path) -> None:
    blob = tmp_path / "blob"
    blob.write_bytes(b"new")
    identity = ContentIdentity("sha256:" + hashlib.sha256(b"new").hexdigest(), 3)
    service = PromotionService(tmp_path / "active", maximum_bytes=1024)
    for key in ("../escape", "sub/file", "bad\\file"):
        with pytest.raises(ValueError, match="target key"):
            service.promote(blob, identity, target_key=key)
    service.active_root.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"old")
    (service.active_root / "current").symlink_to(outside)
    with pytest.raises(ValueError, match="non-symlink"):
        service.promote(blob, identity, target_key="current")
    assert outside.read_bytes() == b"old"

    (service.active_root / "current").unlink()
    errors = []
    threads = [
        threading.Thread(
            target=lambda: service.promote(blob, identity, target_key="current"),
        )
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert (service.active_root / "current").read_bytes() == b"new"


@pytest.mark.parametrize("stage", ["after_prepared", "after_blob", "after_replace"])
def test_reconcile_converges_durable_crash_stages(tmp_path: Path, stage: str) -> None:
    namespace = tmp_path / "staging" / "worker_0001"
    namespace.mkdir(parents=True)
    source = namespace / "result.bin"
    source.write_bytes(b"recover")
    result = _result(_reference(source))
    journal = Journal()
    journal.jobs[result.job_id] = SimpleNamespace(status=JobStatus.RUNNING)
    armed = True

    def fail(point: str) -> None:
        nonlocal armed
        if armed and point == stage:
            armed = False
            raise RuntimeError("crash")

    service = ArtifactService(
        StagingService(tmp_path / "staging", tmp_path / "blobs", maximum_bytes=1024),
        PromotionService(tmp_path / "active", maximum_bytes=1024),
        journal,
        clock=lambda: NOW,
        failure_injector=fail,
    )
    with pytest.raises(RuntimeError, match="crash"):
        service.accept(
            result,
            _fence(result),
            target_key="current.bin",
            commit_result=lambda: SimpleNamespace(committed_at=NOW, result_hash="sha256:" + "e" * 64),
        )
    assert service.reconcile() == 1
    assert journal.artifact_receipt(result.job_id, result.attempt_id).disposition == "promoted"
    assert (tmp_path / "active" / "current.bin").read_bytes() == b"recover"


def test_drain_and_close_block_new_acceptance_and_health_is_bounded(tmp_path: Path) -> None:
    namespace = tmp_path / "staging" / "worker_0001"
    namespace.mkdir(parents=True)
    source = namespace / "result.bin"
    source.write_bytes(b"artifact")
    result = _result(_reference(source))
    accepting = False

    def admission() -> None:
        if not accepting:
            raise RuntimeError("draining")

    service = ArtifactService(
        StagingService(tmp_path / "staging", tmp_path / "blobs", maximum_bytes=1024),
        PromotionService(tmp_path / "active", maximum_bytes=1024),
        Journal(),
        clock=lambda: NOW,
        admission_check=admission,
    )
    with pytest.raises(RuntimeError, match="draining"):
        service.accept(result, _fence(result), target_key="current", commit_result=lambda: None)
    assert source.exists()
    assert service.health_snapshot()["temporary_backlog"] == 0
    service.close()
    service.close()
    with pytest.raises(RuntimeError, match="closed"):
        service.accept(result, _fence(result), target_key="current", commit_result=lambda: None)


def test_reconciliation_does_not_blindly_promote_revalidation_work(tmp_path: Path) -> None:
    namespace = tmp_path / "staging" / "worker_0001"
    namespace.mkdir(parents=True)
    source = namespace / "result.bin"
    source.write_bytes(b"safety critical")
    result = _result(_reference(source))
    fence = ExpectedResultFence(
        job_id=result.job_id,
        job_type=result.job_type,
        lease_id=result.lease_id,
        attempt_id=result.attempt_id,
        result_schema_version=1,
        deadline_at=NOW + dt.timedelta(minutes=1),
        replay_policy=ReplayPolicy.REVALIDATE,
        artifact_class=ArtifactClass.BLOB,
        generation_policy=ConfigFence.REQUIRED,
        configuration_generation=3,
        content_identity=result.content_identity,
    )
    journal = Journal()
    journal.jobs[result.job_id] = SimpleNamespace(status=JobStatus.RUNNING)

    def fail_after_prepare(stage: str) -> None:
        if stage == "after_prepared":
            raise RuntimeError("crash")

    service = ArtifactService(
        StagingService(tmp_path / "staging", tmp_path / "blobs", maximum_bytes=1024),
        PromotionService(tmp_path / "active", maximum_bytes=1024),
        journal,
        clock=lambda: NOW,
        failure_injector=fail_after_prepare,
    )
    with pytest.raises(RuntimeError, match="crash"):
        service.accept(result, fence, target_key="current", commit_result=lambda: None)
    assert service.reconcile() == 1
    assert journal.artifact_receipt(result.job_id, result.attempt_id).disposition == "prepared"
    assert not (tmp_path / "active" / "current").exists()
    assert service.health_snapshot()["last_reason"] == "revalidation_required"
