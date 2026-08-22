"""Controller production composition for artifact publication and result admission."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from seasonalweather.configuration_reload.safe_point import PUBLICATION, WORKER_RESULT, ActivityRegistry
from seasonalweather.lifecycle import WorkClass

from .integration import ArtifactResultCoordinator, CurrentArtifactAuthority
from .promotion import PromotionService
from .service import ArtifactService
from .staging import StagingService
from .transport import ArtifactTransport, SharedVolumeArtifactTransport


@dataclass(frozen=True)
class ControllerArtifactComposition:
    service: ArtifactService
    results: ArtifactResultCoordinator
    activities: ActivityRegistry
    transport: ArtifactTransport


def build_controller_artifact_composition(
    orchestrator: Any,
    repository: Any,
    *,
    work_root: Path,
    maximum_bytes: int,
) -> ControllerArtifactComposition:
    """Compose P1-10 production owners without creating a worker process."""

    activities = orchestrator.reload_activities
    transport = SharedVolumeArtifactTransport(Path(work_root))
    paths = transport.paths
    service = ArtifactService(
        StagingService(paths.staging, paths.blobs, maximum_bytes=maximum_bytes),
        PromotionService(paths.active, maximum_bytes=maximum_bytes),
        repository,
        admission_check=lambda: orchestrator.lifecycle.require(WorkClass.PUBLICATION),
        activity_context=lambda: activities.activity(PUBLICATION),
    )

    def authority(assignment: Any) -> CurrentArtifactAuthority:
        payload = assignment.job.payload
        return CurrentArtifactAuthority(
            configuration_generation=orchestrator.configuration_generation,
            source_identity=_identity(payload.get("source_identity")),
            event_identity=_identity(payload.get("event_identity")),
            content_identity=_identity(payload.get("content_identity") or payload.get("content_ref")),
        )

    def target_policy(assignment: Any) -> str:
        identity = "|".join(
            (
                assignment.job.job_type.value,
                _identity(assignment.job.payload.get("content_identity")) or "none",
                _identity(assignment.job.payload.get("content_ref")) or "none",
                _identity(assignment.job.payload.get("segment_key")) or "none",
            )
        )
        return f"artifact-{hashlib.sha256(identity.encode()).hexdigest()[:32]}.wav"

    results = ArtifactResultCoordinator(
        service,
        authority=authority,
        target_policy=target_policy,
        activity_context=lambda: activities.activity(WORKER_RESULT),
    )
    return ControllerArtifactComposition(service=service, results=results, activities=activities, transport=transport)


def _identity(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    bounded = value.strip()
    return bounded if 3 <= len(bounded) <= 128 else None
