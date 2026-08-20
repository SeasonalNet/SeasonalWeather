from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path
from typing import cast

from seasonalweather.build_metadata import current_build_info
from seasonalweather.health_cli import main as health_main
from seasonalweather.lifecycle import Lifecycle
from seasonalweather.lifecycle_records import LifecycleRecordWriter, LifecycleStage
from seasonalweather.swwp.constants import WorkerReadinessState, WorkerState
from seasonalweather.swwp.messages import Drain
from seasonalweather.swwp.worker import WorkerSession
from seasonalweather.worker.handlers import HandlerRegistry
from seasonalweather.worker.health import WorkerHealthStore, read_health
from seasonalweather.worker.profiles import WorkerProfile, registration_for_profile
from seasonalweather.worker.runtime import WorkerRuntime
from seasonalweather.worker.transport import WorkerTransport


def test_startup_readiness_is_not_implied_by_running_state() -> None:
    lifecycle = Lifecycle()
    lifecycle.mark_running(startup_complete=False)

    assert lifecycle.ready is True
    assert lifecycle.startup_ready is False
    lifecycle.mark_startup_complete()
    assert lifecycle.startup_ready is True


def test_lifecycle_records_are_structured_and_bounded() -> None:
    output: list[str] = []
    writer = LifecycleRecordWriter(
        role="controller",
        instance_id="controller-test-001",
        build_info=current_build_info(),
        output=output.append,
        clock=lambda: dt.datetime(2026, 8, 20, 12, 0, tzinfo=dt.UTC),
    )

    writer.startup_identity(image_profile="controller")
    writer.stage(LifecycleStage.SERVICE_READY, ready=True, reason="all-critical-paths-ready")

    identity = json.loads(output[0])
    ready = json.loads(output[1])
    assert identity["event"] == "startup_identity"
    assert identity["role"] == "controller"
    assert identity["source_revision"]
    assert "environment" not in identity
    assert ready == {
        "event": "service_ready",
        "instance_id": "controller-test-001",
        "observed_at": "2026-08-20T12:00:00Z",
        "ready": True,
        "reason": "all-critical-paths-ready",
        "role": "controller",
    }
    assert writer.last_stage is LifecycleStage.SERVICE_READY


def test_worker_controller_drain_updates_readiness_and_health(tmp_path: Path) -> None:
    registration = registration_for_profile(
        WorkerProfile.MAINTENANCE,
        worker_id="maintenance-worker-1",
        worker_instance_id="instance-1",
        dependency_probe=lambda _: True,
        handler_ready=True,
    )
    session = WorkerSession(
        registration=registration,
        id_factory=lambda prefix: f"{prefix}-id",
        clock=lambda: dt.datetime.now(dt.UTC),
    )
    session.state = WorkerState.ACTIVE
    session.set_readiness(WorkerReadinessState.READY, ready=True, accepting_new_jobs=True)
    output: list[str] = []
    records = LifecycleRecordWriter(
        role="worker",
        instance_id="instance-1",
        build_info=current_build_info(),
        output=output.append,
    )
    runtime = WorkerRuntime(
        session,
        HandlerRegistry.for_profile(WorkerProfile.MAINTENANCE.value),
        transport=cast(WorkerTransport, object()),
        health_file=str(tmp_path / "worker-health.json"),
        records=records,
    )

    asyncio.run(
        runtime._handle_payload(
            Drain(
                deadline_at=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=30),
                reason="controller_shutdown",
            )
        )
    )

    assert session.readiness_state is WorkerReadinessState.DRAINING
    assert session.ready is False
    assert session.accepting_new_jobs is False
    assert runtime._stop.is_set()
    assert records.last_stage is LifecycleStage.SERVICE_DRAINING
    assert read_health(tmp_path / "worker-health.json")[1] == "worker_draining"


def test_worker_health_store_supports_readiness_and_liveness(tmp_path: Path) -> None:
    path = tmp_path / "worker-health.json"
    now = dt.datetime.now(dt.UTC)
    store = WorkerHealthStore(path, clock=lambda: now)
    store.write(
        state="ready",
        ready=True,
        registered=True,
        accepting_new_jobs=True,
        active_leases=0,
        reason="registered",
    )

    assert read_health(path, now=now) == (True, "worker_ready")
    assert health_main(["worker", "--file", str(path), "--mode", "readiness"]) == 0
    assert health_main(["worker", "--file", str(path), "--mode", "liveness"]) == 0

    store.write(
        state="stopped",
        ready=False,
        registered=False,
        accepting_new_jobs=False,
        active_leases=0,
        reason="stopped",
    )
    assert read_health(path, now=now) == (False, "worker_stopped")
    assert health_main(["worker", "--file", str(path), "--mode", "liveness"]) == 1


def test_worker_health_stale_record_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "worker-health.json"
    store = WorkerHealthStore(
        path,
        clock=lambda: dt.datetime(2026, 8, 20, 12, 0, tzinfo=dt.UTC),
    )
    store.write(
        state="ready",
        ready=True,
        registered=True,
        accepting_new_jobs=True,
        active_leases=0,
        reason="registered",
    )

    assert read_health(path, now=dt.datetime(2026, 8, 20, 12, 3, tzinfo=dt.UTC)) == (
        False,
        "health_record_stale",
    )


def test_health_command_rejects_missing_worker_record(tmp_path: Path) -> None:
    assert health_main(["worker", "--file", str(tmp_path / "missing.json")]) == 1


def test_lifecycle_shutdown_remains_intentional_after_startup_gate() -> None:
    async def exercise() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running(startup_complete=False)
        lifecycle.mark_startup_complete()
        assert lifecycle.request_shutdown() is True
        assert lifecycle.startup_ready is False

    asyncio.run(exercise())
