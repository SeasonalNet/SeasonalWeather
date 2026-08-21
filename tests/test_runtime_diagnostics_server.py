from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from seasonalweather.api import server as api_server
from seasonalweather.config import AuthMode
from seasonalweather.database import SeasonalDatabase
from seasonalweather.diagnostics.bindings import RUNTIME_CODES
from seasonalweather.lifecycle import Lifecycle, LifecycleState, LifecycleTimeouts
from seasonalweather.runtime_diagnostics import fatal as runtime_fatal
from seasonalweather.runtime_diagnostics.fatal import FatalBoundary, SecondaryFailureLedger
from seasonalweather.runtime_diagnostics.repository import OccurrenceRepository


class _IdleFence:
    async def wait_idle(self, _timeout: float) -> bool:
        return True


class _ApiClient:
    async def aclose(self) -> None:
        return None


class _CleanOrchestrator:
    def __init__(
        self,
        _cfg: Any,
        *,
        lifecycle: Any,
        supervisor: Any,
        lifecycle_records: Any = None,
    ) -> None:
        self.lifecycle = lifecycle
        self.supervisor = supervisor
        self.lifecycle_records = lifecycle_records
        self.alert_audio = _IdleFence()
        self.publication_fence = _IdleFence()
        self.api = _ApiClient()

    async def run(self) -> None:
        self.lifecycle.mark_running()
        self.lifecycle.request_shutdown()


