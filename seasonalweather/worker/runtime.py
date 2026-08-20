"""Worker process runtime over the typed SWWP/1 session machine."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
from collections.abc import Callable

from ..jobs.contracts import AttemptOutcome
from ..jobs.policies import FailureCategory
from ..swwp.codec import decode, encode
from ..swwp.messages import (
    Cancel,
    JobAssignmentPayload,
    Registered,
    ResultCommitted,
)
from ..swwp.worker import WorkerSession
from .handlers import HandlerContext, HandlerRegistry, WorkerHandlerError
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
    ) -> None:
        self.session = session
        self.handlers = handlers
        self.transport = transport
        self.clock = clock or (lambda: dt.datetime.now(dt.UTC))
        self._send_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._registered = asyncio.Event()
        self._heartbeat_interval = 30
        self._connection: WorkerConnection | None = None
        self._assignment_tasks: dict[tuple[str, str, str, int], asyncio.Task[None]] = {}
        self._cancellations: dict[tuple[str, str, str, int], asyncio.Event] = {}

    async def run(self) -> None:
        connection = await self.transport.connect()
        self._connection = connection
        try:
            await self._send(self.session.connect())
            receiver = asyncio.create_task(self._receive_loop(), name="seasonalweather-worker-receiver")
            heartbeat = asyncio.create_task(self._heartbeat_loop(), name="seasonalweather-worker-heartbeat")
            try:
                await receiver
            finally:
                self._stop.set()
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
                await self._drain_assignment_tasks()
        finally:
            await connection.close()
            self._connection = None

    async def stop(self) -> None:
        self._stop.set()
        for cancellation in self._cancellations.values():
            cancellation.set()

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
            self._heartbeat_interval = payload.heartbeat_interval_seconds
            self._registered.set()
        elif isinstance(payload, JobAssignmentPayload):
            key = self._lease_key(payload.lease)
            cancellation = asyncio.Event()
            self._cancellations[key] = cancellation
            task = asyncio.create_task(
                self._execute(payload, cancellation),
                name=f"seasonalweather-worker-job-{payload.lease.job_id}",
            )
            self._assignment_tasks[key] = task
        elif isinstance(payload, Cancel):
            active_cancellation = self._cancellations.get(self._lease_key(payload.lease))
            if active_cancellation is not None:
                active_cancellation.set()
        elif isinstance(payload, ResultCommitted):
            self._assignment_tasks.pop(self._lease_key(payload.lease), None)
            self._cancellations.pop(self._lease_key(payload.lease), None)

    async def _execute(self, assignment: JobAssignmentPayload, cancellation: asyncio.Event) -> None:
        key = self._lease_key(assignment.lease)
        try:
            result = await self.handlers.execute(
                assignment,
                HandlerContext(cancellation=cancellation, deadline_at=assignment.deadline_at),
            )
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
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - worker boundary sanitizes the exception
            log.exception("worker handler failed job=%s", assignment.lease.job_id)
            await self._send(
                self.session.failure(
                    assignment.lease,
                    outcome=AttemptOutcome.PERMANENT_FAILURE,
                    category=FailureCategory.UNSUPPORTED,
                    error_code="handler_failed",
                    summary=f"worker handler failed: {type(exc).__name__}",
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

    @staticmethod
    def _lease_key(lease) -> tuple[str, str, str, int]:
        return lease.job_id, lease.lease_id, lease.attempt_id, lease.attempt
