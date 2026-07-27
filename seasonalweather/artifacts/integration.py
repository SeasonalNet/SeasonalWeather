"""Narrow P1-07/P1-08 controller adapter for artifact-producing results."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..job_store.models import JobAssignment, ResultCommitReceipt
from ..jobs.policies import JobType
from ..jobs.registry import policy_for
from ..swwp.messages import JobResult
from .fencing import ExpectedResultFence
from .models import ArtifactClass, ArtifactResult
from .service import ArtifactService


@dataclass(frozen=True)
class CurrentArtifactAuthority:
    configuration_generation: int | None
    source_identity: str | None
    event_identity: str | None
    content_identity: str | None
    generation_compatible: bool = False
    superseded: bool = False


class ArtifactResultCoordinator:
    """Returns a P1-07 receipt only after controller validation and promotion."""

    def __init__(
        self,
        service: ArtifactService,
        *,
        authority: Callable[[JobAssignment], CurrentArtifactAuthority],
        target_policy: Callable[[JobAssignment], str],
    ) -> None:
        self.service = service
        self.authority = authority
        self.target_policy = target_policy

    def accept(
        self,
        assignment: JobAssignment,
        message: JobResult,
        commit: Callable[[dict[str, object]], ResultCommitReceipt],
    ) -> ResultCommitReceipt:
        result = ArtifactResult.model_validate(message.result)
        current = self.authority(assignment)
        policy = policy_for(assignment.job.job_type)
        fence = ExpectedResultFence(
            job_id=assignment.job.job_id,
            job_type=assignment.job.job_type.value,
            lease_id=assignment.lease_id,
            attempt_id=assignment.attempt_id,
            result_schema_version=assignment.job.result_schema_version,
            deadline_at=assignment.job.deadline_at,
            replay_policy=assignment.job.replay_policy,
            artifact_class=self._artifact_class(assignment.job.job_type),
            generation_policy=policy.fences.config_generation,
            configuration_generation=assignment.job.config_generation,
            current_configuration_generation=current.configuration_generation,
            generation_compatible=current.generation_compatible,
            source_required=policy.fences.source_identity,
            event_required=policy.fences.event_identity,
            content_required=policy.fences.content_identity,
            source_identity=current.source_identity if policy.fences.source_identity else None,
            event_identity=current.event_identity if policy.fences.event_identity else None,
            content_identity=current.content_identity if policy.fences.content_identity else None,
            superseded=current.superseded,
        )
        commit_payload = self._commit_payload(assignment.job.job_type, result)
        receipt = self.service.accept(
            result,
            fence,
            target_key=self.target_policy(assignment),
            commit_result=lambda: commit(commit_payload),
        )
        return ResultCommitReceipt(
            job_id=receipt.job_id,
            attempt=assignment.attempt,
            result_hash=receipt.result_hash,
            committed_at=receipt.accepted_at,
            idempotent_replay=False,
        )

    @staticmethod
    def supports(job_type: JobType) -> bool:
        return job_type in {
            JobType.SEGMENT_BUILD,
            JobType.TTS_SYNTHESIZE,
            JobType.AUDIO_CONVERT,
            JobType.CYCLE_REGENERATE,
            JobType.ALERT_ARTIFACT_GENERATE,
        }

    @staticmethod
    def _artifact_class(job_type: JobType) -> ArtifactClass:
        if ArtifactResultCoordinator.supports(job_type):
            return ArtifactClass.WAV
        return ArtifactClass.BLOB

    @staticmethod
    def _commit_payload(job_type: JobType, result: ArtifactResult) -> dict[str, object]:
        reference = f"artifact_{result.artifact.claimed_sha256.removeprefix('sha256:')[:24]}"
        content = result.content_identity or f"content_{result.artifact.claimed_sha256[-24:]}"
        duration = result.artifact.media.duration_seconds
        if job_type is JobType.SEGMENT_BUILD:
            if duration is None:
                raise ValueError("segment result requires controller-validated duration")
            return {"segment_ref": reference, "content_identity": content, "duration_seconds": duration}
        return {"artifact_ref": reference, "content_identity": content, "duration_seconds": duration}
