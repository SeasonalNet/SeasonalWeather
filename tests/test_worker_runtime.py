from __future__ import annotations

import asyncio
import datetime as dt
import os
import stat
from collections import deque
from pathlib import Path
from typing import cast

from seasonalweather.jobs.policies import ExecutorClass, JobType, QueueClass
from seasonalweather.swwp.codec import decode, encode
from seasonalweather.swwp.constants import SUBPROTOCOL
from seasonalweather.swwp.messages import (
    Envelope,
    JobAssignmentPayload,
    JobResult,
    LeaseRef,
    Payload,
    Registered,
    SelectedVersions,
)
from seasonalweather.swwp.worker import WorkerSession
from seasonalweather.worker.cli import main as worker_main
from seasonalweather.worker.handlers import HandlerContext, HandlerRegistry, HandlerResult
from seasonalweather.worker.profiles import (
    WorkerProfile,
    capability_manifest,
    profile_spec,
    registration_for_profile,
)
from seasonalweather.worker.runtime import WorkerRuntime


def test_profile_manifest_fails_closed_when_dependencies_are_unavailable() -> None:
    manifest = capability_manifest(WorkerProfile.PIPER, dependency_probe=lambda _: False)

    tts = next(record for record in manifest.records if record.name == "tts.synthesis.v1")
    assert tts.implemented is False
    assert tts.accepting_new_jobs is False
    assert tts.reported_available == 0
    assert tts.operational_state.value == "unavailable"


def test_reference_handlers_do_not_publish_executable_capabilities() -> None:
    handlers = HandlerRegistry.for_profile(WorkerProfile.MAINTENANCE.value)
    registration = registration_for_profile(
        WorkerProfile.MAINTENANCE,
        worker_id="maintenance-worker-1",
        dependency_probe=lambda _: True,
        handler_ready=handlers.ready,
    )

    record = registration.capability_manifest.records[0]
    assert handlers.ready is False
    assert record.implemented is False
    assert record.total_capacity == 0
    assert record.accepting_new_jobs is False
    assert record.reported_available == 0


def test_registration_carries_diagnostic_and_capability_compatibility_stamps() -> None:
    registration = registration_for_profile(
        WorkerProfile.MAINTENANCE,
        worker_id="maintenance-worker-1",
        dependency_probe=lambda _: True,
        handler_ready=True,
    )

    assert registration.supported_versions.swwp == (1,)
    assert registration.supported_versions.diagnostics == (1,)
    assert registration.supported_versions.capability_manifest == (1,)
    assert registration.capability_manifest.schema_version == 1
    assert registration.capability_manifest.digest.startswith("sha256:")


def test_worker_cli_requires_an_outbound_controller_url() -> None:
    try:
        worker_main(["--profile", "maintenance"])
    except SystemExit as exc:
        assert "--controller-url is required" in str(exc)
    else:
        raise AssertionError("worker CLI must reject an empty controller URL")


def test_worker_cli_resolves_the_mounted_worker_secret(tmp_path: Path, monkeypatch) -> None:
    secret = tmp_path / "SEASONAL_WORKER_TOKEN"
    secret.write_text("worker-secret\n", encoding="utf-8")
    secret.chmod(stat.S_IRUSR)
    monkeypatch.setenv("SEASONALWEATHER_SECRET_DIR", str(tmp_path))
    monkeypatch.delenv("SEASONALWEATHER_WORKER_TOKEN", raising=False)

    from seasonalweather.worker import cli

    assert cli._worker_token() == "worker-secret"
    assert os.environ.get("SEASONALWEATHER_WORKER_TOKEN") is None


def test_registration_advertises_only_profile_jobs_and_queues() -> None:
    registration = registration_for_profile(
        WorkerProfile.MAINTENANCE,
        worker_id="maintenance-worker-1",
        dependency_probe=lambda _: True,
    )
    spec = profile_spec(WorkerProfile.MAINTENANCE)

    assert registration.requested_queues == spec.queues
    assert tuple(registration.supported_versions.job_payloads) == spec.job_types
    assert registration.capability_manifest.names == ("maintenance.reconcile.v1",)


