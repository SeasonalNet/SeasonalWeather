from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import seasonalweather.configuration_reload.resources as resources_module
from seasonalweather.configuration import build_runtime_config, compile_path
from seasonalweather.configuration_reload.diff import build_reload_diff
from seasonalweather.configuration_reload.models import ReloadDisposition
from seasonalweather.configuration_reload.resources import OrchestratorResourcePreparer
from seasonalweather.configuration_reload.safe_point import ActivityRegistry, CONDUCTOR, TTS

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "config/config.yaml"
ENVIRONMENT = {
    "ICECAST_SOURCE_PASSWORD": "synthetic-icecast-password",
    "SEASONAL_API_TOKEN": "synthetic-test-api-token",
}


class _Lifecycle:
    def require(self, _work_class: object) -> None:
        return


class _FlakyOrchestrator(SimpleNamespace):
    def __init__(self, **values: object) -> None:
        object.__setattr__(self, "_failures", {})
        super().__init__(**values)

    def __setattr__(self, name: str, value: object) -> None:
        failures = object.__getattribute__(self, "_failures")
        remaining = int(failures.get(name, 0))
        if remaining:
            failures[name] = remaining - 1
            raise RuntimeError(f"injected resource assignment failure: {name}")
        super().__setattr__(name, value)

    def fail_next(self, name: str) -> None:
        self._failures[name] = 1


