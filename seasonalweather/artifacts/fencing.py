"""Pure controller-side result-fence comparisons."""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

from ..jobs.policies import ConfigFence, ReplayPolicy
from .models import ArtifactClass, ArtifactResult

_UNSET = object()


class FenceDecision(StrEnum):
    CURRENT = "current"
    DUPLICATE = "duplicate"
    SUPERSEDED = "superseded"
    STALE_ATTEMPT = "stale_attempt"
    STALE_LEASE = "stale_lease"
    STALE_CONFIGURATION_GENERATION = "stale_configuration_generation"
    INCOMPATIBLE_CONFIGURATION_GENERATION = "incompatible_configuration_generation"
    STALE_SOURCE_IDENTITY = "stale_source_identity"
    STALE_EVENT_OR_PRODUCT_IDENTITY = "stale_event_or_product_identity"
    CONTENT_IDENTITY_MISMATCH = "content_identity_mismatch"
    EXPIRED_DEADLINE = "expired_deadline"
    RESULT_SCHEMA_MISMATCH = "result_schema_mismatch"
    ARTIFACT_CLASS_MISMATCH = "artifact_class_mismatch"
    POLICY_VIOLATION = "policy_violation"
    REVALIDATION_REQUIRED = "revalidation_required"


class GenerationDisposition(StrEnum):
    CURRENT = "current"
    COMPATIBLE = "compatible"
    STALE = "stale"
    UNKNOWN = "unknown"


def generation_disposition(
    admitted: int | None,
    current: int | None,
    *,
    compatible: bool = False,
) -> GenerationDisposition:
    if admitted is None or current is None:
        return GenerationDisposition.UNKNOWN
    if admitted == current:
        return GenerationDisposition.CURRENT
    if compatible:
        return GenerationDisposition.COMPATIBLE
    return GenerationDisposition.STALE


class ExpectedResultFence:
    __slots__ = (
        "_frozen",
        "artifact_class",
        "attempt_id",
        "configuration_generation",
        "content_identity",
        "content_required",
        "current_configuration_generation",
        "deadline_at",
        "event_identity",
        "event_required",
        "generation_disposition",
        "generation_policy",
        "job_id",
        "job_type",
        "lease_id",
        "replay_policy",
        "result_schema_version",
        "source_identity",
        "source_required",
        "superseded",
    )

    def __init__(
        self,
        *,
        job_id: str,
        job_type: str,
        lease_id: str,
        attempt_id: str,
        result_schema_version: int,
        deadline_at: dt.datetime,
        replay_policy: ReplayPolicy,
        artifact_class: ArtifactClass,
        generation_policy: ConfigFence,
        configuration_generation: int | None,
        current_configuration_generation: int | None | object = _UNSET,
        generation_compatible: bool = False,
        source_required: bool = False,
        event_required: bool = False,
        content_required: bool = False,
        source_identity: str | None = None,
        event_identity: str | None = None,
        content_identity: str | None = None,
        superseded: bool = False,
    ) -> None:
        self.job_id, self.job_type, self.lease_id, self.attempt_id = job_id, job_type, lease_id, attempt_id
        self.result_schema_version, self.deadline_at = result_schema_version, deadline_at.astimezone(dt.UTC)
        self.replay_policy, self.artifact_class = replay_policy, artifact_class
        self.generation_policy, self.configuration_generation = generation_policy, configuration_generation
        if current_configuration_generation is _UNSET:
            self.current_configuration_generation: int | None = configuration_generation
        elif current_configuration_generation is None or isinstance(current_configuration_generation, int):
            self.current_configuration_generation = current_configuration_generation
        else:
            raise TypeError("current configuration generation is invalid")
        self.generation_disposition = generation_disposition(
            configuration_generation,
            self.current_configuration_generation,
            compatible=generation_compatible,
        )
        self.source_required, self.event_required, self.content_required = (
            source_required,
            event_required,
            content_required,
        )
        self.source_identity, self.event_identity, self.content_identity = (
            source_identity,
            event_identity,
            content_identity,
        )
        self.superseded = superseded
        self._frozen = True

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_frozen", False):
            raise AttributeError("expected result fence is immutable")
        object.__setattr__(self, name, value)


