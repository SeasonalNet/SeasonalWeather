from __future__ import annotations

from ..validation.modeling import BaseModel
from .policies import (
    BackoffStrategy,
    CancellationMode,
    CapabilityRequirement,
    CommandRelationship,
    ConfigFence,
    DeadlinePolicy,
    DedupeMode,
    DedupePolicy,
    ExecutorClass,
    FailureCategory,
    FenceRequirements,
    FinalCommitAuthority,
    JobPriority,
    JobType,
    PolicyModel,
    QueueClass,
    ReplayPolicy,
    RetryPolicy,
    queue_executor_compatible,
)
from .schemas import (
    AlertArtifactPayloadV1,
    ArtifactResultV1,
    AudioConversionPayloadV1,
    ConfigCommitPayloadV1,
    ConfigCommitResultV1,
    ConfigValidationPayloadV1,
    ConfigValidationResultV1,
    CycleRegenerationPayloadV1,
    MaintenanceReconcilePayloadV1,
    ReconcileResultV1,
    SegmentBuildPayloadV1,
    SegmentResultV1,
    TtsSynthesisPayloadV1,
)

_TRANSIENT_RETRY = frozenset(
    {
        FailureCategory.DEPENDENCY_UNAVAILABLE,
        FailureCategory.RESOURCE_EXHAUSTED,
        FailureCategory.TRANSIENT_TRANSPORT,
    }
)


class JobTypePolicy(PolicyModel):
    job_type: JobType
    payload_schema: type[BaseModel]
    payload_schema_version: int
    result_schema: type[BaseModel]
    result_schema_version: int
    executor: ExecutorClass
    queue: QueueClass
    default_priority: JobPriority
    deadline: DeadlinePolicy
    retry: RetryPolicy
    replay: ReplayPolicy
    dedupe: DedupePolicy
    cancellation: CancellationMode
    capabilities: tuple[CapabilityRequirement, ...]
    fences: FenceRequirements
    command_relationship: CommandRelationship
    final_commit_authority: FinalCommitAuthority

    model_config = PolicyModel.model_config | {"arbitrary_types_allowed": True}


def _retry(
    *,
    attempts: int,
    timeout: int,
    categories: frozenset[FailureCategory] = _TRANSIENT_RETRY,
) -> RetryPolicy:
    return RetryPolicy(
        max_attempts=attempts,
        attempt_timeout_seconds=timeout,
        retryable_categories=categories,
        backoff_strategy=BackoffStrategy.EXPONENTIAL if attempts > 1 else BackoffStrategy.NONE,
        initial_backoff_seconds=2 if attempts > 1 else 0,
        maximum_backoff_seconds=30 if attempts > 1 else 0,
    )


def _fences(
    config: ConfigFence,
    *,
    source: bool = False,
    event: bool = False,
    content: bool = True,
) -> FenceRequirements:
    return FenceRequirements(
        config_generation=config,
        source_identity=source,
        event_identity=event,
        content_identity=content,
    )


