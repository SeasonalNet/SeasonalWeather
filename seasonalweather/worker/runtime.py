"""Worker process runtime over the typed SWWP/1 session machine."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
from collections.abc import Callable

from ..build_metadata import current_build_info
from ..lifecycle_records import LifecycleRecordWriter, LifecycleStage
from ..observability import bind_correlation, bind_trace_context
from ..observability.tracing import TraceContext
from ..swwp.codec import decode, encode
from ..swwp.constants import WorkerReadinessState, WorkerState
from ..swwp.messages import (
    Cancel,
    Drain,
    JobAssignmentPayload,
    Registered,
    ResultCommitted,
)
from ..swwp.worker import WorkerSession
from .handlers import HandlerContext, HandlerRegistry, WorkerHandlerError
from .health import WorkerHealthStore, health_path
from .transport import WorkerConnection, WorkerTransport

log = logging.getLogger("seasonalweather.worker")


class WorkerRuntime:
    def __init__(
        self,
        session: WorkerSession,
        handlers: HandlerRegistry,
        transport: WorkerTransport,
        *,
        clock: Callable[[], dt.datetime] | None = None,
        health_file: str | None = None,
        image_profile: str | None = None,
        records: LifecycleRecordWriter | None = None,
        reconnect: bool = False,
    ) -> None:
        self.session = session
        self.handlers = handlers
        self.transport = transport
        self.clock = clock or (lambda: dt.datetime.now(dt.UTC))
        self.records = records or LifecycleRecordWriter(
            role="worker",
            instance_id=session.registration.worker_instance_id,
            build_info=current_build_info(),
            clock=self.clock,
        )
        self.image_profile = image_profile
        self.reconnect = reconnect
        self.health = WorkerHealthStore(health_path(health_file), clock=self.clock)
        self._send_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._registered = asyncio.Event()
        self._heartbeat_interval = 30
        self._connection: WorkerConnection | None = None
        self._assignment_tasks: dict[tuple[str, str, str, int], asyncio.Task[None]] = {}
        self._cancellations: dict[tuple[str, str, str, int], asyncio.Event] = {}
        self._failure: BaseException | None = None
        self._shutdown_requested = False
        self._draining_recorded = False
        self._prior_session_id: str | None = None
        self._prior_controller_epoch: int | None = None
        self._reconnect_attempt = 0

    async def run(self) -> None:
        self.records.startup_identity(image_profile=self.image_profile)
        self.records.stage(LifecycleStage.SERVICE_STARTING, ready=False)
        self._publish_health(state="starting", ready=False, accepting_new_jobs=False, reason="connecting")
        try:
            await self._run_connection_loop()
        except asyncio.CancelledError:
            if self._shutdown_requested:
                return
            self._failure = asyncio.CancelledError()
            self._set_draining("task_cancelled")
            raise
        except BaseException as exc:
            if self._shutdown_requested:
                return
            self._failure = exc
            self.session.set_readiness(
                WorkerReadinessState.FAILED,
                ready=False,
                accepting_new_jobs=False,
            )
            self.records.stage(LifecycleStage.SERVICE_STARTED_DEGRADED, ready=False, reason="worker_failed")
            self._publish_health(state="failed", ready=False, accepting_new_jobs=False, reason="worker_failed")
            raise
        finally:
            await self._drain_assignment_tasks()
            if self._failure is None:
                self.session.set_readiness(
                    WorkerReadinessState.STOPPED,
                    ready=False,
                    accepting_new_jobs=False,
                )
                self.records.stage(LifecycleStage.SERVICE_STOPPED, ready=False)
                self._publish_health(state="stopped", ready=False, accepting_new_jobs=False, reason="stopped")

    async def _run_connection_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._connect_once()
                self._reconnect_attempt = 0
                if self.session.state in {WorkerState.CLOSED, WorkerState.FAILED}:
                    raise RuntimeError("worker session closed by controller")
                if not self.reconnect:
                    return
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                if self._shutdown_requested:
                    return
                if self.session.state in {WorkerState.CLOSED, WorkerState.FAILED}:
                    raise
                await self._schedule_reconnect(exc)

    async def _schedule_reconnect(self, exc: BaseException) -> None:
        self._reconnect_attempt += 1
        delay = min(30.0, 0.5 * (2 ** min(self._reconnect_attempt - 1, 6)))
        log.warning(
            "worker_swwp_reconnect_scheduled attempt=%s delay_seconds=%s error=%s",
            self._reconnect_attempt,
            delay,
            type(exc).__name__,
        )
        self.records.stage(
            LifecycleStage.SERVICE_STARTED_DEGRADED,
            ready=False,
            reason="connection_lost",
        )
        self._publish_health(
            state="reconnecting",
            ready=False,
            accepting_new_jobs=False,
            reason="connection_lost",
        )
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
        except TimeoutError:
            return

    async def _connect_once(self) -> None:
        self._registered.clear()
        connection = await self.transport.connect()
        self._connection = connection
        try:
            await self._send(self.session.connect())
            receiver = asyncio.create_task(self._receive_loop(), name="seasonalweather-worker-receiver")
            heartbeat = asyncio.create_task(self._heartbeat_loop(), name="seasonalweather-worker-heartbeat")
            try:
                await receiver
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
        finally:
            with contextlib.suppress(Exception):
                await connection.close()
            self._connection = None
            if self._shutdown_requested:
                self._stop.set()
            elif not self._stop.is_set():
                self._prior_session_id = self.session.session_id
                self._prior_controller_epoch = self.session.controller_epoch
                self.session.transport_lost()

    async def stop(self) -> None:
        self._shutdown_requested = True
        self._set_draining("shutdown_requested")
        for cancellation in self._cancellations.values():
            cancellation.set()
        connection = self._connection
        if connection is not None:
            with contextlib.suppress(Exception):
                await connection.close()

    async def _receive_loop(self) -> None:
        connection = self._connection
        if connection is None:
            raise RuntimeError("worker receive loop started without a connection")
        while not self._stop.is_set():
            incoming = await connection.recv()
            if incoming is None:
                return
            if isinstance(incoming, str):
                incoming = incoming.encode("utf-8")
            envelope = decode(incoming)
            responses = self.session.receive(envelope)
            for response in responses:
                await self._send(response)
            await self._handle_payload(envelope.payload)

    async def _handle_payload(self, payload: object) -> None:
        if isinstance(payload, Registered):
            prior_session_id = self._prior_session_id
            prior_controller_epoch = self._prior_controller_epoch
            self._handle_registered(payload)
            if prior_session_id is not None and self.session.state is WorkerState.ACTIVE:
                self._prior_session_id = None
                self._prior_controller_epoch = None
                await self._send(
                    self.session.reconnect_report(
                        prior_session_id=prior_session_id,
                        prior_controller_epoch=prior_controller_epoch,
                    )
                )
        elif isinstance(payload, JobAssignmentPayload):
            self._handle_assignment(payload)
        elif isinstance(payload, Cancel):
            active_cancellation = self._cancellations.get(self._lease_key(payload.lease))
            if active_cancellation is not None:
                active_cancellation.set()
        elif isinstance(payload, Drain):
            self._handle_drain()
        elif isinstance(payload, ResultCommitted):
            self._handle_result_committed(payload)

    def _handle_registered(self, payload: Registered) -> None:
        self._heartbeat_interval = payload.heartbeat_interval_seconds
        if self.handlers.ready:
            self.session.set_readiness(
                WorkerReadinessState.READY,
                ready=True,
                accepting_new_jobs=True,
            )
            self.records.stage(LifecycleStage.SERVICE_READY, ready=True)
            reason = "registered"
        else:
            self.session.set_readiness(
                WorkerReadinessState.DEGRADED,
                ready=False,
                accepting_new_jobs=False,
            )
            self.records.stage(
                LifecycleStage.SERVICE_STARTED_DEGRADED,
                ready=False,
                reason="handler_unavailable",
            )
            reason = "handler_unavailable"
        self._registered.set()
        self._publish_health(
            state=self.session.readiness_state.value,
            ready=self.session.ready,
            accepting_new_jobs=self.session.accepting_new_jobs,
            reason=reason,
        )

    def _handle_assignment(self, payload: JobAssignmentPayload) -> None:
        key = self._lease_key(payload.lease)
        cancellation = asyncio.Event()
        self._cancellations[key] = cancellation
        task = asyncio.create_task(
            self._execute(payload, cancellation),
            name=f"seasonalweather-worker-job-{payload.lease.job_id}",
        )
        self._assignment_tasks[key] = task
        self._publish_health(
            state=self.session.readiness_state.value,
            ready=self.session.ready,
            accepting_new_jobs=self.session.accepting_new_jobs,
            reason="job_started",
        )

    def _handle_drain(self) -> None:
        self._set_draining("controller_drain", stop_receiving=False)
        if not self._assignment_tasks:
            self._stop.set()

    def _handle_result_committed(self, payload: ResultCommitted) -> None:
        self._assignment_tasks.pop(self._lease_key(payload.lease), None)
        self._cancellations.pop(self._lease_key(payload.lease), None)
        self._publish_health(
            state=self.session.readiness_state.value,
            ready=self.session.ready,
            accepting_new_jobs=self.session.accepting_new_jobs,
            reason="job_committed",
        )
        if self.session.readiness_state is WorkerReadinessState.DRAINING and not self._assignment_tasks:
            self._stop.set()

    async def _execute(self, assignment: JobAssignmentPayload, cancellation: asyncio.Event) -> None:
        key = self._lease_key(assignment.lease)
        try:
            trace = TraceContext.parse(assignment.traceparent)
            with (
                bind_trace_context(trace),
                bind_correlation(
                    role="worker",
                    worker_id=self.session.registration.worker_id,
                    instance_id=self.session.registration.worker_instance_id,
                    job_id=assignment.lease.job_id,
                    lease_id=assignment.lease.lease_id,
                    trace_id=trace.trace_id,
                    span_id=trace.span_id,
                ),
            ):
                result = await self.handlers.execute(
                    assignment,
                    HandlerContext(
                        cancellation=cancellation,
                        deadline_at=assignment.deadline_at,
                        worker_id=self.session.registration.worker_id,
                    ),
                )
        except asyncio.CancelledError:
            raise
        except WorkerHandlerError as exc:
            await self._send(
                self.session.failure(
                    assignment.lease,
                    outcome=exc.outcome,
                    category=exc.category,
                    error_code=exc.code,
                    summary=exc.summary,
                )
            )
        else:
            completion_id = self.session.id_factory("completion")
            await self._send(
                self.session.result(
                    assignment.lease,
                    result_schema_version=assignment.result_schema_version,
                    result=result.result,
                    artifact_refs=result.artifact_refs,
                    completion_id=completion_id,
                )
            )
        finally:
            self._assignment_tasks.pop(key, None)

    async def _heartbeat_loop(self) -> None:
        await self._registered.wait()
        while not self._stop.is_set():
            await asyncio.sleep(self._heartbeat_interval)
            if self._stop.is_set():
                return
            await self._send(self.session.heartbeat())
            self._publish_health(
                state=self.session.readiness_state.value,
                ready=self.session.ready,
                accepting_new_jobs=self.session.accepting_new_jobs,
                reason="heartbeat",
            )

    async def _send(self, envelope) -> None:
        connection = self._connection
        if connection is None:
            raise RuntimeError("worker send attempted without a connection")
        data = encode(envelope)
        async with self._send_lock:
            await connection.send(data)

    async def _drain_assignment_tasks(self) -> None:
        tasks = tuple(self._assignment_tasks.values())
        for cancellation in self._cancellations.values():
            cancellation.set()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._assignment_tasks.clear()
        self._cancellations.clear()

    def _set_draining(self, reason: str, *, stop_receiving: bool = True) -> None:
        if stop_receiving:
            self._stop.set()
        if self.session.state in {WorkerState.ACTIVE, WorkerState.DRAINING}:
            self.session.set_readiness(
                WorkerReadinessState.DRAINING,
                ready=False,
                accepting_new_jobs=False,
            )
        if not self._draining_recorded:
            self.records.stage(LifecycleStage.SERVICE_DRAINING, ready=False, reason=reason)
            self._publish_health(state="draining", ready=False, accepting_new_jobs=False, reason=reason)
            self._draining_recorded = True

    def _publish_health(self, *, state: str, ready: bool, accepting_new_jobs: bool, reason: str) -> None:
        self.health.write(
            state=state,
            ready=ready,
            registered=self._registered.is_set(),
            accepting_new_jobs=accepting_new_jobs,
            active_leases=len(self._assignment_tasks),
            reason=reason,
        )

    @staticmethod
    def _lease_key(lease) -> tuple[str, str, str, int]:
        return lease.job_id, lease.lease_id, lease.attempt_id, lease.attempt
