from __future__ import annotations

import asyncio
import datetime as dt
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from seasonalweather.api.api import create_app
from seasonalweather.api.auth import ApiPrincipal, get_api_principal
from seasonalweather.commands.service import CommandStore, IdempotencyConflictError
from seasonalweather.configuration import build_runtime_config, compile_path
from seasonalweather.configuration_reload.candidate_store import CandidateIntegrityError, CandidateStore
from seasonalweather.configuration_reload.models import (
    ReloadDisposition,
    ReloadOutcome,
    ReloadPhase,
    ReloadRequest,
    WarningAcknowledgment,
)
from seasonalweather.configuration_reload.safe_point import TTS, ActivityRegistry, SafePointCoordinator
from seasonalweather.configuration_reload import service as reload_service_module
from seasonalweather.configuration_reload.service import (
    ConfigurationReloadService,
    PostCommitRecoveryRequired,
    ReloadCancelled,
    ReloadRejected,
)
from seasonalweather.configuration_reload.validation_job import ValidationJobExecutionError, ValidationJobRunner
from seasonalweather.database.configuration_reload import ReloadRepository, StaleReloadError
from seasonalweather.database.core import SeasonalDatabase
from seasonalweather.job_store import (
    DurableJobService,
    JobDatabase,
    JobRepository,
    JobStoreConflictError,
    StaleJobMutationError,
)
from seasonalweather.jobs.policies import FailureCategory
from seasonalweather.lifecycle import Lifecycle
from seasonalweather.validation.preflight import ProbeObservation, ProbeStatus

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "config/config.yaml"
NOW = dt.datetime(2026, 8, 1, 12, tzinfo=dt.UTC)


class HealthyPreflight:
    async def observe(self, probe, monotonic):
        del probe, monotonic
        return ProbeObservation(ProbeStatus.AVAILABLE, "available"), None


class OptionalWarningPreflight:
    async def observe(self, probe, monotonic):
        del monotonic
        if probe.identifier == "api.ffmpeg":
            return ProbeObservation(ProbeStatus.DEGRADED, "optional dependency is degraded"), None
        return ProbeObservation(ProbeStatus.AVAILABLE, "available"), None


class MutablePreflight(OptionalWarningPreflight):
    unavailable = False

    async def observe(self, probe, monotonic):
        if self.unavailable:
            return ProbeObservation(ProbeStatus.UNAVAILABLE, "dependency is unavailable"), None
        return await super().observe(probe, monotonic)


class DeferredSupervisor:
    def __init__(self) -> None:
        self.coroutines: list[Any] = []

    def create_task(self, coroutine, **_kwargs):
        self.coroutines.append(coroutine)
        return coroutine


@dataclass
class FakePlan:
    holder: dict[str, Any]
    configuration: Any
    expected_generation: int
    target_generation: int
    candidate_identity_sha256: str
    required_disposition: ReloadDisposition
    diff_sha256: str
    fail_activate: bool = False
    fail_validate: bool = False
    fail_retire: bool = False
    fail_rollback: bool = False
    activated: bool = False
    rolled_back: bool = False
    retire_count: int = 0

    def validate_ready(self) -> None:
        assert self.expected_generation >= 0
        if self.fail_validate:
            raise RuntimeError("injected preparation validation failure")

    def activate(self, *, safe_point_acquired: bool = False) -> object:
        if self.required_disposition is ReloadDisposition.QUIESCENT and not safe_point_acquired:
            raise RuntimeError("quiescent fake plan activated without a safe point")
        if self.fail_activate:
            raise RuntimeError("injected activation failure")
        self.holder["prior"] = self.holder["configuration"]
        self.holder["prior_generation"] = self.holder.get("generation", self.expected_generation)
        self.holder["configuration"] = self.configuration
        self.holder["generation"] = self.target_generation
        self.activated = True
        return self

    def rollback(self) -> None:
        if self.fail_rollback:
            raise RuntimeError("injected rollback failure")
        if self.activated and not self.rolled_back:
            self.holder["configuration"] = self.holder["prior"]
            self.holder["generation"] = self.holder["prior_generation"]
        self.rolled_back = True

    async def retire(self) -> None:
        self.retire_count += 1
        if self.fail_retire:
            raise RuntimeError("injected retirement failure")


class FakePreparer:
    def __init__(self, holder: dict[str, Any]) -> None:
        self.holder = holder
        self.fail_activate = False
        self.fail_validate = False
        self.fail_retire = False
        self.fail_prepare = False
        self.fail_rollback = False
        self.barrier: asyncio.Barrier | None = None
        self.plans: list[FakePlan] = []
        self.force_disposition: ReloadDisposition | None = None

    async def prepare(
        self,
        configuration,
        *,
        diff,
        expected_generation,
        target_generation,
        candidate_identity_sha256,
    ):
        if self.fail_prepare:
            raise RuntimeError("injected preparation failure")
        if self.barrier is not None:
            await self.barrier.wait()
        plan = FakePlan(
            self.holder,
            configuration,
            expected_generation,
            target_generation,
            candidate_identity_sha256,
            self.force_disposition or diff.disposition,
            diff.digest,
            fail_activate=self.fail_activate,
            fail_validate=self.fail_validate,
            fail_retire=self.fail_retire,
            fail_rollback=self.fail_rollback,
        )
        self.plans.append(plan)
        return plan

    def synchronize_generation(self, generation: int) -> None:
        self.holder["generation"] = generation


def _write_candidate(tmp_path: Path, old: str, new: str, *, name: str = "candidate.yaml") -> Path:
    path = tmp_path / name
    path.write_text(EXAMPLE.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")
    return path


def _service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    preflight: Any = None,
    failure_injector=lambda _point: None,
    diagnostic_promoter=lambda _code, _component, _error: None,
    config_path: Path = EXAMPLE,
    clock=lambda: NOW,
) -> tuple[ConfigurationReloadService, CommandStore, FakePreparer, DurableJobService]:
    monkeypatch.setenv("ICECAST_SOURCE_PASSWORD", "synthetic-icecast-password")
    environment = {
        "ICECAST_SOURCE_PASSWORD": "synthetic-icecast-password",
        "SEASONAL_API_TOKEN": "synthetic-test-api-token",
    }
    lifecycle = Lifecycle()
    lifecycle.mark_running()
    operational = SeasonalDatabase(path=str(tmp_path / "operational.sqlite3"))
    operational.bootstrap()
    commands = CommandStore(database=operational, lifecycle=lifecycle, clock=clock)
    jobs = DurableJobService(
        JobRepository(JobDatabase(path=str(tmp_path / "jobs.sqlite3"), busy_timeout_ms=2000)),
        lifecycle,
        reconciliation_batch_size=20,
        clock=clock,
    )
    jobs.initialize()
    store = CandidateStore(
        tmp_path / "candidates",
        environ=environment,
        clock=clock,
        identity_key=b"reload-test-key" * 3,
    )
    active_compiled = compile_path(config_path, environ=environment)
    active_config = build_runtime_config(active_compiled, environ=environment)
    holder = {"configuration": active_config}
    preparer = FakePreparer(holder)
    activities = ActivityRegistry()
    next_id = iter(f"{value:024x}" for value in range(1, 1000))
    service = ConfigurationReloadService(
        config_path=str(config_path),
        candidate_store=store,
        repository=ReloadRepository(operational),
        command_store=commands,
        validation_jobs=ValidationJobRunner(
            store,
            jobs,
            preflight_executor=preflight or HealthyPreflight(),
            clock=clock,
        ),
        resource_preparer=preparer,
        safe_points=SafePointCoordinator(activities, poll_interval_seconds=0.001),
        active_configuration=active_config,
        environ=environment,
        clock=clock,
        id_factory=lambda: next(next_id),
        failure_injector=failure_injector,
        diagnostic_promoter=diagnostic_promoter,
    )
    return service, commands, preparer, jobs


async def _execute(service, commands, request, *, key="reload-key"):
    record, replayed, admitted = await service._admit_durable(request, idempotency_key=key)
    assert not replayed
    return await service.execute_command(record.command_id, admitted), record


def test_live_commit_increments_generation_once_and_validation_job_is_durable(tmp_path: Path, monkeypatch) -> None:
    service, commands, preparer, jobs = _service(tmp_path, monkeypatch)
    candidate = _write_candidate(
        tmp_path,
        "dedupe:\n  ttl_seconds: 900",
        "dedupe:\n  ttl_seconds: 901",
    )
    result, command = asyncio.run(
        _execute(service, commands, ReloadRequest(actor="operator", source_path=str(candidate)))
    )

    assert result.outcome is ReloadOutcome.COMMITTED
    assert result.old_generation == 0
    assert result.final_generation == 1
    assert service.active.generation == 1
    assert preparer.plans[0].activated
    assert len(jobs.repository.list_jobs()) == 1
    assert jobs.repository.list_jobs()[0].command_id == command.command_id
    assert asyncio.run(commands.get(command.command_id)).status.value == "succeeded"