_POLICIES = (
    JobTypePolicy(
        job_type=JobType.SEGMENT_BUILD,
        payload_schema=SegmentBuildPayloadV1,
        payload_schema_version=1,
        result_schema=SegmentResultV1,
        result_schema_version=1,
        executor=ExecutorClass.ROUTINE_WORKER,
        queue=QueueClass.ROUTINE,
        default_priority=JobPriority.NORMAL,
        deadline=DeadlinePolicy(required=False, default_seconds=300),
        retry=_retry(attempts=3, timeout=120),
        replay=ReplayPolicy.IDEMPOTENT_FENCED,
        dedupe=DedupePolicy(
            mode=DedupeMode.COALESCE_LATEST,
            scope="segment_generation",
            supersedes_older=True,
        ),
        cancellation=CancellationMode.COOPERATIVE,
        capabilities=(CapabilityRequirement(name="segment.build.v1"),),
        fences=_fences(ConfigFence.REQUIRED),
        command_relationship=CommandRelationship.REQUIRED,
        final_commit_authority=FinalCommitAuthority.CONTROLLER,
    ),
    JobTypePolicy(
        job_type=JobType.TTS_SYNTHESIZE,
        payload_schema=TtsSynthesisPayloadV1,
        payload_schema_version=1,
        result_schema=ArtifactResultV1,
        result_schema_version=1,
        executor=ExecutorClass.ROUTINE_WORKER,
        queue=QueueClass.ROUTINE,
        default_priority=JobPriority.NORMAL,
        deadline=DeadlinePolicy(required=False, default_seconds=180),
        retry=_retry(attempts=2, timeout=90),
        replay=ReplayPolicy.IDEMPOTENT_FENCED,
        dedupe=DedupePolicy(mode=DedupeMode.EXACT, scope="tts_content"),
        cancellation=CancellationMode.COOPERATIVE,
        capabilities=(CapabilityRequirement(name="tts.synthesis.v1", parameters={"format": "wav"}),),
        fences=_fences(ConfigFence.REQUIRED),
        command_relationship=CommandRelationship.REQUIRED,
        final_commit_authority=FinalCommitAuthority.CONTROLLER,
    ),
    JobTypePolicy(
        job_type=JobType.AUDIO_CONVERT,
        payload_schema=AudioConversionPayloadV1,
        payload_schema_version=1,
        result_schema=ArtifactResultV1,
        result_schema_version=1,
        executor=ExecutorClass.ROUTINE_WORKER,
        queue=QueueClass.ROUTINE,
        default_priority=JobPriority.NORMAL,
        deadline=DeadlinePolicy(required=False, default_seconds=180),
        retry=_retry(attempts=2, timeout=90),
        replay=ReplayPolicy.IDEMPOTENT_FENCED,
        dedupe=DedupePolicy(mode=DedupeMode.EXACT, scope="audio_conversion"),
        cancellation=CancellationMode.COOPERATIVE,
        capabilities=(CapabilityRequirement(name="audio.convert.wav.v1"),),
        fences=_fences(ConfigFence.REQUIRED),
        command_relationship=CommandRelationship.REQUIRED,
        final_commit_authority=FinalCommitAuthority.CONTROLLER,
    ),
    JobTypePolicy(
        job_type=JobType.CYCLE_REGENERATE,
        payload_schema=CycleRegenerationPayloadV1,
        payload_schema_version=1,
        result_schema=ArtifactResultV1,
        result_schema_version=1,
        executor=ExecutorClass.ROUTINE_WORKER,
        queue=QueueClass.ROUTINE,
        default_priority=JobPriority.HIGH,
        deadline=DeadlinePolicy(required=False, default_seconds=300),
        retry=_retry(attempts=2, timeout=180),
        replay=ReplayPolicy.REVALIDATE,
        dedupe=DedupePolicy(
            mode=DedupeMode.COALESCE_LATEST,
            scope="cycle_regeneration",
            supersedes_older=True,
        ),
        cancellation=CancellationMode.CONTROLLER_FENCED,
        capabilities=(CapabilityRequirement(name="cycle.regenerate.v1"),),
        fences=_fences(ConfigFence.REQUIRED),
        command_relationship=CommandRelationship.REQUIRED_WITH_CONTROLLER_FINALIZATION,
        final_commit_authority=FinalCommitAuthority.CONTROLLER,
    ),
    JobTypePolicy(
        job_type=JobType.MAINTENANCE_RECONCILE,
        payload_schema=MaintenanceReconcilePayloadV1,
        payload_schema_version=1,
        result_schema=ReconcileResultV1,
        result_schema_version=1,
        executor=ExecutorClass.MAINTENANCE_WORKER,
        queue=QueueClass.MAINTENANCE,
        default_priority=JobPriority.LOW,
        deadline=DeadlinePolicy(required=False, default_seconds=1800),
        retry=_retry(attempts=3, timeout=600),
        replay=ReplayPolicy.REVALIDATE,
        dedupe=DedupePolicy(mode=DedupeMode.EXACT, scope="maintenance_target"),
        cancellation=CancellationMode.COOPERATIVE,
        capabilities=(CapabilityRequirement(name="maintenance.reconcile.v1"),),
        fences=_fences(ConfigFence.OPTIONAL, content=False),
        command_relationship=CommandRelationship.INTERNAL_ONLY,
        final_commit_authority=FinalCommitAuthority.CONTROLLER,
    ),
    JobTypePolicy(
        job_type=JobType.CONFIG_VALIDATE,
        payload_schema=ConfigValidationPayloadV1,
        payload_schema_version=1,
        result_schema=ConfigValidationResultV1,
        result_schema_version=1,
        executor=ExecutorClass.CONTROLLER,
        queue=QueueClass.CONTROL,
        default_priority=JobPriority.HIGH,
        deadline=DeadlinePolicy(required=False, default_seconds=660),
        retry=_retry(attempts=1, timeout=600, categories=frozenset()),
        replay=ReplayPolicy.REVALIDATE,
        dedupe=DedupePolicy(mode=DedupeMode.EXACT, scope="config_candidate"),
        cancellation=CancellationMode.BEFORE_START,
        capabilities=(),
        fences=_fences(ConfigFence.REQUIRED),
        command_relationship=CommandRelationship.REQUIRED,
        final_commit_authority=FinalCommitAuthority.CONTROLLER,
    ),
    JobTypePolicy(
        job_type=JobType.CONFIG_COMMIT,
        payload_schema=ConfigCommitPayloadV1,
        payload_schema_version=1,
        result_schema=ConfigCommitResultV1,
        result_schema_version=1,
        executor=ExecutorClass.CONTROLLER,
        queue=QueueClass.CONTROL,
        default_priority=JobPriority.HIGH,
        deadline=DeadlinePolicy(required=True),
        retry=_retry(attempts=1, timeout=30, categories=frozenset()),
        replay=ReplayPolicy.NEVER,
        dedupe=DedupePolicy(
            mode=DedupeMode.COALESCE_LATEST,
            scope="config_commit",
            supersedes_older=True,
        ),
        cancellation=CancellationMode.CONTROLLER_FENCED,
        capabilities=(),
        fences=_fences(ConfigFence.REQUIRED),
        command_relationship=CommandRelationship.REQUIRED_WITH_CONTROLLER_FINALIZATION,
        final_commit_authority=FinalCommitAuthority.CONTROLLER,
    ),
    JobTypePolicy(
        job_type=JobType.ALERT_ARTIFACT_GENERATE,
        payload_schema=AlertArtifactPayloadV1,
        payload_schema_version=1,
        result_schema=ArtifactResultV1,
        result_schema_version=1,
        executor=ExecutorClass.ROUTINE_WORKER,
        queue=QueueClass.ROUTINE,
        default_priority=JobPriority.SAFETY_CRITICAL,
        deadline=DeadlinePolicy(required=True),
        retry=_retry(attempts=1, timeout=90, categories=frozenset()),
        replay=ReplayPolicy.REVALIDATE,
        dedupe=DedupePolicy(mode=DedupeMode.EXACT, scope="alert_source_event_content"),
        cancellation=CancellationMode.CONTROLLER_FENCED,
        capabilities=(
            CapabilityRequirement(name="tts.synthesis.v1", parameters={"format": "wav"}),
            CapabilityRequirement(name="audio.alert_artifact.v1"),
        ),
        fences=_fences(ConfigFence.REQUIRED, source=True, event=True, content=True),
        command_relationship=CommandRelationship.REQUIRED_WITH_CONTROLLER_FINALIZATION,
        final_commit_authority=FinalCommitAuthority.CONTROLLER,
    ),
)


