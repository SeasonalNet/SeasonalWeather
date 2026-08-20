"""Controller-owned lifecycle, admission, publication, and task supervision."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import logging
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from typing import Any, cast

log = logging.getLogger("seasonalweather.lifecycle")


class LifecycleState(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    DRAINING = "draining"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class WorkClass(StrEnum):
    COMMAND = "command"
    ROUTINE = "routine"
    SOURCE = "source"
    TTS = "tts"
    ALERT = "alert"
    PUBLICATION = "publication"
    JOB_LEASE = "job_lease"


class LifecycleTransitionError(RuntimeError):
    """Raised when a lifecycle transition is not permitted."""


class AdmissionClosedError(RuntimeError):
    """Bounded rejection raised after controller drain closes admission."""

    code = "service_draining"

    def __init__(self, work_class: WorkClass) -> None:
        self.work_class = work_class
        super().__init__(f"{work_class.value} admission is closed")


class RequiredTaskStoppedError(RuntimeError):
    """A required long-running task returned without a shutdown request."""


TransitionCallback = Callable[[LifecycleState], object]


@dataclass(frozen=True)
class LifecycleTimeouts:
    total_seconds: float = 30.0
    active_request_seconds: float = 10.0
    publication_seconds: float = 8.0
    source_stop_seconds: float = 8.0
    tts_stop_seconds: float = 8.0
    task_cancel_seconds: float = 5.0
    resource_close_seconds: float = 5.0

    def validate(self) -> None:
        from .configuration.semantic_rules import lifecycle_timeout_error

        stage_seconds = (
            self.active_request_seconds,
            self.publication_seconds,
            self.source_stop_seconds,
            self.tts_stop_seconds,
            self.task_cancel_seconds,
            self.resource_close_seconds,
        )
        if error := lifecycle_timeout_error(
            total_seconds=self.total_seconds,
            stage_seconds=stage_seconds,
        ):
            raise ValueError(error)


_ALLOWED_TRANSITIONS: dict[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.STARTING: frozenset({LifecycleState.RUNNING, LifecycleState.DRAINING, LifecycleState.FAILED}),
    LifecycleState.RUNNING: frozenset({LifecycleState.DRAINING, LifecycleState.FAILED}),
    LifecycleState.DRAINING: frozenset({LifecycleState.STOPPING, LifecycleState.FAILED}),
    LifecycleState.STOPPING: frozenset({LifecycleState.STOPPED, LifecycleState.FAILED}),
    LifecycleState.STOPPED: frozenset(),
    LifecycleState.FAILED: frozenset(),
}


class Lifecycle:
    """Small controller authority for state and admission.

    The first shutdown request begins drain. A second request sets ``force`` so
    the controller can skip remaining grace waits and proceed to cancellation.
    """

    def __init__(
        self,
        timeouts: LifecycleTimeouts | None = None,
        *,
        transition_callback: TransitionCallback | None = None,
    ) -> None:
        self.timeouts = timeouts or LifecycleTimeouts()
        self.timeouts.validate()
        self.transition_callback = transition_callback
        self._state = LifecycleState.STARTING
        self._startup_ready = False
        self._shutdown_requested = asyncio.Event()
        self._force_requested = asyncio.Event()
        self._state_changed = asyncio.Condition()

    @property
    def state(self) -> LifecycleState:
        return self._state

    @property
    def is_shutting_down(self) -> bool:
        return self._state in {
            LifecycleState.DRAINING,
            LifecycleState.STOPPING,
            LifecycleState.STOPPED,
            LifecycleState.FAILED,
        }

    @property
    def force_requested(self) -> bool:
        return self._force_requested.is_set()

    @property
    def ready(self) -> bool:
        return self._state is LifecycleState.RUNNING

    @property
    def startup_ready(self) -> bool:
        """Whether broadcast-critical startup has completed."""
        return self.ready and self._startup_ready

    def allows(self, work_class: WorkClass) -> bool:
        del work_class
        return self._state is LifecycleState.RUNNING

    def require(self, work_class: WorkClass) -> None:
        if not self.allows(work_class):
            raise AdmissionClosedError(work_class)

    def permits_service_start(self) -> bool:
        return self._state in {LifecycleState.STARTING, LifecycleState.RUNNING}

    def transition(self, target: LifecycleState) -> None:
        if target is self._state:
            return
        if target not in _ALLOWED_TRANSITIONS[self._state]:
            raise LifecycleTransitionError(f"invalid lifecycle transition {self._state.value} -> {target.value}")
        self._state = target
        if self.transition_callback is not None:
            self.transition_callback(target)
        if target is LifecycleState.RUNNING:
            log.info("lifecycle_event=service_ready state=running")
        elif target is LifecycleState.DRAINING:
            log.info("lifecycle_event=service_draining state=draining")
        elif target is LifecycleState.STOPPED:
            log.info("lifecycle_event=service_stopped state=stopped")
        elif target is LifecycleState.FAILED:
            log.error("lifecycle_event=service_failed state=failed")
        self._notify_state_change()

    def mark_running(self, *, startup_complete: bool = True) -> None:
        self.transition(LifecycleState.RUNNING)
        self._startup_ready = startup_complete

    def mark_startup_complete(self) -> None:
        if self._state is not LifecycleState.RUNNING:
            raise LifecycleTransitionError("startup can complete only while running")
        self._startup_ready = True

    def request_shutdown(self) -> bool:
        if self._state in {LifecycleState.STARTING, LifecycleState.RUNNING}:
            self.transition(LifecycleState.DRAINING)
            self._shutdown_requested.set()
            return True
        if self._state in {LifecycleState.DRAINING, LifecycleState.STOPPING}:
            self._force_requested.set()
            return False
        return False

    def mark_stopping(self) -> None:
        self._startup_ready = False
        self.transition(LifecycleState.STOPPING)

    def mark_stopped(self) -> None:
        self._startup_ready = False
        self.transition(LifecycleState.STOPPED)

    def mark_failed(self) -> None:
        if self._state is LifecycleState.FAILED:
            return
        if self._state is LifecycleState.STOPPED:
            raise LifecycleTransitionError("a stopped lifecycle cannot fail")
        self._startup_ready = False
        self.transition(LifecycleState.FAILED)
        self._shutdown_requested.set()

    async def wait_for_shutdown(self) -> None:
        await self._shutdown_requested.wait()

    async def wait_for_force(self) -> None:
        await self._force_requested.wait()

    async def wait_for_state(self, state: LifecycleState) -> None:
        async with self._state_changed:
            await self._state_changed.wait_for(lambda: self._state is state)

    def snapshot(self) -> dict[str, str | bool]:
        return {
            "state": self._state.value,
            "ready": self.ready,
            "startup_ready": self.startup_ready,
            "admission_open": self._state is LifecycleState.RUNNING,
        }

    def _notify_state_change(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def notify() -> None:
            async with self._state_changed:
                self._state_changed.notify_all()

        task = loop.create_task(notify(), name="lifecycle-state-notify")
        task.add_done_callback(_consume_task_exception)


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    task.exception()


class PublicationFence:
    """Closeable fence around the smallest authoritative publication section."""

    def __init__(self, lifecycle: Lifecycle) -> None:
        self._lifecycle = lifecycle
        self._identity = object()
        self._permit: contextvars.ContextVar[object | None] = contextvars.ContextVar(
            f"publication-permit-{id(self)}",
            default=None,
        )
        self._active = 0
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def active(self) -> int:
        return self._active

    @asynccontextmanager
    async def enter(self):
        if self._permit.get() is not self._identity:
            self._lifecycle.require(WorkClass.PUBLICATION)
        self._active += 1
        self._idle.clear()
        try:
            yield
        finally:
            self._active -= 1
            if self._active == 0:
                self._idle.set()

    def issue_permit(self) -> object:
        """Issue a process-local permit to alert work admitted before drain."""
        self._lifecycle.require(WorkClass.ALERT)
        return self._identity

    def activate_permit(
        self,
        permit: object,
    ) -> contextvars.Token[object | None]:
        if permit is not self._identity:
            raise ValueError("publication permit does not belong to this fence")
        return self._permit.set(permit)

    def deactivate_permit(
        self,
        token: contextvars.Token[object | None],
    ) -> None:
        self._permit.reset(token)

    async def wait_idle(self, timeout_seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=timeout_seconds)
        except TimeoutError:
            return False
        return True


StopCallback = Callable[[], object | Awaitable[object]]
FailureCallback = Callable[[str, BaseException], object]


@dataclass(frozen=True)
class SupervisedTask:
    name: str
    required: bool
    task: asyncio.Task[Any]
    stop: StopCallback | None
    stop_timeout_seconds: float


class TaskSupervisor:
    """Registry and bounded shutdown for controller-owned long-running tasks."""

    def __init__(
        self,
        lifecycle: Lifecycle,
        *,
        optional_failure: FailureCallback | None = None,
    ) -> None:
        self.lifecycle = lifecycle
        self.optional_failure = optional_failure
        self._tasks: dict[str, SupervisedTask] = {}
        self._fatal: asyncio.Future[BaseException] | None = None
        self._optional_failures: set[str] = set()

    @property
    def tasks(self) -> tuple[SupervisedTask, ...]:
        return tuple(self._tasks[name] for name in sorted(self._tasks))

    @property
    def optional_failures(self) -> frozenset[str]:
        return frozenset(self._optional_failures)

    def create_task(
        self,
        coroutine: Coroutine[Any, Any, Any],
        *,
        name: str,
        required: bool,
        stop: StopCallback | None = None,
        stop_timeout_seconds: float | None = None,
    ) -> asyncio.Task[Any]:
        if not self.lifecycle.permits_service_start():
            coroutine.close()
            raise AdmissionClosedError(WorkClass.SOURCE)
        if name in self._tasks:
            coroutine.close()
            raise ValueError(f"duplicate supervised task name: {name}")
        task = asyncio.create_task(coroutine, name=name)
        registration = SupervisedTask(
            name=name,
            required=required,
            task=task,
            stop=stop,
            stop_timeout_seconds=(
                float(stop_timeout_seconds)
                if stop_timeout_seconds is not None
                else self.lifecycle.timeouts.task_cancel_seconds
            ),
        )
        self._tasks[name] = registration
        task.add_done_callback(partial(self._task_done, name))
        return task

    async def wait_for_fatal(self) -> BaseException:
        if self._fatal is None:
            self._fatal = asyncio.get_running_loop().create_future()
        return await asyncio.shield(self._fatal)

    def report_background_failure(self, exception: BaseException) -> None:
        """Promote an otherwise-discarded event-loop failure to required fatal state."""
        if self.lifecycle.is_shutting_down:
            return
        self._record_required_failure(exception)

    async def stop(self) -> None:
        registrations = self.tasks
        for registration in registrations:
            if registration.stop is None or registration.task.done():
                continue
            await self._bounded_stop(registration)

        pending = [registration.task for registration in registrations if not registration.task.done()]
        for task in pending:
            task.cancel()
        if not pending:
            return
        _, still_pending = await asyncio.wait(
            pending,
            timeout=self.lifecycle.timeouts.task_cancel_seconds,
        )
        for task in still_pending:
            log.error("supervised_task_cancel_timeout task=%s", task.get_name())

    async def _bounded_stop(self, registration: SupervisedTask) -> None:
        assert registration.stop is not None

        async def invoke() -> None:
            callback = registration.stop
            if inspect.iscoroutinefunction(callback):
                await callback()
                return
            sync_callback = cast(Callable[[], object], callback)
            result = await asyncio.to_thread(sync_callback)
            if inspect.isawaitable(result):
                await result

        try:
            await asyncio.wait_for(
                invoke(),
                timeout=registration.stop_timeout_seconds,
            )
        except TimeoutError:
            log.warning(
                "supervised_task_stop_timeout task=%s",
                registration.name,
            )
        except Exception:
            log.warning(
                "supervised_task_stop_failed task=%s",
                registration.name,
                exc_info=True,
            )

    def _task_done(self, name: str, task: asyncio.Task[Any]) -> None:
        if task.cancelled() or self.lifecycle.is_shutting_down:
            return
        try:
            exception = task.exception()
        except asyncio.CancelledError:
            return
        registration = self._tasks[name]
        if exception is None and not registration.required:
            return
        if exception is None:
            exception = RequiredTaskStoppedError(f"supervised task ended unexpectedly: {name}")
        if not registration.required:
            self._optional_failures.add(name)
            log.warning("optional_supervised_task_failed task=%s", name)
            if self.optional_failure is not None:
                try:
                    self.optional_failure(name, exception)
                except Exception:
                    log.warning("optional_supervised_task_diagnostic_failed task=%s", name)
            return
        self._record_required_failure(exception)

    def _record_required_failure(self, exception: BaseException) -> None:
        if self._fatal is None:
            self._fatal = asyncio.get_running_loop().create_future()
        if not self._fatal.done():
            self._fatal.set_result(exception)
        self.lifecycle.mark_failed()
