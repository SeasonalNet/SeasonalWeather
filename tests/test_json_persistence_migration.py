from __future__ import annotations

import datetime as dt
from pathlib import Path

from seasonalweather.alerts.cap_ledger import CapLedger
from seasonalweather.broadcast.formatters import ObsPressureCache
from seasonalweather.broadcast.rwt_rmt import TestState as RwtState
from seasonalweather.broadcast.segment_store import SegmentStore
from seasonalweather.configuration_reload.candidate_store import CandidateStore
from seasonalweather.database.core import SeasonalDatabase
from seasonalweather.database.scheduler import SchedulerStateRepository
from seasonalweather.runtime_diagnostics.marker import ProcessMarkerStore, controller_marker
from seasonalweather.tts.audio import write_silence_wav


def test_restart_state_uses_sqlite_without_json_files(tmp_path: Path) -> None:
    db = SeasonalDatabase(path=str(tmp_path / "state.sqlite3"))

    ledger = CapLedger(database=db)
    ledger.mark("alert|sent")
    ledger.flush()
    assert not (tmp_path / "cap_ledger.json").exists()
    assert CapLedger(database=db).has("alert|sent")

    state = RwtState(last_rwt_period="2026-W35")
    state.save(str(tmp_path / "rwt_rmt_state.json"), repository=SchedulerStateRepository(db))
    assert not (tmp_path / "rwt_rmt_state.json").exists()

    cache = ObsPressureCache(database=db)
    cache.update("KAAA", 29.90)
    cache.update("KAAA", 29.95)
    reopened = ObsPressureCache(database=db)
    assert reopened.get_trend("KAAA", 29.95) == "rising"
    assert not (tmp_path / "obs_pressure_cache.json").exists()


def test_marker_candidate_and_segment_state_use_sqlite(tmp_path: Path) -> None:
    db = SeasonalDatabase(path=str(tmp_path / "state.sqlite3"))
    marker_root = tmp_path / "runtime"
    first = ProcessMarkerStore(marker_root, database=db)
    first.start(controller_marker(instance_id="controller_00000001", now=dt.datetime.now(dt.UTC)))
    first._release_lifetime_lock()
    second = ProcessMarkerStore(marker_root, database=db)
    prior = second.start(controller_marker(instance_id="controller_00000002", now=dt.datetime.now(dt.UTC)))
    assert prior is not None
    assert not (marker_root / "controller-runtime.json").exists()
    assert not (marker_root / "controller-runtime.previous.json").exists()
    second.mark_clean()

    source = tmp_path / "config.yaml"
    source.write_text("station:\n  name: Test\n", encoding="utf-8")
    candidates = CandidateStore(tmp_path / "candidates", database=db, identity_key=b"k" * 32)
    record, _ = candidates.capture(source)
    candidates.store_report(record, {"valid": True})
    candidate_dir = candidates.root / record.reference
    assert not (candidate_dir / "metadata.json").exists()
    assert not list(candidate_dir.glob("report_*.json"))

    audio_dir = tmp_path / "audio"
    candidate = audio_dir / ".candidate.wav"
    write_silence_wav(candidate, 0.1, 8000)
    segments = SegmentStore(tmp_path / "work", audio_dir, database=db)
    segments.commit_candidate(
        key="obs",
        title="Observations",
        text="clear",
        candidate_path=candidate,
        duration_s=0.1,
        refresh_interval_s=900,
        command_id="cmd_json_migration",
    )
    assert not (tmp_path / "work" / "segment_store.json").exists()
    assert not list((tmp_path / "work").glob(".segment-commit-*.json"))
    assert not list((tmp_path / "work").glob(".segment-commit-receipt-*.json"))