def test_success_audit_records_real_safe_point_and_complete_bounded_lifecycle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service, commands, _preparer, _jobs = _service(tmp_path, monkeypatch)
    candidate = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"')

    async def scenario():
        request = ReloadRequest(
            actor="operator",
            reason="  planned reload  ",
            source_path=str(candidate),
            authorization_context={"kind": "static", "scopes": ("control:config",)},
        )
        command, _, admitted = await service._admit_durable(request, idempotency_key="audit-safe-point")
        with service.safe_points.registry.activity(TTS):
            task = asyncio.create_task(service.execute_command(command.command_id, admitted))
            for _ in range(500):
                row = service.repository.get_by_command(command.command_id)
                if row is not None and row["phase"] == "awaiting_safe_point":
                    break
                await asyncio.sleep(0.001)
            await asyncio.sleep(0.002)
        return command, await task

    command, result = asyncio.run(scenario())
    audit = json.loads(service.repository.get_attempt(result.attempt_id)["audit_json"])

    assert audit["command_id"] == command.command_id
    assert len(audit["idempotency_identity"]) == 64
    assert audit["actor"] == "operator"
    assert audit["auth_context"] == {"kind": "static", "scopes": ["control:config"]}
    assert audit["reason"] == "planned reload"
    assert audit["requested_at"] == audit["started_at"]
    assert audit["completed_at"] >= audit["started_at"]
    assert audit["duration_seconds"] >= 0
    assert audit["old_candidate_identity_sha256"]
    assert audit["old_candidate_reference"]
    assert len(audit["old_source_sha256"]) == 64
    assert len(audit["candidate_source_sha256"]) == 64
    assert audit["candidate_byte_length"] > 0
    assert len(audit["candidate_source_manifest_sha256"]) == 64
    assert audit["report_sha256"] == result.report_sha256
    assert len(audit["validator_stamp_identity"]) == 64
    assert audit["reload_policy_version"] == 1
    assert audit["safe_point"]["outcome"] == "acquired"
    assert audit["safe_point"]["blockers"] == []
    assert audit["safe_point"]["waited_seconds"] > 0
    assert audit["lifecycle_states"] == {
        "activation": "completed",
        "preparation": "completed",
        "reconciliation": "not_required",
        "retirement": "completed",
        "rollback": "not_required",
    }


def test_restart_required_and_dry_run_never_prepare_or_increment(tmp_path: Path, monkeypatch) -> None:
    service, commands, preparer, _jobs = _service(tmp_path, monkeypatch)
    restart = _write_candidate(
        tmp_path,
        'path: "/var/lib/seasonalweather/state/seasonalweather.sqlite3"',
        'path: "/tmp/replacement.sqlite3"',
        name="restart.yaml",
    )
    result, _ = asyncio.run(
        _execute(service, commands, ReloadRequest(actor="operator", source_path=str(restart)), key="restart")
    )
    assert result.outcome is ReloadOutcome.RESTART_REQUIRED
    assert service.active.generation == 0
    assert not preparer.plans

    dry = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"', name="dry.yaml")
    result, _ = asyncio.run(
        _execute(
            service,
            commands,
            ReloadRequest(actor="operator", dry_run=True, source_path=str(dry)),
            key="dry",
        )
    )
    assert result.outcome is ReloadOutcome.DRY_RUN
    assert service.active.generation == 0
    assert not preparer.plans


