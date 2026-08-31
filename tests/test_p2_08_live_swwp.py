from __future__ import annotations

import datetime as dt
import itertools
from importlib import import_module
from typing import Any, cast

from seasonalweather.api.api import create_app
from seasonalweather.api.worker_sessions import LiveWorkerSession, LiveWorkerSessionManager, WorkerSocket
from seasonalweather.control import OrchestratorControl
from seasonalweather.jobs.policies import JobType, QueueClass
from seasonalweather.swwp.adapter import DurableSwwpPort
from seasonalweather.swwp.auth import (
    AuthenticatedPrincipal,
    BearerTokenRegistrationPolicy,
    StaticRegistrationPolicy,
)
from seasonalweather.swwp.codec import decode, encode
from seasonalweather.swwp.constants import SUBPROTOCOL, WorkerReadinessState
from seasonalweather.swwp.messages import HeartbeatAck, Registered
from seasonalweather.swwp.worker import WorkerSession
from seasonalweather.worker.profiles import WorkerProfile, registration_for_profile


def _test_client(app: object) -> Any:
    testclient_module = cast(Any, import_module("fastapi.testclient"))
    return testclient_module.TestClient(app)


class _Control:
    async def get_status(self) -> dict[str, object]:
        return {"ok": True}


class _NoJobs:
    def acquire(self, **_kwargs: object) -> None:
        return None


def test_controller_accepts_a_real_websocket_worker_session() -> None:
    registration = registration_for_profile(
        WorkerProfile.MAINTENANCE,
        worker_id="maintenance-worker-001",
        dependency_probe=lambda _spec: True,
        handler_ready=True,
    )
    message_ids = itertools.count(1)
    worker = WorkerSession(
        registration=registration,
        id_factory=lambda prefix: f"{prefix}-{next(message_ids):020d}",
        clock=lambda: dt.datetime(2026, 8, 20, tzinfo=dt.UTC),
    )
    principal = AuthenticatedPrincipal(
        principal_id="worker-principal-001",
        worker_id=registration.worker_id,
        enabled=True,
        revoked=False,
        expires_at=None,
        queues=frozenset({QueueClass.MAINTENANCE}),
        job_types=frozenset({JobType.MAINTENANCE_RECONCILE}),
        capabilities=frozenset({"maintenance.reconcile.v1"}),
    )
    manager = LiveWorkerSessionManager()

    def factory(websocket: WorkerSocket) -> LiveWorkerSession:
        return LiveWorkerSession(
            websocket,
            durable=cast(DurableSwwpPort, cast(object, _NoJobs())),
            policy=StaticRegistrationPolicy(principal),
            controller_epoch=1,
            clock=lambda: dt.datetime(2026, 8, 20, tzinfo=dt.UTC),
        )

    app = create_app(
        cast(OrchestratorControl, cast(object, _Control())),
        swwp_manager=manager,
        swwp_session_factory=factory,
    )

    with (
        _test_client(app) as client,
        client.websocket_connect(
            "/v1/workers/connect",
            subprotocols=[SUBPROTOCOL],
        ) as websocket,
    ):
        websocket.send_bytes(encode(worker.connect()))
        registered = decode(websocket.receive_bytes())
        assert isinstance(registered.payload, Registered)
        worker.receive(registered)
        worker.set_readiness(
            WorkerReadinessState.READY,
            ready=True,
            accepting_new_jobs=True,
        )
        websocket.send_bytes(encode(worker.heartbeat()))
        heartbeat_ack = decode(websocket.receive_bytes())
        assert isinstance(heartbeat_ack.payload, HeartbeatAck)
        websocket.close()
        closed = websocket.receive()
        assert closed["type"] == "websocket.close"


def test_live_route_rejects_an_unoffered_or_ambiguous_subprotocol() -> None:
    manager = LiveWorkerSessionManager()

    def factory(websocket: WorkerSocket) -> LiveWorkerSession:
        return LiveWorkerSession(
            websocket,
            durable=cast(DurableSwwpPort, cast(object, _NoJobs())),
            policy=StaticRegistrationPolicy(None),
        )

    app = create_app(
        cast(OrchestratorControl, cast(object, _Control())),
        swwp_manager=manager,
        swwp_session_factory=factory,
    )
    with _test_client(app) as client:
        try:
            with client.websocket_connect("/v1/workers/connect"):
                pass
        except Exception as exc:
            assert getattr(exc, "code", None) == 1002
        else:
            raise AssertionError("a worker connection without SWWP/1 must be rejected")


def test_worker_bearer_auth_is_transport_only_and_fails_closed() -> None:
    policy = BearerTokenRegistrationPolicy(
        expected_token="worker-secret",
        presented_token="worker-secret",
        queues=frozenset({QueueClass.ROUTINE}),
        job_types=frozenset({JobType.TTS_SYNTHESIZE}),
        capabilities=frozenset({"tts.synthesis.v1"}),
    )
    registration = registration_for_profile(
        WorkerProfile.PIPER,
        worker_id="piper-worker-001",
        dependency_probe=lambda _spec: True,
    )
    principal = policy.authorize(registration, dt.datetime.now(dt.UTC))
    assert principal.principal_id == "worker-bearer-token"
    assert principal.worker_id == registration.worker_id
    assert policy.principal() is None

    try:
        BearerTokenRegistrationPolicy(
            expected_token="worker-secret",
            presented_token="wrong-secret",
            queues=policy.queues,
            job_types=policy.job_types,
            capabilities=policy.capabilities,
        ).authorize(registration, dt.datetime.now(dt.UTC))
    except PermissionError:
        pass
    else:
        raise AssertionError("invalid worker bearer token was accepted")
