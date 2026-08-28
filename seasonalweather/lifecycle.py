"""Controller-owned lifecycle, admission, publication, and task supervision."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
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


class OptionalTaskRestartPolicy(StrEnum):
    NEVER = "never"
    RESTART = "restart"
    ALWAYS = "always"


@dataclass(frozen=True)
class OptionalTaskRestartConfig:
    """Bounded recovery policy for optional long-running tasks.

    ``restart`` permits one bounded recovery circuit and then leaves the task
    degraded if it continues to fail.  ``always`` keeps trying after each
    cooldown.  Neither policy applies once lifecycle shutdown has begun.
    """

    policy: str = OptionalTaskRestartPolicy.RESTART.value
    stable_after_seconds: float = 60.0
    restart_initial_delay_seconds: float = 1.0
    restart_max_delay_seconds: float = 30.0
    thrash_window_seconds: float = 300.0
    thrash_limit: int = 3
    cooldown_seconds: float = 300.0

    def validate(self) -> None:
        try:
            policy = OptionalTaskRestartPolicy(str(self.policy).strip().lower())
        except ValueError as exc:
            raise ValueError("lifecycle.optional_tasks.policy must be one of: never, restart, always") from exc
        if any(
            value <= 0
            for value in (
                self.stable_after_seconds,
                self.restart_initial_delay_seconds,
                self.restart_max_delay_seconds,
                self.thrash_window_seconds,
                self.cooldown_seconds,
            )
        ):
            raise ValueError("lifecycle.optional_tasks timing values must be positive")
        if self.restart_max_delay_seconds < self.restart_initial_delay_seconds:
            raise ValueError("lifecycle.optional_tasks.restart_max_delay_seconds must cover the initial restart delay")
        if self.thrash_limit < 1:
            raise ValueError("lifecycle.optional_tasks.thrash_limit must be positive")
        del policy


@dataclass(frozen=True)
class LifecycleTimeouts:
    total_seconds: float = 30.0
    active_request_seconds: float = 10.0
    publication_seconds: float = 8.0
    source_stop_seconds: float = 8.0
    tts_stop_seconds: float = 8.0
    task_cancel_seconds: float = 5.0
    resource_close_seconds: float = 5.0
    optional_tasks: OptionalTaskRestartConfig = field(default_factory=OptionalTaskRestartConfig)

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
        self.optional_tasks.validate()


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
RestartFactory = Callable[[], Coroutine[Any, Any, Any]]


@dataclass
class SupervisedTask:
    name: str
    required: bool
    task: asyncio.Task[Any]
    stop: StopCallback | None
    stop_timeout_seconds: float
    restart_factory: RestartFactory | None = None
    restart_config: OptionalTaskRestartConfig = field(default_factory=OptionalTaskRestartConfig)
    started_at: float = 0.0
    failure_times: deque[float] = field(default_factory=deque)
    restart_task: asyncio.Task[Any] | None = None
    restart_exhausted: bool = False


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
        self._stopping = False

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
        restart_factory: RestartFactory | None = None,
        restart_config: OptionalTaskRestartConfig | None = None,
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
            restart_factory=restart_factory,
            restart_config=restart_config or self.lifecycle.timeouts.optional_tasks,
            started_at=time.monotonic(),
        )
        self._tasks[name] = registration
        task.add_done_callback(partial(self._task_done, registration))
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
        self._stopping = True
        registrations = self.tasks
        await self._stop_restart_workers(registrations)
        for registration in registrations:
            if registration.stop is None or registration.task.done():
                continue
            await self._bounded_stop(registration)

        await self._cancel_pending_tasks(registrations)

    async def _stop_restart_workers(self, registrations: tuple[SupervisedTask, ...]) -> None:
        workers = [registration.restart_task for registration in registrations if registration.restart_task is not None]
        for worker in workers:
            if not worker.done():
                worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

    async def _cancel_pending_tasks(self, registrations: tuple[SupervisedTask, ...]) -> None:
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

    def _task_done(self, registration: SupervisedTask, task: asyncio.Task[Any]) -> None:
        if registration.task is not task or task.cancelled() or self._stopping or self.lifecycle.is_shutting_down:
            return
        try:
            exception = task.exception()
        except asyncio.CancelledError:
            return
        if exception is None and not registration.required:
            self._handle_optional_clean_return(registration)
            return
        if exception is None:
            exception = RequiredTaskStoppedError(f"supervised task ended unexpectedly: {registration.name}")
        if not registration.required:
            self._handle_optional_failure(registration, exception)
            return
        self._record_required_failure(exception)

    def _handle_optional_clean_return(self, registration: SupervisedTask) -> None:
        if registration.restart_config.policy == OptionalTaskRestartPolicy.ALWAYS.value:
            self._schedule_optional_restart(registration, clean_return=True)

    def _handle_optional_failure(self, registration: SupervisedTask, exception: BaseException) -> None:
        self._optional_failures.add(registration.name)
        log.warning("optional_supervised_task_failed task=%s", registration.name)
        if self.optional_failure is not None:
            try:
                self.optional_failure(registration.name, exception)
            except Exception:
                log.warning("optional_supervised_task_diagnostic_failed task=%s", registration.name)
        self._schedule_optional_restart(registration, clean_return=False)

    def _schedule_optional_restart(self, registration: SupervisedTask, *, clean_return: bool) -> None:
        config = registration.restart_config
        policy = str(config.policy).strip().lower()
        if not self._restart_is_enabled(registration, policy, clean_return):
            return
        if registration.restart_task is not None and not registration.restart_task.done():
            return

        failure_count = self._record_optional_failure(registration, config)
        recovery = self._restart_recovery(registration, config, policy, failure_count)
        if recovery is None:
            return
        delay, thrashing = recovery
        worker = asyncio.create_task(
            self._restart_optional(registration, delay=delay, thrashing=thrashing),
            name=f"restart:{registration.name}",
        )
        registration.restart_task = worker
        worker.add_done_callback(partial(self._restart_done, registration))

    @staticmethod
    def _restart_is_enabled(registration: SupervisedTask, policy: str, clean_return: bool) -> bool:
        if registration.restart_factory is None or policy == OptionalTaskRestartPolicy.NEVER.value:
            return False
        return not clean_return or policy == OptionalTaskRestartPolicy.ALWAYS.value

    def _restart_recovery(
        self,
        registration: SupervisedTask,
        config: OptionalTaskRestartConfig,
        policy: str,
        failure_count: int,
    ) -> tuple[float, bool] | None:
        if policy == OptionalTaskRestartPolicy.RESTART.value and registration.restart_exhausted:
            log.error("optional_supervised_task_restart_exhausted task=%s", registration.name)
            return None
        thrashing = failure_count >= config.thrash_limit
        if thrashing:
            if policy == OptionalTaskRestartPolicy.RESTART.value:
                registration.restart_exhausted = True
            return config.cooldown_seconds, True
        delay = min(
            config.restart_max_delay_seconds,
            config.restart_initial_delay_seconds * (2 ** max(0, failure_count - 1)),
        )
        return delay, False

    def _record_optional_failure(self, registration: SupervisedTask, config: OptionalTaskRestartConfig) -> int:
        now = time.monotonic()
        while registration.failure_times and now - registration.failure_times[0] > config.thrash_window_seconds:
            registration.failure_times.popleft()
        if registration.started_at and now - registration.started_at >= config.stable_after_seconds:
            registration.failure_times.clear()
            registration.restart_exhausted = False
        registration.failure_times.append(now)
        return len(registration.failure_times)

    async def _restart_optional(self, registration: SupervisedTask, *, delay: float, thrashing: bool) -> None:
        log.warning(
            "optional_supervised_task_restart_scheduled task=%s delay=%.1fs thrashing=%s policy=%s",
            registration.name,
            delay,
            thrashing,
            registration.restart_config.policy,
        )
        await asyncio.sleep(delay)
        if self._stopping or self.lifecycle.is_shutting_down or registration.restart_factory is None:
            return
        coroutine = registration.restart_factory()
        task = asyncio.create_task(coroutine, name=registration.name)
        registration.task = task
        registration.started_at = time.monotonic()
        task.add_done_callback(partial(self._task_done, registration))

    def _restart_done(self, registration: SupervisedTask, task: asyncio.Task[Any]) -> None:
        if registration.restart_task is task:
            registration.restart_task = None
        if task.cancelled():
            return
        try:
            task.exception()
        except asyncio.CancelledError:
            return
        except Exception:
            log.warning("optional_supervised_task_restart_worker_failed task=%s", registration.name, exc_info=True)

    def _record_required_failure(self, exception: BaseException) -> None:
        if self._fatal is None:
            self._fatal = asyncio.get_running_loop().create_future()
        if not self._fatal.done():
            self._fatal.set_result(exception)
        self.lifecycle.mark_failed()