@pytest.mark.parametrize("case", ("noop", "dry_run", "restart_required", "acknowledgment_required"))
def test_task_cancellation_prevents_every_report_only_success(
    tmp_path: Path,
    monkeypatch,
    case: str,
) -> None:
    preflight = OptionalWarningPreflight() if case == "acknowledgment_required" else None
    service, commands, preparer, _jobs = _service(tmp_path, monkeypatch, preflight=preflight)
    if case == "noop":
        source = EXAMPLE
        dry_run = False
    elif case == "restart_required":
        source = _write_candidate(
            tmp_path,
            'path: "/var/lib/seasonalweather/state/seasonalweather.sqlite3"',
            'path: "/tmp/cancelled-restart.sqlite3"',
        )
        dry_run = False
    else:
        source = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"')
        dry_run = case == "dry_run"
    entered = asyncio.Event()
    release = asyncio.Event()
    original = service._report_only_result

    async def paused(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(service, "_report_only_result", paused)

    async def scenario():
        request = ReloadRequest(actor="operator", source_path=str(source), dry_run=dry_run)
        command, _, admitted = await service._admit_durable(request, idempotency_key=f"cancel-{case}")
        task = asyncio.create_task(service.execute_command(command.command_id, admitted))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return command

    command = asyncio.run(scenario())
    assert asyncio.run(commands.get(command.command_id)).status.value == "cancelled"
    assert service.active.generation == 0
    assert not preparer.plans


def test_durable_cancellation_is_checked_after_capture_validation_and_preparation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service, commands, preparer, _jobs = _service(tmp_path, monkeypatch)
    candidate = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"')

    async def after_capture() -> None:
        request = ReloadRequest(actor="operator", source_path=str(candidate))
        command, _, admitted = await service._admit_durable(request, idempotency_key="cancel-after-capture")
        await commands.request_cancellation(command.command_id)
        with pytest.raises(ReloadCancelled):
            await service.execute_command(command.command_id, admitted)

    asyncio.run(after_capture())

    original_report_only = service._report_only_result

    async def cancel_after_validation(attempt_id, request, *args, **kwargs):
        row = service.repository.get_attempt(attempt_id)
        await commands.request_cancellation(str(row["command_id"]))
        return await original_report_only(attempt_id, request, *args, **kwargs)

    monkeypatch.setattr(service, "_report_only_result", cancel_after_validation)
    with pytest.raises(ReloadCancelled):
        asyncio.run(
            _execute(
                service,
                commands,
                ReloadRequest(actor="operator", source_path=str(candidate)),
                key="cancel-after-validation",
            )
        )
    monkeypatch.setattr(service, "_report_only_result", original_report_only)

    original_prepare = preparer.prepare

    async def cancel_during_prepare(*args, **kwargs):
        plan = await original_prepare(*args, **kwargs)
        row = next(
            item
            for item in service.repository.incomplete(limit=500)
            if item["phase"] == ReloadPhase.PREPARING.value
        )
        await commands.request_cancellation(str(row["command_id"]))
        return plan

    monkeypatch.setattr(preparer, "prepare", cancel_during_prepare)
    with pytest.raises(ReloadCancelled):
        asyncio.run(
            _execute(
                service,
                commands,
                ReloadRequest(actor="operator", source_path=str(candidate)),
                key="cancel-during-prepare",
            )
        )
    assert preparer.plans[-1].rolled_back

def test_environment_secret_rotation_is_redacted_restart_required(tmp_path: Path, monkeypatch) -> None:
    service, commands, preparer, _jobs = _service(tmp_path, monkeypatch)
    environment = service.candidates._environ
    assert isinstance(environment, dict)
    environment["ICECAST_SOURCE_PASSWORD"] = "rotated-private-sentinel"

    result, _ = asyncio.run(
        _execute(service, commands, ReloadRequest(actor="operator", source_path=str(EXAMPLE)), key="secret-rotation")
    )

    assert result.outcome is ReloadOutcome.RESTART_REQUIRED
    assert result.changed_paths["restart_required"] == ("/secrets/icecast_source_password",)
    assert service.active.generation == 0
    assert not preparer.plans
    row = service.repository.get_attempt(result.attempt_id)
    assert row is not None
    assert "rotated-private-sentinel" not in row["audit_json"]
    assert "icecast_source_password" in row["audit_json"]


def test_warning_acknowledgment_is_exact_and_reuses_bound_report(tmp_path: Path, monkeypatch) -> None:
    service, commands, preparer, _jobs = _service(tmp_path, monkeypatch, preflight=OptionalWarningPreflight())
    candidate = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"')
    first, _ = asyncio.run(
        _execute(service, commands, ReloadRequest(actor="operator", source_path=str(candidate)), key="warn-1")
    )
    assert first.outcome is ReloadOutcome.ACKNOWLEDGMENT_REQUIRED
    acknowledgment = WarningAcknowledgment(
        actor="operator",
        candidate_sha256=first.candidate_sha256,
        candidate_identity_sha256=first.candidate_identity_sha256,
        report_sha256=first.report_sha256,
        active_generation=first.old_generation,
        warning_identities=first.warning_identities,
        acknowledged_at=NOW,
        validator_completed_at=NOW,
        expires_at=NOW + dt.timedelta(seconds=300),
    )
    second, _ = asyncio.run(
        _execute(
            service,
            commands,
            ReloadRequest(actor="operator", source_path=str(candidate), acknowledgment=acknowledgment),
            key="warn-2",
        )
    )
    assert second.outcome is ReloadOutcome.COMMITTED
    assert service.active.generation == 1
    assert preparer.plans[0].activated


def test_authorized_api_acknowledgment_challenge_round_trip(tmp_path: Path, monkeypatch) -> None:
    configured = tmp_path / "configured.yaml"
    configured.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    service, commands, _preparer, _jobs = _service(
        tmp_path,
        monkeypatch,
        preflight=OptionalWarningPreflight(),
        config_path=configured,
    )
    configured.write_text(
        EXAMPLE.read_text(encoding="utf-8").replace('voice: "9"', 'voice: "8"', 1),
        encoding="utf-8",
    )
    supervisor = DeferredSupervisor()
    service.supervisor = supervisor
    app = create_app(object(), store=commands, reload_service=service)

    async def principal() -> ApiPrincipal:
        return ApiPrincipal(
            subject="operator",
            scopes=frozenset({"*"}),
            client_host="127.0.0.1",
        )

    app.dependency_overrides[get_api_principal] = principal

    async def scenario() -> tuple[dict[str, object], dict[str, object]]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            accepted = await client.post(
                "/v1/config/reload",
                json={"reason": "review warning"},
                headers={"Idempotency-Key": "api-challenge-1"},
            )
            assert accepted.status_code == 202
            await supervisor.coroutines.pop(0)
            first = await client.get(f"/v1/commands/{accepted.json()['command_id']}")
            assert first.json()["result"]["details"]["outcome"] == "acknowledgment_required", first.json()
            challenge = first.json()["result"]["details"]["acknowledgment_challenge"]
            acknowledged = await client.post(
                "/v1/config/reload",
                json={"reason": "review warning", "acknowledgment": challenge},
                headers={"Idempotency-Key": "api-challenge-2"},
            )
            assert acknowledged.status_code == 202
            await supervisor.coroutines.pop(0)
            final = await client.get(f"/v1/commands/{acknowledged.json()['command_id']}")
            return challenge, final.json()

    challenge, final = asyncio.run(scenario())
    assert challenge["actor"] == "operator"
    assert challenge["warning_identities"] == sorted(challenge["warning_identities"])
    assert challenge["maximum_age_seconds"] == 300
    assert challenge["clock_skew_seconds"] == 5
    assert final["status"] == "succeeded"
    assert final["result"]["details"]["outcome"] == "committed"


def test_endpoint_secrets_never_reach_audit_diagnostics_logs_or_api(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    configured = tmp_path / "configured.yaml"
    configured.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    diagnostics: list[tuple[str, str, str]] = []
    service, commands, preparer, _jobs = _service(
        tmp_path,
        monkeypatch,
        config_path=configured,
        diagnostic_promoter=lambda code, component, error: diagnostics.append((code, component, str(error))),
    )
    configured.write_text(
        EXAMPLE.read_text(encoding="utf-8").replace(
            '  url: ""                      # leave empty to use default NWS alerts endpoint',
            '  url: "https://endpoint-user:endpoint-pass@example.invalid/path?query-token=sentinel#fragment-sentinel"',
            1,
        ),
        encoding="utf-8",
    )
    preparer.fail_prepare = True
    supervisor = DeferredSupervisor()
    service.supervisor = supervisor
    app = create_app(object(), store=commands, reload_service=service)

    async def principal() -> ApiPrincipal:
        return ApiPrincipal(subject="operator", scopes=frozenset({"*"}), client_host="127.0.0.1")

    app.dependency_overrides[get_api_principal] = principal

    async def scenario() -> tuple[dict[str, object], str]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            accepted = await client.post(
                "/v1/config/reload",
                json={"reason": "redaction proof"},
                headers={"Idempotency-Key": "endpoint-redaction"},
            )
            assert accepted.status_code == 202
            with pytest.raises(ReloadRejected):
                await supervisor.coroutines.pop(0)
            response = await client.get(f"/v1/commands/{accepted.json()['command_id']}")
            row = service.repository.get_by_command(accepted.json()["command_id"])
            assert row is not None
            return response.json(), str(row["audit_json"])

    response, audit_json = asyncio.run(scenario())
    rendered = json.dumps(
        {
            "audit": audit_json,
            "command_response": response,
            "diagnostics": diagnostics,
            "logs": caplog.text,
        }
    )
    for sentinel in ("endpoint-user", "endpoint-pass", "query-token", "fragment-sentinel"):
        assert sentinel not in rendered


def test_expired_acknowledged_report_runs_fresh_preflight_and_cannot_prepare(
    tmp_path: Path,
    monkeypatch,
) -> None:
    current = [NOW]
    preflight = MutablePreflight()
    service, commands, preparer, jobs = _service(
        tmp_path,
        monkeypatch,
        preflight=preflight,
        clock=lambda: current[0],
    )
    candidate = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"')
    first, _ = asyncio.run(
        _execute(service, commands, ReloadRequest(actor="operator", source_path=str(candidate)), key="freshness-1")
    )
    challenge = dict(first.acknowledgment_challenge or {})
    acknowledgment = WarningAcknowledgment(
        actor=str(challenge["actor"]),
        candidate_sha256=str(challenge["candidate_sha256"]),
        candidate_identity_sha256=str(challenge["candidate_identity_sha256"]),
        report_sha256=str(challenge["report_sha256"]),
        active_generation=int(challenge["active_generation"]),
        warning_identities=tuple(challenge["warning_identities"]),
        acknowledged_at=dt.datetime.fromisoformat(str(challenge["acknowledged_at"])),
        validator_completed_at=dt.datetime.fromisoformat(str(challenge["validator_completed_at"])),
        expires_at=dt.datetime.fromisoformat(str(challenge["expires_at"])),
    )
    current[0] = NOW + dt.timedelta(seconds=301)
    preflight.unavailable = True

    with pytest.raises(ReloadRejected, match="not valid and ready"):
        asyncio.run(
            _execute(
                service,
                commands,
                ReloadRequest(actor="operator", source_path=str(candidate), acknowledgment=acknowledgment),
                key="freshness-2",
            )
        )

    assert not preparer.plans
    assert len(jobs.repository.list_jobs()) == 2


def test_report_freshness_is_rechecked_under_final_commit_serialization(
    tmp_path: Path,
    monkeypatch,
) -> None:
    current = [NOW]
    service, commands, preparer, _jobs = _service(
        tmp_path,
        monkeypatch,
        preflight=OptionalWarningPreflight(),
        clock=lambda: current[0],
    )
    candidate = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"')
    first, _ = asyncio.run(
        _execute(service, commands, ReloadRequest(actor="operator", source_path=str(candidate)), key="final-age-1")
    )
    challenge = dict(first.acknowledgment_challenge or {})
    acknowledgment = WarningAcknowledgment(
        actor=str(challenge["actor"]),
        candidate_sha256=str(challenge["candidate_sha256"]),
        candidate_identity_sha256=str(challenge["candidate_identity_sha256"]),
        report_sha256=str(challenge["report_sha256"]),
        active_generation=int(challenge["active_generation"]),
        warning_identities=tuple(challenge["warning_identities"]),
        acknowledged_at=dt.datetime.fromisoformat(str(challenge["acknowledged_at"])),
        validator_completed_at=dt.datetime.fromisoformat(str(challenge["validator_completed_at"])),
        expires_at=dt.datetime.fromisoformat(str(challenge["expires_at"])),
    )

    async def scenario() -> None:
        request = ReloadRequest(actor="operator", source_path=str(candidate), acknowledgment=acknowledgment)
        command, _, admitted = await service._admit_durable(request, idempotency_key="final-age-2")
        with service.safe_points.registry.activity(TTS):
            task = asyncio.create_task(service.execute_command(command.command_id, admitted))
            for _ in range(500):
                row = service.repository.get_by_command(command.command_id)
                if row is not None and row["phase"] == "awaiting_safe_point":
                    break
                await asyncio.sleep(0.001)
            current[0] = NOW + dt.timedelta(seconds=301)
        with pytest.raises(ReloadRejected, match="freshness window"):
            await task

    asyncio.run(scenario())
    assert service.active.generation == 0
    assert preparer.plans[0].rolled_back


def test_precommit_activation_failure_rolls_back_old_generation(tmp_path: Path, monkeypatch) -> None:
    service, commands, preparer, _jobs = _service(tmp_path, monkeypatch)
    preparer.fail_activate = True
    candidate = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"')
    result, _ = asyncio.run(_execute(service, commands, ReloadRequest(actor="operator", source_path=str(candidate))))

    assert result.outcome is ReloadOutcome.ROLLED_BACK
    assert service.active.generation == 0
    assert preparer.plans[0].rolled_back


def test_after_swap_failure_requires_reconciliation_and_restores_old_resources(tmp_path: Path, monkeypatch) -> None:
    def inject(point: str) -> None:
        if point == "after_reference_swap":
            raise RuntimeError("injected ambiguous commit failure")

    service, commands, preparer, _jobs = _service(tmp_path, monkeypatch, failure_injector=inject)
    old_config = service.active.configuration
    candidate = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"')
    result, _ = asyncio.run(_execute(service, commands, ReloadRequest(actor="operator", source_path=str(candidate))))

    assert result.outcome is ReloadOutcome.RECONCILIATION_REQUIRED
    assert service.active.generation == 0
    assert preparer.holder["configuration"] is old_config


def test_idempotency_replay_and_conflict_are_owned_by_durable_command_store(tmp_path: Path, monkeypatch) -> None:
    service, commands, _preparer, _jobs = _service(tmp_path, monkeypatch)
    request = ReloadRequest(actor="operator", dry_run=True)

    async def scenario() -> None:
        record, replayed, admitted = await service._admit_durable(request, idempotency_key="same-key")
        assert not replayed
        await service.execute_command(record.command_id, admitted)
        prior, replayed, _ = await service._admit_durable(request, idempotency_key="same-key")
        assert replayed and prior.command_id == record.command_id
        with pytest.raises(IdempotencyConflictError):
            await service._admit_durable(
                ReloadRequest(actor="operator", dry_run=False),
                idempotency_key="same-key",
            )

    asyncio.run(scenario())


def test_durable_admission_executes_captured_bytes_after_configured_file_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service, commands, _preparer, _jobs = _service(tmp_path, monkeypatch)
    configured = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"')

    async def scenario():
        request = ReloadRequest(actor="operator", source_path=str(configured))
        command, _, admitted = await service._admit_durable(request, idempotency_key="captured-before-change")
        configured.write_text(
            EXAMPLE.read_text(encoding="utf-8").replace('voice: "9"', 'voice: "7"', 1),
            encoding="utf-8",
        )
        return await service.execute_command(command.command_id), admitted, command

    result, admitted, command = asyncio.run(scenario())
    assert result.outcome is ReloadOutcome.COMMITTED
    assert service.active.configuration.tts.voice == "8"
    row = service.repository.get_by_command(command.command_id)
    assert row is not None and row["candidate_reference"] == admitted.candidate.reference
    assert row["source_sha256"] == admitted.candidate.source_sha256
    assert row["source_byte_length"] == admitted.candidate.byte_length
    assert row["source_manifest_sha256"] == admitted.candidate.source_manifest_sha256


def test_idempotency_conflicts_when_candidate_bytes_change_and_exact_capture_replays(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service, _commands, _preparer, _jobs = _service(tmp_path, monkeypatch)
    configured = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"')
    request = ReloadRequest(actor="operator", dry_run=True, source_path=str(configured))

    async def scenario() -> None:
        first, replayed, admitted = await service._admit_durable(request, idempotency_key="candidate-key")
        assert not replayed
        exact, replayed, exact_admission = await service._admit_durable(request, idempotency_key="candidate-key")
        assert replayed and exact.command_id == first.command_id
        assert exact_admission.candidate == admitted.candidate
        configured.write_text(
            EXAMPLE.read_text(encoding="utf-8").replace('voice: "9"', 'voice: "7"', 1),
            encoding="utf-8",
        )
        with pytest.raises(IdempotencyConflictError):
            await service._admit_durable(request, idempotency_key="candidate-key")

    asyncio.run(scenario())


def test_startup_resumes_captured_command_without_recapturing_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    first, _commands, _preparer, _jobs = _service(tmp_path, monkeypatch)
    configured = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"')

    async def admit_only():
        return await first._admit_durable(
            ReloadRequest(actor="operator", dry_run=True, source_path=str(configured)),
            idempotency_key="restart-before-validation",
        )

    command, _, admitted = asyncio.run(admit_only())
    configured.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    restarted, restarted_commands, _preparer, _jobs = _service(tmp_path, monkeypatch)
    repaired = asyncio.run(restarted.reconcile_startup())

    assert restarted.repository.get_by_command(command.command_id)["candidate_reference"] == admitted.candidate.reference
    assert asyncio.run(restarted_commands.get(command.command_id)).status.value == "succeeded"
    assert restarted.repository.get_by_command(command.command_id)["outcome"] == ReloadOutcome.DRY_RUN.value
    assert repaired


def test_durable_command_before_attempt_failure_replays_exact_journal_without_recapture(
    tmp_path: Path,
    monkeypatch,
) -> None:
    armed = {"value": True}

    def inject(point: str) -> None:
        if point == "after_durable_command_before_reload_attempt" and armed["value"]:
            armed["value"] = False
            raise RuntimeError("injected admission window failure")

    service, commands, _preparer, _jobs = _service(tmp_path, monkeypatch, failure_injector=inject)
    configured = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"')
    request = ReloadRequest(actor="operator", dry_run=True, source_path=str(configured))

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="admission window"):
            await service._admit_durable(request, idempotency_key="admission-window")
        journal = service.repository.get_admission("admission-window")
        assert journal is not None and service.repository.get_by_command(str(journal["command_id"])) is None
        exact_reference = str(journal["candidate_reference"])
        exact_bytes = service.candidates.read_bytes(service.candidates.load(exact_reference))
        configured.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        original_capture = service.candidates.capture
        monkeypatch.setattr(service.candidates, "capture", lambda *_args, **_kwargs: pytest.fail("recaptured"))
        record, replayed, admitted = await service._admit_durable(request, idempotency_key="admission-window")
        monkeypatch.setattr(service.candidates, "capture", original_capture)
        assert replayed and admitted.candidate is not None
        assert admitted.candidate.reference == exact_reference
        assert service.candidates.read_bytes(service.candidates.load(exact_reference)) == exact_bytes
        result = await service.execute_command(record.command_id)
        assert result.outcome is ReloadOutcome.DRY_RUN

    asyncio.run(scenario())


def test_durable_admission_journal_repairs_after_process_restart_without_recapture(
    tmp_path: Path,
    monkeypatch,
) -> None:
    armed = {"value": True}

    def inject(point: str) -> None:
        if point == "after_durable_command_before_reload_attempt" and armed["value"]:
            armed["value"] = False
            raise RuntimeError("injected restart admission window")

    first, _commands, _preparer, _jobs = _service(tmp_path, monkeypatch, failure_injector=inject)
    configured = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"')
    request = ReloadRequest(actor="operator", dry_run=True, source_path=str(configured))

    with pytest.raises(RuntimeError, match="restart admission window"):
        asyncio.run(first._admit_durable(request, idempotency_key="restart-admission-window"))
    journal = first.repository.get_admission("restart-admission-window")
    assert journal is not None
    exact_reference = str(journal["candidate_reference"])
    configured.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")

    restarted, restarted_commands, _preparer, _jobs = _service(tmp_path, monkeypatch)
    original_capture = restarted.candidates.capture
    monkeypatch.setattr(restarted.candidates, "capture", lambda *_args, **_kwargs: pytest.fail("recaptured"))
    repaired = asyncio.run(restarted.reconcile_startup())
    monkeypatch.setattr(restarted.candidates, "capture", original_capture)

    assert "reload_000000000000000000000001" in repaired
    row = restarted.repository.get_by_command(str(journal["command_id"]))
    assert row is not None and row["candidate_reference"] == exact_reference
    assert row["outcome"] == ReloadOutcome.DRY_RUN.value
    assert asyncio.run(restarted_commands.get(str(journal["command_id"]))).status.value == "succeeded"


def test_startup_reconciliation_rehydrates_durable_generation_idempotently(tmp_path: Path, monkeypatch) -> None:
    first, commands, _preparer, _jobs = _service(tmp_path, monkeypatch)
    candidate = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"')
    committed, _ = asyncio.run(_execute(first, commands, ReloadRequest(actor="operator", source_path=str(candidate))))
    assert committed.final_generation == 1

    restarted, _commands, preparer, _jobs = _service(tmp_path, monkeypatch)
    assert restarted.active.generation == 1
    assert preparer.holder["configuration"] is not restarted.active.configuration
    assert asyncio.run(restarted.reconcile_startup()) == ()
    assert preparer.holder["configuration"] is restarted.active.configuration
    assert preparer.plans[0].activated
    assert preparer.plans[0].retire_count == 1
    assert asyncio.run(restarted.reconcile_startup()) == ()
    assert len(preparer.plans) == 1


def test_startup_synchronizes_identical_durable_generation_without_preparation(tmp_path: Path, monkeypatch) -> None:
    first, commands, _preparer, _jobs = _service(tmp_path, monkeypatch)
    candidate = _write_candidate(
        tmp_path,
        "dedupe:\n  ttl_seconds: 900",
        "dedupe:\n  ttl_seconds: 901",
    )
    committed, _ = asyncio.run(
        _execute(first, commands, ReloadRequest(actor="operator", source_path=str(candidate)))
    )
    assert committed.final_generation == 1

    restarted, _commands, preparer, _jobs = _service(tmp_path, monkeypatch, config_path=candidate)
    assert restarted.active.generation == 1
    assert not preparer.plans
    assert asyncio.run(restarted.reconcile_startup()) == ()
    assert preparer.holder["generation"] == 1
    assert not preparer.plans


@pytest.mark.parametrize("durable_generation", (0, 1, 4))
def test_startup_reconstruction_activates_explicit_durable_generation(
    tmp_path: Path,
    monkeypatch,
    durable_generation: int,
) -> None:
    seeded, _commands, _preparer, _jobs = _service(tmp_path, monkeypatch)
    durable_path = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"', name="durable.yaml")
    durable_candidate, _ = seeded.candidates.capture(durable_path)
    with seeded.repository.database.transaction() as conn:
        conn.execute(
            """
            UPDATE configuration_reload_active
               SET generation = ?, candidate_reference = ?, source_sha256 = ?,
                   candidate_identity_sha256 = ?, updated_at = ?
             WHERE singleton = 1
            """,
            (
                durable_generation,
                durable_candidate.reference,
                durable_candidate.source_sha256,
                durable_candidate.candidate_identity_sha256,
                NOW.isoformat(),
            ),
        )

    restarted, commands, preparer, jobs = _service(tmp_path, monkeypatch, config_path=EXAMPLE)
    asyncio.run(restarted.reconcile_startup())

    assert restarted.active.generation == durable_generation
    assert restarted.repository.active()["generation"] == durable_generation
    assert preparer.holder["generation"] == durable_generation
    assert preparer.plans[0].expected_generation == durable_generation
    assert preparer.plans[0].target_generation == durable_generation
    assert preparer.holder["configuration"] is restarted.active.configuration

    report, _ = asyncio.run(
        _execute(
            restarted,
            commands,
            ReloadRequest(actor="operator", dry_run=True, source_path=str(EXAMPLE)),
            key=f"generation-fence-{durable_generation}",
        )
    )
    assert report.old_generation == durable_generation
    assert jobs.repository.list_jobs()[-1].config_generation == durable_generation


def test_startup_reconciliation_terminalizes_incomplete_command_and_audit(tmp_path: Path, monkeypatch) -> None:
    service, commands, _preparer, _jobs = _service(tmp_path, monkeypatch)
    request = ReloadRequest(actor="operator", dry_run=True)

    async def scenario():
        command, replayed = await commands.create_or_replay(
            command_type="config.reload",
            idempotency_key="startup-incomplete",
            actor=request.actor,
            payload=request.command_payload(),
        )
        assert not replayed
        service.repository.create_attempt(
            attempt_id="reload_000000000000000000000999",
            command_id=command.command_id,
            actor=request.actor,
            reason=None,
            dry_run=True,
            old_generation=0,
            expected_generation=None,
            at=NOW,
        )
        reconciled = await service.reconcile_startup()
        return command, reconciled

    command, reconciled = asyncio.run(scenario())
    assert reconciled == ("reload_000000000000000000000999",)
    assert asyncio.run(commands.get(command.command_id)).status.value == "cancelled"
    row = service.repository.get_attempt(reconciled[0])
    assert row is not None and row["phase"] == "cancelled" and row["audit_json"]
    assert asyncio.run(service.reconcile_startup()) == ()


def test_two_concurrent_reloads_cannot_commit_the_same_generation(tmp_path: Path, monkeypatch) -> None:
    service, commands, preparer, _jobs = _service(tmp_path, monkeypatch)
    preparer.barrier = asyncio.Barrier(2)
    candidate = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"')
    request = ReloadRequest(actor="operator", source_path=str(candidate))

    async def scenario() -> tuple[list[object], list[object]]:
        records = []
        requests = []
        for key in ("concurrent-1", "concurrent-2"):
            record, replayed, admitted = await service._admit_durable(request, idempotency_key=key)
            assert not replayed
            records.append(record)
            requests.append(admitted)
        results = await asyncio.gather(
            *(
                service.execute_command(record.command_id, admitted)
                for record, admitted in zip(records, requests, strict=True)
            ),
            return_exceptions=True,
        )
        return results, records

    results, records = asyncio.run(scenario())
    assert sum(getattr(result, "outcome", None) is ReloadOutcome.COMMITTED for result in results) == 1
    assert sum(isinstance(result, ReloadRejected) for result in results) == 1
    assert service.active.generation == 1
    assert sum(plan.activated for plan in preparer.plans) == 1
    states = [asyncio.run(commands.get(record.command_id)).status.value for record in records]
    assert sorted(states) == ["failed", "succeeded"]


def test_expensive_final_verification_finishes_before_safe_point_gate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service, commands, _preparer, _jobs = _service(tmp_path, monkeypatch)
    candidate = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"')
    registry = service.safe_points.registry
    events: list[tuple[str, bool]] = []

    original_compile = service.candidates.compile
    original_load_report = service.candidates.load_report
    original_verify = reload_service_module.verify_report_mapping

    def compile_candidate(*args, **kwargs):
        events.append(("compile", registry._commit_active))
        return original_compile(*args, **kwargs)

    def load_report(*args, **kwargs):
        events.append(("report", registry._commit_active))
        return original_load_report(*args, **kwargs)

    def verify_report(*args, **kwargs):
        events.append(("verify", registry._commit_active))
        return original_verify(*args, **kwargs)

    monkeypatch.setattr(service.candidates, "compile", compile_candidate)
    monkeypatch.setattr(service.candidates, "load_report", load_report)
    monkeypatch.setattr(reload_service_module, "verify_report_mapping", verify_report)

    result, _command = asyncio.run(
        _execute(service, commands, ReloadRequest(actor="operator", source_path=str(candidate)), key="gate-split")
    )
    assert result.outcome is ReloadOutcome.COMMITTED
    assert events
    assert all(not held for _operation, held in events)


@pytest.mark.parametrize("mutation", ("source", "metadata", "report"))
def test_final_artifact_integrity_fence_rejects_safe_point_races(
    tmp_path: Path,
    monkeypatch,
    mutation: str,
) -> None:
    service, commands, _preparer, _jobs = _service(tmp_path, monkeypatch)
    source = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"')
    registry = service.safe_points.registry

    async def scenario():
        request = ReloadRequest(actor="operator", source_path=str(source), safe_point_timeout_seconds=1.0)
        command, replayed, admitted = await service._admit_durable(request, idempotency_key=f"artifact-{mutation}")
        assert not replayed and admitted.candidate is not None
        with registry.activity(TTS):
            task = asyncio.create_task(service.execute_command(command.command_id, admitted))
            for _ in range(500):
                row = service.repository.get_by_command(command.command_id)
                if row is not None and row["phase"] == "awaiting_safe_point":
                    break
                await asyncio.sleep(0.001)
            else:
                raise AssertionError("reload did not reach safe-point wait")

            candidate_dir = service.candidates.root / admitted.candidate.reference
            if mutation == "source":
                (candidate_dir / "source.bin").write_bytes(b"tampered candidate bytes")
            elif mutation == "metadata":
                metadata_path = candidate_dir / "metadata.json"
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                metadata["candidate_identity_sha256"] = "f" * 64
                metadata_path.write_text(
                    json.dumps(metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
                    encoding="utf-8",
                )
            else:
                report_path = next(candidate_dir.glob("report_*.json"))
                report = json.loads(report_path.read_text(encoding="utf-8"))
                report["attempt_6_tampered"] = True
                report_path.write_text(
                    json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
                    encoding="utf-8",
                )
        return command, await task

    command, result = asyncio.run(scenario())
    row = service.repository.get_by_command(command.command_id)
    assert result.outcome is ReloadOutcome.ROLLED_BACK
    assert row is not None and row["phase"] == ReloadPhase.ROLLED_BACK.value
    assert row["intent_json"] is None
    assert service.active.generation == 0
    assert service.repository.active()["generation"] == 0


@pytest.mark.parametrize("mutation", ("active", "plan"))
def test_final_volatile_fence_rejects_live_identity_changes(
    tmp_path: Path,
    monkeypatch,
    mutation: str,
) -> None:
    service, commands, preparer, _jobs = _service(tmp_path, monkeypatch)
    candidate = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"')
    original_acquire = service.safe_points.acquire

    async def acquire(*args, **kwargs):
        lease = await original_acquire(*args, **kwargs)
        if mutation == "active":
            service._active = replace(service.active, generation=service.active.generation + 1)
        else:
            preparer.plans[0].diff_sha256 = "f" * 64
        return lease

    monkeypatch.setattr(service.safe_points, "acquire", acquire)
    with pytest.raises(ReloadRejected, match="Prepared resource plan|fence"):
        asyncio.run(
            _execute(
                service,
                commands,
                ReloadRequest(actor="operator", source_path=str(candidate)),
                key=f"volatile-{mutation}",
            )
        )


def test_stale_acknowledged_report_is_rejected_after_generation_changes(tmp_path: Path, monkeypatch) -> None:
    service, commands, _preparer, _jobs = _service(
        tmp_path,
        monkeypatch,
        preflight=OptionalWarningPreflight(),
    )
    candidate = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"')
    first, _ = asyncio.run(
        _execute(service, commands, ReloadRequest(actor="operator", source_path=str(candidate)), key="stale-1")
    )
    acknowledgment = WarningAcknowledgment(
        actor="operator",
        candidate_sha256=first.candidate_sha256,
        candidate_identity_sha256=first.candidate_identity_sha256,
        report_sha256=first.report_sha256,
        active_generation=first.old_generation,
        warning_identities=first.warning_identities,
        acknowledged_at=NOW,
        validator_completed_at=NOW,
        expires_at=NOW + dt.timedelta(seconds=300),
    )
    committed, _ = asyncio.run(
        _execute(
            service,
            commands,
            ReloadRequest(actor="operator", source_path=str(candidate), acknowledgment=acknowledgment),
            key="stale-2",
        )
    )
    assert committed.outcome is ReloadOutcome.COMMITTED
    active = service.active

    with pytest.raises(ReloadRejected, match="independent controller verification"):
        asyncio.run(
            _execute(
                service,
                commands,
                ReloadRequest(actor="operator", source_path=str(candidate), acknowledgment=acknowledgment),
                key="stale-3",
            )
        )
    assert service.active == active
    rejected = service.repository.get_attempt("reload_000000000000000000000003")
    assert rejected is not None and rejected["audit_json"]


def test_safe_point_timeout_and_cancellation_preserve_old_state(tmp_path: Path, monkeypatch) -> None:
    service, commands, preparer, _jobs = _service(tmp_path, monkeypatch)
    candidate = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"')
    old = service.active
    registry = service.safe_points.registry

    async def timeout_scenario():
        with registry.activity(TTS):
            return await _execute(
                service,
                commands,
                ReloadRequest(
                    actor="operator",
                    source_path=str(candidate),
                    safe_point_timeout_seconds=0.1,
                ),
                key="timeout",
            )

    timed_out, _ = asyncio.run(timeout_scenario())
    assert timed_out.outcome is ReloadOutcome.DEFERRED
    assert service.active == old
    assert preparer.plans[0].rolled_back

    async def cancellation_scenario():
        request = ReloadRequest(actor="operator", source_path=str(candidate), safe_point_timeout_seconds=1.0)
        record, replayed, admitted = await service._admit_durable(request, idempotency_key="cancel")
        assert not replayed
        with registry.activity(TTS):
            task = asyncio.create_task(service.execute_command(record.command_id, admitted))
            for _ in range(500):
                row = service.repository.get_by_command(record.command_id)
                if row is not None and row["phase"] == "awaiting_safe_point":
                    break
                await asyncio.sleep(0.001)
            else:
                raise AssertionError("reload did not reach safe-point wait")
            await commands.request_cancellation(record.command_id)
            result = await task
        return result, record

    cancelled, command = asyncio.run(cancellation_scenario())
    assert cancelled.outcome is ReloadOutcome.CANCELLED
    assert asyncio.run(commands.get(command.command_id)).status.value == "cancelled"
    assert service.active == old
    assert preparer.plans[1].rolled_back


def test_preparation_failure_is_audited_without_mutating_active_state(tmp_path: Path, monkeypatch) -> None:
    promoted: list[tuple[str, str, BaseException]] = []
    service, commands, preparer, _jobs = _service(
        tmp_path,
        monkeypatch,
        diagnostic_promoter=lambda code, component, error: promoted.append((code, component, error)),
    )
    preparer.fail_prepare = True
    candidate = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"')
    old = service.active
    with pytest.raises(ReloadRejected, match="failed safely"):
        asyncio.run(_execute(service, commands, ReloadRequest(actor="operator", source_path=str(candidate))))
    assert service.active == old
    row = service.repository.get_attempt("reload_000000000000000000000001")
    assert row is not None
    audit = json.loads(row["audit_json"])
    assert audit["outcome"] == "failed"
    assert "candidate-store-secret" not in row["audit_json"]
    assert promoted[-1][1] == "configuration_reload.preparation"


def test_rollback_and_retirement_failures_promote_bounded_operational_evidence(tmp_path: Path, monkeypatch) -> None:
    promoted: list[tuple[str, str, BaseException]] = []
    service, commands, preparer, _jobs = _service(
        tmp_path,
        monkeypatch,
        diagnostic_promoter=lambda code, component, error: promoted.append((code, component, error)),
    )
    candidate = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"')
    preparer.fail_activate = True
    preparer.fail_rollback = True
    rolled_back, _ = asyncio.run(
        _execute(service, commands, ReloadRequest(actor="operator", source_path=str(candidate)), key="rollback")
    )
    assert rolled_back.outcome is ReloadOutcome.FAILED
    assert rolled_back.phase is ReloadPhase.RECONCILIATION_REQUIRED
    assert rolled_back.cleanup_state == "pending"
    assert promoted[-1][1] == "configuration_reload.commit"
    assert str(promoted[-1][2]) == "injected activation failure"
    assert service.active.generation == 0

    still_pending = asyncio.run(service.reconcile_startup())
    pending_row = service.repository.get_attempt(rolled_back.attempt_id)
    assert rolled_back.attempt_id not in still_pending
    assert pending_row is not None and pending_row["phase"] == ReloadPhase.RECONCILIATION_REQUIRED.value
    assert pending_row["finished_at"] is None
    assert "process_restart_rebuilt_old_generation" not in str(pending_row["audit_json"])

    preparer.fail_activate = False
    preparer.fail_rollback = False
    preparer.plans[0].fail_rollback = False
    asyncio.run(service.reconcile_startup())
    recovered = service.repository.get_attempt(rolled_back.attempt_id)
    assert recovered is not None and recovered["phase"] == ReloadPhase.ROLLED_BACK.value
    assert asyncio.run(commands.get(recovered["command_id"])).status.value == "failed"
    preparer.fail_retire = True
    committed, _ = asyncio.run(
        _execute(service, commands, ReloadRequest(actor="operator", source_path=str(candidate)), key="retirement")
    )
    assert committed.outcome is ReloadOutcome.COMMITTED
    assert committed.retirement_pending
    assert service.active.generation == 1
    assert promoted[-1][1] == "configuration_reload.retirement"


def test_rollback_failure_retains_preparation_primary_and_bounded_audit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    promoted: list[tuple[str, str, BaseException]] = []
    service, commands, preparer, _jobs = _service(
        tmp_path,
        monkeypatch,
        diagnostic_promoter=lambda code, component, error: promoted.append((code, component, error)),
    )
    preparer.fail_validate = True
    preparer.fail_rollback = True
    candidate = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"')

    result, command = asyncio.run(
        _execute(service, commands, ReloadRequest(actor="operator", source_path=str(candidate)))
    )

    assert result.outcome is ReloadOutcome.FAILED
    assert result.phase is ReloadPhase.RECONCILIATION_REQUIRED
    assert result.cleanup_state == "pending"
    row = service.repository.get_attempt("reload_000000000000000000000001")
    audit = json.loads(row["audit_json"])
    assert audit["failure_evidence"] == {
        "primary": {"code": "internal_failure", "type": "RuntimeError"},
        "rollback": {"code": "rollback_failed", "type": "RuntimeError"},
    }
    assert asyncio.run(commands.get(command.command_id)).status.value == "running"
    assert any(component == "configuration_reload.rollback" for _code, component, _error in promoted)


def test_rollback_failure_retains_timeout_cancellation_stale_fence_and_swap_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def safe_point_case(service, commands, preparer, candidate, *, cancel: bool):
        preparer.fail_rollback = True
        request = ReloadRequest(
            actor="operator",
            source_path=str(candidate),
            safe_point_timeout_seconds=1.0 if cancel else 0.1,
        )
        command, _, admitted = await service._admit_durable(
            request,
            idempotency_key="cancel-rollback" if cancel else "timeout-rollback",
        )
        with service.safe_points.registry.activity(TTS):
            task = asyncio.create_task(service.execute_command(command.command_id, admitted))
            if cancel:
                for _ in range(500):
                    row = service.repository.get_by_command(command.command_id)
                    if row is not None and row["phase"] == "awaiting_safe_point":
                        break
                    await asyncio.sleep(0.001)
                else:
                    raise AssertionError("reload did not reach safe-point wait")
                cancellation = await commands.request_cancellation(command.command_id)
                assert cancellation.cancel_requested_at is not None
                durable_command = await commands.get(command.command_id)
                assert durable_command.cancel_requested_at == cancellation.cancel_requested_at
            result = await task
        return result

    timeout_service, timeout_commands, timeout_preparer, _ = _service(tmp_path / "timeout", monkeypatch)
    timeout_candidate = _write_candidate(tmp_path / "timeout", 'voice: "9"', 'voice: "8"')
    timeout = asyncio.run(
        safe_point_case(timeout_service, timeout_commands, timeout_preparer, timeout_candidate, cancel=False)
    )
    assert timeout.outcome is ReloadOutcome.DEFERRED
    assert timeout.phase is ReloadPhase.RECONCILIATION_REQUIRED
    assert timeout.cleanup_state == "pending"
    timeout_audit = json.loads(timeout_service.repository.get_attempt(timeout.attempt_id)["audit_json"])
    assert timeout_audit["failure_evidence"]["primary"]["type"] == "SafePointTimeout"
    assert timeout_audit["failure_evidence"]["rollback"]["code"] == "rollback_failed"

    cancel_service, cancel_commands, cancel_preparer, _ = _service(tmp_path / "cancel", monkeypatch)
    cancel_candidate = _write_candidate(tmp_path / "cancel", 'voice: "9"', 'voice: "8"')
    cancelled = asyncio.run(
        safe_point_case(cancel_service, cancel_commands, cancel_preparer, cancel_candidate, cancel=True)
    )
    assert cancelled.outcome is ReloadOutcome.CANCELLED
    assert cancelled.phase is ReloadPhase.RECONCILIATION_REQUIRED
    assert cancelled.cleanup_state == "pending"
    cancel_audit = json.loads(cancel_service.repository.get_attempt(cancelled.attempt_id)["audit_json"])
    assert cancel_audit["failure_evidence"]["primary"]["code"] == "reload_cancelled"
    assert cancel_audit["failure_evidence"]["rollback"]["code"] == "rollback_failed"

    stale_service, stale_commands, stale_preparer, _ = _service(tmp_path / "stale", monkeypatch)
    stale_preparer.fail_rollback = True
    stale_candidate = _write_candidate(tmp_path / "stale", 'voice: "9"', 'voice: "8"')

    async def stale_case() -> str:
        request = ReloadRequest(actor="operator", source_path=str(stale_candidate), safe_point_timeout_seconds=1.0)
        command, _, admitted = await stale_service._admit_durable(request, idempotency_key="stale-rollback")
        with stale_service.safe_points.registry.activity(TTS):
            task = asyncio.create_task(stale_service.execute_command(command.command_id, admitted))
            for _ in range(500):
                row = stale_service.repository.get_by_command(command.command_id)
                if row is not None and row["phase"] == "awaiting_safe_point":
                    break
                await asyncio.sleep(0.001)
            stale_service.candidates._environ["ICECAST_SOURCE_PASSWORD"] = "stale-secret-sentinel"
        result = await task
        assert result.outcome is ReloadOutcome.FAILED
        assert result.phase is ReloadPhase.RECONCILIATION_REQUIRED
        assert result.cleanup_state == "pending"
        return result.attempt_id

    stale_attempt = asyncio.run(stale_case())
    stale_audit = json.loads(stale_service.repository.get_attempt(stale_attempt)["audit_json"])
    assert stale_audit["failure_evidence"]["primary"]["type"] == "CandidateIntegrityError"
    assert stale_audit["failure_evidence"]["rollback"]["code"] == "rollback_failed"
    assert "stale-secret-sentinel" not in json.dumps(stale_audit)

    def after_swap(point: str) -> None:
        if point == "after_reference_swap":
            raise RuntimeError("primary after swap")

    swap_service, swap_commands, swap_preparer, _ = _service(
        tmp_path / "swap",
        monkeypatch,
        failure_injector=after_swap,
    )
    swap_preparer.fail_rollback = True
    swap_candidate = _write_candidate(tmp_path / "swap", 'voice: "9"', 'voice: "8"')
    swapped, _ = asyncio.run(
        _execute(
            swap_service,
            swap_commands,
            ReloadRequest(actor="operator", source_path=str(swap_candidate)),
        )
    )
    swap_audit = json.loads(swap_service.repository.get_attempt(swapped.attempt_id)["audit_json"])
    assert swapped.outcome is ReloadOutcome.FAILED
    assert swapped.phase is ReloadPhase.RECONCILIATION_REQUIRED
    assert swapped.cleanup_state == "pending"
    assert swap_audit["failure_evidence"]["primary"]["type"] == "RuntimeError"
    assert swap_audit["failure_evidence"]["rollback"]["code"] == "rollback_failed"


def test_task_cancellation_survives_rollback_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service, commands, preparer, _jobs = _service(tmp_path, monkeypatch)
    preparer.fail_rollback = True
    candidate = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"')

    async def scenario():
        request = ReloadRequest(actor="operator", source_path=str(candidate), safe_point_timeout_seconds=1.0)
        command, _, admitted = await service._admit_durable(request, idempotency_key="task-cancel-rollback")
        with service.safe_points.registry.activity(TTS):
            task = asyncio.create_task(service.execute_command(command.command_id, admitted))
            for _ in range(500):
                row = service.repository.get_by_command(command.command_id)
                if row is not None and row["phase"] == "awaiting_safe_point":
                    break
                await asyncio.sleep(0.001)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        return command

    command = asyncio.run(scenario())
    assert asyncio.run(commands.get(command.command_id)).status.value == "running"
    row = service.repository.get_by_command(command.command_id)
    audit = json.loads(row["audit_json"])
    assert audit["failure_evidence"]["primary"]["type"] == "CancelledError"
    assert audit["failure_evidence"]["rollback"]["code"] == "rollback_failed"
    assert audit["cleanup_state"] == "pending"


def test_environment_input_change_while_waiting_for_safe_point_rolls_back_plan(tmp_path: Path, monkeypatch) -> None:
    service, commands, preparer, _jobs = _service(tmp_path, monkeypatch)
    candidate = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"')
    registry = service.safe_points.registry

    async def scenario():
        request = ReloadRequest(actor="operator", source_path=str(candidate), safe_point_timeout_seconds=1.0)
        command, replayed, admitted = await service._admit_durable(
            request,
            idempotency_key="environment-final-fence",
        )
        assert not replayed
        with registry.activity(TTS):
            task = asyncio.create_task(service.execute_command(command.command_id, admitted))
            for _ in range(500):
                row = service.repository.get_by_command(command.command_id)
                if row is not None and row["phase"] == "awaiting_safe_point":
                    break
                await asyncio.sleep(0.001)
            else:
                raise AssertionError("reload did not reach safe-point wait")
            environment = service.candidates._environ
            assert isinstance(environment, dict)
            environment["ICECAST_SOURCE_PASSWORD"] = "changed-during-safe-point"
        return await task, command

    result, command = asyncio.run(scenario())
    assert result.outcome is ReloadOutcome.ROLLED_BACK
    assert service.active.generation == 0
    assert preparer.plans[0].rolled_back
    assert asyncio.run(commands.get(command.command_id)).status.value == "failed"
    row = service.repository.get_attempt(result.attempt_id)
    assert row is not None and "changed-during-safe-point" not in str(row["audit_json"])


def test_acknowledgment_actor_blocks_cross_principal_replay_and_admission_is_immutable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service, commands, preparer, _jobs = _service(
        tmp_path,
        monkeypatch,
        preflight=OptionalWarningPreflight(),
    )
    candidate = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"')
    first, _ = asyncio.run(
        _execute(service, commands, ReloadRequest(actor="operator", source_path=str(candidate)), key="actor-warning")
    )
    acknowledgment = WarningAcknowledgment(
        actor="operator",
        candidate_sha256=first.candidate_sha256,
        candidate_identity_sha256=first.candidate_identity_sha256,
        report_sha256=first.report_sha256,
        active_generation=first.old_generation,
        warning_identities=first.warning_identities,
        acknowledged_at=NOW,
        validator_completed_at=NOW,
        expires_at=NOW + dt.timedelta(seconds=300),
    )
    with pytest.raises(ReloadRejected, match="does not match"):
        asyncio.run(
            service.admit(
                ReloadRequest(actor="different-principal", source_path=str(candidate), acknowledgment=acknowledgment),
                idempotency_key="cross-principal",
            )
        )

    async def tampering_scenario():
        request = ReloadRequest(actor="operator", source_path=str(candidate), acknowledgment=acknowledgment)
        command, replayed, admitted = await service._admit_durable(request, idempotency_key="actor-tampering")
        assert not replayed
        with service.safe_points.registry.activity(TTS):
            task = asyncio.create_task(service.execute_command(command.command_id, admitted))
            for _ in range(500):
                row = service.repository.get_by_command(command.command_id)
                if row is not None and row["phase"] == "awaiting_safe_point":
                    break
                await asyncio.sleep(0.001)
            else:
                raise AssertionError("reload did not reach safe-point wait")
            object.__setattr__(acknowledgment, "actor", "tampered-principal")
        return command, await task

    command, result = asyncio.run(tampering_scenario())
    assert result.outcome is ReloadOutcome.COMMITTED
    assert preparer.plans[0].activated
    assert service.active.generation == 1
    assert asyncio.run(commands.get(command.command_id)).status.value == "succeeded"


@pytest.mark.parametrize(
    ("case", "expected_outcome"),
    (
        ("committed", ReloadOutcome.COMMITTED),
        ("restart", ReloadOutcome.RESTART_REQUIRED),
        ("acknowledgment", ReloadOutcome.ACKNOWLEDGMENT_REQUIRED),
        ("noop", ReloadOutcome.NOOP),
    ),
)
def test_terminal_audit_repairs_lost_successful_command_finalization(
    tmp_path: Path,
    monkeypatch,
    case: str,
    expected_outcome: ReloadOutcome,
) -> None:
    preflight = OptionalWarningPreflight() if case == "acknowledgment" else None
    service, commands, _preparer, _jobs = _service(tmp_path, monkeypatch, preflight=preflight)
    if case == "committed":
        source = _write_candidate(
            tmp_path,
            "dedupe:\n  ttl_seconds: 900",
            "dedupe:\n  ttl_seconds: 901",
        )
    elif case == "restart":
        source = _write_candidate(
            tmp_path,
            'path: "/var/lib/seasonalweather/state/seasonalweather.sqlite3"',
            'path: "/tmp/replay.sqlite3"',
        )
    elif case == "acknowledgment":
        source = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"')
    else:
        source = EXAMPLE
    request = ReloadRequest(actor="operator", source_path=str(source))

    async def scenario():
        command, replayed, admitted = await service._admit_durable(
            request,
            idempotency_key=f"lost-{case}",
        )
        assert not replayed
        original = commands.mark_succeeded

        async def lose_ack(_command_id, _result):
            raise RuntimeError("injected lost command acknowledgment")

        monkeypatch.setattr(commands, "mark_succeeded", lose_ack)
        with pytest.raises(RuntimeError, match="lost command acknowledgment"):
            await service.execute_command(command.command_id, admitted)
        monkeypatch.setattr(commands, "mark_succeeded", original)
        assert (await commands.get(command.command_id)).status.value == "running"
        replay, replayed = await service.admit(request, idempotency_key=f"lost-{case}")
        return command, replay, replayed

    command, replay, replayed = asyncio.run(scenario())
    assert replayed and replay.command_id == command.command_id
    assert replay.status.value == "succeeded"
    row = service.repository.get_by_command(command.command_id)
    assert row is not None and row["outcome"] == expected_outcome.value


def test_terminal_cancelled_and_failed_audits_repair_lost_command_finalization(tmp_path: Path, monkeypatch) -> None:
    service, commands, preparer, _jobs = _service(tmp_path, monkeypatch)
    candidate = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"')

    async def cancelled_scenario():
        request = ReloadRequest(actor="operator", source_path=str(candidate), safe_point_timeout_seconds=1.0)
        command, _, admitted = await service._admit_durable(request, idempotency_key="lost-cancelled")
        original = commands.mark_cancelled

        async def lose_cancel(_command_id):
            raise RuntimeError("injected lost cancellation finalization")

        monkeypatch.setattr(commands, "mark_cancelled", lose_cancel)
        with service.safe_points.registry.activity(TTS):
            task = asyncio.create_task(service.execute_command(command.command_id, admitted))
            for _ in range(500):
                row = service.repository.get_by_command(command.command_id)
                if row is not None and row["phase"] == "awaiting_safe_point":
                    break
                await asyncio.sleep(0.001)
            await commands.request_cancellation(command.command_id)
            with pytest.raises(RuntimeError, match="lost cancellation finalization"):
                await task
        monkeypatch.setattr(commands, "mark_cancelled", original)
        await service.reconcile_startup()
        return command

    cancelled = asyncio.run(cancelled_scenario())
    assert asyncio.run(commands.get(cancelled.command_id)).status.value == "cancelled"

    preparer.fail_prepare = True

    async def failed_scenario():
        request = ReloadRequest(actor="operator", source_path=str(candidate))
        command, _, admitted = await service._admit_durable(request, idempotency_key="lost-failed")
        original = commands.mark_failed

        async def lose_failure(_command_id, _error):
            raise RuntimeError("injected lost failure finalization")

        monkeypatch.setattr(commands, "mark_failed", lose_failure)
        with pytest.raises(RuntimeError, match="lost failure finalization"):
            await service.execute_command(command.command_id, admitted)
        monkeypatch.setattr(commands, "mark_failed", original)
        await service.reconcile_startup()
        return command

    failed = asyncio.run(failed_scenario())
    assert asyncio.run(commands.get(failed.command_id)).status.value == "failed"


def test_startup_repairs_lost_command_after_more_than_500_terminal_attempts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service, commands, _preparer, _jobs = _service(tmp_path, monkeypatch)

    async def seed() -> object:
        historical_ids: list[str] = []
        for index in range(501):
            command, _ = await commands.create_or_replay(
                command_type="config.reload",
                idempotency_key=f"historical-{index:04d}",
                actor="operator",
                payload={"schema_version": 1, "historical": index},
            )
            attempt_id = f"reload_history_{index:06d}"
            service.repository.create_attempt(
                attempt_id=attempt_id,
                command_id=command.command_id,
                actor="operator",
                reason=None,
                dry_run=True,
                old_generation=0,
                expected_generation=None,
                at=NOW,
            )
            service.repository.fail_attempt(
                attempt_id,
                phase=ReloadPhase.REJECTED,
                outcome=ReloadOutcome.FAILED.value,
                audit={"outcome": "failed", "failure_code": "historical_failure"},
                at=NOW,
            )
            historical_ids.append(command.command_id)
        with service.repository.database.transaction() as conn:
            conn.executemany(
                "UPDATE api_commands SET status = 'failed', finished_at = ? WHERE command_id = ?",
                ((NOW.isoformat(), command_id) for command_id in historical_ids),
            )
        newest, _ = await commands.create_or_replay(
            command_type="config.reload",
            idempotency_key="newest-lost-ack",
            actor="operator",
            payload={"schema_version": 1, "newest": True},
        )
        service.repository.create_attempt(
            attempt_id="reload_newest_lost_ack",
            command_id=newest.command_id,
            actor="operator",
            reason=None,
            dry_run=True,
            old_generation=0,
            expected_generation=None,
            at=NOW,
        )
        service.repository.fail_attempt(
            "reload_newest_lost_ack",
            phase=ReloadPhase.REJECTED,
            outcome=ReloadOutcome.FAILED.value,
            audit={"outcome": "failed", "failure_code": "newest_failure"},
            at=NOW,
        )
        return newest

    newest = asyncio.run(seed())
    asyncio.run(service.reconcile_startup())
    assert asyncio.run(commands.get(newest.command_id)).status.value == "failed"


def test_durable_intent_failure_rolls_back_before_swap(tmp_path: Path, monkeypatch) -> None:
    def inject(point: str) -> None:
        if point == "after_durable_intent":
            raise RuntimeError("injected crash after durable intent")

    service, commands, preparer, _jobs = _service(tmp_path, monkeypatch, failure_injector=inject)
    candidate = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"')
    result, _ = asyncio.run(_execute(service, commands, ReloadRequest(actor="operator", source_path=str(candidate))))

    assert result.outcome is ReloadOutcome.ROLLED_BACK
    assert service.active.generation == 0
    assert preparer.plans[0].rolled_back and not preparer.plans[0].activated


def test_prepared_plan_more_restrictive_than_trusted_diff_fails_closed(tmp_path: Path, monkeypatch) -> None:
    service, commands, preparer, _jobs = _service(tmp_path, monkeypatch)
    preparer.force_disposition = ReloadDisposition.QUIESCENT
    candidate = _write_candidate(
        tmp_path,
        "dedupe:\n  ttl_seconds: 900",
        "dedupe:\n  ttl_seconds: 901",
    )
    with pytest.raises(ReloadRejected, match="different reload disposition"):
        asyncio.run(_execute(service, commands, ReloadRequest(actor="operator", source_path=str(candidate))))
    assert service.active.generation == 0
    assert preparer.plans[0].rolled_back and not preparer.plans[0].activated


def test_postcommit_interruption_keeps_new_generation_and_retires_during_reconciliation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def inject(point: str) -> None:
        if point == "after_durable_completion":
            raise RuntimeError("injected crash after durable completion")

    service, commands, preparer, _jobs = _service(tmp_path, monkeypatch, failure_injector=inject)
    candidate = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"')
    request = ReloadRequest(actor="operator", source_path=str(candidate))

    async def scenario():
        command, _, admitted = await service._admit_durable(request, idempotency_key="postcommit-crash")
        with pytest.raises(PostCommitRecoveryRequired):
            await service.execute_command(command.command_id, admitted)
        assert (await commands.get(command.command_id)).status.value == "running"
        row = service.repository.get_by_command(command.command_id)
        assert row is not None and row["phase"] == "committed" and row["finished_at"] is None
        preparer.plans[0].fail_retire = True
        assert await service.reconcile_startup() == ()
        assert (await commands.get(command.command_id)).status.value == "running"
        row = service.repository.get_by_command(command.command_id)
        assert row is not None and row["phase"] == "committed" and row["finished_at"] is None
        preparer.plans[0].fail_retire = False
        reconciled = await service.reconcile_startup()
        return command, reconciled

    command, reconciled = asyncio.run(scenario())
    assert service.active.generation == 1
    assert preparer.plans[0].retire_count == 2
    assert "reload_000000000000000000000001" in reconciled
    row = service.repository.get_by_command(command.command_id)
    assert row is not None and row["phase"] == "completed" and row["finished_at"]
    assert asyncio.run(commands.get(command.command_id)).status.value == "succeeded"


def test_retirement_failure_remains_pending_until_retry(tmp_path: Path, monkeypatch) -> None:
    service, commands, preparer, _jobs = _service(tmp_path, monkeypatch)
    preparer.fail_retire = True
    candidate = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"')
    result, command = asyncio.run(
        _execute(service, commands, ReloadRequest(actor="operator", source_path=str(candidate)))
    )
    row = service.repository.get_attempt(result.attempt_id)
    assert result.retirement_pending and result.phase.value == "retiring"
    assert row is not None and row["finished_at"] is None
    assert service.active.generation == 1

    preparer.plans[0].fail_retire = False
    asyncio.run(service.reconcile_startup())
    row = service.repository.get_attempt(result.attempt_id)
    assert row is not None and row["phase"] == "completed" and row["finished_at"]
    assert asyncio.run(commands.get(command.command_id)).status.value == "succeeded"


def test_restart_proves_process_local_retirement_and_reconciles_audit(tmp_path: Path, monkeypatch) -> None:
    service, commands, preparer, _jobs = _service(tmp_path, monkeypatch)
    preparer.fail_retire = True
    candidate = _write_candidate(tmp_path, 'voice: "9"', 'voice: "8"')
    result, command = asyncio.run(
        _execute(service, commands, ReloadRequest(actor="operator", source_path=str(candidate)))
    )
    assert result.retirement_pending
    assert asyncio.run(commands.get(command.command_id)).status.value == "running"

    restarted, restarted_commands, _preparer, _jobs = _service(tmp_path, monkeypatch)
    repaired = asyncio.run(restarted.reconcile_startup())
    assert result.attempt_id in repaired
    row = restarted.repository.get_attempt(result.attempt_id)
    assert row is not None and row["phase"] == ReloadPhase.COMPLETED.value
    audit = json.loads(row["audit_json"])
    assert audit["retirement_pending"] is False
    assert audit["cleanup_state"] == "completed"
    assert audit["retirement_evidence"]["proof"] == "process_restart_superseded_resource_gone"
    assert audit["lifecycle_states"]["retirement"] == "completed"
    assert asyncio.run(restarted_commands.get(command.command_id)).status.value == "succeeded"


def test_complete_commit_attempt_cas_failure_rolls_back_active_generation(tmp_path: Path, monkeypatch) -> None:
    service, _commands, _preparer, _jobs = _service(tmp_path, monkeypatch)
    repository = service.repository
    candidate = service._active_candidate
    repository.create_attempt(
        attempt_id="reload_000000000000000000000777",
        command_id="cmd_00000000000000000777",
        actor="operator",
        reason=None,
        dry_run=False,
        old_generation=0,
        expected_generation=0,
        at=NOW,
    )
    with repository.database.transaction() as conn:
        conn.execute(
            "UPDATE configuration_reload_attempts SET phase = 'rejected' WHERE attempt_id = ?",
            ("reload_000000000000000000000777",),
        )
    with pytest.raises(StaleReloadError, match="attempt changed"):
        repository.complete_commit(
            "reload_000000000000000000000777",
            expected_generation=0,
            candidate=candidate,
            report_sha256="a" * 64,
            diff_sha256="b" * 64,
            audit_reference="audit_000000000000000000000777",
            at=NOW,
        )
    assert repository.active()["generation"] == 0


def test_validation_job_cancellation_is_recorded_and_propagated(tmp_path: Path, monkeypatch) -> None:
    service, _commands, _preparer, jobs = _service(tmp_path, monkeypatch)
    runner = service.validation_jobs
    candidate = service._active_candidate

    async def scenario() -> None:
        entered = asyncio.Event()

        async def wait_forever(*_args, **_kwargs):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(runner, "_handle", wait_forever)
        task = asyncio.create_task(
            runner.execute(
                candidate,
                command_id="cmd_validation_cancelled",
                active_generation=0,
                preflight_required=False,
            )
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    job = jobs.repository.list_jobs()[0]
    assert job.status.value == "cancelled"
    assert job.error is not None and job.error.category is FailureCategory.CANCELLED


@pytest.mark.parametrize(
    ("failure", "expected_code", "expected_category"),
    (
        (TimeoutError("timeout"), "config_validation_timed_out", FailureCategory.TIMED_OUT),
        (
            CandidateIntegrityError("invalid candidate"),
            "config_validation_invalid_candidate",
            FailureCategory.INVALID_INPUT,
        ),
        (
            RuntimeError("dependency failure"),
            "config_validation_dependency_failed",
            FailureCategory.DEPENDENCY_UNAVAILABLE,
        ),
    ),
)
def test_validation_job_failure_taxonomy(
    tmp_path: Path,
    monkeypatch,
    failure: Exception,
    expected_code: str,
    expected_category: FailureCategory,
) -> None:
    service, _commands, _preparer, jobs = _service(tmp_path, monkeypatch)
    runner = service.validation_jobs

    async def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(runner, "_handle", fail)
    with pytest.raises(ValidationJobExecutionError) as raised:
        asyncio.run(
            runner.execute(
                service._active_candidate,
                command_id="cmd_validation_failure",
                active_generation=0,
                preflight_required=False,
            )
        )
    assert raised.value.code == expected_code
    assert raised.value.category is expected_category
    job = jobs.repository.list_jobs()[0]
    assert job.status.value in {"failed", "expired"}
    assert job.error is not None and job.error.category is expected_category


@pytest.mark.parametrize(
    ("store_failure", "expected_code"),
    (
        (StaleJobMutationError("lease lost"), "config_validation_lease_lost"),
        (JobStoreConflictError("conflicting result"), "config_validation_result_conflict"),
    ),
)
def test_validation_job_distinguishes_lease_loss_and_result_conflict(
    tmp_path: Path,
    monkeypatch,
    store_failure: Exception,
    expected_code: str,
) -> None:
    service, _commands, _preparer, _jobs = _service(tmp_path, monkeypatch)
    runner = service.validation_jobs
    original = runner.job_service.repository.record_outcome

    def fail_success(*args, **kwargs):
        if kwargs.get("outcome").value == "succeeded":
            raise store_failure
        return original(*args, **kwargs)

    monkeypatch.setattr(runner.job_service.repository, "record_outcome", fail_success)
    with pytest.raises(ValidationJobExecutionError) as raised:
        asyncio.run(
            runner.execute(
                service._active_candidate,
                command_id="cmd_validation_store_failure",
                active_generation=0,
                preflight_required=False,
            )
        )
    assert raised.value.code == expected_code
    assert raised.value.category is FailureCategory.SIDE_EFFECT_UNCERTAIN