def _validate_registry(policies: tuple[JobTypePolicy, ...]) -> dict[JobType, JobTypePolicy]:
    registry: dict[JobType, JobTypePolicy] = {}
    for policy in policies:
        if policy.job_type in registry:
            raise ValueError(f"duplicate job type: {policy.job_type.value}")
        _validate_policy(policy)
        registry[policy.job_type] = policy
    if set(registry) != set(JobType):
        raise ValueError("every initial job type must be registered exactly once")
    return registry


def _validate_policy(policy: JobTypePolicy) -> None:
    if not queue_executor_compatible(policy.queue, policy.executor):
        raise ValueError(f"incompatible queue/executor for {policy.job_type.value}")
    if policy.payload_schema_version != 1 or policy.result_schema_version != 1:
        raise ValueError(f"unsupported schema version for {policy.job_type.value}")
    if policy.job_type is JobType.ALERT_ARTIFACT_GENERATE:
        _validate_alert_policy(policy)
    if policy.final_commit_authority is not FinalCommitAuthority.CONTROLLER:
        raise ValueError("workers cannot own final result commitment")


def _validate_alert_policy(policy: JobTypePolicy) -> None:
    if not policy.deadline.required or policy.replay is not ReplayPolicy.REVALIDATE:
        raise ValueError("alert artifact work requires a strict deadline and authoritative revalidation")
    if policy.dedupe.mode is not DedupeMode.EXACT:
        raise ValueError("alert artifact work cannot coalesce across event identities")
    identity_fences = (
        policy.fences.source_identity,
        policy.fences.event_identity,
        policy.fences.content_identity,
    )
    if not all(identity_fences):
        raise ValueError("alert artifact work requires source/event/content fencing")


JOB_TYPE_POLICIES = _validate_registry(_POLICIES)


def policy_for(job_type: JobType) -> JobTypePolicy:
    return JOB_TYPE_POLICIES[job_type]