class _Orchestrator(_CleanOrchestrator):
    async def run(self) -> None:
        self.lifecycle.mark_running()

        async def fail_optional() -> None:
            raise RuntimeError("optional production seam")

        self.supervisor.create_task(
            fail_optional(),
            name="optional-production-seam",
            required=False,
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.lifecycle.request_shutdown()


class _HangingFence:
    async def wait_idle(self, _timeout: float) -> bool:
        await asyncio.Future()
        return True


class _TimeoutOrchestrator(_CleanOrchestrator):
    def __init__(
        self,
        cfg: Any,
        *,
        lifecycle: Any,
        supervisor: Any,
        lifecycle_records: Any = None,
    ) -> None:
        super().__init__(
            cfg,
            lifecycle=lifecycle,
            supervisor=supervisor,
            lifecycle_records=lifecycle_records,
        )
        self.alert_audio = _HangingFence()


class _RunningOnlyOrchestrator(_CleanOrchestrator):
    async def run(self) -> None:
        self.lifecycle.mark_running()
        await asyncio.Future()


REQUIRED_FAILURE = ExceptionGroup("required production seam", [ValueError("required child")])
REQUIRED_FAILURE.add_note("required-note")
REQUIRED_CAUSE = KeyError("required cause")
REQUIRED_CONTEXT = LookupError("required context")
REQUIRED_FAILURE.__cause__ = REQUIRED_CAUSE
REQUIRED_FAILURE.__context__ = REQUIRED_CONTEXT


class _BrokenApiClient:
    async def aclose(self) -> None:
        raise RuntimeError("password=cleanup-private")


class _RequiredFailureOrchestrator(_CleanOrchestrator):
    def __init__(
        self,
        cfg: Any,
        *,
        lifecycle: Any,
        supervisor: Any,
        lifecycle_records: Any = None,
    ) -> None:
        super().__init__(
            cfg,
            lifecycle=lifecycle,
            supervisor=supervisor,
            lifecycle_records=lifecycle_records,
        )
        self.api = _BrokenApiClient()

    async def run(self) -> None:
        self.lifecycle.mark_running()
        raise REQUIRED_FAILURE


class _Server:
    def __init__(self, _config: object) -> None:
        self.should_exit = False

    async def serve(self) -> None:
        while not self.should_exit:
            await asyncio.sleep(0)


class _Marker:
    latest: _Marker | None = None

    def __init__(self, _state_root: Path) -> None:
        type(self).latest = self
        self.stages: list[str] = []
        self.cleaned = False

    def start(self, marker: Any) -> None:
        self.stages.append(marker.lifecycle_stage)

    def update_stage(self, stage: str) -> None:
        self.stages.append(stage)

    def reconcile_pending(self, _service: object, *, current_context: object) -> None:
        return None

    def mark_clean(self) -> None:
        self.cleaned = True


class _RunningUpdateFailureMarker(_Marker):
    def update_stage(self, stage: str) -> None:
        if stage == "running":
            raise RuntimeError("password=running-marker-private")
        super().update_stage(stage)


class _DrainingUpdateFailureMarker(_Marker):
    def update_stage(self, stage: str) -> None:
        if stage == "draining":
            raise RuntimeError("password=draining-marker-private")
        super().update_stage(stage)


class _MarkCleanFailureMarker(_Marker):
    def mark_clean(self) -> None:
        raise RuntimeError("password=clean-marker-private")


class _Database(SeasonalDatabase):
    def checkpoint(self) -> None:
        return None


def _config(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        database=SimpleNamespace(
            enabled=True,
            path=str(tmp_path / "operational.sqlite3"),
            busy_timeout_ms=5000,
            journal_mode="WAL",
        ),
        paths=SimpleNamespace(work_dir=str(tmp_path)),
        lifecycle=LifecycleTimeouts(
            total_seconds=2,
            active_request_seconds=0.2,
            publication_seconds=0.2,
            source_stop_seconds=0.2,
            tts_stop_seconds=0.2,
            task_cancel_seconds=0.2,
            resource_close_seconds=0.2,
        ),
        jobs=SimpleNamespace(
            enabled=False,
            reconciliation_batch_size=10,
            shutdown_reconciliation_seconds=0.2,
        ),
        api=SimpleNamespace(auth=SimpleNamespace(mode=AuthMode.STATIC)),
    )


def _install_server_seams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    orchestrator: type[_CleanOrchestrator] = _CleanOrchestrator,
    marker: type[_Marker] = _Marker,
    cfg: SimpleNamespace | None = None,
) -> tuple[SimpleNamespace, _Database]:
    selected = cfg or _config(tmp_path)
    monkeypatch.setattr(api_server, "load_config", lambda _path: selected)
    monkeypatch.setattr(api_server, "_setup_logging", lambda _cfg, **_kwargs: None)
    monkeypatch.setattr(api_server, "ProcessMarkerStore", marker)
    monkeypatch.setattr(api_server, "Orchestrator", orchestrator)
    monkeypatch.setattr(api_server, "OrchestratorControl", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(api_server, "build_runtime_health_service", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(api_server, "create_app", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(api_server, "_ControllerOwnedUvicornServer", _Server)
    monkeypatch.setattr(api_server, "_install_signal_handlers", lambda *_args: lambda: None)
    database = _Database(
        path=selected.database.path,
        busy_timeout_ms=selected.database.busy_timeout_ms,
        journal_mode=selected.database.journal_mode,
    )
    database.bootstrap()
    monkeypatch.setattr(api_server, "bootstrap_database_from_config", lambda _cfg: database)
    return selected, database


def test_default_executor_shutdown_supports_python_311_and_newer_signatures() -> None:
    legacy_calls: list[bool] = []
    modern_timeouts: list[float | None] = []

    class LegacyLoop:
        async def shutdown_default_executor(self) -> None:
            legacy_calls.append(True)

    class ModernLoop:
        async def shutdown_default_executor(self, timeout: float | None = None) -> None:
            modern_timeouts.append(timeout)

    class HangingLegacyLoop:
        async def shutdown_default_executor(self) -> None:
            await asyncio.Future()

    async def scenario() -> None:
        await api_server._shutdown_default_executor(
            LegacyLoop(),
            timeout_seconds=0.25,
        )
        await api_server._shutdown_default_executor(
            ModernLoop(),
            timeout_seconds=0.25,
        )
        with pytest.raises(TimeoutError):
            await api_server._shutdown_default_executor(
                HangingLegacyLoop(),
                timeout_seconds=0.01,
            )

    asyncio.run(scenario())
    assert legacy_calls == [True]
    assert modern_timeouts == [0.25]


@pytest.mark.filterwarnings("ignore:The executor did not finishing joining its threads")
def test_run_api_server_impl_wires_marker_optional_failure_and_pruning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = _config(tmp_path)
    pruned: list[tuple[int, int]] = []
    original_prune = api_server.RuntimeDiagnosticService.prune_resolved

    def observe_prune(
        service: api_server.RuntimeDiagnosticService,
        *,
        retention_days: int,
        retain_resolved: int,
    ) -> int:
        pruned.append((retention_days, retain_resolved))
        return original_prune(
            service,
            retention_days=retention_days,
            retain_resolved=retain_resolved,
        )

    caplog.set_level("INFO", logger="seasonalweather.lifecycle")
    _, database = _install_server_seams(
        tmp_path,
        monkeypatch,
        orchestrator=_Orchestrator,
        cfg=cfg,
    )
    monkeypatch.setattr(api_server.RuntimeDiagnosticService, "prune_resolved", observe_prune)

    assert api_server.main(["--config", "unused", "--host", "127.0.0.1", "--port", "0"]) == 0

    marker = _Marker.latest
    assert marker is not None
    assert marker.stages == ["starting", "running", "draining", "stopping", "stopped"]
    assert marker.cleaned
    assert "lifecycle_event=service_stopped state=stopped" in caplog.messages
    assert pruned == [(90, 1_000)]
    database = SeasonalDatabase(path=cfg.database.path)
    repository = OccurrenceRepository(database)
    active = repository.active()
    assert any(item.code == RUNTIME_CODES["optional_task_degraded"] for item in active)


def test_actual_run_api_server_reports_fatal_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = RuntimeError("bounded fatal seam")
    reported: list[BaseException] = []

    class Boundary:
        def __init__(self, _service: object, _context: object, _secondary: object) -> None:
            return None

        def report(self, exception: BaseException) -> None:
            reported.append(exception)

    async def fail(**_kwargs: object) -> None:
        raise original

    monkeypatch.setattr(api_server, "FatalBoundary", Boundary)
    monkeypatch.setattr(api_server, "enable_faulthandler", lambda: True)
    monkeypatch.setattr(api_server, "_run_api_server_impl", fail)

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(api_server.run_api_server(config_path="unused", host="127.0.0.1", port=0))
    assert caught.value is original
    assert reported == [original]


@pytest.mark.filterwarnings("ignore:The executor did not finishing joining its threads")
def test_shutdown_deadline_is_fatal_retains_marker_and_skips_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = _config(tmp_path)
    cfg.lifecycle = LifecycleTimeouts(
        total_seconds=0.05,
        active_request_seconds=0.01,
        publication_seconds=0.01,
        source_stop_seconds=0.01,
        tts_stop_seconds=0.01,
        task_cancel_seconds=0.01,
        resource_close_seconds=0.01,
    )
    cfg.jobs.shutdown_reconciliation_seconds = 0.01
    _, database = _install_server_seams(
        tmp_path,
        monkeypatch,
        orchestrator=_TimeoutOrchestrator,
        cfg=cfg,
    )
    emergency: list[bytes] = []
    monkeypatch.setattr(runtime_fatal, "direct_stderr", emergency.append)
    caplog.set_level("INFO", logger="seasonalweather.lifecycle")

    assert api_server.main(["--config", "unused", "--host", "127.0.0.1", "--port", "0"]) == 1

    marker = _Marker.latest
    assert marker is not None
    assert not marker.cleaned
    assert "stopped" not in marker.stages
    assert "lifecycle_event=service_stopped state=stopped" not in caplog.messages
    occurrences = OccurrenceRepository(database).active()
    fatal_occurrence = next(item for item in occurrences if item.code == RUNTIME_CODES["fatal_controller"])
    assert "controller shutdown deadline exceeded" in str(fatal_occurrence.latest_instance)
    assert emergency


@pytest.mark.filterwarnings("ignore:The executor did not finishing joining its threads")
def test_running_marker_failure_does_not_strand_controller_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _database = _install_server_seams(
        tmp_path,
        monkeypatch,
        orchestrator=_RunningOnlyOrchestrator,
        marker=_RunningUpdateFailureMarker,
    )
    secondary = SecondaryFailureLedger()
    fatal = [
        FatalBoundary(
            None,
            api_server.CorrelationContext(
                role=api_server.DiagnosticRole.CONTROLLER,
                instance_id="controller_00000001",
                component="controller",
            ),
            secondary,
        )
    ]
    with pytest.raises(RuntimeError, match="marker integration"):
        asyncio.run(
            asyncio.wait_for(
                api_server._run_api_server_impl(
                    config_path="unused",
                    host="127.0.0.1",
                    port=0,
                    instance_id="controller_00000001",
                    fatal=fatal,
                ),
                timeout=1,
            )
        )
    marker = _RunningUpdateFailureMarker.latest
    assert marker is not None
    assert not marker.cleaned
    assert any("process_marker_stage_update_failed" in item for item in secondary.snapshot())
    assert all("running-marker-private" not in item for item in secondary.snapshot())


def test_draining_marker_failure_sets_shutdown_event_and_does_not_hang(tmp_path: Path) -> None:
    async def scenario() -> None:
        marker = _DrainingUpdateFailureMarker(tmp_path)
        marker.start(SimpleNamespace(lifecycle_stage="starting"))
        secondary = SecondaryFailureLedger()
        integration = api_server._MarkerLifecycleIntegration(cast(api_server.ProcessMarkerStore, marker), secondary)
        lifecycle = Lifecycle(transition_callback=integration.transition)
        lifecycle.mark_running()
        assert lifecycle.request_shutdown()
        await asyncio.wait_for(lifecycle.wait_for_shutdown(), timeout=0.1)
        assert lifecycle.state is LifecycleState.DRAINING
        assert integration.terminal_failure() is not None
        assert not marker.cleaned
        assert any("process_marker_stage_update_failed" in item for item in secondary.snapshot())
        assert all("draining-marker-private" not in item for item in secondary.snapshot())

    asyncio.run(scenario())


@pytest.mark.filterwarnings("ignore:The executor did not finishing joining its threads")
def test_required_failure_remains_primary_across_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_server_seams(
        tmp_path,
        monkeypatch,
        orchestrator=_RequiredFailureOrchestrator,
    )
    secondary = SecondaryFailureLedger()
    fatal = [
        FatalBoundary(
            None,
            api_server.CorrelationContext(
                role=api_server.DiagnosticRole.CONTROLLER,
                instance_id="controller_00000001",
                component="controller",
            ),
            secondary,
        )
    ]
    with pytest.raises(ExceptionGroup) as caught:
        asyncio.run(
            api_server._run_api_server_impl(
                config_path="unused",
                host="127.0.0.1",
                port=0,
                instance_id="controller_00000001",
                fatal=fatal,
            )
        )
    assert caught.value is REQUIRED_FAILURE
    assert caught.value.__notes__ == ["required-note"]
    assert caught.value.__cause__ is REQUIRED_CAUSE
    assert caught.value.__context__ is REQUIRED_CONTEXT
    assert isinstance(caught.value.exceptions[0], ValueError)
    assert '"event":"service_started_degraded"' in capsys.readouterr().out
    traceback_names: list[str] = []
    current = caught.value.__traceback__
    while current is not None:
        traceback_names.append(current.tb_frame.f_code.co_name)
        current = current.tb_next
    assert "run" in traceback_names
    assert any("nws_api_close_failed" in item for item in secondary.snapshot())
    assert all("cleanup-private" not in item for item in secondary.snapshot())
    marker = _Marker.latest
    assert marker is not None
    assert not marker.cleaned


@pytest.mark.filterwarnings("ignore:The executor did not finishing joining its threads")
def test_mark_clean_failure_never_emits_clean_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _install_server_seams(
        tmp_path,
        monkeypatch,
        marker=_MarkCleanFailureMarker,
    )
    caplog.set_level("INFO", logger="seasonalweather.lifecycle")
    with pytest.raises(RuntimeError, match="marker finalization"):
        asyncio.run(
            api_server._run_api_server_impl(
                config_path="unused",
                host="127.0.0.1",
                port=0,
                instance_id="controller_00000001",
                fatal=[
                    FatalBoundary(
                        None,
                        api_server.CorrelationContext(
                            role=api_server.DiagnosticRole.CONTROLLER,
                            instance_id="controller_00000001",
                            component="controller",
                        ),
                    )
                ],
            )
        )
    marker = _MarkCleanFailureMarker.latest
    assert marker is not None
    assert marker.stages[-1] == "stopped"
    assert not marker.cleaned
    assert "lifecycle_event=service_stopped state=stopped" not in caplog.messages


@pytest.mark.filterwarnings("ignore:The executor did not finishing joining its threads")
def test_handler_removal_failures_cannot_replace_required_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_server_seams(
        tmp_path,
        monkeypatch,
        orchestrator=_RequiredFailureOrchestrator,
    )

    def failing_remover(event: str):
        def remove() -> None:
            raise RuntimeError(f"password={event}-private")

        return remove

    monkeypatch.setattr(
        api_server,
        "_install_loop_exception_handler",
        lambda *_args: failing_remover("loop-removal"),
    )
    monkeypatch.setattr(
        api_server,
        "_install_signal_handlers",
        lambda *_args: failing_remover("signal-removal"),
    )
    secondary = SecondaryFailureLedger()
    fatal = [
        FatalBoundary(
            None,
            api_server.CorrelationContext(
                role=api_server.DiagnosticRole.CONTROLLER,
                instance_id="controller_00000001",
                component="controller",
            ),
            secondary,
        )
    ]
    with pytest.raises(ExceptionGroup) as caught:
        asyncio.run(
            api_server._run_api_server_impl(
                config_path="unused",
                host="127.0.0.1",
                port=0,
                instance_id="controller_00000001",
                fatal=fatal,
            )
        )
    assert caught.value is REQUIRED_FAILURE
    retained = secondary.snapshot()
    assert any("loop_exception_handler_removal_failed" in item for item in retained)
    assert any("signal_handler_removal_failed" in item for item in retained)
    assert all("-private" not in item for item in retained)


def test_preconfiguration_fatal_stderr_contains_application_build_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emergency: list[bytes] = []
    original = RuntimeError("preconfiguration fatal")

    async def fail(**_kwargs: object) -> None:
        raise original

    monkeypatch.setattr(api_server, "_run_api_server_impl", fail)
    monkeypatch.setattr(runtime_fatal, "direct_stderr", emergency.append)
    monkeypatch.setattr(api_server, "enable_faulthandler", lambda: True)

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(api_server.run_api_server(config_path="unused", host="127.0.0.1", port=0))
    assert caught.value is original
    assert emergency
    assert f"build=seasonalweather-{api_server.__version__}".encode() in emergency[0]
