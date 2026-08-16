from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import seasonalweather.configuration_reload.resources as resources_module
from seasonalweather.broadcast.conductor import CycleConductor
from seasonalweather.broadcast.segment_registry import DEFAULT_SEGMENT_REGISTRY
from seasonalweather.configuration import build_runtime_config, compile_path
from seasonalweather.configuration_reload.diff import build_reload_diff
from seasonalweather.configuration_reload.models import ReloadDisposition
from seasonalweather.configuration_reload.resources import OrchestratorResourcePreparer
from seasonalweather.configuration_reload.safe_point import CONDUCTOR, TTS, ActivityRegistry

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
    segment_registry = DEFAULT_SEGMENT_REGISTRY.resolve(configuration.cycle)
    conductor = CycleConductor(
        store=SimpleNamespace(),
        telnet=SimpleNamespace(),
        tts=tts,
        alert_tracker=SimpleNamespace(get_cycle_alerts=lambda: ()),
        tz=ZoneInfo(configuration.station.timezone),
        audio_dir=Path("/tmp"),
        sample_rate=configuration.audio.sample_rate,
        np_meta_fn=lambda **_kwargs: {},
        registry=segment_registry,
        alert_focus_policy=configuration.cycle.alert_focus,
    )
    refresher = SimpleNamespace(
        _registry=segment_registry,
        _builder=object(),
        _tts=tts,
        _tz=ZoneInfo(configuration.station.timezone),
        _sample_rate=configuration.audio.sample_rate,
        _station_name=configuration.station.name,
        _service_area_name=configuration.station.service_area_name,
        _disclaimer=configuration.station.disclaimer,
        _seg_cache=None,
        _seg_cache_ts=0.0,
        _seg_cache_mode="",
    )
    originator = SimpleNamespace(cfg=configuration, tts=tts)
    return _FlakyOrchestrator(
        cfg=configuration,
        api=SimpleNamespace(),
        configuration_generation=4,
        lifecycle=_Lifecycle(),
        local_tz=ZoneInfo(configuration.station.timezone),
        tts=tts,
        conductor=conductor,
        refresher=refresher,
        segment_registry=segment_registry,
        cycle_builder=SimpleNamespace(_registry=segment_registry),
        audio_originator=originator,
        _dedupe_ttl_seconds=configuration.dedupe.ttl_seconds,
        _same_fips_allow_set=set(configuration.service_area.same_fips_all),
        targeting=SimpleNamespace(),
        target_resolver=SimpleNamespace(),
        _norm_wfo_set=lambda values: {str(value).upper() for value in values},
    )


def _cycle_toggle_diff(paths: tuple[tuple[str, ...], ...]):
    entries = tuple(
        SimpleNamespace(
            path=SimpleNamespace(
                segments=path,
                to_pointer=lambda path=path: "/" + "/".join(path),
            )
        )
        for path in paths
    )
    return SimpleNamespace(
        entries=entries,
        disposition=ReloadDisposition.QUIESCENT,
        digest="sha256:" + "e" * 64,
    )