def _candidate(tmp_path: Path, old: str, new: str):
    path = tmp_path / "candidate.yaml"
    path.write_text(EXAMPLE.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")
    return compile_path(path, environ=ENVIRONMENT)


def _diff(active, candidate):
    return build_reload_diff(
        active,
        candidate,
        active_generation=4,
        active_identity_sha256="a" * 64,
        candidate_identity_sha256="b" * 64,
        report_sha256="c" * 64,
    )


def _orchestrator(configuration):
    tts = object()
    conductor = SimpleNamespace(
        _tts=tts,
        _tz=ZoneInfo(configuration.station.timezone),
        _sample_rate=configuration.audio.sample_rate,
        _alert_focus_policy=configuration.cycle.alert_focus,
    )
    originator = SimpleNamespace(cfg=configuration, tts=tts)
    return _FlakyOrchestrator(
        cfg=configuration,
        configuration_generation=4,
        lifecycle=_Lifecycle(),
        local_tz=ZoneInfo(configuration.station.timezone),
        tts=tts,
        conductor=conductor,
        audio_originator=originator,
        _dedupe_ttl_seconds=configuration.dedupe.ttl_seconds,
        _norm_wfo_set=lambda values: {str(value).upper() for value in values},
    )


def test_production_live_dedupe_plan_preserves_quiescent_references_during_active_work(
    tmp_path: Path,
    monkeypatch,
) -> None:
    active_compiled = compile_path(EXAMPLE, environ=ENVIRONMENT)
    candidate_compiled = _candidate(
        tmp_path,
        "dedupe:\n  ttl_seconds: 900",
        "dedupe:\n  ttl_seconds: 901",
    )
    active = build_runtime_config(active_compiled, environ=ENVIRONMENT)
    candidate = build_runtime_config(candidate_compiled, environ=ENVIRONMENT)
    orch = _orchestrator(active)
    registry = ActivityRegistry()
    preparer = OrchestratorResourcePreparer(orch, registry)
    plan = asyncio.run(
        preparer.prepare(
            candidate,
                diff=_diff(active_compiled, candidate_compiled),
                expected_generation=4,
                target_generation=5,
            candidate_identity_sha256="b" * 64,
        )
    )

    old_tts = orch.tts
    old_conductor_tts = orch.conductor._tts
    old_originator_config = orch.audio_originator.cfg
    station_feed_updates: list[object] = []
    monkeypatch.setattr(resources_module, "set_station_feed_config", station_feed_updates.append)
    assert plan.required_disposition is ReloadDisposition.LIVE
    assert plan.tts is None and plan.cycle_builder is None and plan.targeting is None
    with registry.activity(TTS), registry.activity(CONDUCTOR):
        plan.activate()
    assert orch.tts is old_tts
    assert orch.conductor._tts is old_conductor_tts
    assert orch.audio_originator.cfg is old_originator_config
    assert station_feed_updates == []
    assert orch._dedupe_ttl_seconds == 901


def test_production_quiescent_replacement_plan_requires_safe_point_proof(tmp_path: Path) -> None:
    active_compiled = compile_path(EXAMPLE, environ=ENVIRONMENT)
    candidate_compiled = _candidate(tmp_path, 'voice: "9"', 'voice: "8"')
    active = build_runtime_config(active_compiled, environ=ENVIRONMENT)
    candidate = build_runtime_config(candidate_compiled, environ=ENVIRONMENT)
    orch = _orchestrator(active)
    registry = ActivityRegistry()
    plan = asyncio.run(
        OrchestratorResourcePreparer(orch, registry).prepare(
            candidate,
                diff=_diff(active_compiled, candidate_compiled),
                expected_generation=4,
                target_generation=5,
            candidate_identity_sha256="b" * 64,
        )
    )

    assert plan.required_disposition is ReloadDisposition.QUIESCENT
    assert plan.tts is not None
    with pytest.raises(ValueError, match="held safe point"):
        plan.activate()
    assert orch.tts is not plan.tts
    plan.activate(safe_point_acquired=True)
    assert orch.tts is plan.tts


def _production_quiescent_plan(tmp_path: Path, monkeypatch):
    active_compiled = compile_path(EXAMPLE, environ=ENVIRONMENT)
    candidate_compiled = _candidate(tmp_path, 'voice: "9"', 'voice: "8"')
    active = build_runtime_config(active_compiled, environ=ENVIRONMENT)
    candidate = build_runtime_config(candidate_compiled, environ=ENVIRONMENT)
    orch = _orchestrator(active)
    registry = ActivityRegistry()
    plan = asyncio.run(
        OrchestratorResourcePreparer(orch, registry).prepare(
            candidate,
            diff=_diff(active_compiled, candidate_compiled),
            expected_generation=4,
            target_generation=5,
            candidate_identity_sha256="b" * 64,
        )
    )
    station_feed_updates: list[object] = []
    monkeypatch.setattr(resources_module, "set_station_feed_config", station_feed_updates.append)
    return orch, plan, active, candidate, station_feed_updates


def _assert_production_resources_restored(orch, plan, active, old_values) -> None:
    for owner, name, value in old_values:
        assert getattr(owner, name) == value
    assert orch.cfg is active
    assert orch.configuration_generation == 4
    assert plan._rolled_back


def test_production_activation_failure_after_reference_change_remains_rollback_capable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    orch, plan, active, _candidate_config, station_feed_updates = _production_quiescent_plan(tmp_path, monkeypatch)
    orch.fail_next("tts")

    with pytest.raises(RuntimeError, match="assignment failure: tts"):
        plan.activate(safe_point_acquired=True)

    assert not plan._activated
    assert plan._activation_started
    assert orch.cfg is not active
    plan.rollback()
    _assert_production_resources_restored(orch, plan, active, plan._snapshot)
    assert station_feed_updates == [active]


def test_production_rollback_retries_reference_and_station_feed_failures(
    tmp_path: Path,
    monkeypatch,
) -> None:
    orch, plan, active, candidate_config, station_feed_updates = _production_quiescent_plan(tmp_path, monkeypatch)
    plan.activate(safe_point_acquired=True)
    old_values = plan._snapshot
    assert station_feed_updates == [candidate_config]

    orch.fail_next("tts")
    with pytest.raises(RuntimeError, match="assignment failure: tts"):
        plan.rollback()
    assert not plan._rolled_back
    assert orch.tts is plan.tts
    assert orch.cfg is active
    assert orch.configuration_generation == 4

    plan.rollback()
    _assert_production_resources_restored(orch, plan, active, old_values)
    assert station_feed_updates[-1] is active


def test_production_station_feed_failure_keeps_rollback_retryable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    orch, plan, active, candidate_config, station_feed_updates = _production_quiescent_plan(tmp_path, monkeypatch)
    plan.activate(safe_point_acquired=True)
    old_values = plan._snapshot
    assert station_feed_updates == [candidate_config]

    failures = 1

    def fail_once(configuration):
        nonlocal failures
        station_feed_updates.append(configuration)
        if failures:
            failures -= 1
            raise RuntimeError("injected station-feed restoration failure")

    monkeypatch.setattr(resources_module, "set_station_feed_config", fail_once)
    with pytest.raises(RuntimeError, match="station-feed restoration failure"):
        plan.rollback()
    assert not plan._rolled_back
    plan.rollback()
    _assert_production_resources_restored(orch, plan, active, old_values)
    assert station_feed_updates[-1] is active