def evaluate_fence(result: ArtifactResult, fence: ExpectedResultFence, *, now: dt.datetime) -> FenceDecision:
    now = now.astimezone(dt.UTC)
    if result.job_id != fence.job_id or result.job_type != fence.job_type:
        return FenceDecision.POLICY_VIOLATION
    if result.lease_id != fence.lease_id:
        return FenceDecision.STALE_LEASE
    if result.attempt_id != fence.attempt_id:
        return FenceDecision.STALE_ATTEMPT
    if result.result_schema_version != fence.result_schema_version:
        return FenceDecision.RESULT_SCHEMA_MISMATCH
    if now >= fence.deadline_at:
        return FenceDecision.EXPIRED_DEADLINE
    if fence.superseded:
        return FenceDecision.SUPERSEDED
    if result.artifact.artifact_class is not fence.artifact_class:
        return FenceDecision.ARTIFACT_CLASS_MISMATCH
    return _generation_and_identity_decision(result, fence)


def _generation_and_identity_decision(result: ArtifactResult, fence: ExpectedResultFence) -> FenceDecision:
    generation = _generation_decision(result, fence)
    return generation if generation is not FenceDecision.CURRENT else _identity_decision(result, fence)


def _generation_decision(result: ArtifactResult, fence: ExpectedResultFence) -> FenceDecision:
    contract = _generation_contract_decision(result, fence)
    return contract if contract is not FenceDecision.CURRENT else _generation_currency_decision(result, fence)


def _generation_contract_decision(result: ArtifactResult, fence: ExpectedResultFence) -> FenceDecision:
    if fence.generation_policy is ConfigFence.REQUIRED and result.configuration_generation is None:
        return FenceDecision.POLICY_VIOLATION
    if fence.generation_policy is ConfigFence.NOT_APPLICABLE and result.configuration_generation is not None:
        return FenceDecision.POLICY_VIOLATION
    if (
        fence.generation_policy is not ConfigFence.NOT_APPLICABLE
        and result.configuration_generation is not None
        and fence.configuration_generation != result.configuration_generation
    ):
        return FenceDecision.INCOMPATIBLE_CONFIGURATION_GENERATION
    return FenceDecision.CURRENT


def _generation_currency_decision(result: ArtifactResult, fence: ExpectedResultFence) -> FenceDecision:
    if fence.generation_policy is ConfigFence.NOT_APPLICABLE or (
        fence.generation_policy is ConfigFence.OPTIONAL and result.configuration_generation is None
    ):
        pass
    elif fence.generation_disposition is GenerationDisposition.UNKNOWN:
        return FenceDecision.REVALIDATION_REQUIRED
    elif fence.generation_disposition is GenerationDisposition.STALE:
        return FenceDecision.STALE_CONFIGURATION_GENERATION
    return FenceDecision.CURRENT


def _identity_decision(result: ArtifactResult, fence: ExpectedResultFence) -> FenceDecision:
    comparisons = (
        (fence.source_required, fence.source_identity, result.source_identity, FenceDecision.STALE_SOURCE_IDENTITY),
        (
            fence.event_required,
            fence.event_identity,
            result.event_identity,
            FenceDecision.STALE_EVENT_OR_PRODUCT_IDENTITY,
        ),
        (
            fence.content_required,
            fence.content_identity,
            result.content_identity,
            FenceDecision.CONTENT_IDENTITY_MISMATCH,
        ),
    )
    for required, expected, actual, decision in comparisons:
        if required and expected is None:
            return FenceDecision.REVALIDATION_REQUIRED
        if expected is not None and actual != expected:
            return decision
    return FenceDecision.CURRENT
