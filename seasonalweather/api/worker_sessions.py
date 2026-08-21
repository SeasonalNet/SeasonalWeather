"""Live controller-side SWWP sessions over the application WebSocket."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol, cast, final

from ..capabilities.service import CapabilitySchedulerService
from ..observability.metrics import WorkerTelemetryMetricsPort
from ..swwp.adapter import DurableSwwpPort
from ..swwp.auth import ControllerVersionSupport, RegistrationPolicy
from ..swwp.codec import decode, encode
from ..swwp.constants import (
    DEFAULT_LIMITS,
    SUBPROTOCOL,
    ControllerState,
    ProtocolLimits,
)
from ..swwp.controller import CapabilityControllerPort, ControllerSession, WorkerDiagnosticPort
from ..swwp.messages import Envelope

log = logging.getLogger("seasonalweather.swwp.live")


class WorkerSocket(Protocol):
    headers: Mapping[str, str]

    async def accept(self, subprotocol: str | None = None) -> None: ...

    async def receive(self) -> dict[str, object]: ...

    async def send_bytes(self, data: bytes) -> None: ...

    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...


PolicyFactory = Callable[[WorkerSocket], RegistrationPolicy]


class LiveWorkerRouteApp(Protocol):
    def add_api_websocket_route(
        self,
        path: str,
        endpoint: Callable[..., Awaitable[None]],
        *,
        name: str,
    ) -> None: ...


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(microsecond=0)


def _offered_subprotocols(websocket: WorkerSocket) -> tuple[str, ...]:
    raw = str(websocket.headers.get("sec-websocket-protocol", ""))
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@final
class LiveWorkerSession:
    """Own one WebSocket transport and one controller session machine."""

    def __init__(
        self,
        websocket: WorkerSocket,
        *,
        durable: DurableSwwpPort,
        policy: RegistrationPolicy,
        capabilities: CapabilitySchedulerService | None = None,
        diagnostics: WorkerDiagnosticPort | None = None,
        telemetry: WorkerTelemetryMetricsPort | None = None,
        version_support: ControllerVersionSupport | None = None,
        limits: ProtocolLimits = DEFAULT_LIMITS,
        heartbeat_interval_seconds: int = 15,
        heartbeat_timeout_seconds: int = 45,
        lease_seconds: int = 60,
        assignment_ack_seconds: int = 10,
        controller_epoch: int = 1,
        clock: Callable[[], dt.datetime] = _utc_now,
    ) -> None:
        self.websocket: WorkerSocket = websocket
        self.clock: Callable[[], dt.datetime] = clock
        self.session: ControllerSession = ControllerSession(
            controller_epoch=controller_epoch,
            offered_subprotocols=_offered_subprotocols(websocket),
            policy=policy,
            durable=durable,
            id_factory=lambda prefix: f"{prefix}-{uuid.uuid4().hex[:20]}",
            clock=clock,
            version_support=version_support,
            limits=limits,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            heartbeat_timeout_seconds=heartbeat_timeout_seconds,
            lease_seconds=lease_seconds,
            assignment_ack_seconds=assignment_ack_seconds,
            capabilities=cast(CapabilityControllerPort | None, capabilities),
            diagnostics=diagnostics,
            telemetry=telemetry,
            require_worker_readiness=True,
        )
        self._send_lock: asyncio.Lock = asyncio.Lock()
        self._stop: asyncio.Event = asyncio.Event()
        self._registered: asyncio.Event = asyncio.Event()

    async def run(self) -> None:
        offered = _offered_subprotocols(self.websocket)
        if offered != (SUBPROTOCOL,):
            await self.websocket.close(code=1002, reason="exact SWWP/1 subprotocol required")
            return
        await self.websocket.accept(subprotocol=SUBPROTOCOL)
        receiver = asyncio.create_task(self._receive_loop(), name="swwp-worker-receiver")
        assignments = asyncio.create_task(self._assignment_loop(), name="swwp-worker-assignment-pump")
        watchdog = asyncio.create_task(self._watchdog_loop(), name="swwp-worker-watchdog")
        try:
            await receiver
        finally:
            self._stop.set()
            for task in (assignments, watchdog):
                _ = task.cancel()
            _ = await asyncio.gather(assignments, watchdog, return_exceptions=True)
            self.session.transport_lost()
            with contextlib.suppress(Exception):
                self.session.reconcile_missed_acknowledgments()
            with contextlib.suppress(Exception):
                await self.websocket.close()

    async def drain(self, *, deadline_at: dt.datetime, reason: str) -> None:
        if self.session.state is not ControllerState.ACTIVE:
            return
        await self._send(self.session.request_drain(deadline_at=deadline_at, reason=reason))

    async def _receive_loop(self) -> None:
        while not self._stop.is_set():
            message = await self.websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
            raw = message.get("bytes")
            if raw is None:
                text = message.get("text")
                if not isinstance(text, str):
                    raise ValueError("SWWP WebSocket message must contain bytes or text")
                raw = text.encode("utf-8")
            if not isinstance(raw, bytes):
                raise ValueError("SWWP WebSocket message must contain bytes or text")
            try:
                incoming = decode(raw)
            except Exception:
                await self.websocket.close(code=1002, reason="invalid SWWP message")
                return
            responses = self.session.receive(incoming)
            if self.session.state is ControllerState.ACTIVE:
                self._registered.set()
            for response in responses:
                await self._send(response)

    async def _assignment_loop(self) -> None:
        _ = await self._registered.wait()
        while not self._stop.is_set():
            if self.session.state is not ControllerState.ACTIVE:
                return
            assignment = self.session.assign_next()
            if assignment is not None:
                await self._send(assignment)
                continue
            await asyncio.sleep(0.05)

    async def _watchdog_loop(self) -> None:
        _ = await self._registered.wait()
        interval = max(0.25, min(self.session.heartbeat_timeout_seconds / 4, 5.0))
        while not self._stop.is_set():
            await asyncio.sleep(interval)
            if self.session.timed_out():
                with contextlib.suppress(Exception):
                    await self.websocket.close(code=1011, reason="worker heartbeat timeout")
                return

    async def _send(self, envelope: Envelope) -> None:
        data = encode(envelope)
        async with self._send_lock:
            await self.websocket.send_bytes(data)


@final
class LiveWorkerSessionManager:
    """Bounded controller owner for active live worker sessions."""

    def __init__(self) -> None:
        self._sessions: set[LiveWorkerSession] = set()
        self._lock: asyncio.Lock = asyncio.Lock()

    @property
    def active_count(self) -> int:
        return len(self._sessions)

    async def run(self, session: LiveWorkerSession) -> None:
        async with self._lock:
            self._sessions.add(session)
        try:
            await session.run()
        finally:
            async with self._lock:
                self._sessions.discard(session)

    async def drain(self, *, deadline_at: dt.datetime, reason: str) -> None:
        async with self._lock:
            sessions = tuple(self._sessions)
        if sessions:
            _ = await asyncio.gather(
                *(session.drain(deadline_at=deadline_at, reason=reason) for session in sessions),
                return_exceptions=True,
            )


async def run_live_worker_session(
    websocket: WorkerSocket,
    *,
    manager: LiveWorkerSessionManager,
    session_factory: Callable[[WorkerSocket], LiveWorkerSession],
) -> None:
    """Run one framework-adapted WebSocket session."""

    try:
        session = session_factory(websocket)
    except Exception:
        await websocket.close(code=1013, reason="SWWP worker service unavailable")
        return
    await manager.run(session)


def install_live_worker_route(
    app: object,
    *,
    endpoint: Callable[..., Awaitable[None]],
    path: str,
) -> None:
    """Attach a framework-owned route to the framework-neutral session runner."""

    router = cast(LiveWorkerRouteApp, app)
    router.add_api_websocket_route(path, endpoint, name="swwp_worker_connect")


__all__ = [
    "LiveWorkerSession",
    "LiveWorkerSessionManager",
    "LiveWorkerRouteApp",
    "PolicyFactory",
    "WorkerSocket",
    "install_live_worker_route",
    "run_live_worker_session",
]