def test_cycle_registry_replacement_is_prepared_and_activated_atomically(monkeypatch) -> None:
    active_compiled = compile_path(EXAMPLE, environ=ENVIRONMENT)
    active = build_runtime_config(active_compiled, environ=ENVIRONMENT)
    target_cycle = replace(
        active.cycle,
        spc=replace(active.cycle.spc, enabled=True),
        cwf=replace(active.cycle.cwf, enabled=True),
        marine_obs=replace(active.cycle.marine_obs, enabled=True),
    )
    candidate = replace(active, cycle=target_cycle)
    orch = _orchestrator(active)
    old_registry = orch.segment_registry
    old_builder = orch.cycle_builder
    old_refs = (orch.refresher._registry, orch.conductor._registry)
    old_cache = [SimpleNamespace(key="old-generation")]
    orch.refresher._seg_cache = old_cache
    orch.refresher._seg_cache_ts = 123.0
    orch.refresher._seg_cache_mode = "normal|normal|False"
    old_order = ["old-generation"]
    old_last_order = ["old-generation"]
    orch.conductor._cycle_order = old_order
    orch.conductor._position_in_rotation = 1
    orch.conductor._last_cycle_order = old_last_order
    station_feed_updates: list[object] = []
    monkeypatch.setattr(resources_module, "set_station_feed_config", station_feed_updates.append)

    plan = asyncio.run(
        OrchestratorResourcePreparer(orch, ActivityRegistry()).prepare(
            candidate,
            diff=_cycle_toggle_diff(
                (
                    ("cycle", "spc", "enabled"),
                    ("cycle", "cwf", "enabled"),
                    ("cycle", "marine_obs", "enabled"),
                )
            ),
            expected_generation=4,
            target_generation=5,
            candidate_identity_sha256="b" * 64,
        )
    )

    assert orch.cfg is active
    assert orch.segment_registry is old_registry
    assert orch.refresher._registry is old_refs[0]
    assert orch.conductor._registry is old_refs[1]
    assert orch.refresher._seg_cache is old_cache
    assert orch.refresher._seg_cache_ts == 123.0
    assert orch.refresher._seg_cache_mode == "normal|normal|False"
    assert orch.conductor._cycle_order is old_order
    assert orch.conductor._position_in_rotation == 1
    assert orch.conductor._last_cycle_order is old_last_order
    assert plan.segment_registry is not None
    assert all(plan.segment_registry.enabled(key) for key in ("spc", "cwf", "marine_obs"))
    assert plan.cycle_builder._registry is plan.segment_registry

    plan.activate(safe_point_acquired=True)
    assert orch.segment_registry is plan.segment_registry
    assert orch.refresher._registry is plan.segment_registry
    assert orch.conductor._registry is plan.segment_registry
    assert orch.cycle_builder._registry is plan.segment_registry
    assert orch.refresher._seg_cache is None
    assert orch.refresher._seg_cache_ts == 0.0
    assert orch.refresher._seg_cache_mode == ""
    assert orch.conductor._cycle_order == []
    assert orch.conductor._position_in_rotation == 0
    assert orch.conductor._last_cycle_order == []

    orch.conductor._rebuild_cycle_order()
    assert tuple(orch.conductor._cycle_order) == plan.segment_registry.static_order(focus=False)

    plan.rollback()
    assert orch.segment_registry is old_registry
    assert orch.refresher._registry is old_refs[0]
    assert orch.conductor._registry is old_refs[1]
    assert orch.cycle_builder is old_builder
    assert orch.refresher._seg_cache is old_cache
    assert orch.refresher._seg_cache_ts == 123.0
    assert orch.refresher._seg_cache_mode == "normal|normal|False"
    assert orch.conductor._cycle_order is old_order
    assert orch.conductor._position_in_rotation == 1
    assert orch.conductor._last_cycle_order is old_last_order
    assert station_feed_updates[-1] is active


@pytest.mark.parametrize("section", ["service_area", "observations"])
def test_non_registry_cycle_builder_replacement_invalidates_and_restores_refresher_cache(
    section: str,
    monkeypatch,
) -> None:
    active_compiled = compile_path(EXAMPLE, environ=ENVIRONMENT)
    active = build_runtime_config(active_compiled, environ=ENVIRONMENT)
    if section == "service_area":
        candidate = replace(
            active,
            service_area=replace(
                active.service_area,
                same_fips_all=[*active.service_area.same_fips_all, "999999"],
            ),
        )
        changed_path = ("service_area", "same_fips_all")
    else:
        candidate = replace(
            active,
            observations=replace(
                active.observations,
                stations=[*active.observations.stations, "KXXX"],
            ),
        )
        changed_path = ("observations", "stations")

    orch = _orchestrator(active)
    old_cache = [SimpleNamespace(key="old-generation")]
    orch.refresher._seg_cache = old_cache
    orch.refresher._seg_cache_ts = 321.0
    orch.refresher._seg_cache_mode = "normal|normal|False"
    old_registry = orch.segment_registry
    station_feed_updates: list[object] = []
    monkeypatch.setattr(resources_module, "set_station_feed_config", station_feed_updates.append)

    plan = asyncio.run(
        OrchestratorResourcePreparer(orch, ActivityRegistry()).prepare(
            candidate,
            diff=_cycle_toggle_diff((changed_path,)),
            expected_generation=4,
            target_generation=5,
            candidate_identity_sha256="b" * 64,
        )
    )

    assert plan.segment_registry is None
    assert orch.segment_registry is old_registry
    assert orch.refresher._seg_cache is old_cache

    plan.activate(safe_point_acquired=True)
    assert orch.segment_registry is old_registry
    assert orch.refresher._seg_cache is None
    assert orch.refresher._seg_cache_ts == 0.0
    assert orch.refresher._seg_cache_mode == ""

    plan.rollback()
    assert orch.refresher._seg_cache is old_cache
    assert orch.refresher._seg_cache_ts == 321.0
    assert orch.refresher._seg_cache_mode == "normal|normal|False"
    assert station_feed_updates[-1] is active


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