class _FakeConnection:
    def __init__(self, inbound: list[bytes | None]) -> None:
        self.inbound = deque(inbound)
        self.sent: list[bytes] = []

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def recv(self) -> bytes | None:
        await asyncio.sleep(0)
        return self.inbound.popleft()

    async def close(self) -> None:
        return None


class _FakeTransport:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    async def connect(self) -> _FakeConnection:
        return self.connection


class _MaintenanceHandler:
    async def execute(self, assignment: JobAssignmentPayload, context: HandlerContext) -> HandlerResult:
        context.check()
        return HandlerResult(
            {
                "target": assignment.payload["target"],
                "inspected_count": 1,
                "changed_count": 0,
            }
        )


def _controller_envelope(payload: Payload, *, message_id: str, worker_id: str) -> Envelope:
    return Envelope(
        message_type=payload.message_type,
        message_id=message_id,
        sent_at=dt.datetime.now(dt.UTC),
        session_id="session-1",
        worker_id=worker_id,
        worker_instance_id="instance-1",
        controller_epoch=1,
        worker_epoch=1,
        payload=payload,
    )


def test_runtime_registers_executes_handler_and_emits_typed_result() -> None:
    registration = registration_for_profile(
        WorkerProfile.MAINTENANCE,
        worker_id="maintenance-worker-1",
        worker_instance_id="instance-1",
        dependency_probe=lambda _: True,
    )
    session = WorkerSession(
        registration=registration,
        id_factory=lambda prefix: f"{prefix}-id",
        clock=lambda: dt.datetime.now(dt.UTC),
    )
    registered = Registered(
        session_id="session-1",
        controller_epoch=1,
        selected_subprotocol=SUBPROTOCOL,
        heartbeat_interval_seconds=30,
        heartbeat_timeout_seconds=60,
        lease_seconds=60,
        assignment_ack_seconds=10,
        accepted_queues=(QueueClass.MAINTENANCE,),
        authorized_job_types=(JobType.MAINTENANCE_RECONCILE,),
        authorized_capabilities=("maintenance.reconcile.v1",),
        selected_versions=SelectedVersions(
            swwp=1,
            job_payloads={JobType.MAINTENANCE_RECONCILE: 1},
            job_results={JobType.MAINTENANCE_RECONCILE: 1},
            diagnostics=1,
            capability_manifest=1,
            configuration_schema=1,
        ),
        max_message_bytes=65_536,
        max_active_assignments=1,
        effective_capabilities=("maintenance.reconcile.v1",),
        capability_epoch=1,
        capability_digest=registration.capability_manifest.digest,
    )
    now = dt.datetime.now(dt.UTC)
    assignment = JobAssignmentPayload(
        lease=LeaseRef(job_id="job-1", lease_id="lease-1", attempt_id="attempt-1", attempt=1),
        deadline_at=now + dt.timedelta(seconds=30),
        lease_expires_at=now + dt.timedelta(seconds=20),
        acknowledgment_deadline_at=now + dt.timedelta(seconds=5),
        job_type=JobType.MAINTENANCE_RECONCILE,
        queue=QueueClass.MAINTENANCE,
        executor=ExecutorClass.MAINTENANCE_WORKER,
        payload_schema_version=1,
        result_schema_version=1,
        payload={"target": "station_feed"},
        capability_requirements=("maintenance.reconcile.v1",),
    )
    connection = _FakeConnection(
        [
            encode(_controller_envelope(registered, message_id="registered-1", worker_id=registration.worker_id)),
            encode(_controller_envelope(assignment, message_id="job-1", worker_id=registration.worker_id)),
            None,
        ]
    )
    handlers = HandlerRegistry({JobType.MAINTENANCE_RECONCILE: _MaintenanceHandler()})
    session.assignment_acceptor = handlers.supports

    asyncio.run(WorkerRuntime(session, handlers, _FakeTransport(connection)).run())

    sent = [decode(item).payload for item in connection.sent]
    assert [item.message_type for item in sent] == ["register", "job_accepted", "job_result"]
    result = cast(JobResult, sent[-1])
    assert result.result == {"target": "station_feed", "inspected_count": 1, "changed_count": 0}
