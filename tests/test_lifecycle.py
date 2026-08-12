from __future__ import annotations

import ast
import asyncio
import signal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import yaml

from seasonalweather.api import server as api_server
from seasonalweather.api.api import create_app
from seasonalweather.api.commands import CommandStore
from seasonalweather.api.server import _install_signal_handlers
from seasonalweather.broadcast.alert_audio_jobs import AlertAudioDispatcher
from seasonalweather.broadcast.segment_store import render_segment_wav
from seasonalweather.config import load_config
from seasonalweather.health_service import HealthService
from seasonalweather.lifecycle import (
    AdmissionClosedError,
    Lifecycle,
    LifecycleState,
    LifecycleTimeouts,
    LifecycleTransitionError,
    PublicationFence,
    TaskSupervisor,
    WorkClass,
)
from seasonalweather.nwws.client import NWWSClient
from seasonalweather.tts.tts import TTS


class FakeControl:
    async def get_status(self) -> dict[str, Any]:
        return {"ok": True}


def _request(app: Any, method: str, path: str) -> httpx.Response:
    async def send() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            return await client.request(method, path)

    return asyncio.run(send())


def _short_timeouts() -> LifecycleTimeouts:
    return LifecycleTimeouts(
        total_seconds=0.2,
        active_request_seconds=0.05,
        publication_seconds=0.05,
        source_stop_seconds=0.05,
        tts_stop_seconds=0.05,
        task_cancel_seconds=0.05,
        resource_close_seconds=0.05,
    )


def test_lifecycle_transitions_admission_and_repeated_shutdown() -> None:
    async def exercise() -> None:
        lifecycle = Lifecycle(_short_timeouts())
        assert lifecycle.state is LifecycleState.STARTING
        with pytest.raises(AdmissionClosedError):
            lifecycle.require(WorkClass.COMMAND)

        lifecycle.mark_running()
        for work_class in WorkClass:
            lifecycle.require(work_class)

        assert lifecycle.request_shutdown() is True
        assert lifecycle.state is LifecycleState.DRAINING
        assert lifecycle.ready is False
        for work_class in WorkClass:
            with pytest.raises(AdmissionClosedError) as exc_info:
                lifecycle.require(work_class)
            assert exc_info.value.code == "service_draining"

        assert lifecycle.request_shutdown() is False
        assert lifecycle.force_requested is True
        lifecycle.mark_stopping()
        lifecycle.mark_stopped()
        assert lifecycle.state is LifecycleState.STOPPED
        with pytest.raises(LifecycleTransitionError):
            lifecycle.mark_failed()

    asyncio.run(exercise())


def test_invalid_transition_fails_closed_and_failed_is_terminal() -> None:
    async def exercise() -> None:
        lifecycle = Lifecycle(_short_timeouts())
        with pytest.raises(LifecycleTransitionError):
            lifecycle.mark_stopped()
        lifecycle.mark_failed()
        assert lifecycle.state is LifecycleState.FAILED
        assert lifecycle.request_shutdown() is False
        with pytest.raises(LifecycleTransitionError):
            lifecycle.mark_running()

    asyncio.run(exercise())


def test_publication_fence_waits_for_entered_section_and_rejects_late_entry() -> None:
    async def exercise() -> None:
        lifecycle = Lifecycle(_short_timeouts())
        lifecycle.mark_running()
        fence = PublicationFence(lifecycle)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def publish() -> None:
            async with fence.enter():
                entered.set()
                await release.wait()

        task = asyncio.create_task(publish(), name="test-publication")
        await entered.wait()
        admitted_permit = fence.issue_permit()
        lifecycle.request_shutdown()
        assert await fence.wait_idle(0.01) is False
        with pytest.raises(AdmissionClosedError):
            async with fence.enter():
                raise AssertionError("unreachable")
        token = fence.activate_permit(admitted_permit)
        try:
            async with fence.enter():
                assert fence.active == 2
        finally:
            fence.deactivate_permit(token)
        release.set()
        await task
        assert await fence.wait_idle(0.05) is True

    asyncio.run(exercise())


def test_supervisor_preserves_required_exception_group_and_bounds_cancel() -> None:
    async def exercise() -> None:
        lifecycle = Lifecycle(_short_timeouts())
        supervisor = TaskSupervisor(lifecycle)
        lifecycle.mark_running()
        original = ExceptionGroup(
            "required failure",
            [ValueError("first"), RuntimeError("second")],
        )

        async def fail() -> None:
            raise original

        async def linger() -> None:
            await asyncio.Event().wait()

        supervisor.create_task(
            fail(),
            name="required",
            required=True,
        )
        linger_task = supervisor.create_task(
            linger(),
            name="optional",
            required=False,
        )
        observed = await supervisor.wait_for_fatal()
        assert observed is original
        assert lifecycle.state is LifecycleState.FAILED
        await supervisor.stop()
        assert linger_task.cancelled()

    asyncio.run(exercise())


