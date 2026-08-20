from dataclasses import replace
import inspect

from seasonalweather.config import load_config
from seasonalweather.main import Orchestrator
from seasonalweather.broadcast.cap_policy import best_expiry_from_vtec


def _minimal_config(tmp_path, monkeypatch):
    monkeypatch.setenv("ICECAST_SOURCE_PASSWORD", "test-source")
    monkeypatch.setenv("NWWS_JID", "changeme@nwws-oi.weather.gov")
    monkeypatch.setenv("NWWS_PASSWORD", "CHANGEME")
    cfg = load_config("config/config.yaml")
    return replace(
        cfg,
        paths=replace(
            cfg.paths,
            work_dir=str(tmp_path / "work"),
            operational_state_dir=str(tmp_path / "state"),
            job_state_dir=str(tmp_path / "jobs"),
            artifact_dir=str(tmp_path / "artifacts"),
            audio_dir=str(tmp_path / "audio"),
            cache_dir=str(tmp_path / "cache"),
            config_dir=str(tmp_path / "config"),
            log_dir=str(tmp_path / "log"),
        ),
        network=replace(
            cfg.network,
            liquidsoap=replace(cfg.network.liquidsoap, host="liquidsoap.test", port=21234, timeout_seconds=4.5),
        ),
        database=replace(cfg.database, enabled=False),
        station_feed=replace(cfg.station_feed, enabled=False),
    )


def test_orchestrator_initializes_runtime_wiring(tmp_path, monkeypatch):
    cfg = _minimal_config(tmp_path, monkeypatch)

    orch = Orchestrator(cfg)

    assert orch.telnet.host == "liquidsoap.test"
    assert orch.telnet.port == 21234
    assert orch.telnet.timeout == 4.5
    assert orch.alert_tracker._path == tmp_path / "state" / "alert_state.json"
    assert orch.cap_text._best_expiry_from_vtec is best_expiry_from_vtec
    assert orch.target_resolver is orch.targeting
    assert orch.cap_runtime.orchestrator is orch
    assert orch.nwws_runtime._orchestrator is orch
    assert inspect.iscoroutinefunction(orch.run)


def test_best_expiry_from_vtec_returns_latest_utc_end():
    latest = best_expiry_from_vtec([
        "/O.NEW.KLWX.SV.W.0123.260614T0500Z-260614T0530Z/",
        "/O.NEW.KLWX.TO.W.0124.20260614T0500Z-20260614T0615Z/",
    ])

    assert latest is not None
    assert latest.isoformat() == "2026-06-14T06:15:00+00:00"
