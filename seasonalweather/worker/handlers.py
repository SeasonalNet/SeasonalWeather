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
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

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
    ) -> HandlerRegistry:
        spec = profile_spec(profile)
        return cls({job_type: factory(job_type) for job_type in spec.job_types})

    def supports(self, job_type: JobType | JobAssignmentPayload) -> bool:
        key = job_type.job_type if isinstance(job_type, JobAssignmentPayload) else job_type
        return key in self._handlers

    @property
    def ready(self) -> bool:
        """Whether every registered job has a real executable handler."""

        return bool(self._handlers) and all(
            not isinstance(handler, ReferenceHandler) for handler in self._handlers.values()
        )

    async def execute(self, assignment: JobAssignmentPayload, context: HandlerContext) -> HandlerResult:
        handler = self._handlers.get(assignment.job_type)
        if handler is None:
            raise WorkerHandlerError(
                "capability_unavailable",
                f"worker profile does not implement {assignment.job_type.value}",
                category=FailureCategory.UNSUPPORTED,
            )
        return await handler.execute(assignment, context)