def test_optional_task_failure_is_degraded_not_fatal() -> None:
    async def exercise() -> None:
        lifecycle = Lifecycle(_short_timeouts())
        supervisor = TaskSupervisor(lifecycle)
        lifecycle.mark_running()

        async def fail() -> None:
            raise RuntimeError("SENTINEL-OPTIONAL-FAILURE")

        task = supervisor.create_task(
            fail(),
            name="optional-source",
            required=False,
        )
        await asyncio.gather(task, return_exceptions=True)
        assert lifecycle.state is LifecycleState.RUNNING
        assert supervisor.optional_failures == frozenset({"optional-source"})

    asyncio.run(exercise())


def test_command_tts_alert_and_http_admission_close_during_drain(
    tmp_path: Path,
) -> None:
    async def command_rejected(lifecycle: Lifecycle) -> None:
        store = CommandStore(lifecycle=lifecycle)
        with pytest.raises(AdmissionClosedError):
            await store.create_or_replay(
                command_type="test",
                idempotency_key="key",
                actor="operator",
                payload={},
            )

    lifecycle = Lifecycle(_short_timeouts())
    lifecycle.mark_running()
    lifecycle.request_shutdown()
    asyncio.run(command_rejected(lifecycle))

    tts = TTS(
        backend="unsupported",
        voice="",
        rate_wpm=100,
        volume=1.0,
        sample_rate=44100,
        admission_check=lambda: lifecycle.require(WorkClass.TTS),
    )
    with pytest.raises(AdmissionClosedError):
        tts.synth_to_wav("never synthesized", tmp_path / "blocked.wav")

    dispatcher = AlertAudioDispatcher(admission_check=lambda: lifecycle.require(WorkClass.ALERT))

    async def alert_rejected() -> None:
        with pytest.raises(AdmissionClosedError):
            await dispatcher.submit(
                priority=0,
                mode="full",
                source="test",
                render=lambda: asyncio.sleep(
                    0,
                    result=tmp_path / "never.wav",
                ),
                push=lambda _path: asyncio.sleep(0),
            )

    asyncio.run(alert_rejected())

    app = create_app(
        FakeControl(),
        health_service=HealthService(
            [],
            lifecycle_state=lambda: lifecycle.state.value,
        ),
        lifecycle=lifecycle,
    )
    assert _request(app, "GET", "/healthz").status_code == 200
    readiness = _request(app, "GET", "/readyz")
    assert readiness.status_code == 503
    assert readiness.json()["lifecycle_state"] == "draining"
    response = _request(app, "POST", "/v1/auth/token")
    assert response.status_code == 503
    assert response.json()["code"] == "service_draining"


