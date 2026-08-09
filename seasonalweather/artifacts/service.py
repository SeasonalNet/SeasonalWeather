"""Controller-owned fenced artifact acceptance and durable result ordering."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from ..job_store.models import ArtifactPublicationReceipt
from ..jobs.contracts import JobStatus
from .fencing import ExpectedResultFence, FenceDecision, evaluate_fence
from .hashing import ContentIdentity, hash_file
from .media import validate_wav
from .models import ArtifactClass, ArtifactResult, MediaMetadata
from .promotion import PromotionService
from .staging import StagingService


class AcceptanceState(StrEnum):
    RECEIVED = "received"
    FENCE_VALIDATED = "fence_validated"
    ARTIFACT_CLAIMED = "artifact_claimed"
    CONTENT_VALIDATED = "content_validated"
    PREPARED = "prepared"
    PROMOTED = "promoted"
    DURABLY_COMMITTED = "committed"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class ArtifactJournal(Protocol):
    def record_artifact_receipt(self, **kwargs: Any) -> ArtifactPublicationReceipt: ...
    def artifact_receipt(self, job_id: str, attempt_id: str) -> ArtifactPublicationReceipt | None: ...
    def pending_artifact_receipts(self, *, limit: int = 256) -> tuple[ArtifactPublicationReceipt, ...]: ...
    def get(self, job_id: str): ...
    def result_commit_receipt(self, job_id: str): ...


@dataclass(frozen=True)
class AcceptanceReceipt:
    job_id: str
    attempt_id: str
    digest: str
    target_key: str
    state: AcceptanceState
    accepted_at: dt.datetime
    result_hash: str


class ArtifactService:
    """Makes promotion durable before, and commitment durable after, P1-07 success."""

    def __init__(
        self,
        staging: StagingService,
        promotion: PromotionService,
        journal: ArtifactJournal,
        *,
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
        admission_check: Callable[[], None] = lambda: None,
        failure_injector: Callable[[str], None] = lambda _: None,
        required_targets: tuple[str, ...] = (),
        activity_context: Callable[[], AbstractContextManager[None]] | None = None,
    ) -> None:
        self._staging, self._promotion, self._journal, self._clock = staging, promotion, journal, clock
        self._closed = False
        self._last_reason = "none"
        self._admission_check = admission_check
        self._failure_injector = failure_injector
        self._activity_context = activity_context
        if len(required_targets) > 8:
            raise ValueError("too many required artifact targets")
        self._required_targets = required_targets

    @staticmethod
    def _metadata(result: ArtifactResult, computed_media: MediaMetadata | None) -> dict[str, Any]:
        media = computed_media or result.artifact.media
        values = {
            "job_type": result.job_type,
            "lease_id": result.lease_id,
            "result_schema_version": result.result_schema_version,
            "configuration_generation": result.configuration_generation,
            "source_identity": result.source_identity,
            "event_identity": result.event_identity,
            "content_identity": result.content_identity,
            "media_type": media.media_type,
            "encoding": media.encoding,
            "sample_width_bytes": media.sample_width_bytes,
            "sample_rate_hz": media.sample_rate_hz,
            "channels": media.channels,
            "frame_count": media.frame_count,
            "duration_seconds": media.duration_seconds,
            "claimed_media_type": result.artifact.media.media_type,
            "claimed_encoding": result.artifact.media.encoding,
            "claimed_sample_width_bytes": result.artifact.media.sample_width_bytes,
            "claimed_sample_rate_hz": result.artifact.media.sample_rate_hz,
            "claimed_channels": result.artifact.media.channels,
            "claimed_frame_count": result.artifact.media.frame_count,
            "claimed_duration_seconds": result.artifact.media.duration_seconds,
            "completed_at": result.completed_at.isoformat(),
            "provenance": result.provenance,
        }
        return {key: value for key, value in values.items() if value is not None}

    def accept(
        self,
        result: ArtifactResult,
        fence: ExpectedResultFence,
        *,
        target_key: str,
        commit_result: Callable[[], Any],
    ) -> AcceptanceReceipt:
        if self._activity_context is not None:
            with self._activity_context():
                return self._accept(result, fence, target_key=target_key, commit_result=commit_result)
        return self._accept(result, fence, target_key=target_key, commit_result=commit_result)

    def _accept(
        self,
        result: ArtifactResult,
        fence: ExpectedResultFence,
        *,
        target_key: str,
        commit_result: Callable[[], Any],
    ) -> AcceptanceReceipt:
        if self._closed:
            raise RuntimeError("artifact service is closed")
        self._admission_check()
        prior = self._journal.artifact_receipt(result.job_id, result.attempt_id)
        replay = self._committed_duplicate(result, prior, target_key)
        if replay is not None:
            return replay
        self._require_current(result, fence)
        if prior is not None:
            return self._resume(result, fence, prior, target_key, commit_result)
        claim = self._staging.claim(result.artifact)
        self._failure_injector("after_claim")
        computed_media = (
            validate_wav(claim.path, result.artifact.media)
            if result.artifact.artifact_class is ArtifactClass.WAV
            else None
        )
        metadata = self._metadata(result, computed_media)
        metadata["replay_policy"] = fence.replay_policy.value
        now = self._clock()
        self._admission_check()
        if not self._is_current(result, fence):
            claim.path.unlink(missing_ok=True)
            raise ValueError("artifact result was superseded before preparation")
        prior_active = self._promotion.active_identity(target_key)
        self._record(
            result,
            claim.identity,
            target_key,
            "prepared",
            metadata,
            prepared_at=now,
            prior_digest=prior_active.sha256 if prior_active else None,
        )
        self._failure_injector("after_prepared")
        blob = self._staging.import_claimed(claim)
        self._failure_injector("after_blob")
        if hash_file(blob, maximum_bytes=self._staging.maximum_bytes) != claim.identity:
            raise RuntimeError("controller blob identity changed after insertion")
        self._admission_check()
        if not self._is_current(result, fence):
            self._record(result, claim.identity, target_key, "superseded", metadata)
            raise ValueError("artifact result was superseded before promotion")
        promotion = self._promotion.promote(
            blob,
            claim.identity,
            target_key=target_key,
            authorize=lambda: self._authorize_promotion(result, fence),
        )
        self._failure_injector("after_replace")
        self._record(
            result,
            claim.identity,
            target_key,
            "promoted",
            metadata,
            prepared_at=now,
            promoted_at=self._clock(),
            prior_digest=promotion.prior_digest,
        )
        try:
            durable = commit_result()
            self._failure_injector("after_result_commit")
        except BaseException:
            self._record(
                result,
                claim.identity,
                target_key,
                "reconciliation_required",
                metadata,
                prior_digest=promotion.prior_digest,
            )
            raise
        committed_at = durable.committed_at.astimezone(dt.UTC)
        self._record(
            result,
            claim.identity,
            target_key,
            "committed",
            metadata,
            committed_at=committed_at,
            prior_digest=promotion.prior_digest,
            result_hash=durable.result_hash,
        )
        return AcceptanceReceipt(
            result.job_id,
            result.attempt_id,
            claim.identity.sha256,
            target_key,
            AcceptanceState.DURABLY_COMMITTED,
            committed_at,
            durable.result_hash,
        )

    def _committed_duplicate(
        self,
        result: ArtifactResult,
        prior: ArtifactPublicationReceipt | None,
        target_key: str,
    ) -> AcceptanceReceipt | None:
        if prior is None or prior.disposition != AcceptanceState.DURABLY_COMMITTED.value:
            return None
        self._validate_duplicate(result, prior, target_key)
        return self._replay_receipt(prior)

    def _is_current(self, result: ArtifactResult, fence: ExpectedResultFence) -> bool:
        return evaluate_fence(result, fence, now=self._clock()) is FenceDecision.CURRENT

    def _require_current(self, result: ArtifactResult, fence: ExpectedResultFence) -> None:
        decision = evaluate_fence(result, fence, now=self._clock())
        if decision is not FenceDecision.CURRENT:
            self._last_reason = decision.value
            raise ValueError(f"artifact result rejected: {decision.value}")

    def _resume(
        self,
        result: ArtifactResult,
        fence: ExpectedResultFence,
        prior: ArtifactPublicationReceipt,
        target_key: str,
        commit_result: Callable[[], Any],
    ) -> AcceptanceReceipt:
        self._validate_duplicate(result, prior, target_key)
        blob = self._staging.blob_path(prior.artifact_digest)
        active = self._promotion.active_identity(target_key)
        if (
            not blob.is_file()
            or hash_file(blob, maximum_bytes=self._staging.maximum_bytes).sha256 != prior.artifact_digest
        ):
            raise RuntimeError("durable artifact receipt has no trustworthy blob")
        if active is None or active.sha256 != prior.artifact_digest:
            self._promotion.promote(
                blob,
                ContentIdentity(prior.artifact_digest, prior.artifact_size_bytes),
                target_key=target_key,
                authorize=lambda: self._authorize_promotion(result, fence),
            )
            self._journal.record_artifact_receipt(
                job_id=prior.job_id,
                attempt_id=prior.attempt_id,
                artifact_digest=prior.artifact_digest,
                artifact_size_bytes=prior.artifact_size_bytes,
                artifact_class=prior.artifact_class,
                target_key=prior.target_key,
                disposition="promoted",
                metadata=prior.metadata,
                committed_at=None,
                promoted_at=self._clock(),
                prior_digest=prior.prior_digest,
            )
        committed = commit_result()
        final = self._journal.record_artifact_receipt(
            job_id=prior.job_id,
            attempt_id=prior.attempt_id,
            artifact_digest=prior.artifact_digest,
            artifact_size_bytes=prior.artifact_size_bytes,
            artifact_class=prior.artifact_class,
            target_key=prior.target_key,
            disposition="committed",
            metadata=prior.metadata,
            committed_at=committed.committed_at,
            prior_digest=prior.prior_digest,
            result_hash=committed.result_hash,
        )
        return self._replay_receipt(final)

    def _authorize_promotion(self, result: ArtifactResult, fence: ExpectedResultFence) -> None:
        self._admission_check()
        self._require_current(result, fence)

    def _validate_duplicate(
        self,
        result: ArtifactResult,
        prior: ArtifactPublicationReceipt,
        target_key: str,
    ) -> None:
        expected = (
            result.artifact.claimed_sha256,
            result.artifact.claimed_size_bytes,
            result.artifact.artifact_class.value,
            target_key,
        )
        durable = (prior.artifact_digest, prior.artifact_size_bytes, prior.artifact_class, prior.target_key)
        if expected != durable:
            raise ValueError("conflicting duplicate artifact result")
        supplied = self._metadata(result, None)
        authoritative_keys = {
            "job_type",
            "lease_id",
            "result_schema_version",
            "configuration_generation",
            "source_identity",
            "event_identity",
            "content_identity",
            "completed_at",
            "provenance",
            "claimed_media_type",
            "claimed_encoding",
            "claimed_sample_width_bytes",
            "claimed_sample_rate_hz",
            "claimed_channels",
            "claimed_frame_count",
            "claimed_duration_seconds",
        }
        for key in authoritative_keys:
            if supplied.get(key) != prior.metadata.get(key):
                raise ValueError("conflicting duplicate artifact result")

    def _record(
        self,
        result: ArtifactResult,
        identity: ContentIdentity,
        target_key: str,
        disposition: str,
        metadata: dict[str, Any],
        **timestamps: Any,
    ) -> ArtifactPublicationReceipt:
        return self._journal.record_artifact_receipt(
            job_id=result.job_id,
            attempt_id=result.attempt_id,
            artifact_digest=identity.sha256,
            artifact_size_bytes=identity.size_bytes,
            artifact_class=result.artifact.artifact_class.value,
            target_key=target_key,
            disposition=disposition,
            metadata=metadata,
            committed_at=timestamps.pop("committed_at", None),
            **timestamps,
        )

    @staticmethod
    def _replay_receipt(prior: ArtifactPublicationReceipt) -> AcceptanceReceipt:
        if prior.committed_at is None:
            raise RuntimeError("committed artifact receipt lacks commit time")
        if prior.result_hash is None:
            raise RuntimeError("committed artifact receipt lacks P1-07 result hash")
        return AcceptanceReceipt(
            prior.job_id,
            prior.attempt_id,
            prior.artifact_digest,
            prior.target_key,
            AcceptanceState.DURABLY_COMMITTED,
            prior.committed_at,
            prior.result_hash,
        )

    def reconcile(self) -> int:
        unresolved = sum(
            self._reconcile_receipt(receipt) for receipt in self._journal.pending_artifact_receipts(limit=256)
        )
        self._staging.cleanup_pending(maximum_files=64)
        return unresolved

    def _reconcile_receipt(self, receipt: ArtifactPublicationReceipt) -> int:
        identity = ContentIdentity(receipt.artifact_digest, receipt.artifact_size_bytes)
        active = self._promotion.active_identity(receipt.target_key)
        active_matches = active is not None and active == identity
        job = self._journal.get(receipt.job_id)
        if receipt.disposition == "prepared":
            return self._reconcile_prepared(receipt, identity, job, active_matches)
        if receipt.disposition in {"promoted", "reconciliation_required"} and active_matches:
            return self._reconcile_published(receipt, job)
        self._last_reason = "contradictory_reconciliation_evidence"
        return 1

    def _reconcile_prepared(
        self,
        receipt: ArtifactPublicationReceipt,
        identity: ContentIdentity,
        job: Any,
        active_matches: bool,
    ) -> int:
        if active_matches:
            return self._reconcile_published(receipt, job, permit_promoted_transition=True)
        if receipt.metadata.get("replay_policy") != "idempotent_all_fences":
            self._last_reason = "revalidation_required"
            return 1
        blob = self._recover_blob(identity)
        if blob is None or job is None or job.status not in {JobStatus.LEASED, JobStatus.RUNNING}:
            self._last_reason = "contradictory_reconciliation_evidence"
            return 1
        self._promotion.promote(blob, identity, target_key=receipt.target_key)
        self._record_reconciled(receipt, disposition="promoted", promoted_at=self._clock())
        return 1

    def _recover_blob(self, identity: ContentIdentity):
        blob = self._staging.blob_path(identity.sha256)
        if blob.is_file() and hash_file(blob, maximum_bytes=self._staging.maximum_bytes) == identity:
            return blob
        recovered = self._staging.recover_claim(identity)
        if recovered is None:
            return None
        blob = self._staging.import_claimed(recovered)
        return blob if hash_file(blob, maximum_bytes=self._staging.maximum_bytes) == identity else None

    def _reconcile_published(
        self,
        receipt: ArtifactPublicationReceipt,
        job: Any,
        *,
        permit_promoted_transition: bool = False,
    ) -> int:
        durable = self._journal.result_commit_receipt(receipt.job_id)
        if job is not None and job.status is JobStatus.SUCCEEDED and durable is not None:
            self._record_reconciled(
                receipt,
                disposition="committed",
                committed_at=durable.committed_at,
                result_hash=durable.result_hash,
            )
            return 0
        if permit_promoted_transition and job is not None and job.status in {JobStatus.LEASED, JobStatus.RUNNING}:
            self._record_reconciled(receipt, disposition="promoted", promoted_at=self._clock())
        return 1

    def _record_reconciled(self, receipt: ArtifactPublicationReceipt, *, disposition: str, **values: Any) -> None:
        self._journal.record_artifact_receipt(
            job_id=receipt.job_id,
            attempt_id=receipt.attempt_id,
            artifact_digest=receipt.artifact_digest,
            artifact_size_bytes=receipt.artifact_size_bytes,
            artifact_class=receipt.artifact_class,
            target_key=receipt.target_key,
            disposition=disposition,
            metadata=receipt.metadata,
            committed_at=values.get("committed_at"),
            promoted_at=values.get("promoted_at"),
            prior_digest=receipt.prior_digest,
            result_hash=values.get("result_hash"),
        )

    def health_snapshot(self) -> dict[str, int | str]:
        required_missing = sum(not self._promotion.target_present(key) for key in self._required_targets)
        return {
            "pending_reconciliation": len(self._journal.pending_artifact_receipts(limit=256)),
            "temporary_backlog": self._staging.pending_count(),
            "storage_available": int(self._staging.storage_available() and self._promotion.storage_available()),
            "required_active_missing": required_missing,
            "state": "closed" if self._closed else "ready",
            "last_reason": self._last_reason,
        }

    def close(self) -> None:
        self._closed = True
