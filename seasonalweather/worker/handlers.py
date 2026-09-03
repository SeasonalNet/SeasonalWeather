"""Worker handler contracts and profile-owned dispatch.

Handlers receive typed SWWP assignments and return bounded result metadata.
They never receive controller repositories or publication authorities.  The
default reference handler fails closed until a deployment supplies the
profile's input/artifact resolver, rather than fabricating a successful result
from an opaque controller reference.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol

from ..artifacts.hashing import hash_file
from ..artifacts.media import WavPolicy, inspect_wav
from ..artifacts.models import ArtifactClass, ArtifactReference, ArtifactResult
from ..jobs.contracts import AttemptOutcome
from ..jobs.policies import FailureCategory, JobType
from ..swwp.messages import JobAssignmentPayload
from .profiles import profile_spec


class WorkerHandlerError(RuntimeError):
    def __init__(
        self,
        code: str,
        summary: str,
        *,
        category: FailureCategory = FailureCategory.UNSUPPORTED,
        outcome: AttemptOutcome = AttemptOutcome.PERMANENT_FAILURE,
    ) -> None:
        self.code = code
        self.summary = summary[:256]
        self.category = category
        self.outcome = outcome
        super().__init__(self.summary)


@dataclass(frozen=True)
class HandlerContext:
    cancellation: asyncio.Event
    deadline_at: dt.datetime
    worker_id: str = "worker"
    input_root: Path | None = None

    def check(self) -> None:
        if self.cancellation.is_set():
            raise WorkerHandlerError(
                "cancelled",
                "worker assignment cancellation was observed",
                category=FailureCategory.CANCELLED,
                outcome=AttemptOutcome.CANCELLED,
            )
        if dt.datetime.now(dt.UTC) >= self.deadline_at:
            raise WorkerHandlerError(
                "deadline_expired",
                "worker assignment deadline expired",
                category=FailureCategory.TIMED_OUT,
                outcome=AttemptOutcome.TIMED_OUT,
            )


@dataclass(frozen=True)
class HandlerResult:
    result: dict[str, object]
    artifact_refs: tuple[str, ...] = ()


class WorkerHandler(Protocol):
    async def execute(self, assignment: JobAssignmentPayload, context: HandlerContext) -> HandlerResult: ...


class ReferenceHandler:
    """Safe default for a handler whose controller-owned reference resolver is absent."""

    def __init__(self, job_type: JobType) -> None:
        self.job_type = job_type

    async def execute(self, assignment: JobAssignmentPayload, context: HandlerContext) -> HandlerResult:
        context.check()
        await asyncio.sleep(0)
        raise WorkerHandlerError(
            "input_resolver_unconfigured",
            f"{self.job_type.value} requires a deployment-owned input resolver",
            category=FailureCategory.DEPENDENCY_UNAVAILABLE,
            outcome=AttemptOutcome.RETRYABLE_FAILURE,
        )


class LocalTtsHandler:
    """Execute local synthesis inside the dedicated worker process only."""

    def __init__(self, *, worker_id: str, input_root: Path) -> None:
        self.worker_id = worker_id
        self.input_store = input_root

    async def execute(self, assignment: JobAssignmentPayload, context: HandlerContext) -> HandlerResult:
        context.check()
        payload = dict(assignment.payload)
        content_ref = str(payload.get("content_ref", ""))
        descriptor = self._read_descriptor(content_ref)
        if (
            descriptor.get("voice_profile_ref") != payload.get("voice_profile_ref")
            and assignment.job_type is JobType.TTS_SYNTHESIZE
        ):
            raise WorkerHandlerError("input_identity_mismatch", "worker input profile identity is inconsistent")
        output_root = self.input_store.parent / self.worker_id
        output_root.mkdir(mode=0o750, parents=True, exist_ok=True)
        output_path = output_root / f"{assignment.lease.job_id}_{assignment.lease.attempt_id}.wav"
        output_path.unlink(missing_ok=True)
        try:
            await asyncio.to_thread(
                self._synthesize,
                descriptor,
                output_path,
                context,
                assignment.job_type is JobType.ALERT_ARTIFACT_GENERATE,
            )
            context.check()
            return HandlerResult(result=self._artifact_result(assignment, descriptor, output_path))
        except WorkerHandlerError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            raise WorkerHandlerError(
                "local_synthesis_failed",
                f"local worker synthesis failed: {type(exc).__name__}",
                category=FailureCategory.DEPENDENCY_UNAVAILABLE,
                outcome=AttemptOutcome.RETRYABLE_FAILURE,
            ) from exc

    def _read_descriptor(self, content_ref: str) -> dict[str, Any]:
        if not content_ref.startswith("content_") or not content_ref.replace("_", "").isalnum():
            raise WorkerHandlerError("input_reference_invalid", "worker synthesis input reference is invalid")
        path = self.input_store / f"{content_ref}.json"
        try:
            raw = path.read_bytes()
            if len(raw) > 131_072:
                raise ValueError("worker synthesis descriptor exceeds configured bounds")
            descriptor = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkerHandlerError(
                "input_resolver_unavailable",
                "worker synthesis input could not be resolved",
                category=FailureCategory.DEPENDENCY_UNAVAILABLE,
                outcome=AttemptOutcome.RETRYABLE_FAILURE,
            ) from exc
        if not isinstance(descriptor, dict) or descriptor.get("content_ref") != content_ref:
            raise WorkerHandlerError("input_identity_mismatch", "worker synthesis input identity is invalid")
        if not isinstance(descriptor.get("text"), str) or not descriptor["text"]:
            raise WorkerHandlerError(
                "input_invalid", "worker synthesis input text is invalid", category=FailureCategory.INVALID_INPUT
            )
        return descriptor

    @staticmethod
    def _synthesize(
        descriptor: dict[str, Any],
        output_path: Path,
        context: HandlerContext,
        alert: bool,
    ) -> None:
        from ..tts.tts import TTS

        vtp = descriptor.get("voicetext_paul")
        tts = TTS(
            backend="local",
            local_engine=str(descriptor.get("engine", "")),
            voice=str(descriptor.get("voice", "")),
            rate_wpm=int(descriptor.get("rate_wpm", 180)),
            volume=float(descriptor.get("volume", 1.0)),
            sample_rate=int(descriptor.get("sample_rate", 48_000)),
            text_overrides=list(descriptor.get("text_overrides") or ()),
            vtp_cfg=SimpleNamespace(**(vtp if isinstance(vtp, dict) else {})),
            tts_data_base=str(descriptor.get("data_base", "")),
            capability_check=_assigned_local_capability,
        )
        try:
            tts.synth_to_wav(descriptor["text"], output_path, purpose="alert" if alert else "routine")
        finally:
            tts.close()

    def _artifact_result(
        self,
        assignment: JobAssignmentPayload,
        descriptor: dict[str, Any],
        output_path: Path,
    ) -> dict[str, object]:
        media = inspect_wav(output_path, policy=WavPolicy(allowed_channels=(1, 2)))
        identity = hash_file(output_path, maximum_bytes=1_073_741_824)
        payload = assignment.payload
        artifact = ArtifactReference(
            artifact_class=ArtifactClass.WAV,
            staging_namespace=self.worker_id,
            staging_token=output_path.name,
            claimed_sha256=identity.sha256,
            claimed_size_bytes=identity.size_bytes,
            media=media,
        )
        result = ArtifactResult(
            job_id=assignment.lease.job_id,
            job_type=assignment.job_type.value,
            lease_id=assignment.lease.lease_id,
            attempt_id=assignment.lease.attempt_id,
            result_schema_version=assignment.result_schema_version,
            configuration_generation=int(payload.get("config_generation") or assignment.configuration_generation or 0),
            source_identity=str(payload["source_identity"]) if "source_identity" in payload else None,
            event_identity=str(payload["event_identity"]) if "event_identity" in payload else None,
            content_identity=str(payload.get("content_identity", descriptor["content_ref"])),
            artifact=artifact,
            completed_at=dt.datetime.now(dt.UTC),
            provenance=f"local-worker:{descriptor.get('engine', 'unknown')}",
        )
        return result.model_dump(mode="json")


def _assigned_local_capability(request: object, capability: str) -> object:
    """Permit execution of this assigned local job, never controller admission."""
    from ..tts.local import LocalEngineRegistry
    from ..tts.models import BackendId, LocalQualification, LocalQualificationDisposition, SynthesisRequest

    matches = (
        isinstance(request, SynthesisRequest)
        and request.backend is BackendId.LOCAL
        and capability == LocalEngineRegistry.capability_for(request.local.engine)
    )
    return LocalQualification(
        disposition=LocalQualificationDisposition.SATISFIED if matches else LocalQualificationDisposition.INCOMPATIBLE,
        capability=capability,
        evidence=("controller_assigned_worker_job",),
        effective_capacity=1 if matches else 0,
    )


HandlerFactory = Callable[[JobType], WorkerHandler]


class HandlerRegistry:
    def __init__(self, handlers: dict[JobType, WorkerHandler]) -> None:
        self._handlers = dict(handlers)

    @classmethod
    def for_profile(
        cls,
        profile: str,
        *,
        factory: HandlerFactory = ReferenceHandler,
        worker_id: str = "worker",
        input_root: Path | None = None,
    ) -> HandlerRegistry:
        spec = profile_spec(profile)
        handlers = {job_type: factory(job_type) for job_type in spec.job_types}
        if input_root is not None:
            for job_type in (JobType.TTS_SYNTHESIZE, JobType.ALERT_ARTIFACT_GENERATE):
                if job_type in handlers:
                    handlers[job_type] = LocalTtsHandler(worker_id=worker_id, input_root=input_root)
        return cls(handlers)

    def supports(self, job_type: JobType | JobAssignmentPayload) -> bool:
        key = job_type.job_type if isinstance(job_type, JobAssignmentPayload) else job_type
        return key in self._handlers and not isinstance(self._handlers[key], ReferenceHandler)

    @property
    def ready(self) -> bool:
        """Whether every registered job has a real executable handler."""

        return any(not isinstance(handler, ReferenceHandler) for handler in self._handlers.values())

    @property
    def executable_job_types(self) -> frozenset[JobType]:
        return frozenset(
            job_type for job_type, handler in self._handlers.items() if not isinstance(handler, ReferenceHandler)
        )

    async def execute(self, assignment: JobAssignmentPayload, context: HandlerContext) -> HandlerResult:
        handler = self._handlers.get(assignment.job_type)
        if handler is None:
            raise WorkerHandlerError(
                "capability_unavailable",
                f"worker profile does not implement {assignment.job_type.value}",
                category=FailureCategory.UNSUPPORTED,
            )
        try:
            return await handler.execute(assignment, context)
        except WorkerHandlerError:
            raise
        except BaseException as exc:
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit, GeneratorExit)):
                raise
            raise WorkerHandlerError(
                "handler_failed",
                f"worker handler failed: {type(exc).__name__}",
                category=getattr(exc, "category", FailureCategory.UNSUPPORTED),
            ) from exc