def test_alert_admitted_before_drain_may_reach_publication_boundary(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        lifecycle = Lifecycle(_short_timeouts())
        lifecycle.mark_running()
        fence = PublicationFence(lifecycle)
        supervisor = TaskSupervisor(lifecycle)
        dispatcher = AlertAudioDispatcher(
            admission_check=lambda: lifecycle.require(WorkClass.ALERT),
            publication_fence=fence,
        )
        dispatcher.start_supervised(supervisor)
        render_started = asyncio.Event()
        release_render = asyncio.Event()
        pushed: list[Path] = []

        async def render() -> Path:
            render_started.set()
            await release_render.wait()
            return tmp_path / "admitted-alert.wav"

        async def push(path: Path) -> None:
            async with fence.enter():
                pushed.append(path)

        submitted = asyncio.create_task(
            dispatcher.render_and_push_full(
                source="test",
                render=render,
                push=push,
            ),
            name="test-admitted-alert",
        )
        await render_started.wait()
        lifecycle.request_shutdown()
        release_render.set()
        await submitted
        assert pushed == [tmp_path / "admitted-alert.wav"]
        assert await dispatcher.wait_idle(0.05) is True
        await supervisor.stop()

    asyncio.run(exercise())


def test_late_routine_synthesis_cannot_replace_authoritative_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "active.wav"
    output.write_bytes(b"previous-authoritative-audio")
    checks = 0

    def admission_check() -> None:
        nonlocal checks
        checks += 1
        if checks > 1:
            raise AdmissionClosedError(WorkClass.TTS)

    class FakeTTS:
        def __init__(self) -> None:
            self.admission_check = admission_check

        def synth_to_wav(self, _text: str, path: Path, *, purpose: str = "routine") -> None:
            assert purpose == "routine"
            self.admission_check()
            path.write_bytes(b"new-tts")

    monkeypatch.setattr(
        "seasonalweather.broadcast.segment_store.write_silence_wav",
        lambda path, *_args: Path(path).write_bytes(b"gap"),
    )
    monkeypatch.setattr(
        "seasonalweather.broadcast.segment_store.concat_wavs",
        lambda path, _parts: Path(path).write_bytes(b"new-complete-audio"),
    )
    monkeypatch.setattr(
        "seasonalweather.broadcast.segment_store.wav_duration_seconds",
        lambda _path: 1.0,
    )

    with pytest.raises(AdmissionClosedError):
        render_segment_wav(
            FakeTTS(),  # type: ignore[arg-type]
            "routine text",
            output,
            sample_rate=44100,
        )

    assert output.read_bytes() == b"previous-authoritative-audio"
    assert not list(tmp_path.glob("*.tmp.wav"))


def test_controller_installs_and_removes_exact_signal_handlers() -> None:
    installed: list[signal.Signals] = []
    removed: list[signal.Signals] = []

    def callback() -> None:
        return None

    loop = SimpleNamespace(
        add_signal_handler=lambda sig, cb: (
            installed.append(sig),
            cb is callback,
        ),
        remove_signal_handler=lambda sig: removed.append(sig),
    )

    remove = _install_signal_handlers(loop, callback)  # type: ignore[arg-type]
    assert installed == [signal.SIGTERM, signal.SIGINT]
    remove()
    assert removed == installed


def test_nwws_shutdown_closes_future_worker_start() -> None:
    async def exercise() -> None:
        client = NWWSClient(
            "synthetic@example.invalid",
            "synthetic-password",
            "example.invalid",
            5222,
            asyncio.Queue(),
        )
        client.request_shutdown()
        await client.run_forever()
        assert client._worker_id == 0
        assert client._thread is None

    asyncio.run(exercise())


def test_entrypoint_returns_zero_for_clean_and_nonzero_for_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def clean(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(api_server, "run_api_server", clean)
    assert api_server.main(["--config", "unused"]) == 0

    original = RuntimeError("SENTINEL-FATAL-ENTRYPOINT")

    async def fail(**_kwargs: Any) -> None:
        raise original

    monkeypatch.setattr(api_server, "run_api_server", fail)
    assert api_server.main(["--config", "unused"]) == 1


def test_lifecycle_timeout_configuration_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    raw = yaml.safe_load((repo_root / "config" / "config.yaml").read_text(encoding="utf-8"))
    raw["lifecycle"]["total_seconds"] = 2.0
    raw["lifecycle"]["publication_seconds"] = 3.0
    config_path = tmp_path / "invalid-lifecycle.yaml"
    config_path.write_text(
        yaml.safe_dump(raw, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("ICECAST_SOURCE_PASSWORD", "synthetic-source-password")

    with pytest.raises(ValueError, match="must cover every stage timeout"):
        load_config(str(config_path))


def test_production_controller_long_running_tasks_use_supervision() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    runtime_path = repo_root / "seasonalweather" / "broadcast" / "service_runtime.py"
    server_path = repo_root / "seasonalweather" / "api" / "server.py"
    test_runtime_path = repo_root / "seasonalweather" / "broadcast" / "tests_runtime.py"
    alert_audio_path = repo_root / "seasonalweather" / "broadcast" / "alert_audio_jobs.py"
    runtime_source = runtime_path.read_text(encoding="utf-8")
    assert "asyncio.create_task(" not in runtime_source

    names: set[str] = set()
    for path in (
        runtime_path,
        server_path,
        test_runtime_path,
        alert_audio_path,
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "create_task":
                continue
            for keyword in node.keywords:
                if (
                    keyword.arg == "name"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    names.add(keyword.value.value)

    assert {
        "seasonalweather-api",
        "seasonalweather-orchestrator",
        "health_state",
        "alert_audio_dispatcher",
        "conductor",
        "segment_refresher",
        "pns_api_backfill",
        "now_cycle_worker",
        "now_api_backfill",
        "nwws_xmpp",
        "nwws_consumer",
        "cap_poller",
        "cap_consumer",
        "ipaws_poller",
        "ipaws_consumer",
        "ern_monitor",
        "ern_consumer",
        "rwt_rmt_scheduler",
        "database_housekeeping",
        "discord_log_drain",
    }.issubset(names)
