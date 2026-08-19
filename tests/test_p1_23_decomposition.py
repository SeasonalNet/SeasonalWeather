from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from seasonalweather.application.read_models import RuntimeReadModelService
from seasonalweather.artifacts.audio_assets import AudioAssetService
from seasonalweather.broadcast.cycle_insert_service import CycleInsertService
from seasonalweather.broadcast.manual_api_service import ManualOriginationService
from seasonalweather.broadcast.operator_service import BroadcastOperatorService
from seasonalweather.control import OrchestratorControl


ROOT = Path(__file__).parents[1]


def test_control_is_only_composition_and_compatibility_facade():
    source = (ROOT / "seasonalweather" / "control.py").read_text(encoding="utf-8")

    assert len(source.splitlines()) < 220
    assert "AudioAssetRepository" not in source
    assert "CycleInsertRepository" not in source
    assert "StationFeedRepository" not in source
    assert "render_segment_wav_async" not in source
    assert "validate_wav_upload" not in source
    assert "subprocess" not in source
    assert "sqlite3" not in source
    assert "_schedule_cycle_refill" not in source
    assert "_update_mode" not in source
    assert ".upsert_insert(" not in source
    assert ".copy2(" not in source


def test_control_composes_explicit_phase_one_application_owners():
    orchestrator = SimpleNamespace(database=None, station_feed_repo=None)
    control = OrchestratorControl(orchestrator, config_path="/nonexistent/config.yaml")

    assert isinstance(control._read_models, RuntimeReadModelService)
    assert isinstance(control._audio_assets, AudioAssetService)
    assert isinstance(control._broadcast_operations, BroadcastOperatorService)
    assert isinstance(control._manual_origination, ManualOriginationService)
    assert isinstance(control._cycle_inserts, CycleInsertService)


def test_control_mutation_methods_delegate_to_their_packet_owned_services(monkeypatch):
    orchestrator = SimpleNamespace(database=None, station_feed_repo=None)
    control = OrchestratorControl(orchestrator, config_path="/nonexistent/config.yaml")
    calls: list[tuple[str, dict[str, object]]] = []

    async def rebuild_cycle(**kwargs: object) -> dict[str, object]:
        calls.append(("broadcast", kwargs))
        return {"owner": "broadcast"}

    async def stage_upload(**kwargs: object) -> dict[str, object]:
        calls.append(("assets", kwargs))
        return {"owner": "assets"}

    monkeypatch.setattr(control._broadcast_operations, "rebuild_cycle", rebuild_cycle)
    monkeypatch.setattr(control._audio_assets, "stage_upload", stage_upload)

    async def exercise() -> tuple[dict[str, object], dict[str, object]]:
        return (
            await control.rebuild_cycle(reason="test", actor="tester"),
            await control.stage_wav_upload(
                filename="test.wav",
                content_type="audio/wav",
                data=b"bounded",
                actor="tester",
            ),
        )

    assert asyncio.run(exercise()) == ({"owner": "broadcast"}, {"owner": "assets"})
    assert [name for name, _kwargs in calls] == ["broadcast", "assets"]
