from __future__ import annotations

import asyncio
import datetime as dt
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from seasonalweather.artifacts.models import MediaMetadata
from seasonalweather.broadcast.cycle import CycleContext, CycleSegment, station_id_text
from seasonalweather.broadcast.segment_builders import (
    SegmentBuildInput,
    SegmentCandidate,
    SegmentProvenance,
    sanitize_error,
)
from seasonalweather.broadcast.segment_refresher import SegmentRefresher
from seasonalweather.broadcast.segment_registry import DEFAULT_SEGMENT_REGISTRY
from seasonalweather.broadcast.segment_service import SegmentApplicationService, SegmentServiceError
from seasonalweather.broadcast.segment_store import (
    RefreshReconciliationOutcome,
    SegmentCommitAmbiguousError,
    SegmentStore,
)
from seasonalweather.commands import CommandStore
from seasonalweather.commands.contracts import CommandRecord, CommandStatus, CommandType
from seasonalweather.database.core import SeasonalDatabase
from seasonalweather.lifecycle import Lifecycle, TaskSupervisor
from seasonalweather.tts.async_bridge import FinalizationEvidence, synthesize_completed_wav_async
from seasonalweather.tts.audio import write_silence_wav
from seasonalweather.tts.local import LocalHandlerResult
from seasonalweather.tts.tts import TTS


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        spc=SimpleNamespace(enabled=True),
        cwf=SimpleNamespace(enabled=True),
        offnt2=SimpleNamespace(enabled=False),
        marine_obs=SimpleNamespace(enabled=True),
        hwo=SimpleNamespace(speak_unavailable=True),
    )


def _real_segment_refresher(store: SegmentStore, tmp_path: Path) -> SegmentRefresher:
    """Use production refresher/store/TTS/service/async-bridge seams."""

    class Builder:
        async def build_obs_segment(self, request: SegmentBuildInput) -> SegmentCandidate:
            return SegmentCandidate.from_cycle_segment(
                CycleSegment(key=request.key, title="Observations", text="new observation")
            )

    tts = TTS(
        backend="local",
        voice="test",
        rate_wpm=175,
        volume=1.0,
        sample_rate=8000,
        local_engine="espeak-ng",
        allow_transitional_qualification=True,
    )
    synthesis_service = tts._service()

    def fake_local_handler(request, text, engine, raw_dir, deadline, cancellation, capacity_reservation=None):
        del request, text, deadline, cancellation, capacity_reservation
        output = raw_dir / "engine.wav"
        write_silence_wav(output, 0.1, 8000)
        return LocalHandlerResult(output_path=output, engine=engine)

    def fake_normalize(source, request, raw_dir, deadline, cancellation):
        del request, raw_dir, deadline, cancellation
        return source, MediaMetadata(
            media_type="audio/wav",
            encoding="pcm_s16le",
            sample_width_bytes=2,
            sample_rate_hz=8000,
            channels=1,
            frame_count=800,
            duration_seconds=0.1,
        )

    # Only the physical engine and ffmpeg normalization resources are bounded
    # test doubles; TTS, SynthesisService, the finalizer, and async bridge are
    # production implementations.
    synthesis_service._invoke_local_handler = fake_local_handler  # type: ignore[method-assign]
    synthesis_service._normalize_local_audio = fake_normalize  # type: ignore[method-assign]

    class TestSynthesisClient:
        async def synthesize(self, text: str, output_path: Path, *, purpose: str = "routine") -> None:
            del purpose
            await asyncio.to_thread(tts.synth_to_wav, text, output_path)

    refresher = SegmentRefresher(
        store=store,
        cycle_builder=Builder(),  # type: ignore[arg-type]
        tts=TestSynthesisClient(),
        alert_tracker=SimpleNamespace(),
        ctx_fn=lambda: CycleContext(mode="normal", last_heightened_ago=None, last_product_desc=None),
        station_name="station",
        service_area_name="area",
        disclaimer="disclaimer",
        tz=ZoneInfo("UTC"),
        sample_rate=8000,
        registry=DEFAULT_SEGMENT_REGISTRY.resolve(_config()),
    )
    refresher._legacy_tts_for_bridge_tests = tts
    return refresher


def test_every_static_product_uses_one_independent_builder_method() -> None:
    registry = DEFAULT_SEGMENT_REGISTRY.resolve(_config())
    product_keys = ("health", "hwo", "spc", "zfp", "fcst", "cwf", "offnt2", "obs", "marine_obs")
    operations = [registry.get(key).builder.operation for key in product_keys]
    assert len(operations) == len(set(operations))
    assert all(operation.startswith("CycleBuilder.build_") for operation in operations)
    assert all("build_segments" not in operation for operation in operations)


def test_refreshing_one_target_calls_only_that_builder() -> None:
    registry = DEFAULT_SEGMENT_REGISTRY.resolve(_config())
    calls: list[str] = []

    class Store:
        async def update(self, **_kwargs) -> None:
            return None

        async def mark_placeholder(self, *_args, **_kwargs) -> None:
            raise AssertionError("test builder must return a candidate")

        async def synth_and_update(self, *_args, **_kwargs) -> float:
            return 1.0

    class Builder:
        def __getattr__(self, name: str):
            if not name.startswith("build_"):
                raise AttributeError(name)

            async def build(request):
                calls.append(name)
                return SegmentCandidate.from_cycle_segment(
                    CycleSegment(key=request.key, title=request.key, text=f"text for {request.key}")
                )

            return build

    refresher = SegmentRefresher(
        store=Store(),
        cycle_builder=Builder(),  # type: ignore[arg-type]
        tts=SimpleNamespace(),
        alert_tracker=SimpleNamespace(),
        ctx_fn=lambda: CycleContext(mode="normal", last_heightened_ago=None, last_product_desc=None),
        station_name="station",
        service_area_name="area",
        disclaimer="disclaimer",
        tz=ZoneInfo("UTC"),
        sample_rate=8000,
        registry=registry,
    )
    synthesized: list[str] = []

    async def capture(**kwargs) -> None:
        synthesized.append(str(kwargs["key"]))

    refresher._synth = capture  # type: ignore[method-assign]
    asyncio.run(refresher.refresh_one("obs"))
    assert calls == ["build_obs_segment"]
    assert synthesized == ["obs"]


def test_provenance_round_trip_and_secret_safe_failure(tmp_path: Path) -> None:
    wav = tmp_path / "audio" / "cycle_seg_obs.wav"
    wav.parent.mkdir()
    wav.write_bytes(b"synthetic wav")
    store = SegmentStore(tmp_path / "work", tmp_path / "audio")
    provenance = SegmentProvenance(
        source_name="nws",
        product_identifier="RWR-123",
        product_type="RWR",
        issuing_office="LWX",
        source_reference="https://user:password@example.test/product?id=secret",
    )
    asyncio.run(
        store.update(
            "obs",
            "Observations",
            "conditions",
            wav,
            2.0,
            900,
            1800,
            provenance=provenance,
        )
    )
    asyncio.run(store.record_failure("obs", "Authorization: bearer secret-value"))
    entry = store.get("obs")
    assert entry is not None
    assert entry.provenance.current_content_hash
    assert entry.provenance.source_reference == "https://example.test/product"
    assert entry.provenance.consecutive_failures == 1
    assert "secret-value" not in (entry.provenance.last_error or "")
    assert store.is_ready("obs")

    reopened = SegmentStore(tmp_path / "work", tmp_path / "audio")
    assert reopened.load() == 1
    restored = reopened.get("obs")
    assert restored is not None
    assert restored.provenance.source_reference == "https://example.test/product"


def test_cold_failure_is_placeholder_stale_and_retries_then_success_resets(tmp_path: Path) -> None:
    store = SegmentStore(tmp_path / "work", tmp_path / "audio")
    asyncio.run(
        store.record_failure(
            "hwo",
            "upstream unavailable",
            title="Hazardous weather outlook",
            refresh_interval_s=60,
            max_age_s=120,
        )
    )
    first = store.get("hwo")
    assert first is not None and first.is_placeholder and first.is_stale()
    assert first.refresh_interval_s == 60 and first.max_age_s == 120
    assert store.is_stale("hwo")

    asyncio.run(
        store.record_failure(
            "hwo",
            "second failure",
            title="Hazardous weather outlook",
            refresh_interval_s=60,
            max_age_s=120,
        )
    )
    assert store.get("hwo").provenance.consecutive_failures == 2

    wav = tmp_path / "audio" / "cycle_seg_hwo.wav"
    wav.parent.mkdir(parents=True, exist_ok=True)
    wav.write_bytes(b"wav")
    asyncio.run(store.update("hwo", "Hazardous weather outlook", "fresh", wav, 1.0, 60, 120))
    recovered = store.get("hwo")
    assert recovered is not None and not recovered.is_placeholder
    assert recovered.provenance.consecutive_failures == 0
    assert recovered.provenance.last_error is None


def test_error_redaction_covers_credentials_urls_and_control_data() -> None:
    rendered = sanitize_error(
        "client_secret=alpha access_token=beta refresh-token=gamma "
        "password:delta https://user:pw@example.test/path?api_key=epsilon\nnext"
    )
    assert rendered is not None
    assert all(secret not in rendered for secret in ("alpha", "beta", "gamma", "delta", "epsilon", "user", "pw"))
    assert "example.test/path" in rendered
    assert "\n" not in rendered and len(rendered) <= 256


def test_dirty_legacy_provenance_is_sanitized_before_api_projection(tmp_path: Path) -> None:
    sentinels = ("ABC123", "TOPSECRET", "REFRESH", "PW", "QUERYSECRET")
    audio = tmp_path / "audio" / "cycle_seg_obs.wav"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"accepted")
    payload = {
        "entries": [
            {
                "key": "obs",
                "title": "Observations",
                "text": "accepted",
                "audio_path": str(audio),
                "duration_s": 1.0,
                "last_updated_ts": 1.0,
                "refresh_interval_s": 900,
                "max_age_s": 1800,
                "is_placeholder": False,
                "provenance": {
                    "source_name": "client_secret=TOPSECRET",
                    "product_identifier": "access_token=ABC123",
                    "product_type": "RWR",
                    "issuing_office": "LWX",
                    "issuance_time": "2026-08-16T12:00:00Z",
                    "fetch_time": "2026-08-16T12:01:00Z",
                    "last_successful_synthesis": "2026-08-16T12:02:00Z",
                    "current_content_hash": "a" * 64,
                    "source_reference": "https://user:PW@example.test/x?token=QUERYSECRET",
                    "last_error": '{"access_token":"ABC123","password":"PW"}',
                    "consecutive_failures": 2,
                    "stale": False,
                    "placeholder": False,
                    "last_aired": None,
                    "next_eligible_airtime": None,
                },
            }
        ]
    }
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "segment_store.json").write_text(json.dumps(payload), encoding="utf-8")
    store = SegmentStore(tmp_path / "work", tmp_path / "audio")
    assert store.load() == 1
    registry = DEFAULT_SEGMENT_REGISTRY.resolve(_config())
    service = SegmentApplicationService(
        registry=lambda: registry,
        store=store,
        refresher=SimpleNamespace(),
        mode=lambda: "normal",
    )
    serialized = json.dumps({"list": service.list_segments(), "detail": service.get_segment("obs")})
    assert all(sentinel not in serialized for sentinel in sentinels)
    assert "example.test/x" in serialized


def _seed_store_with_audio(tmp_path: Path) -> tuple[SegmentStore, bytes]:
    store = SegmentStore(tmp_path / "work", tmp_path / "audio")
    stable = store.audio_path_for("obs")
    stable.parent.mkdir(parents=True, exist_ok=True)
    write_silence_wav(stable, 0.1, 8000)
    old_bytes = stable.read_bytes()
    asyncio.run(store.update("obs", "Observations", "old", stable, 0.1, 900, 1800))
    return store, old_bytes


def test_segment_commit_persists_state_and_artifact_together(tmp_path: Path) -> None:
    store, old_bytes = _seed_store_with_audio(tmp_path)
    candidate = tmp_path / "audio" / ".candidate.wav"
    write_silence_wav(candidate, 0.2, 8000)
    store.commit_candidate(
        key="obs",
        title="Observations",
        text="new",
        candidate_path=candidate,
        duration_s=0.2,
        refresh_interval_s=900,
        max_age_s=1800,
        provenance=SegmentProvenance(source_name="asos", product_type="ASOS"),
    )
    assert store.get("obs").text == "new"
    assert store.get("obs").provenance.source_name == "asos"
    assert store.audio_path_for("obs").read_bytes() == old_bytes
    assert Path(store.get("obs").audio_path).read_bytes() != old_bytes
    reopened = SegmentStore(tmp_path / "work", tmp_path / "audio")
    assert reopened.load() == 1
    assert reopened.get("obs").text == "new"
    assert reopened.get("obs").provenance.product_type == "ASOS"


def test_segment_commit_persistence_failure_restores_lkg(tmp_path: Path, monkeypatch) -> None:
    store, old_bytes = _seed_store_with_audio(tmp_path)
    candidate = tmp_path / "audio" / ".candidate.wav"
    write_silence_wav(candidate, 0.2, 8000)

    def fail_persist() -> None:
        raise OSError("injected persistence failure")

    monkeypatch.setattr(store, "_persist_unlocked", fail_persist)
    with pytest.raises(OSError):
        store.commit_candidate(
            key="obs",
            title="Observations",
            text="new",
            candidate_path=candidate,
            duration_s=0.2,
            refresh_interval_s=900,
            max_age_s=1800,
        )
    assert store.get("obs").text == "old"
    assert store.audio_path_for("obs").read_bytes() == old_bytes
    assert not candidate.exists()
    reopened = SegmentStore(tmp_path / "work", tmp_path / "audio")
    assert reopened.load() == 1
    assert reopened.get("obs").text == "old"
    assert Path(reopened.get("obs").audio_path).read_bytes() == old_bytes


def _inject_metadata_directory_failure(monkeypatch, store: SegmentStore) -> list[int]:
    import seasonalweather.broadcast.segment_store as segment_store_module

    workdir_calls = [0]
    original_fsync_directory = segment_store_module._fsync_directory

    def fail_after_replace(path: Path) -> None:
        if path == store._work_dir:
            workdir_calls[0] += 1
            if workdir_calls[0] == 2:
                raise OSError("injected metadata directory fsync failure")
        original_fsync_directory(path)

    monkeypatch.setattr(segment_store_module, "_fsync_directory", fail_after_replace)
    return workdir_calls


async def _prepare_commanded_ambiguous_commit(tmp_path: Path, monkeypatch):
    lifecycle = Lifecycle()
    lifecycle.mark_running()
    command_store = CommandStore(lifecycle=lifecycle)
    store = SegmentStore(tmp_path / "work", tmp_path / "audio")
    stable = store.audio_path_for("obs")
    stable.parent.mkdir(parents=True, exist_ok=True)
    write_silence_wav(stable, 0.1, 8000)
    await store.update("obs", "Observations", "old", stable, 0.1, 900, 1800)
    command, _ = await command_store.create_or_replay(
        command_type="segment.refresh",
        idempotency_key="metadata-reconciliation",
        actor="tester",
        payload={"segment_key": "obs"},
        reason="segment-refresh:obs",
    )
    candidate = tmp_path / "audio" / ".metadata-reconciliation.wav"
    write_silence_wav(candidate, 0.2, 8000)
    _inject_metadata_directory_failure(monkeypatch, store)
    with pytest.raises(SegmentCommitAmbiguousError):
        store.commit_candidate(
            key="obs",
            title="Observations",
            text="new metadata",
            candidate_path=candidate,
            duration_s=0.2,
            refresh_interval_s=900,
            max_age_s=1800,
            command_id=command.command_id,
        )
    target = Path(store.get("obs").audio_path)
    metadata_path = tmp_path / "work" / "segment_store.json"
    return store, command_store, command, target, metadata_path


def test_segment_commit_post_replace_failure_retains_unresolved_evidence(tmp_path: Path, monkeypatch) -> None:
    store, _old_bytes = _seed_store_with_audio(tmp_path)
    candidate = tmp_path / "audio" / ".candidate.wav"
    write_silence_wav(candidate, 0.2, 8000)
    calls = _inject_metadata_directory_failure(monkeypatch, store)

    with pytest.raises(SegmentCommitAmbiguousError):
        store.commit_candidate(
            key="obs",
            title="Observations",
            text="new",
            candidate_path=candidate,
            duration_s=0.2,
            refresh_interval_s=900,
            command_id="cmd_ambiguous",
        )

    assert calls == [2]
    target = Path(store.get("obs").audio_path)
    assert target.exists()
    assert json.loads((tmp_path / "work" / "segment_store.json").read_text(encoding="utf-8"))["entries"][0][
        "audio_path"
    ] == str(target)
    assert store.refresh_evidence_state("obs", "cmd_ambiguous").value == "unresolved"
    assert store._journal_paths_for_key("obs")
    assert not store._receipt_path("obs", "cmd_ambiguous").exists()


def test_segment_commit_post_replace_restart_completes_new_publication(tmp_path: Path, monkeypatch) -> None:
    store, old_bytes = _seed_store_with_audio(tmp_path)
    candidate = tmp_path / "audio" / ".candidate.wav"
    write_silence_wav(candidate, 0.2, 8000)
    _inject_metadata_directory_failure(monkeypatch, store)

    with pytest.raises(SegmentCommitAmbiguousError):
        store.commit_candidate(
            key="obs",
            title="Observations",
            text="new",
            candidate_path=candidate,
            duration_s=0.2,
            refresh_interval_s=900,
            command_id="cmd_restart_ambiguous",
        )
    new_target = Path(store.get("obs").audio_path)

    reopened = SegmentStore(tmp_path / "work", tmp_path / "audio")
    assert reopened.load() == 1
    assert reopened.get("obs").text == "new"
    assert Path(reopened.get("obs").audio_path) == new_target
    assert new_target.exists()
    assert new_target.read_bytes() != old_bytes
    assert reopened.committed_refresh_receipts()[0].command_id == "cmd_restart_ambiguous"
    assert not reopened._journal_paths_for_key("obs")


def test_command_bearing_ambiguous_commit_repairs_only_after_publication_reconciliation(
    tmp_path: Path, monkeypatch
) -> None:
    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        command_store = CommandStore(lifecycle=lifecycle)
        store = SegmentStore(tmp_path / "work", tmp_path / "audio")
        stable = store.audio_path_for("obs")
        stable.parent.mkdir(parents=True, exist_ok=True)
        write_silence_wav(stable, 0.1, 8000)
        await store.update("obs", "Observations", "old", stable, 0.1, 900, 1800)
        command, _ = await command_store.create_or_replay(
            command_type="segment.refresh",
            idempotency_key="ambiguous-command",
            actor="tester",
            payload={"segment_key": "obs"},
            reason="segment-refresh:obs",
        )
        _inject_metadata_directory_failure(monkeypatch, store)

        class Refresher:
            async def refresh_one(self, key, **kwargs):
                candidate = tmp_path / "audio" / ".ambiguous-command.wav"
                write_silence_wav(candidate, 0.2, 8000)
                kwargs["commit_guard"]()
                store.commit_candidate(
                    key=key,
                    title="Observations",
                    text="new",
                    candidate_path=candidate,
                    duration_s=0.2,
                    refresh_interval_s=900,
                    command_id=kwargs["commit_identity"],
                )

        service = SegmentApplicationService(
            registry=lambda: DEFAULT_SEGMENT_REGISTRY.resolve(_config()),
            store=store,
            refresher=Refresher(),
            mode=lambda: "normal",
            supervisor=TaskSupervisor(lifecycle),
        )
        await service._run_refresh(command_store, command.command_id, "obs")
        assert (await command_store.get(command.command_id)).status is CommandStatus.SUCCEEDED
        assert store.get("obs").text == "new"
        assert not store._journal_paths_for_key("obs")

    asyncio.run(scenario())


def test_commandless_ambiguous_commit_reconciles_without_receipt(tmp_path: Path, monkeypatch) -> None:
    store, old_bytes = _seed_store_with_audio(tmp_path)
    candidate = tmp_path / "audio" / ".background.wav"
    write_silence_wav(candidate, 0.2, 8000)
    _inject_metadata_directory_failure(monkeypatch, store)

    with pytest.raises(SegmentCommitAmbiguousError):
        store.commit_candidate(
            key="obs",
            title="Observations",
            text="background-new",
            candidate_path=candidate,
            duration_s=0.2,
            refresh_interval_s=900,
        )
    target = Path(store.get("obs").audio_path)
    reopened = SegmentStore(tmp_path / "work", tmp_path / "audio")
    assert reopened.load() == 1
    assert reopened.get("obs").text == "background-new"
    assert target.exists() and target.read_bytes() != old_bytes
    assert reopened.committed_refresh_receipts() == ()
    assert not reopened._journal_paths_for_key("obs")


def test_malformed_exact_authoritative_metadata_stays_unresolved_and_preserves_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    async def scenario() -> None:
        store, command_store, command, target, metadata_path = await _prepare_commanded_ambiguous_commit(
            tmp_path, monkeypatch
        )
        raw = json.loads(metadata_path.read_text(encoding="utf-8"))
        raw["entries"][0]["max_age_s"] = "not-an-integer"
        metadata_path.write_text(json.dumps(raw), encoding="utf-8")
        before = metadata_path.read_bytes()

        outcome = await store.reconcile_committed_refresh_command(command_store, command.command_id, "obs")

        assert outcome is RefreshReconciliationOutcome.STILL_UNRESOLVED
        assert (await command_store.get(command.command_id)).status is CommandStatus.ACCEPTED
        assert target.exists()
        assert store._journal_paths_for_key("obs")
        assert metadata_path.read_bytes() == before

    asyncio.run(scenario())


def test_repaired_exact_metadata_converges_and_second_reconciliation_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    async def scenario() -> None:
        store, command_store, command, _target, metadata_path = await _prepare_commanded_ambiguous_commit(
            tmp_path, monkeypatch
        )
        raw = json.loads(metadata_path.read_text(encoding="utf-8"))
        raw["entries"][0]["max_age_s"] = "not-an-integer"
        metadata_path.write_text(json.dumps(raw), encoding="utf-8")
        assert (
            await store.reconcile_committed_refresh_command(command_store, command.command_id, "obs")
        ) is RefreshReconciliationOutcome.STILL_UNRESOLVED

        monkeypatch.undo()
        raw["entries"][0]["max_age_s"] = 1800
        metadata_path.write_text(json.dumps(raw), encoding="utf-8")
        assert (
            await store.reconcile_committed_refresh_command(command_store, command.command_id, "obs")
        ) is RefreshReconciliationOutcome.PUBLICATION_PROVEN
        assert (await command_store.get(command.command_id)).status is CommandStatus.SUCCEEDED
        assert (
            await store.reconcile_committed_refresh_command(command_store, command.command_id, "obs")
        ) is RefreshReconciliationOutcome.PUBLICATION_NOT_PROVEN
        assert not store._journal_paths_for_key("obs")

    asyncio.run(scenario())


def test_duplicate_exact_metadata_entries_are_unresolved_without_evidence_loss(tmp_path: Path, monkeypatch) -> None:
    async def scenario() -> None:
        store, command_store, command, target, metadata_path = await _prepare_commanded_ambiguous_commit(
            tmp_path, monkeypatch
        )
        raw = json.loads(metadata_path.read_text(encoding="utf-8"))
        raw["entries"].append(dict(raw["entries"][0]))
        metadata_path.write_text(json.dumps(raw), encoding="utf-8")
        before = metadata_path.read_bytes()

        assert (
            await store.reconcile_committed_refresh_command(command_store, command.command_id, "obs")
        ) is RefreshReconciliationOutcome.STILL_UNRESOLVED
        assert (await command_store.get(command.command_id)).status is CommandStatus.ACCEPTED
        assert target.exists() and store._journal_paths_for_key("obs")
        assert metadata_path.read_bytes() == before

    asyncio.run(scenario())


def test_malformed_provably_different_metadata_entry_does_not_poison_exact_reconciliation(
    tmp_path: Path, monkeypatch
) -> None:
    async def scenario() -> None:
        store, command_store, command, target, metadata_path = await _prepare_commanded_ambiguous_commit(
            tmp_path, monkeypatch
        )
        raw = json.loads(metadata_path.read_text(encoding="utf-8"))
        raw["entries"].append(
            {
                "key": "fcst",
                "audio_path": str(tmp_path / "audio" / "cycle_seg_fcst.wav"),
                "max_age_s": "not-an-integer",
            }
        )
        metadata_path.write_text(json.dumps(raw), encoding="utf-8")
        monkeypatch.undo()

        assert (
            await store.reconcile_committed_refresh_command(command_store, command.command_id, "obs")
        ) is RefreshReconciliationOutcome.PUBLICATION_PROVEN
        assert (await command_store.get(command.command_id)).status is CommandStatus.SUCCEEDED
        assert target.exists() and not store._journal_paths_for_key("obs")

    asyncio.run(scenario())


def test_malformed_metadata_with_unknown_identity_stays_unresolved(tmp_path: Path, monkeypatch) -> None:
    async def scenario() -> None:
        store, command_store, command, target, metadata_path = await _prepare_commanded_ambiguous_commit(
            tmp_path, monkeypatch
        )
        raw = json.loads(metadata_path.read_text(encoding="utf-8"))
        raw["entries"].append({"audio_path": str(target), "max_age_s": "not-an-integer"})
        metadata_path.write_text(json.dumps(raw), encoding="utf-8")
        before = metadata_path.read_bytes()

        assert (
            await store.reconcile_committed_refresh_command(command_store, command.command_id, "obs")
        ) is RefreshReconciliationOutcome.STILL_UNRESOLVED
        assert (await command_store.get(command.command_id)).status is CommandStatus.ACCEPTED
        assert target.exists() and store._journal_paths_for_key("obs")
        assert metadata_path.read_bytes() == before

    asyncio.run(scenario())


@pytest.mark.parametrize("corrupt_top_level", ["missing", "malformed"])
def test_missing_or_malformed_top_level_metadata_preserves_pending_evidence(
    tmp_path: Path, monkeypatch, corrupt_top_level: str
) -> None:
    async def scenario() -> None:
        store, command_store, command, target, metadata_path = await _prepare_commanded_ambiguous_commit(
            tmp_path, monkeypatch
        )
        monkeypatch.undo()
        if corrupt_top_level == "missing":
            metadata_path.unlink()
        else:
            metadata_path.write_text("{not-json", encoding="utf-8")

        assert (
            await store.reconcile_committed_refresh_command(command_store, command.command_id, "obs")
        ) is RefreshReconciliationOutcome.STILL_UNRESOLVED
        assert (await command_store.get(command.command_id)).status is CommandStatus.ACCEPTED
        assert target.exists() and store._journal_paths_for_key("obs")

    asyncio.run(scenario())


def test_unreadable_metadata_does_not_promote_an_exact_receipt(tmp_path: Path, monkeypatch) -> None:
    async def scenario() -> None:
        store, command_store, command, target, metadata_path = await _prepare_commanded_ambiguous_commit(
            tmp_path, monkeypatch
        )
        monkeypatch.undo()
        store._write_commit_receipt(key="obs", target=target, command_id=command.command_id)
        metadata_path.unlink()

        assert (
            await store.reconcile_committed_refresh_command(command_store, command.command_id, "obs")
        ) is RefreshReconciliationOutcome.STILL_UNRESOLVED
        assert (await command_store.get(command.command_id)).status is CommandStatus.ACCEPTED
        assert target.exists() and store._journal_paths_for_key("obs")
        assert store._receipt_path("obs", command.command_id).exists()

    asyncio.run(scenario())


def test_command_publication_cancellation_yields_and_is_command_scoped() -> None:
    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        command_store = CommandStore(lifecycle=lifecycle)
        command_a, _ = await command_store.create_or_replay(
            command_type="segment.refresh",
            idempotency_key="heartbeat-a",
            actor="tester",
            payload={"segment_key": "obs"},
            reason="segment-refresh:obs",
        )
        command_b, _ = await command_store.create_or_replay(
            command_type="segment.refresh",
            idempotency_key="heartbeat-b",
            actor="tester",
            payload={"segment_key": "fcst"},
            reason="segment-refresh:fcst",
        )
        command_c, _ = await command_store.create_or_replay(
            command_type="segment.refresh",
            idempotency_key="heartbeat-c",
            actor="tester",
            payload={"segment_key": "hwo"},
            reason="segment-refresh:hwo",
        )
        acquired = threading.Event()
        release = threading.Event()

        def hold_publication() -> None:
            assert command_store.begin_publication(command_a.command_id)
            acquired.set()
            assert release.wait(1.0)
            assert command_store.finish_publication(command_a.command_id)

        worker = threading.Thread(target=hold_publication)
        worker.start()
        for _ in range(100):
            if acquired.is_set():
                break
            await asyncio.sleep(0.001)
        assert acquired.is_set()

        heartbeat = 0
        cancel_b = asyncio.create_task(command_store.request_cancellation(command_b.command_id))
        while not cancel_b.done():
            heartbeat += 1
            await asyncio.sleep(0.005)
        assert (await cancel_b).command_id == command_b.command_id

        cancel_a = asyncio.create_task(command_store.request_cancellation(command_a.command_id))
        for _ in range(10):
            heartbeat += 1
            await asyncio.sleep(0.005)
        assert not cancel_a.done()
        assert heartbeat > 5
        release.set()
        await asyncio.wait_for(cancel_a, timeout=1.0)
        worker.join(timeout=1.0)
        assert not worker.is_alive()

        assert command_store.begin_publication(command_c.command_id)
        assert command_store.finish_publication(command_c.command_id)
        assert command_store.finish_publication(command_a.command_id) is False

    asyncio.run(scenario())


def test_publication_gate_ownership_is_task_safe_under_cancellation_races() -> None:
    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        command_store = CommandStore(lifecycle=lifecycle)
        commands = await _create_refresh_commands(command_store, ["obs", "fcst", "hwo"])
        command_a, command_b, command_c = commands

        await command_store._lock.acquire()
        cancel_a = asyncio.create_task(command_store.request_cancellation(command_a.command_id))
        cancel_b = asyncio.create_task(command_store.request_cancellation(command_b.command_id))
        cancel_a_waiting = asyncio.create_task(command_store.request_cancellation(command_a.command_id))
        try:
            for _ in range(200):
                with command_store._publication_gates_lock:
                    owned = set(command_store._publication_owners)
                if {command_a.command_id, command_b.command_id}.issubset(owned):
                    break
                await asyncio.sleep(0.001)
            assert {command_a.command_id, command_b.command_id}.issubset(owned)

            heartbeat = 0
            for _ in range(10):
                heartbeat += 1
                await asyncio.sleep(0.005)
            assert heartbeat == 10

            cancel_a_waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancel_a_waiting
            assert command_a.command_id in command_store._publication_owners

            cancel_b.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancel_b
            assert command_b.command_id not in command_store._publication_owners
        finally:
            command_store._lock.release()

        await asyncio.wait_for(cancel_a, timeout=1.0)
        assert not command_store._publication_owners

        probe_results: list[bool] = []

        def probe_gates() -> None:
            for command in (command_a, command_b):
                gate = command_store._publication_gates[command.command_id]
                acquired = gate.acquire(blocking=False)
                probe_results.append(acquired)
                if acquired:
                    gate.release()

        probe = threading.Thread(target=probe_gates)
        probe.start()
        probe.join(timeout=1.0)
        assert not probe.is_alive()
        assert probe_results == [True, True]

        worker_result: list[bool] = []

        def real_worker_publication() -> None:
            worker_result.append(command_store.begin_publication(command_c.command_id))
            if worker_result[-1]:
                worker_result.append(command_store.finish_publication(command_c.command_id))

        worker = threading.Thread(target=real_worker_publication)
        worker.start()
        worker.join(timeout=1.0)
        assert not worker.is_alive()
        assert worker_result == [True, True]
        assert not command_store._publication_owners

    asyncio.run(scenario())


def test_fresh_file_bootstrap_installs_independent_lkg_and_skips_malformed_key(tmp_path: Path) -> None:
    async def scenario() -> None:
        seed = SegmentStore(tmp_path / "work", tmp_path / "audio")
        for key, title in (("obs", "Observations"), ("id", "Station identity")):
            target = seed.audio_path_for(key)
            target.parent.mkdir(parents=True, exist_ok=True)
            write_silence_wav(target, 0.1, 8000)
            await seed.update(key, title, f"{key} text", target, 0.1, 900, 1800)
        metadata_path = tmp_path / "work" / "segment_store.json"
        raw = json.loads(metadata_path.read_text(encoding="utf-8"))
        raw["entries"].append(
            {"key": "fcst", "audio_path": str(tmp_path / "audio" / "cycle_seg_fcst.wav"), "max_age_s": "bad"}
        )
        metadata_path.write_text(json.dumps(raw), encoding="utf-8")

        reopened = SegmentStore(tmp_path / "work", tmp_path / "audio")
        assert reopened.load() == 2
        assert reopened.is_ready("obs")
        assert reopened.is_ready("id")
        assert reopened.get("fcst") is None

        refresher = _real_segment_refresher(reopened, tmp_path)
        refresher._registry = SimpleNamespace(refresh_keys=lambda: ("obs", "id", "fcst"))
        rebuilt: list[str] = []

        async def capture_refresh(key: str, **_kwargs) -> None:
            rebuilt.append(key)

        refresher._refresh_one = capture_refresh  # type: ignore[method-assign]
        await refresher._populate_all()
        assert rebuilt == ["fcst"]

    asyncio.run(scenario())


@pytest.mark.parametrize("ambiguity", ["duplicate", "same_key", "unknown_alias"])
def test_fresh_file_bootstrap_excludes_ambiguous_exact_entry(tmp_path: Path, ambiguity: str) -> None:
    async def scenario() -> None:
        seed = SegmentStore(tmp_path / "work", tmp_path / "audio")
        for key, title in (("obs", "Observations"), ("id", "Station identity")):
            target = seed.audio_path_for(key)
            target.parent.mkdir(parents=True, exist_ok=True)
            write_silence_wav(target, 0.1, 8000)
            await seed.update(key, title, f"{key} text", target, 0.1, 900, 1800)
        metadata_path = tmp_path / "work" / "segment_store.json"
        raw = json.loads(metadata_path.read_text(encoding="utf-8"))
        obs_entry = next(item for item in raw["entries"] if item["key"] == "obs")
        if ambiguity == "duplicate":
            raw["entries"].append(dict(obs_entry))
        elif ambiguity == "same_key":
            raw["entries"].append({"key": "obs", "audio_path": obs_entry["audio_path"], "max_age_s": "bad"})
        else:
            raw["entries"].append({"audio_path": obs_entry["audio_path"], "max_age_s": "bad"})
        metadata_path.write_text(json.dumps(raw), encoding="utf-8")

        reopened = SegmentStore(tmp_path / "work", tmp_path / "audio")
        assert reopened.load() == 1
        assert reopened.get("obs") is None
        assert reopened.is_ready("id")

    asyncio.run(scenario())


def test_tts_cancellation_does_not_wait_for_worker_publication_callback(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SegmentStore(tmp_path / "work", tmp_path / "audio")
        refresher = _real_segment_refresher(store, tmp_path)
        entered = threading.Event()
        release = threading.Event()
        callback_done = threading.Event()

        def finalize(tts_path: Path, cancellation, _fence) -> FinalizationEvidence:
            del cancellation
            completed = tts_path.parent / "completed.wav"
            write_silence_wav(completed, 0.1, 8000)
            return FinalizationEvidence(completed)

        def blocking_publication() -> None:
            entered.set()
            assert release.wait(1.0)
            callback_done.set()

        output = tmp_path / "audio" / "bridge-output.wav"
        task = asyncio.create_task(
            synthesize_completed_wav_async(
                refresher._legacy_tts_for_bridge_tests,
                "blocking publication",
                output,
                purpose="routine",
                shutdown_timeout=0.01,
                finalize=finalize,
                publication_fence=lambda: None,
                publication_committed=blocking_publication,
            )
        )
        for _ in range(200):
            if entered.is_set():
                break
            await asyncio.sleep(0.001)
        assert entered.is_set()

        started = time.monotonic()
        task.cancel()
        heartbeat = 0
        while not task.done() and time.monotonic() - started < 0.4:
            heartbeat += 1
            await asyncio.sleep(0.005)
        with pytest.raises(asyncio.CancelledError):
            await task
        elapsed = time.monotonic() - started
        assert elapsed < 0.25
        for _ in range(10):
            heartbeat += 1
            await asyncio.sleep(0.005)
        assert heartbeat > 5

        release.set()
        for _ in range(200):
            if callback_done.is_set():
                break
            await asyncio.sleep(0.001)
        assert callback_done.is_set()
        assert output.is_file()

    asyncio.run(scenario())


def test_required_refresher_isolates_commandless_ambiguity_and_keeps_running() -> None:
    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        resolve_ambiguity = asyncio.Event()
        refresh_calls: list[str] = []

        class Store:
            def is_stale(self, _key: str) -> bool:
                return _key == "obs" and resolve_ambiguity.is_set()

            async def reconcile_commandless_refresh(self, _key: str) -> RefreshReconciliationOutcome:
                return (
                    RefreshReconciliationOutcome.STILL_UNRESOLVED
                    if not resolve_ambiguity.is_set()
                    else RefreshReconciliationOutcome.PUBLICATION_PROVEN
                )

            async def record_failure(self, *_args, **_kwargs):
                raise AssertionError("publication ambiguity entered ordinary failure bookkeeping")

        registry = SimpleNamespace(refresh_keys=lambda: ("obs", "fcst"))
        refresher = SegmentRefresher(
            store=Store(),  # type: ignore[arg-type]
            cycle_builder=SimpleNamespace(),  # type: ignore[arg-type]
            tts=SimpleNamespace(),  # type: ignore[arg-type]
            alert_tracker=SimpleNamespace(purge_expired=lambda: 0, get_cycle_alerts=lambda: []),
            ctx_fn=lambda: CycleContext(mode="normal", last_heightened_ago=None, last_product_desc=None),
            station_name="station",
            service_area_name="area",
            disclaimer="disclaimer",
            tz=ZoneInfo("UTC"),
            sample_rate=8000,
            registry=registry,  # type: ignore[arg-type]
            tick_s=0.01,
        )

        async def fake_refresh(key: str, **_kwargs) -> None:
            refresh_calls.append(key)
            if key == "obs" and refresh_calls.count("obs") == 1:
                raise SegmentCommitAmbiguousError(key=key, command_id=None)

        refresher._refresh_one = fake_refresh  # type: ignore[method-assign]
        supervisor = TaskSupervisor(lifecycle)
        task = supervisor.create_task(refresher.run(), name="segment_refresher", required=True)
        for _ in range(200):
            if "fcst" in refresh_calls:
                break
            await asyncio.sleep(0.001)
        assert "obs" in refresh_calls and "fcst" in refresh_calls
        assert not task.done()
        assert lifecycle.state.value == "running"
        assert "obs" in refresher._deferred_ambiguities
        resolve_ambiguity.set()
        refresher._wake_event.set()
        for _ in range(200):
            if "obs" not in refresher._deferred_ambiguities:
                break
            await asyncio.sleep(0.001)
        assert "obs" not in refresher._deferred_ambiguities
        assert refresh_calls.count("obs") == 2
        lifecycle.request_shutdown()
        await supervisor.stop()
        assert lifecycle.state.value != "failed"

    asyncio.run(scenario())


def test_pending_committed_journal_target_survives_same_key_generation_gc(tmp_path: Path, monkeypatch) -> None:
    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        command_store = CommandStore(lifecycle=lifecycle)
        store = SegmentStore(tmp_path / "work", tmp_path / "audio")
        stable = store.audio_path_for("obs")
        stable.parent.mkdir(parents=True, exist_ok=True)
        write_silence_wav(stable, 0.1, 8000)
        await store.update("obs", "Observations", "old", stable, 0.1, 900, 1800)
        command, _ = await command_store.create_or_replay(
            command_type="segment.refresh",
            idempotency_key="gc-pending-command",
            actor="tester",
            payload={"segment_key": "obs"},
            reason="segment-refresh:obs",
        )
        await command_store.mark_running(command.command_id)
        original_receipt = SegmentStore._write_commit_receipt

        def fail_receipt(self, *, key: str, target: Path, command_id: str):
            if self is store and command_id == command.command_id:
                raise OSError("injected receipt materialization failure")
            return original_receipt(self, key=key, target=target, command_id=command_id)

        monkeypatch.setattr(SegmentStore, "_write_commit_receipt", fail_receipt)
        candidate = tmp_path / "audio" / "gc-pending.wav"
        write_silence_wav(candidate, 0.2, 8000)
        result = store.commit_candidate(
            key="obs",
            title="Observations",
            text="pending",
            candidate_path=candidate,
            duration_s=0.2,
            refresh_interval_s=900,
            command_id=command.command_id,
        )
        pending_target = Path(result.entry.audio_path)
        assert pending_target.exists()
        assert store._journal_paths_for_key("obs")

        for index in range(3):
            candidate = tmp_path / "audio" / f"gc-later-{index}.wav"
            write_silence_wav(candidate, 0.2, 8000)
            store.commit_candidate(
                key="obs",
                title="Observations",
                text=f"later-{index}",
                candidate_path=candidate,
                duration_s=0.2,
                refresh_interval_s=900,
            )

        assert pending_target.exists()
        assert len(store._unreferenced_generations("obs")) <= store._MAX_UNREFERENCED_GENERATIONS
        monkeypatch.setattr(SegmentStore, "_write_commit_receipt", original_receipt)
        assert await store.reconcile_committed_refresh_command(command_store, command.command_id, "obs")
        assert (await command_store.get(command.command_id)).status is CommandStatus.SUCCEEDED
        assert store.get("obs").text == "later-2"
        assert pending_target.exists()

    asyncio.run(scenario())


def test_pending_committed_journal_restarts_before_later_same_key_commits(tmp_path: Path, monkeypatch) -> None:
    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        command_store = CommandStore(lifecycle=lifecycle)
        store = SegmentStore(tmp_path / "work", tmp_path / "audio")
        stable = store.audio_path_for("obs")
        stable.parent.mkdir(parents=True, exist_ok=True)
        write_silence_wav(stable, 0.1, 8000)
        await store.update("obs", "Observations", "old", stable, 0.1, 900, 1800)
        command, _ = await command_store.create_or_replay(
            command_type="segment.refresh",
            idempotency_key="gc-restart-before",
            actor="tester",
            payload={"segment_key": "obs"},
            reason="segment-refresh:obs",
        )
        await command_store.mark_running(command.command_id)
        original_receipt = SegmentStore._write_commit_receipt

        def fail_receipt(self, *, key: str, target: Path, command_id: str):
            raise OSError("receipt materialization remains unavailable")

        monkeypatch.setattr(SegmentStore, "_write_commit_receipt", fail_receipt)
        candidate = tmp_path / "audio" / "gc-restart-before.wav"
        write_silence_wav(candidate, 0.2, 8000)
        result = store.commit_candidate(
            key="obs",
            title="Observations",
            text="pending-before-restart",
            candidate_path=candidate,
            duration_s=0.2,
            refresh_interval_s=900,
            command_id=command.command_id,
        )
        pending_target = Path(result.entry.audio_path)

        reopened = SegmentStore(tmp_path / "work", tmp_path / "audio")
        assert reopened.load() == 1
        assert pending_target.exists()
        assert reopened._journal_paths_for_key("obs")

        for index in range(3):
            candidate = tmp_path / "audio" / f"gc-restart-later-{index}.wav"
            write_silence_wav(candidate, 0.2, 8000)
            reopened.commit_candidate(
                key="obs",
                title="Observations",
                text=f"restart-later-{index}",
                candidate_path=candidate,
                duration_s=0.2,
                refresh_interval_s=900,
            )
        assert pending_target.exists()

        monkeypatch.setattr(SegmentStore, "_write_commit_receipt", original_receipt)
        assert await reopened.reconcile_committed_refresh_command(command_store, command.command_id, "obs")
        assert (await command_store.get(command.command_id)).status is CommandStatus.SUCCEEDED
        assert reopened.get("obs").text == "restart-later-2"

        after_recovery = SegmentStore(tmp_path / "work", tmp_path / "audio")
        assert after_recovery.load() == 1
        assert after_recovery.load() == 1
        assert after_recovery.get("obs").text == "restart-later-2"
        assert not after_recovery._journal_paths_for_key("obs")

    asyncio.run(scenario())


def test_published_command_survives_marker_and_receipt_loss_across_later_same_key_refreshes(
    tmp_path: Path, monkeypatch
) -> None:
    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        command_store = CommandStore(lifecycle=lifecycle)
        store = SegmentStore(tmp_path / "work", tmp_path / "audio")
        stable = store.audio_path_for("obs")
        stable.parent.mkdir(parents=True, exist_ok=True)
        write_silence_wav(stable, 0.1, 8000)
        await store.update("obs", "Observations", "old", stable, 0.1, 900, 1800)
        refresher = _real_segment_refresher(store, tmp_path)
        command_b, _ = await command_store.create_or_replay(
            command_type="segment.refresh",
            idempotency_key="historical-proof-b",
            actor="tester",
            payload={"segment_key": "obs"},
            reason="segment-refresh:obs",
        )
        unrelated, _ = await command_store.create_or_replay(
            command_type="config.reload",
            idempotency_key="historical-proof-unrelated",
            actor="tester",
            payload={},
            reason="unrelated-command",
        )
        await command_store.mark_running(unrelated.command_id)

        original_mark = store._mark_commit_committed
        original_receipt = store._write_commit_receipt

        def fail_b_marker(*, key: str, target: Path, command_id: str | None):
            if command_id == command_b.command_id:
                raise OSError("forced B committed-marker failure")
            return original_mark(key=key, target=target, command_id=command_id)

        def fail_b_receipt(*, key: str, target: Path, command_id: str):
            if command_id == command_b.command_id:
                raise OSError("forced B receipt-materialization failure")
            return original_receipt(key=key, target=target, command_id=command_id)

        monkeypatch.setattr(store, "_mark_commit_committed", fail_b_marker)
        monkeypatch.setattr(store, "_write_commit_receipt", fail_b_receipt)
        original_mark_succeeded = command_store.mark_succeeded

        async def fail_b_success(command_id: str, result: dict[str, object]):
            if command_id == command_b.command_id:
                raise OSError("forced nonterminal B success persistence")
            return await original_mark_succeeded(command_id, result)

        monkeypatch.setattr(command_store, "mark_succeeded", fail_b_success)
        service = SegmentApplicationService(
            registry=lambda: DEFAULT_SEGMENT_REGISTRY.resolve(_config()),
            store=store,
            refresher=refresher,
            mode=lambda: "normal",
            supervisor=TaskSupervisor(lifecycle),
        )

        await service._run_refresh(command_store, command_b.command_id, "obs")
        pending_journals = store._journal_paths_for_key("obs")
        assert len(pending_journals) == 1
        journal = json.loads(pending_journals[0].read_text(encoding="utf-8"))
        assert journal["command_id"] == command_b.command_id
        assert journal["publication_won"] is True
        b_target = Path(journal["target"])
        assert b_target.exists()
        assert (await command_store.get(command_b.command_id)).status is CommandStatus.RUNNING

        for _index in range(3):
            await refresher.refresh_one("obs")
            assert store.get("obs").text == "new observation"
            assert b_target.exists()

        assert len(store._unreferenced_generations("obs")) <= store._MAX_UNREFERENCED_GENERATIONS
        monkeypatch.setattr(store, "_mark_commit_committed", original_mark)
        monkeypatch.setattr(store, "_write_commit_receipt", original_receipt)
        monkeypatch.setattr(command_store, "mark_succeeded", original_mark_succeeded)

        reopened = SegmentStore(tmp_path / "work", tmp_path / "audio")
        assert reopened.load() == 1
        assert reopened.get("obs").text == "new observation"
        assert b_target.exists()
        assert not reopened._journal_paths_for_key("obs")
        assert {receipt.command_id for receipt in reopened.committed_refresh_receipts()} == {command_b.command_id}

        reopened_service = SegmentApplicationService(
            registry=lambda: DEFAULT_SEGMENT_REGISTRY.resolve(_config()),
            store=reopened,
            refresher=refresher,
            mode=lambda: "normal",
            supervisor=TaskSupervisor(lifecycle),
        )
        assert await reopened.reconcile_committed_refresh_commands(command_store) == 1
        assert await reopened_service.reconcile_orphaned_refreshes(command_store) == 0
        assert (await command_store.get(command_b.command_id)).status is CommandStatus.SUCCEEDED
        assert (await command_store.get(unrelated.command_id)).status is CommandStatus.RUNNING

        assert await reopened.reconcile_committed_refresh_commands(command_store) == 0
        assert await reopened_service.reconcile_orphaned_refreshes(command_store) == 0
        assert (await command_store.get(command_b.command_id)).status is CommandStatus.SUCCEEDED
        assert (await command_store.get(unrelated.command_id)).status is CommandStatus.RUNNING

    asyncio.run(scenario())


def test_metadata_replace_after_rename_raise_is_ambiguous_and_restart_idempotent(tmp_path: Path, monkeypatch) -> None:
    import seasonalweather.broadcast.segment_store as segment_store_module

    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        command_store = CommandStore(lifecycle=lifecycle)
        store = SegmentStore(tmp_path / "work", tmp_path / "audio")
        stable = store.audio_path_for("obs")
        stable.parent.mkdir(parents=True, exist_ok=True)
        write_silence_wav(stable, 0.1, 8000)
        await store.update("obs", "Observations", "old", stable, 0.1, 900, 1800)
        command, _ = await command_store.create_or_replay(
            command_type="segment.refresh",
            idempotency_key="metadata-after-rename",
            actor="tester",
            payload={"segment_key": "obs"},
            reason="segment-refresh:obs",
        )
        await command_store.mark_running(command.command_id)
        original_replace = segment_store_module.os.replace

        def replace_then_raise(source, target):
            if Path(target) == store._index_path:
                original_replace(source, target)
                raise OSError("metadata rename completed but wrapper failed")
            original_replace(source, target)

        monkeypatch.setattr(segment_store_module.os, "replace", replace_then_raise)
        candidate = tmp_path / "audio" / "metadata-after-rename.wav"
        write_silence_wav(candidate, 0.2, 8000)
        with pytest.raises(SegmentCommitAmbiguousError):
            store.commit_candidate(
                key="obs",
                title="Observations",
                text="new-after-rename",
                candidate_path=candidate,
                duration_s=0.2,
                refresh_interval_s=900,
                command_id=command.command_id,
            )
        target = Path(store.get("obs").audio_path)
        assert target.exists()
        assert store._journal_paths_for_key("obs")
        assert (await command_store.get(command.command_id)).status is CommandStatus.RUNNING
        assert json.loads(store._index_path.read_text(encoding="utf-8"))["entries"][0]["audio_path"] == str(target)

        monkeypatch.setattr(segment_store_module.os, "replace", original_replace)
        reopened = SegmentStore(tmp_path / "work", tmp_path / "audio")
        assert reopened.load() == 1
        assert reopened.get("obs").text == "new-after-rename"
        assert target.exists()
        assert (
            await reopened.reconcile_committed_refresh_command(command_store, command.command_id, "obs")
        ) is RefreshReconciliationOutcome.PUBLICATION_PROVEN
        assert (
            await reopened.reconcile_committed_refresh_command(command_store, command.command_id, "obs")
        ) is RefreshReconciliationOutcome.PUBLICATION_NOT_PROVEN
        assert (await command_store.get(command.command_id)).status is CommandStatus.SUCCEEDED

    asyncio.run(scenario())


def test_metadata_replace_before_rename_raise_is_conservative_and_restart_idempotent(
    tmp_path: Path, monkeypatch
) -> None:
    import seasonalweather.broadcast.segment_store as segment_store_module

    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        command_store = CommandStore(lifecycle=lifecycle)
        store = SegmentStore(tmp_path / "work", tmp_path / "audio")
        stable = store.audio_path_for("obs")
        stable.parent.mkdir(parents=True, exist_ok=True)
        write_silence_wav(stable, 0.1, 8000)
        old_bytes = stable.read_bytes()
        await store.update("obs", "Observations", "old", stable, 0.1, 900, 1800)
        command, _ = await command_store.create_or_replay(
            command_type="segment.refresh",
            idempotency_key="metadata-before-rename",
            actor="tester",
            payload={"segment_key": "obs"},
            reason="segment-refresh:obs",
        )
        await command_store.mark_running(command.command_id)
        original_replace = segment_store_module.os.replace

        def raise_before_replace(source, target):
            if Path(target) == store._index_path:
                raise OSError("metadata rename was not attempted by the wrapper")
            original_replace(source, target)

        monkeypatch.setattr(segment_store_module.os, "replace", raise_before_replace)
        candidate = tmp_path / "audio" / "metadata-before-rename.wav"
        write_silence_wav(candidate, 0.2, 8000)
        with pytest.raises(SegmentCommitAmbiguousError):
            store.commit_candidate(
                key="obs",
                title="Observations",
                text="new-before-rename",
                candidate_path=candidate,
                duration_s=0.2,
                refresh_interval_s=900,
                command_id=command.command_id,
            )
        target = Path(store.get("obs").audio_path)
        assert target.exists()
        assert store._journal_paths_for_key("obs")
        assert (await command_store.get(command.command_id)).status is CommandStatus.RUNNING
        assert store._index_path.read_text(encoding="utf-8").find("old") >= 0

        monkeypatch.setattr(segment_store_module.os, "replace", original_replace)
        reopened = SegmentStore(tmp_path / "work", tmp_path / "audio")
        assert reopened.load() == 1
        assert reopened.get("obs").text == "old"
        assert reopened.audio_path_for("obs").read_bytes() == old_bytes
        assert not target.exists()
        assert not reopened._journal_paths_for_key("obs")
        assert (
            await reopened.reconcile_committed_refresh_command(command_store, command.command_id, "obs")
        ) is RefreshReconciliationOutcome.PUBLICATION_NOT_PROVEN
        assert (await command_store.get(command.command_id)).status is CommandStatus.RUNNING
        assert reopened.load() == 1

    asyncio.run(scenario())


def test_real_refresher_before_rename_ambiguity_bypasses_failure_bookkeeping_and_restart(
    tmp_path: Path, monkeypatch
) -> None:
    import seasonalweather.broadcast.segment_store as segment_store_module

    store, old_bytes = _seed_store_with_audio(tmp_path)
    refresher = _real_segment_refresher(store, tmp_path)
    failure_calls: list[object] = []

    async def forbidden_failure(*args, **kwargs):
        failure_calls.append((args, kwargs))
        raise AssertionError("metadata ambiguity must not enter refresher failure policy")

    monkeypatch.setattr(store, "record_failure", forbidden_failure)
    monkeypatch.setattr(store, "mark_placeholder", forbidden_failure)
    original_replace = segment_store_module.os.replace

    def raise_before_replace(source, target):
        if Path(target) == store._index_path:
            raise OSError("metadata rename was not attempted")
        original_replace(source, target)

    monkeypatch.setattr(segment_store_module.os, "replace", raise_before_replace)

    with pytest.raises(SegmentCommitAmbiguousError):
        asyncio.run(refresher.refresh_one("obs"))

    assert failure_calls == []
    assert store.get("obs").text == "new observation"
    reopened = SegmentStore(tmp_path / "work", tmp_path / "audio")
    assert reopened.load() == 1
    assert reopened.get("obs").text == "old"
    assert reopened.audio_path_for("obs").read_bytes() == old_bytes


def test_real_refresher_after_rename_ambiguity_reconciles_disk_truth_on_restart(tmp_path: Path, monkeypatch) -> None:
    import seasonalweather.broadcast.segment_store as segment_store_module

    store, old_bytes = _seed_store_with_audio(tmp_path)
    refresher = _real_segment_refresher(store, tmp_path)
    original_replace = segment_store_module.os.replace

    def replace_then_raise(source, target):
        if Path(target) == store._index_path:
            original_replace(source, target)
            raise OSError("metadata rename completed but wrapper failed")
        original_replace(source, target)

    monkeypatch.setattr(segment_store_module.os, "replace", replace_then_raise)
    with pytest.raises(SegmentCommitAmbiguousError):
        asyncio.run(refresher.refresh_one("obs"))

    target = Path(store.get("obs").audio_path)
    assert target.exists()
    monkeypatch.setattr(segment_store_module.os, "replace", original_replace)
    reopened = SegmentStore(tmp_path / "work", tmp_path / "audio")
    assert reopened.load() == 1
    assert reopened.get("obs").text == "new observation"
    assert target.read_bytes() != old_bytes


def test_command_refresh_before_rename_ambiguity_uses_real_refresher_and_terminalizes_nonpublication(
    tmp_path: Path, monkeypatch
) -> None:
    import seasonalweather.broadcast.segment_store as segment_store_module

    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        command_store = CommandStore(lifecycle=lifecycle)
        store = SegmentStore(tmp_path / "work", tmp_path / "audio")
        stable = store.audio_path_for("obs")
        stable.parent.mkdir(parents=True, exist_ok=True)
        write_silence_wav(stable, 0.1, 8000)
        old_bytes = stable.read_bytes()
        await store.update("obs", "Observations", "old", stable, 0.1, 900, 1800)
        refresher = _real_segment_refresher(store, tmp_path)
        command, _ = await command_store.create_or_replay(
            command_type="segment.refresh",
            idempotency_key="real-before-rename-command",
            actor="tester",
            payload={"segment_key": "obs"},
            reason="segment-refresh:obs",
        )
        failure_calls: list[object] = []

        async def forbidden_failure(*args, **kwargs):
            failure_calls.append((args, kwargs))
            raise AssertionError("metadata ambiguity must bypass ordinary failure policy")

        monkeypatch.setattr(store, "record_failure", forbidden_failure)
        monkeypatch.setattr(store, "mark_placeholder", forbidden_failure)
        original_replace = segment_store_module.os.replace

        def raise_before_replace(source, target):
            if Path(target) == store._index_path:
                raise OSError("metadata rename was not attempted")
            original_replace(source, target)

        monkeypatch.setattr(segment_store_module.os, "replace", raise_before_replace)
        repair_calls: list[tuple[str, str]] = []
        original_reconcile = store.reconcile_committed_refresh_command

        async def track_reconcile(command_store_arg, command_id, key):
            repair_calls.append((command_id, key))
            return await original_reconcile(command_store_arg, command_id, key)

        monkeypatch.setattr(store, "reconcile_committed_refresh_command", track_reconcile)
        service = SegmentApplicationService(
            registry=lambda: DEFAULT_SEGMENT_REGISTRY.resolve(_config()),
            store=store,
            refresher=refresher,
            mode=lambda: "normal",
            supervisor=TaskSupervisor(lifecycle),
        )
        await service._run_refresh(command_store, command.command_id, "obs")
        assert repair_calls == [(command.command_id, "obs")]
        assert failure_calls == []
        assert (await command_store.get(command.command_id)).status is CommandStatus.CANCELLED
        assert store.get("obs").text == "old"
        assert store.audio_path_for("obs").read_bytes() == old_bytes

    asyncio.run(scenario())


def test_command_refresh_after_rename_ambiguity_uses_real_refresher_and_repairs_after_proof(
    tmp_path: Path, monkeypatch
) -> None:
    import seasonalweather.broadcast.segment_store as segment_store_module

    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        command_store = CommandStore(lifecycle=lifecycle)
        store = SegmentStore(tmp_path / "work", tmp_path / "audio")
        stable = store.audio_path_for("obs")
        stable.parent.mkdir(parents=True, exist_ok=True)
        write_silence_wav(stable, 0.1, 8000)
        await store.update("obs", "Observations", "old", stable, 0.1, 900, 1800)
        refresher = _real_segment_refresher(store, tmp_path)
        command, _ = await command_store.create_or_replay(
            command_type="segment.refresh",
            idempotency_key="real-after-rename-command",
            actor="tester",
            payload={"segment_key": "obs"},
            reason="segment-refresh:obs",
        )
        original_replace = segment_store_module.os.replace

        def replace_then_raise(source, target):
            if Path(target) == store._index_path:
                original_replace(source, target)
                raise OSError("metadata rename completed but wrapper failed")
            original_replace(source, target)

        monkeypatch.setattr(segment_store_module.os, "replace", replace_then_raise)
        repair_calls: list[tuple[str, str]] = []
        original_reconcile = store.reconcile_committed_refresh_command

        async def track_reconcile(command_store_arg, command_id, key):
            repair_calls.append((command_id, key))
            return await original_reconcile(command_store_arg, command_id, key)

        monkeypatch.setattr(store, "reconcile_committed_refresh_command", track_reconcile)
        service = SegmentApplicationService(
            registry=lambda: DEFAULT_SEGMENT_REGISTRY.resolve(_config()),
            store=store,
            refresher=refresher,
            mode=lambda: "normal",
            supervisor=TaskSupervisor(lifecycle),
        )
        await service._run_refresh(command_store, command.command_id, "obs")
        assert repair_calls == [(command.command_id, "obs")]
        assert (await command_store.get(command.command_id)).status is CommandStatus.SUCCEEDED
        assert store.get("obs").text == "new observation"
        assert not store._journal_paths_for_key("obs")

    asyncio.run(scenario())


def test_worker_abort_releases_publication_gate_after_callback_failure(tmp_path: Path) -> None:
    from seasonalweather.tts.tts import TTSCompatibilityError

    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        command_store = CommandStore(lifecycle=lifecycle)
        store = SegmentStore(tmp_path / "work", tmp_path / "audio")
        refresher = _real_segment_refresher(store, tmp_path)
        command, _ = await command_store.create_or_replay(
            command_type="segment.refresh",
            idempotency_key="callback-failure-gate",
            actor="tester",
            payload={"segment_key": "obs"},
            reason="segment-refresh:obs",
        )
        gate_depth = 0

        def acquire_gate() -> None:
            nonlocal gate_depth
            assert command_store.begin_publication(command.command_id)
            gate_depth += 1

        def abort_gate() -> None:
            nonlocal gate_depth
            if gate_depth:
                gate_depth -= 1
                command_store.finish_publication()

        def callback_failure(_result) -> None:
            raise RuntimeError("injected publication callback failure")

        with pytest.raises(TTSCompatibilityError):
            await store.synth_and_update(
                refresher._tts,
                key="obs",
                title="Observations",
                text="callback failure",
                refresh_interval_s=900,
                sample_rate=8000,
                publication_fence=acquire_gate,
                publication_committed=callback_failure,
                publication_aborted=abort_gate,
                command_id=command.command_id,
            )
        assert gate_depth == 0
        await asyncio.wait_for(command_store.request_cancellation(command.command_id), timeout=1.0)

        followup, _ = await command_store.create_or_replay(
            command_type="segment.refresh",
            idempotency_key="callback-failure-followup",
            actor="tester",
            payload={"segment_key": "obs"},
            reason="segment-refresh:obs",
        )
        service = SegmentApplicationService(
            registry=lambda: DEFAULT_SEGMENT_REGISTRY.resolve(_config()),
            store=store,
            refresher=refresher,
            mode=lambda: "normal",
            supervisor=TaskSupervisor(lifecycle),
        )
        await service._run_refresh(command_store, followup.command_id, "obs")
        assert (await command_store.get(followup.command_id)).status is CommandStatus.SUCCEEDED

    asyncio.run(scenario())


def test_unresolved_prior_same_key_does_not_strand_later_command(tmp_path: Path, monkeypatch) -> None:
    import seasonalweather.broadcast.segment_store as segment_store_module

    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        command_store = CommandStore(lifecycle=lifecycle)
        store = SegmentStore(tmp_path / "work", tmp_path / "audio")
        stable = store.audio_path_for("obs")
        stable.parent.mkdir(parents=True, exist_ok=True)
        write_silence_wav(stable, 0.1, 8000)
        await store.update("obs", "Observations", "old", stable, 0.1, 900, 1800)
        command_a, _ = await command_store.create_or_replay(
            command_type="segment.refresh",
            idempotency_key="prior-unresolved-a",
            actor="tester",
            payload={"segment_key": "obs"},
            reason="segment-refresh:obs",
        )
        await command_store.mark_running(command_a.command_id)
        original_replace = segment_store_module.os.replace

        def fail_metadata_replace(source, target):
            if Path(target) == store._index_path:
                raise OSError("metadata persistence is unresolved")
            original_replace(source, target)

        monkeypatch.setattr(segment_store_module.os, "replace", fail_metadata_replace)
        candidate = tmp_path / "audio" / "prior-unresolved.wav"
        write_silence_wav(candidate, 0.2, 8000)
        with pytest.raises(SegmentCommitAmbiguousError):
            store.commit_candidate(
                key="obs",
                title="Observations",
                text="unresolved A",
                candidate_path=candidate,
                duration_s=0.2,
                refresh_interval_s=900,
                command_id=command_a.command_id,
            )
        assert store.refresh_evidence_state("obs", command_a.command_id).value == "unresolved"
        journal_paths = tuple(store._journal_paths_for_key("obs"))

        command_b, _ = await command_store.create_or_replay(
            command_type="segment.refresh",
            idempotency_key="later-blocked-b",
            actor="tester",
            payload={"segment_key": "obs"},
            reason="segment-refresh:obs",
        )
        refresher = _real_segment_refresher(store, tmp_path)
        service = SegmentApplicationService(
            registry=lambda: DEFAULT_SEGMENT_REGISTRY.resolve(_config()),
            store=store,
            refresher=refresher,
            mode=lambda: "normal",
            supervisor=TaskSupervisor(lifecycle),
        )
        await service._run_refresh(command_store, command_b.command_id, "obs")
        assert (await command_store.get(command_b.command_id)).status is CommandStatus.CANCELLED
        assert tuple(store._journal_paths_for_key("obs")) == journal_paths
        assert store.refresh_evidence_state("obs", command_a.command_id).value == "unresolved"
        assert store.get("obs").text == "old"

        monkeypatch.setattr(segment_store_module.os, "replace", original_replace)
        outcome = await store.reconcile_committed_refresh_command(command_store, command_a.command_id, "obs")
        assert outcome is RefreshReconciliationOutcome.PUBLICATION_NOT_PROVEN
        assert store.refresh_evidence_state("obs", command_a.command_id).value == "none"
        await command_store.mark_cancelled(command_a.command_id)

        unrelated, _ = await command_store.create_or_replay(
            command_type="segment.refresh",
            idempotency_key="unrelated-key-b",
            actor="tester",
            payload={"segment_key": "fcst"},
            reason="segment-refresh:fcst",
        )
        unrelated_candidate = tmp_path / "audio" / "unrelated.wav"
        write_silence_wav(unrelated_candidate, 0.1, 8000)
        store.commit_candidate(
            key="fcst",
            title="Forecast",
            text="unrelated publication",
            candidate_path=unrelated_candidate,
            duration_s=0.1,
            refresh_interval_s=900,
            command_id=unrelated.command_id,
        )
        assert store.get("fcst").text == "unrelated publication"

        command_c, _ = await command_store.create_or_replay(
            command_type="segment.refresh",
            idempotency_key="later-after-reconcile-c",
            actor="tester",
            payload={"segment_key": "obs"},
            reason="segment-refresh:obs",
        )
        await service._run_refresh(command_store, command_c.command_id, "obs")
        assert (await command_store.get(command_c.command_id)).status is CommandStatus.SUCCEEDED

    asyncio.run(scenario())


def test_synthesis_callback_commits_store_before_returning_success(tmp_path: Path) -> None:
    class TTS:
        async def synthesize(self, _text: str, output_path: Path, *, purpose: str = "routine") -> None:
            del purpose
            write_silence_wav(output_path, 0.1, 8000)

    async def scenario() -> None:
        store = SegmentStore(tmp_path / "work", tmp_path / "audio")
        await store.synth_and_update(
            TTS(),
            key="obs",
            title="Observations",
            text="new",
            refresh_interval_s=900,
            max_age_s=1800,
            sample_rate=8000,
        )
        assert store.get("obs").text == "new"
        assert store.is_ready("obs")

    asyncio.run(scenario())


def test_segment_commit_file_promotion_failure_restores_lkg(tmp_path: Path, monkeypatch) -> None:
    import seasonalweather.broadcast.segment_store as segment_store_module

    store, old_bytes = _seed_store_with_audio(tmp_path)
    candidate = tmp_path / "audio" / ".candidate.wav"
    write_silence_wav(candidate, 0.2, 8000)
    original_replace = segment_store_module.os.replace

    def fail_candidate(source, target):
        if Path(source) == candidate:
            raise OSError("injected file promotion failure")
        original_replace(source, target)

    monkeypatch.setattr(segment_store_module.os, "replace", fail_candidate)
    with pytest.raises(OSError):
        store.commit_candidate(
            key="obs",
            title="Observations",
            text="new",
            candidate_path=candidate,
            duration_s=0.2,
            refresh_interval_s=900,
            max_age_s=1800,
        )
    assert store.get("obs").text == "old"
    assert store.audio_path_for("obs").read_bytes() == old_bytes


def test_hwo_builder_carries_actual_product_evidence_without_fabricated_controller_fetch(tmp_path: Path) -> None:
    from seasonalweather.alerts.nws_api import NWSProduct
    from seasonalweather.broadcast.cycle import CycleBuilder

    class Api:
        async def latest_product_id(self, *_args):
            return "KXYZ-123"

        async def get_product(self, _product_id):
            return NWSProduct(
                "KXYZ-123",
                "Hazardous Weather Outlook\nIssued this afternoon.\nNo severe weather expected.",
                issuance_time="2026-08-16T12:00:00Z",
                product_type="HWO",
                wfo="LWX",
            )

    builder = CycleBuilder(
        api=Api(),
        tz_name="UTC",
        obs_stations=[],
        reference_points=[],
        same_fips_all=[],
        cycle_cfg=None,
        registry=DEFAULT_SEGMENT_REGISTRY.resolve(_config()),
        work_dir=str(tmp_path),
    )
    candidate = asyncio.run(
        builder.build_hwo_segment(
            SegmentBuildInput(
                key="hwo",
                context=CycleContext(mode="normal", last_heightened_ago=None, last_product_desc=None),
                station_name="station",
                service_area_name="area",
                disclaimer="disclaimer",
            )
        )
    )
    assert candidate is not None
    assert candidate.provenance.product_identifier == "KXYZ-123"
    assert candidate.provenance.issuance_time == "2026-08-16T12:00:00Z"
    assert candidate.provenance.issuing_office == "LWX"
    assert candidate.provenance.fetch_time is not None

    outro = asyncio.run(
        builder.build_outro_segment(
            SegmentBuildInput(
                key="outro",
                context=CycleContext(mode="normal", last_heightened_ago=None, last_product_desc=None),
                station_name="station",
                service_area_name="area",
                disclaimer="disclaimer",
            )
        )
    )
    assert outro.provenance.fetch_time is None


def _observation_config() -> SimpleNamespace:
    return SimpleNamespace(
        spc=SimpleNamespace(enabled=True),
        cwf=SimpleNamespace(enabled=True, offices=["LWX"], max_chars_normal=2000),
        offnt2=SimpleNamespace(enabled=False),
        marine_obs=SimpleNamespace(
            enabled=True,
            max_stations=2,
            anchor_stations=[],
            station_names={},
        ),
        hwo=SimpleNamespace(speak_unavailable=True, max_chars_normal=1000),
        rwr=SimpleNamespace(
            enabled=True,
            office="LWX",
            staleness_minutes=75,
            station_names={},
            anchor_stations=[],
            max_compact_per_section=8,
            fallback_stations=["KAAA"],
            pressure_cache_hours=3,
            pressure_trend_threshold_inhg=0.02,
        ),
        obs=SimpleNamespace(
            aliases={},
            max_normal=2,
            rotate_period_s=300,
            rotate_step=2,
        ),
        fc=SimpleNamespace(
            max_points_normal=1,
            max_points_7day=1,
            periods_normal=1,
            use_short=True,
            rotate_period_s=300,
            rotate_step=1,
        ),
    )


def _observation_builder(tmp_path: Path, api: object):
    from seasonalweather.broadcast.cycle import CycleBuilder

    config = _observation_config()
    return CycleBuilder(
        api=api,
        tz_name="UTC",
        obs_stations=["KAAA"],
        reference_points=[],
        same_fips_all=[],
        cycle_cfg=config,
        registry=DEFAULT_SEGMENT_REGISTRY.resolve(config),
        work_dir=str(tmp_path),
    )


def test_obs_and_marine_rwr_winner_preserves_product_evidence_and_isolated_calls(tmp_path: Path, monkeypatch) -> None:
    import seasonalweather.broadcast.cycle as cycle_module
    from seasonalweather.alerts.nws_api import NWSProduct
    from seasonalweather.broadcast.rwr import RwrProduct

    calls: list[str] = []

    class Api:
        async def latest_product_id(self, kind, office):
            calls.append(f"product:{kind}:{office}")
            return "RWR-123"

        async def get_product(self, _product_id):
            return NWSProduct("RWR-123", "raw", "2026-08-16T12:00:00Z", "RWR", "LWX")

        async def latest_observation(self, station):
            calls.append(f"asos:{station}")
            return {"timestamp": "2026-08-16T12:00:00Z"}

    product = RwrProduct("noon", dt.datetime.now(dt.UTC), "LWX", [], [object()])
    builder = _observation_builder(tmp_path, Api())
    monkeypatch.setattr(cycle_module, "parse_rwr", lambda *_args, **_kwargs: product)
    monkeypatch.setattr(cycle_module, "build_rwr_obs_text", lambda **_kwargs: "RWR spoken")
    monkeypatch.setattr(cycle_module, "build_marine_obs_text", lambda **_kwargs: "marine spoken")
    request = SegmentBuildInput(
        key="obs",
        context=CycleContext(mode="normal", last_heightened_ago=None, last_product_desc=None),
        station_name="station",
        service_area_name="area",
        disclaimer="disclaimer",
    )
    obs = asyncio.run(builder.build_obs_segment(request))
    marine = asyncio.run(builder.build_marine_obs_segment(request))
    assert obs is not None and obs.text == "RWR spoken"
    assert obs.provenance.product_identifier == "RWR-123"
    assert obs.provenance.product_type == "RWR"
    assert marine is not None and marine.provenance.product_identifier == "RWR-123"
    assert not any(call.startswith("asos:") for call in calls)


def test_obs_asos_fallback_is_honest_and_marine_never_runs_asos(tmp_path: Path, monkeypatch) -> None:
    import seasonalweather.broadcast.cycle as cycle_module

    calls: list[str] = []

    class Api:
        async def latest_product_id(self, *_args):
            return None

        async def latest_observation(self, station):
            calls.append(station)
            return {"timestamp": "2026-08-16T12:00:00Z"}

    builder = _observation_builder(tmp_path, Api())
    monkeypatch.setattr(cycle_module, "build_asos_obs_text", lambda **_kwargs: "ASOS spoken")
    request = SegmentBuildInput(
        key="obs",
        context=CycleContext(mode="normal", last_heightened_ago=None, last_product_desc=None),
        station_name="station",
        service_area_name="area",
        disclaimer="disclaimer",
    )
    obs = asyncio.run(builder.build_obs_segment(request))
    calls.clear()
    marine = asyncio.run(builder.build_marine_obs_segment(request))
    assert obs is not None
    assert obs.provenance.source_name == "asos"
    assert obs.provenance.product_type == "ASOS"
    assert obs.provenance.product_identifier is None
    assert marine is None
    assert calls == []


def test_real_independent_builder_seams_have_distinct_target_results(tmp_path: Path, monkeypatch) -> None:
    import seasonalweather.broadcast.cycle as cycle_module
    from seasonalweather.alerts.nws_api import NWSProduct
    from seasonalweather.broadcast.rwr import RwrProduct

    class Api:
        async def latest_product_id(self, *_args):
            return "HWO-1"

        async def get_product(self, _product_id):
            return NWSProduct("HWO-1", "Hazardous Weather Outlook\nClear.", product_type="HWO", wfo="LWX")

    builder = _observation_builder(tmp_path, Api())
    context = CycleContext(mode="normal", last_heightened_ago=None, last_product_desc=None, health_notice="healthy")
    request = SegmentBuildInput("", context, "station", "area", "disclaimer")
    monkeypatch.setattr(builder, "_build_spc_outlook_text", lambda *_args: asyncio.sleep(0, result="SPC"))
    monkeypatch.setattr(builder, "_build_synopsis_text", lambda *_args: asyncio.sleep(0, result="SYNOPSIS"))
    monkeypatch.setattr(builder, "_forecast_settings", lambda _ctx: (1, "shortForecast", 1, 1, 200, ["MDZ001"]))
    monkeypatch.setattr(builder, "_forecast_zone_lines", lambda *_args: asyncio.sleep(0, result=["FORECAST"]))
    monkeypatch.setattr(
        builder,
        "_build_cwf_text_with_evidence",
        lambda _ctx: asyncio.sleep(0, result=("CWF", cycle_module.SegmentSourceEvidence(source_name="nws"))),
    )
    monkeypatch.setattr(
        builder,
        "_build_obs_rwr_segment",
        lambda _ctx: asyncio.sleep(0, result=("OBS", None, None)),
    )
    monkeypatch.setattr(
        builder,
        "_acquire_rwr_source",
        lambda _ctx: asyncio.sleep(0, result=(RwrProduct("x", dt.datetime.now(dt.UTC), "LWX", [], [object()]), None)),
    )
    monkeypatch.setattr(builder, "_build_marine_obs_segment", lambda *_args: asyncio.sleep(0, result="MARINE"))
    candidates = [
        asyncio.run(builder.build_health_segment(request)),
        asyncio.run(builder.build_hwo_segment(request)),
        asyncio.run(builder.build_spc_segment(request)),
        asyncio.run(builder.build_zfp_segment(request)),
        asyncio.run(builder.build_fcst_segment(request)),
        asyncio.run(builder.build_cwf_segment(request)),
        asyncio.run(builder.build_obs_segment(request)),
        asyncio.run(builder.build_marine_obs_segment(request)),
        asyncio.run(builder.build_outro_segment(request)),
    ]
    assert [candidate.key for candidate in candidates if candidate] == [
        "health",
        "hwo",
        "spc",
        "zfp",
        "fcst",
        "cwf",
        "obs",
        "marine_obs",
        "outro",
    ]
    assert len({candidate.text for candidate in candidates if candidate}) == 9
    assert station_id_text(context, "station", "area", "disclaimer").startswith("This is the SeasonalNet")
    custom_id = station_id_text(
        context,
        "CustomStation",
        "Custom service area",
        "Custom disclaimer",
        organization_name="Example Broadcaster",
        service_name="Weather Radio Service",
    )
    assert custom_id.startswith("This is the Example Broadcaster Weather Radio Service, CustomStation")


def test_refresh_command_cancellation_before_publication_cannot_commit(tmp_path: Path) -> None:
    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        command_store = CommandStore(lifecycle=lifecycle)
        registry = DEFAULT_SEGMENT_REGISTRY.resolve(_config())
        store = SegmentStore(tmp_path / "work", tmp_path / "audio")

        class Refresher:
            async def refresh_one(self, _key, **kwargs):
                await asyncio.sleep(0)
                kwargs["commit_guard"]()

        service = SegmentApplicationService(
            registry=lambda: registry,
            store=store,
            refresher=Refresher(),
            mode=lambda: "normal",
            supervisor=TaskSupervisor(lifecycle),
        )
        record, replayed = await service.accept_refresh(
            key="obs",
            actor="tester",
            idempotency_key="cancel-before-publication",
            command_store=command_store,
        )
        assert not replayed
        await command_store.request_cancellation(record.command_id)
        await asyncio.sleep(0.05)
        result = await command_store.get(record.command_id)
        assert result.status is CommandStatus.CANCELLED
        assert store.get("obs") is None

    asyncio.run(scenario())


def test_refresh_command_late_cancellation_preserves_success_after_commit(tmp_path: Path) -> None:
    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        command_store = CommandStore(lifecycle=lifecycle)
        registry = DEFAULT_SEGMENT_REGISTRY.resolve(_config())
        store = SegmentStore(tmp_path / "work", tmp_path / "audio")
        committed = asyncio.Event()
        release = asyncio.Event()

        class Refresher:
            async def refresh_one(self, _key, **kwargs):
                candidate = tmp_path / "audio" / ".candidate.wav"
                candidate.parent.mkdir(parents=True, exist_ok=True)
                write_silence_wav(candidate, 0.1, 8000)
                kwargs["commit_guard"]()
                store.commit_candidate(
                    key="obs",
                    title="Observations",
                    text="fresh",
                    candidate_path=candidate,
                    duration_s=0.1,
                    refresh_interval_s=900,
                    max_age_s=900,
                )
                kwargs["commit_won"]()
                committed.set()
                await release.wait()

        service = SegmentApplicationService(
            registry=lambda: registry,
            store=store,
            refresher=Refresher(),
            mode=lambda: "normal",
            supervisor=TaskSupervisor(lifecycle),
        )
        record, _ = await service.accept_refresh(
            key="obs",
            actor="tester",
            idempotency_key="cancel-after-publication",
            command_store=command_store,
        )
        await asyncio.wait_for(committed.wait(), timeout=1)
        await command_store.request_cancellation(record.command_id)
        release.set()
        await asyncio.sleep(0.05)
        result = await command_store.get(record.command_id)
        assert result.status is CommandStatus.SUCCEEDED

    asyncio.run(scenario())


def test_refresh_persistence_failure_cannot_publish_new_wav_or_command_success(tmp_path: Path) -> None:
    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        command_store = CommandStore(lifecycle=lifecycle)
        registry = DEFAULT_SEGMENT_REGISTRY.resolve(_config())
        store = SegmentStore(tmp_path / "work", tmp_path / "audio")
        stable = store.audio_path_for("obs")
        stable.parent.mkdir(parents=True, exist_ok=True)
        write_silence_wav(stable, 0.1, 8000)
        old_bytes = stable.read_bytes()
        await store.update("obs", "Observations", "old", stable, 0.1, 900, 1800)

        def fail_persist() -> None:
            raise OSError("injected persistence failure")

        store._persist_unlocked = fail_persist  # type: ignore[method-assign]

        class Refresher:
            async def refresh_one(self, _key, **kwargs):
                candidate = tmp_path / "audio" / ".candidate.wav"
                write_silence_wav(candidate, 0.2, 8000)
                kwargs["commit_guard"]()
                store.commit_candidate(
                    key="obs",
                    title="Observations",
                    text="new",
                    candidate_path=candidate,
                    duration_s=0.2,
                    refresh_interval_s=900,
                    max_age_s=900,
                )

        service = SegmentApplicationService(
            registry=lambda: registry,
            store=store,
            refresher=Refresher(),
            mode=lambda: "normal",
            supervisor=TaskSupervisor(lifecycle),
        )
        record, _ = await service.accept_refresh(
            key="obs",
            actor="tester",
            idempotency_key="persistence-failure",
            command_store=command_store,
        )
        await asyncio.sleep(0.05)
        result = await command_store.get(record.command_id)
        assert result.status is CommandStatus.FAILED
        assert store.get("obs").text == "old"
        assert store.audio_path_for("obs").read_bytes() == old_bytes

    asyncio.run(scenario())


def test_reconciliation_waits_for_inflight_commit_and_does_not_fabricate_receipt(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        command_store = CommandStore(lifecycle=lifecycle)
        command, _ = await command_store.create_or_replay(
            command_type="segment.refresh",
            idempotency_key="reconcile-inflight-failure",
            actor="tester",
            payload={"segment_key": "obs"},
            reason="segment-refresh:obs",
        )
        await command_store.mark_running(command.command_id)
        store = SegmentStore(tmp_path / "work", tmp_path / "audio")
        old_audio = store.audio_path_for("obs")
        old_audio.parent.mkdir(parents=True, exist_ok=True)
        write_silence_wav(old_audio, 0.1, 8000)
        await store.update("obs", "Observations", "old", old_audio, 0.1, 900, 1800)
        old_text = store.get("obs").text
        entered = threading.Event()
        release = threading.Event()

        def fail_persist() -> None:
            entered.set()
            assert release.wait(2)
            raise OSError("injected in-flight metadata failure")

        store._persist_unlocked = fail_persist  # type: ignore[method-assign]
        candidate = tmp_path / "audio" / ".inflight-failure.wav"
        write_silence_wav(candidate, 0.2, 8000)
        commit_task = asyncio.create_task(
            asyncio.to_thread(
                store.commit_candidate,
                key="obs",
                title="Observations",
                text="new",
                candidate_path=candidate,
                duration_s=0.2,
                refresh_interval_s=900,
                command_id=command.command_id,
            )
        )
        assert await asyncio.to_thread(entered.wait, 1)
        repair_task = asyncio.create_task(store.reconcile_committed_refresh_commands(command_store))
        await asyncio.sleep(0.05)
        assert not repair_task.done()
        release.set()
        with pytest.raises(OSError, match="in-flight metadata failure"):
            await commit_task
        assert await repair_task == 0
        assert store.get("obs").text == old_text
        assert store.audio_path_for("obs").read_bytes() == old_audio.read_bytes()
        assert store.committed_refresh_receipts() == ()
        assert (await command_store.get(command.command_id)).status is CommandStatus.RUNNING

    asyncio.run(scenario())


def test_reconciliation_racing_successful_commit_converges_after_durable_truth(tmp_path: Path) -> None:
    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        command_store = CommandStore(lifecycle=lifecycle)
        command, _ = await command_store.create_or_replay(
            command_type="segment.refresh",
            idempotency_key="reconcile-inflight-success",
            actor="tester",
            payload={"segment_key": "obs"},
            reason="segment-refresh:obs",
        )
        await command_store.mark_running(command.command_id)
        store = SegmentStore(tmp_path / "work", tmp_path / "audio")
        old_audio = store.audio_path_for("obs")
        old_audio.parent.mkdir(parents=True, exist_ok=True)
        write_silence_wav(old_audio, 0.1, 8000)
        await store.update("obs", "Observations", "old", old_audio, 0.1, 900, 1800)
        entered = threading.Event()
        release = threading.Event()
        original_persist = store._persist_unlocked

        def pause_persist() -> None:
            entered.set()
            assert release.wait(2)
            original_persist()

        store._persist_unlocked = pause_persist  # type: ignore[method-assign]
        candidate = tmp_path / "audio" / ".inflight-success.wav"
        write_silence_wav(candidate, 0.2, 8000)
        commit_task = asyncio.create_task(
            asyncio.to_thread(
                store.commit_candidate,
                key="obs",
                title="Observations",
                text="new",
                candidate_path=candidate,
                duration_s=0.2,
                refresh_interval_s=900,
                command_id=command.command_id,
            )
        )
        assert await asyncio.to_thread(entered.wait, 1)
        repair_task = asyncio.create_task(store.reconcile_committed_refresh_commands(command_store))
        await asyncio.sleep(0.05)
        assert not repair_task.done()
        release.set()
        result = await commit_task
        assert result.committed
        assert await repair_task == 1
        assert (await command_store.get(command.command_id)).status is CommandStatus.SUCCEEDED
        assert store.committed_refresh_receipts() == ()

    asyncio.run(scenario())


def test_preview_is_read_only_and_rejects_live_or_build_only_refresh() -> None:
    registry = DEFAULT_SEGMENT_REGISTRY.resolve(_config())

    class Store:
        def get(self, _key):
            return None

        def is_ready(self, _key):
            return False

    service = SegmentApplicationService(
        registry=lambda: registry,
        store=Store(),  # type: ignore[arg-type]
        refresher=SimpleNamespace(),
        mode=lambda: "normal",
    )
    preview = service.cycle_preview()
    assert preview["read_only"] is True
    assert preview["segments"]
    assert "time" in {item["key"] for item in preview["segments"]}
    try:
        service.get_segment("missing")
    except SegmentServiceError as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("unknown segment must be rejected")


def test_preview_includes_currently_due_deferred_focus_segments_without_mutation(tmp_path: Path) -> None:
    registry = DEFAULT_SEGMENT_REGISTRY.resolve(_config())
    store = SegmentStore(tmp_path / "work", tmp_path / "audio")
    stable = store.audio_path_for("zfp")
    stable.parent.mkdir(parents=True, exist_ok=True)
    stable.write_bytes(b"zfp")
    asyncio.run(
        store.update(
            "zfp",
            registry.title_for("zfp"),
            "synopsis",
            stable,
            1.0,
            900,
            1800,
            provenance=SegmentProvenance(next_eligible_airtime="2020-01-01T00:00:00+00:00"),
        )
    )
    snapshot = {
        "focus": True,
        "deferred_keys": ("zfp", "fcst"),
        "deferred_due_keys": ("zfp",),
        "deferred": (
            {"key": "zfp", "due": True},
            {"key": "fcst", "due": False},
        ),
    }
    service = SegmentApplicationService(
        registry=lambda: registry,
        store=store,
        refresher=SimpleNamespace(),
        mode=lambda: "normal",
        runtime_snapshot=lambda: snapshot.copy(),
    )
    before = json.dumps(snapshot, sort_keys=True)
    first = service.cycle_preview()
    second = service.cycle_preview()
    assert before == json.dumps(snapshot, sort_keys=True)
    assert first == second
    due = next(item for item in first["segments"] if item["key"] == "zfp")
    assert due["deferred"] is True and due["deferred_due"] is True and due["selected"] is True
    assert any(item["key"] == "zfp" and item["due"] for item in first["deferred"])


def test_focus_projection_uses_current_alert_and_mode_inputs() -> None:
    from seasonalweather.alerts.focus import AlertFocusPolicy
    from seasonalweather.broadcast.conductor import focus_mode_from_inputs

    alert = SimpleNamespace(
        source="CAP",
        event="Tornado Warning",
        code="TOR",
        headline="Tornado warning",
        vtec_track_ids=lambda: [],
    )
    policy = AlertFocusPolicy()
    assert focus_mode_from_inputs((), policy, "normal") is False
    assert focus_mode_from_inputs((alert,), policy, "normal") is True
    assert focus_mode_from_inputs((), policy, "heightened") is True


def test_conductor_focus_projection_matches_rebuild_on_entry_and_stable_due_order(tmp_path: Path) -> None:
    from seasonalweather.broadcast.conductor import CycleConductor

    registry = DEFAULT_SEGMENT_REGISTRY.resolve(_config())
    mode = ["normal"]

    class Tracker:
        def get_cycle_alerts(self):
            return ()

    conductor = CycleConductor(
        store=SegmentStore(tmp_path / "work", tmp_path / "audio"),
        telnet=SimpleNamespace(),
        tts=SimpleNamespace(),
        alert_tracker=Tracker(),
        tz=ZoneInfo("UTC"),
        audio_dir=tmp_path / "audio",
        sample_rate=8000,
        np_meta_fn=lambda **_kwargs: {},
        registry=registry,
        mode_fn=lambda: mode[0],
    )
    old = time.time() - 10_000
    for key in registry.deferred_focus_keys():
        conductor._last_pushed_at[key] = old

    normal = conductor.inspection_snapshot()
    assert not normal["focus"] and not normal["deferred_due_keys"]
    mode[0] = "heightened"
    entering = conductor.inspection_snapshot()
    assert entering["focus"] and not entering["deferred_due_keys"]
    conductor._rebuild_cycle_order()
    assert not any(key in conductor._cycle_order for key in registry.deferred_focus_keys())

    for key in registry.deferred_focus_keys():
        conductor._last_pushed_at[key] = old
    stable = conductor.inspection_snapshot()
    conductor._rebuild_cycle_order()
    actual_deferred = [key for key in conductor._cycle_order if key in registry.deferred_focus_keys()]
    assert actual_deferred == list(stable["deferred_due_keys"])

    mode[0] = "normal"
    normal_again = conductor.inspection_snapshot()
    assert not normal_again["focus"] and not normal_again["deferred_due_keys"]


def test_normal_preview_keeps_deferred_policy_inactive_and_focus_preserves_due_order(tmp_path: Path) -> None:
    registry = DEFAULT_SEGMENT_REGISTRY.resolve(_config())
    store = SegmentStore(tmp_path / "work", tmp_path / "audio")
    for key in ("zfp", "fcst"):
        audio = store.audio_path_for(key)
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(key.encode())
        asyncio.run(store.update(key, registry.title_for(key), key, audio, 1.0, 900, 1800))

    snapshot = {
        "focus": False,
        "deferred_keys": ("zfp", "fcst"),
        "deferred_due_keys": ("fcst", "zfp"),
        "deferred": (
            {"key": "fcst", "due": True},
            {"key": "zfp", "due": True},
        ),
    }
    service = SegmentApplicationService(
        registry=lambda: registry,
        store=store,
        refresher=SimpleNamespace(),
        mode=lambda: "normal",
        runtime_snapshot=lambda: snapshot,
    )
    normal = {item["key"]: item for item in service.cycle_preview()["segments"]}
    assert normal["zfp"]["selected"] and normal["fcst"]["selected"]
    assert not normal["zfp"]["deferred"] and not normal["fcst"]["deferred"]

    snapshot["focus"] = True
    focused = service.cycle_preview()
    focused_order = [item["key"] for item in focused["segments"] if item["key"] in {"zfp", "fcst"}]
    assert focused_order == ["fcst", "zfp"]
    assert all(item["selected"] for item in focused["segments"] if item["key"] in {"zfp", "fcst"})
    assert focused == service.cycle_preview()


def test_unavailable_refresh_through_refresher_retains_lkg_failure_evidence_and_resets(tmp_path: Path) -> None:
    registry = DEFAULT_SEGMENT_REGISTRY.resolve(_config())
    store = SegmentStore(tmp_path / "work", tmp_path / "audio")
    audio = store.audio_path_for("obs")
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"lkg")
    asyncio.run(store.update("obs", "Observations", "old", audio, 1.0, 900, 1800))
    attempts = 0

    class Builder:
        async def build_obs_segment(self, request):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                return None
            return SegmentCandidate(
                key=request.key,
                title="Observations",
                text="fresh",
                provenance=SegmentProvenance(source_name="asos", product_type="ASOS"),
            )

    refresher = SegmentRefresher(
        store=store,
        cycle_builder=Builder(),
        tts=SimpleNamespace(),
        alert_tracker=SimpleNamespace(),
        ctx_fn=lambda: CycleContext(mode="normal", last_heightened_ago=None, last_product_desc=None),
        station_name="station",
        service_area_name="area",
        disclaimer="disclaimer",
        tz=ZoneInfo("UTC"),
        sample_rate=8000,
        registry=registry,
    )

    async def fake_synth(**kwargs):
        await store.update(
            kwargs["key"],
            kwargs["title"],
            kwargs["text"],
            audio,
            1.0,
            kwargs["interval"],
            kwargs["max_age"],
            provenance=kwargs["provenance"],
        )

    refresher._synth = fake_synth  # type: ignore[method-assign]
    asyncio.run(refresher.refresh_one("obs"))
    first = store.get("obs")
    assert first is not None and store.is_ready("obs") and not first.is_placeholder
    assert first.provenance.consecutive_failures == 1 and first.provenance.last_error
    asyncio.run(refresher.refresh_one("obs"))
    assert store.get("obs").provenance.consecutive_failures == 2
    asyncio.run(refresher.refresh_one("obs"))
    recovered = store.get("obs")
    assert recovered is not None and recovered.text == "fresh"
    assert recovered.provenance.consecutive_failures == 0 and recovered.provenance.last_error is None


def test_segment_store_reconciles_interrupted_commit_without_losing_lkg(tmp_path: Path) -> None:
    store, old_bytes = _seed_store_with_audio(tmp_path)
    target = store._versioned_audio_path("obs")
    target.write_bytes(b"interrupted candidate")
    store._write_commit_journal(key="obs", target=target, previous=Path(store.get("obs").audio_path))

    reopened = SegmentStore(tmp_path / "work", tmp_path / "audio")
    assert reopened.load() == 1
    assert reopened.get("obs").text == "old"
    assert reopened.audio_path_for("obs").read_bytes() == old_bytes
    assert not target.exists()
    assert not store._journal_path("obs").exists()


def test_segment_store_metadata_failure_and_target_cleanup_failure_keep_lkg(tmp_path: Path, monkeypatch) -> None:
    import seasonalweather.broadcast.segment_store as segment_store_module

    store, old_bytes = _seed_store_with_audio(tmp_path)
    candidate = tmp_path / "audio" / ".candidate.wav"
    write_silence_wav(candidate, 0.2, 8000)
    original_unlink = Path.unlink

    def fail_new_target(path, *args, **kwargs):
        if path.name.startswith("cycle_seg_obs."):
            raise OSError("injected target cleanup failure")
        return original_unlink(path, *args, **kwargs)

    def fail_persist() -> None:
        raise OSError("injected metadata failure")

    monkeypatch.setattr(store, "_persist_unlocked", fail_persist)
    monkeypatch.setattr(segment_store_module.Path, "unlink", fail_new_target)
    with pytest.raises(OSError, match="injected metadata failure"):
        store.commit_candidate(
            key="obs",
            title="Observations",
            text="new",
            candidate_path=candidate,
            duration_s=0.2,
            refresh_interval_s=900,
            max_age_s=1800,
        )
    assert store.get("obs").text == "old"
    assert store.audio_path_for("obs").read_bytes() == old_bytes


def test_two_successful_refreshes_in_one_second_use_commit_truth(tmp_path: Path, monkeypatch) -> None:
    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        command_store = CommandStore(lifecycle=lifecycle)
        registry = DEFAULT_SEGMENT_REGISTRY.resolve(_config())
        store = SegmentStore(tmp_path / "work", tmp_path / "audio")

        class Refresher:
            count = 0

            async def refresh_one(self, _key, **kwargs):
                self.count += 1
                candidate = tmp_path / "audio" / f".candidate-{self.count}.wav"
                candidate.parent.mkdir(parents=True, exist_ok=True)
                write_silence_wav(candidate, 0.1, 8000)
                kwargs["commit_guard"]()
                result = store.commit_candidate(
                    key="obs",
                    title="Observations",
                    text=f"fresh-{self.count}",
                    candidate_path=candidate,
                    duration_s=0.1,
                    refresh_interval_s=900,
                    max_age_s=900,
                )
                kwargs["commit_won"](result)
                return result

        refresher = Refresher()
        service = SegmentApplicationService(
            registry=lambda: registry,
            store=store,
            refresher=refresher,
            mode=lambda: "normal",
            supervisor=TaskSupervisor(lifecycle),
        )
        first, _ = await service.accept_refresh(
            key="obs", actor="tester", idempotency_key="same-second-1", command_store=command_store
        )
        second, _ = await service.accept_refresh(
            key="obs", actor="tester", idempotency_key="same-second-2", command_store=command_store
        )
        for record in (first, second):
            for _ in range(20):
                current = await command_store.get(record.command_id)
                if current.status is not CommandStatus.RUNNING and current.status is not CommandStatus.ACCEPTED:
                    break
                await asyncio.sleep(0.01)
            assert current.status is CommandStatus.SUCCEEDED
        assert store.get("obs").text == "fresh-2"

    asyncio.run(scenario())


def test_zfp_synopsis_source_evidence_survives_fallback_and_api_projection(tmp_path: Path) -> None:
    from seasonalweather.alerts.nws_api import NWSProduct
    from seasonalweather.broadcast.cycle import CycleBuilder

    config = _observation_config()
    config.syn = SimpleNamespace(max_chars_normal=1000)

    class Api:
        async def latest_product_id(self, kind, _office):
            return {"SYN": "SYN-1", "RWS": None, "AFD": None}.get(kind)

        async def get_product(self, product_id):
            return NWSProduct(
                product_id,
                "Synopsis conditions are calm.",
                issuance_time="2026-08-16T12:00:00Z",
                product_type="SYN",
                wfo="LWX",
            )

    builder = CycleBuilder(
        api=Api(),
        tz_name="UTC",
        obs_stations=[],
        reference_points=[],
        same_fips_all=[],
        cycle_cfg=config,
        registry=DEFAULT_SEGMENT_REGISTRY.resolve(config),
        work_dir=str(tmp_path),
    )
    request = SegmentBuildInput(
        key="zfp",
        context=CycleContext(mode="normal", last_heightened_ago=None, last_product_desc=None),
        station_name="station",
        service_area_name="area",
        disclaimer="disclaimer",
    )
    candidate = asyncio.run(builder.build_zfp_segment(request))
    assert candidate is not None
    assert candidate.provenance.product_identifier == "SYN-1"
    assert candidate.provenance.product_type == "SYN"
    assert candidate.provenance.issuing_office == "LWX"
    assert candidate.provenance.source_reference == "https://api.weather.gov/products/SYN-1"

    audio = tmp_path / "audio" / "cycle_seg_zfp.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"zfp")
    store = SegmentStore(tmp_path / "store", tmp_path / "audio")
    asyncio.run(
        store.update("zfp", candidate.title, candidate.text, audio, 1.0, 900, 1800, provenance=candidate.provenance)
    )
    service = SegmentApplicationService(
        registry=lambda: DEFAULT_SEGMENT_REGISTRY.resolve(config),
        store=store,
        refresher=SimpleNamespace(),
        mode=lambda: "normal",
    )
    projection = service.get_segment("zfp")["provenance"]
    assert projection["product_identifier"] == "SYN-1"
    assert projection["product_type"] == "SYN"

    class FallbackApi(Api):
        async def latest_product_id(self, kind, _office):
            return "AFD-1" if kind == "AFD" else None

        async def get_product(self, product_id):
            return NWSProduct(
                product_id,
                ".SYNOPSIS...\nFallback synopsis conditions.\n.NEAR TERM...\nIgnored.",
                issuance_time="2026-08-16T13:00:00Z",
                product_type="AFD",
                wfo="LWX",
            )

    fallback_builder = CycleBuilder(
        api=FallbackApi(),
        tz_name="UTC",
        obs_stations=[],
        reference_points=[],
        same_fips_all=[],
        cycle_cfg=config,
        registry=DEFAULT_SEGMENT_REGISTRY.resolve(config),
        work_dir=str(tmp_path),
    )
    fallback = asyncio.run(fallback_builder.build_zfp_segment(request))
    assert fallback is not None
    assert fallback.provenance.product_identifier == "AFD-1"
    assert fallback.provenance.product_type == "AFD"


def _segment_request(key: str, context: CycleContext | None = None) -> SegmentBuildInput:
    return SegmentBuildInput(
        key=key,
        context=context or CycleContext(mode="normal", last_heightened_ago=None, last_product_desc=None),
        station_name="station",
        service_area_name="area",
        disclaimer="disclaimer",
    )


def test_real_spc_builder_acquisition_and_assembly_cover_success_unavailable_and_failure(tmp_path: Path) -> None:
    from seasonalweather.broadcast.cycle import CycleBuilder

    config = _observation_config()
    config.spc = SimpleNamespace(enabled=True, wfos=["LWX"], days=1, min_dn=3, timeout_s=1.0)

    class Api:
        pass

    builder = CycleBuilder(
        api=Api(),
        tz_name="UTC",
        obs_stations=[],
        reference_points=[],
        same_fips_all=[],
        cycle_cfg=config,
        registry=DEFAULT_SEGMENT_REGISTRY.resolve(config),
        work_dir=str(tmp_path),
    )
    builder._arcgis_find_layer_id = lambda _base, names, _timeout: asyncio.sleep(
        0, result=1 if "categorical" in names else 2
    )  # type: ignore[method-assign]
    builder._wfo_cwa_geometry = lambda _wfo, _timeout: asyncio.sleep(0, result={"rings": []})  # type: ignore[method-assign]

    async def query(_base, layer, *_args, **_kwargs):
        return [{"attributes": {"DN": 3}}] if layer == 1 else [{"attributes": {"PROB": 15}}]

    builder._arcgis_query = query  # type: ignore[method-assign]
    candidate = asyncio.run(builder.build_spc_segment(_segment_request("spc")))
    assert candidate is not None
    assert "marginal risk" in candidate.text
    assert candidate.provenance.source_name == "spc"
    assert candidate.provenance.product_type == "convective_outlook"

    builder._arcgis_find_layer_id = lambda *_args: asyncio.sleep(0, result=None)  # type: ignore[method-assign]
    assert asyncio.run(builder.build_spc_segment(_segment_request("spc"))) is None

    async def failing_query(*_args, **_kwargs):
        raise RuntimeError("bounded SPC acquisition failure")

    builder._arcgis_find_layer_id = lambda *_args: asyncio.sleep(0, result=1)  # type: ignore[method-assign]
    builder._arcgis_query = failing_query  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="bounded SPC acquisition failure"):
        asyncio.run(builder.build_spc_segment(_segment_request("spc")))


def test_real_fcst_and_cwf_builders_cover_success_unavailable_and_provenance(tmp_path: Path) -> None:
    from seasonalweather.alerts.nws_api import NWSProduct
    from seasonalweather.broadcast.cycle import CycleBuilder

    config = _observation_config()
    config.fc.forecast_zones = [("MDZ001", "Zone one")]
    config.fc.periods_per_group = 4
    config.fc.point_max_chars = 1600
    config.cwf.max_chars_heightened = 1200

    class Api:
        async def zone_forecast_periods(self, _zone):
            return [{"name": "Today", "detailedForecast": "Sunny and calm."}]

        async def coastal_waters_forecast_product(self, _office):
            return NWSProduct("CWF-1", ".SYNOPSIS... Calm waters.", "2026-08-16T12:00:00Z", "CWF", "LWX")

    builder = CycleBuilder(
        api=Api(),
        tz_name="UTC",
        obs_stations=[],
        reference_points=[],
        same_fips_all=[],
        cycle_cfg=config,
        registry=DEFAULT_SEGMENT_REGISTRY.resolve(config),
        work_dir=str(tmp_path),
    )
    fcst = asyncio.run(builder.build_fcst_segment(_segment_request("fcst")))
    cwf = asyncio.run(builder.build_cwf_segment(_segment_request("cwf")))
    assert fcst is not None and "Sunny and calm" in fcst.text
    assert fcst.provenance.source_name == "nws" and fcst.provenance.product_type == "forecast"
    assert cwf is not None and cwf.provenance.product_identifier == "CWF-1"
    assert cwf.provenance.issuing_office == "LWX"

    class UnavailableApi(Api):
        async def zone_forecast_periods(self, _zone):
            return []

        async def coastal_waters_forecast_product(self, _office):
            return None

    unavailable = CycleBuilder(
        api=UnavailableApi(),
        tz_name="UTC",
        obs_stations=[],
        reference_points=[],
        same_fips_all=[],
        cycle_cfg=config,
        registry=DEFAULT_SEGMENT_REGISTRY.resolve(config),
        work_dir=str(tmp_path),
    )
    assert asyncio.run(unavailable.build_fcst_segment(_segment_request("fcst"))) is None
    assert asyncio.run(unavailable.build_cwf_segment(_segment_request("cwf"))) is None


def test_commit_recovery_rejects_hostile_targets_without_mutation(tmp_path: Path) -> None:
    store, _old_bytes = _seed_store_with_audio(tmp_path)
    accepted_candidate = tmp_path / "audio" / ".accepted.wav"
    write_silence_wav(accepted_candidate, 0.1, 8000)
    accepted = store.commit_candidate(
        key="obs",
        title="Observations",
        text="accepted",
        candidate_path=accepted_candidate,
        duration_s=0.1,
        refresh_interval_s=900,
    )
    accepted_path = Path(accepted.entry.audio_path)
    committed_journal = store._journal_path("obs")
    committed_journal.write_text(
        json.dumps({"key": "obs", "target": str(accepted_path), "previous": None}),
        encoding="utf-8",
    )
    reopened = SegmentStore(tmp_path / "work", tmp_path / "audio")
    reopened.load()
    assert accepted_path.exists() and not committed_journal.exists()
    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"outside")

    hostile = [
        ("hwo", {"key": "hwo", "target": str(accepted_path), "previous": None}),
        ("obs", {"key": "obs", "target": str(outside), "previous": None}),
        ("obs", {"key": "obs", "target": "not-json-path", "previous": None}),
    ]
    for key, payload in hostile:
        path = store._journal_path(key)
        path.write_text(json.dumps(payload), encoding="utf-8")
        reopened = SegmentStore(tmp_path / "work", tmp_path / "audio")
        reopened.load()
        assert accepted_path.exists() and outside.read_bytes() == b"outside"
        assert path.exists()
        path.unlink()

    asyncio.run(store.update("hwo", "HWO", "hwo", accepted_path, 0.1, 900, 1800))
    path = store._journal_path("obs")
    path.write_text(
        json.dumps({"key": "obs", "target": str(accepted_path), "previous": None}),
        encoding="utf-8",
    )
    reopened = SegmentStore(tmp_path / "work", tmp_path / "audio")
    reopened.load()
    assert accepted_path.exists() and path.exists()

    symlink_target = tmp_path / "audio" / f"cycle_seg_obs.{('b' * 32)}.wav"
    symlink_target.symlink_to(outside)
    symlink_journal = store._journal_path("obs")
    symlink_journal.write_text(
        json.dumps({"key": "obs", "target": str(symlink_target), "previous": None}),
        encoding="utf-8",
    )
    SegmentStore(tmp_path / "work", tmp_path / "audio").load()
    assert symlink_target.is_symlink() and outside.read_bytes() == b"outside"


def test_commit_recovery_cleans_only_valid_interrupted_targets_and_candidates(tmp_path: Path) -> None:
    store, _old_bytes = _seed_store_with_audio(tmp_path)
    interrupted = store._versioned_audio_path("obs")
    interrupted.write_bytes(b"interrupted")
    store._write_commit_journal(key="obs", target=interrupted, previous=Path(store.get("obs").audio_path))
    candidates = [tmp_path / "audio" / f".segment-candidate-{index:032x}.wav" for index in range(2)]
    for path in candidates:
        path.write_bytes(b"candidate")
    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"outside")
    linked = tmp_path / "audio" / (".segment-candidate-" + "f" * 32 + ".wav")
    linked.symlink_to(outside)
    accepted = store._versioned_audio_path("obs")
    accepted.write_bytes(b"accepted")

    reopened = SegmentStore(tmp_path / "work", tmp_path / "audio")
    reopened.load()
    assert not interrupted.exists()
    assert all(not path.exists() for path in candidates)
    assert linked.is_symlink() and outside.read_bytes() == b"outside"
    assert accepted.exists()


def test_segment_store_uses_injected_registry_identity_and_separate_dynamic_alert_syntax(tmp_path: Path) -> None:
    registry = DEFAULT_SEGMENT_REGISTRY.resolve(_config())
    assert all(registry.is_managed(item.definition.key) for item in registry.definitions)

    synthetic_store = SegmentStore(
        tmp_path / "synthetic-work",
        tmp_path / "synthetic-audio",
        static_key_predicate=lambda key: registry.is_managed(key) or key == "synthetic",
    )
    candidate = tmp_path / "synthetic-audio" / ".candidate.wav"
    write_silence_wav(candidate, 0.1, 8000)
    committed = synthetic_store.commit_candidate(
        key="synthetic",
        title="Synthetic",
        text="synthetic",
        candidate_path=candidate,
        duration_s=0.1,
        refresh_interval_s=900,
    )
    journal = synthetic_store._journal_path("synthetic")
    journal.write_text(
        json.dumps({"key": "synthetic", "target": committed.entry.audio_path, "previous": None}),
        encoding="utf-8",
    )
    reopened = SegmentStore(
        tmp_path / "synthetic-work",
        tmp_path / "synthetic-audio",
        static_key_predicate=lambda key: registry.is_managed(key) or key == "synthetic",
    )
    reopened.load()
    assert not journal.exists()

    arbitrary = reopened._journal_path("arbitrary-static")
    arbitrary.write_text(
        json.dumps(
            {
                "key": "arbitrary-static",
                "target": str(reopened._versioned_audio_path("arbitrary-static")),
                "previous": None,
            }
        ),
        encoding="utf-8",
    )
    reopened.load()
    assert arbitrary.exists()
    assert reopened._journal_key_is_governed("_alert_runtime-1")
    assert not reopened._journal_key_is_governed("alert-runtime-1")


def test_cycle_preview_matches_conductor_dynamic_order_and_is_read_only(tmp_path: Path) -> None:
    from seasonalweather.broadcast.conductor import CycleConductor

    registry = DEFAULT_SEGMENT_REGISTRY.resolve(_config())
    store = SegmentStore(tmp_path / "work", tmp_path / "audio")
    alert_audio = store.audio_path_for("_alert_a1")
    alert_audio.parent.mkdir(parents=True, exist_ok=True)
    alert_audio.write_bytes(b"alert")
    asyncio.run(store.update("_alert_a1", "Alert A1", "alert", alert_audio, 1.0, 900, 1800))
    insert_audio = tmp_path / "insert.wav"
    insert_audio.write_bytes(b"insert")
    alerts = (
        SimpleNamespace(
            id="a1",
            source="CAP",
            event="Special Weather Statement",
            code="SPS",
            headline="Special weather statement",
            vtec_track_ids=lambda: [],
        ),
        SimpleNamespace(
            id="a2",
            source="CAP",
            event="Special Weather Statement",
            code="SPS",
            headline="Another special weather statement",
            vtec_track_ids=lambda: [],
        ),
    )
    inserts = {
        "after_time": [{"insert_id": "time1", "title": "After time", "audio_path": str(insert_audio)}],
        "after_status": [{"insert_id": "status1", "title": "After status", "audio_path": str(insert_audio)}],
        "end_of_rotation": [{"insert_id": "end1", "title": "At end", "audio_path": str(insert_audio)}],
    }

    class Tracker:
        _alerts = {}

        def get_cycle_alerts(self):
            return alerts

    mode = ["normal"]
    seen_now: list[str] = []

    def scheduled_inserts(placement, _rotation, _focus, now_iso):
        seen_now.append(now_iso)
        return inserts[placement]

    conductor = CycleConductor(
        store=store,
        telnet=SimpleNamespace(push_cycle=lambda *_args, **_kwargs: None),
        tts=SimpleNamespace(),
        alert_tracker=Tracker(),
        tz=ZoneInfo("UTC"),
        audio_dir=tmp_path / "audio",
        sample_rate=8000,
        np_meta_fn=lambda **_kwargs: {},
        registry=registry,
        mode_fn=lambda: mode[0],
        scheduled_inserts_fn=scheduled_inserts,
        scheduled_inserts_snapshot_fn=scheduled_inserts,
    )
    before = (list(conductor._cycle_order), dict(conductor._insert_cache), conductor._rotation_count)
    snapshot = conductor.inspection_snapshot()
    assert len(seen_now) == 3 and len(set(seen_now)) == 1
    assert (conductor._cycle_order, conductor._insert_cache, conductor._rotation_count) == before
    seen_now.clear()
    conductor._rebuild_cycle_order()
    assert len(seen_now) == 3 and len(set(seen_now)) == 1
    assert list(snapshot["order"]) == conductor._cycle_order
    assert snapshot["order"].index("_alert_a1") == snapshot["order"].index("time") + 1
    assert snapshot["order"].index("_alert_a2") == snapshot["order"].index("_alert_a1") + 1
    assert snapshot["order"].index("_insert_time1") == snapshot["order"].index("_alert_a2") + 1
    assert snapshot["order"].index("_insert_status1") > snapshot["order"].index("status")
    assert snapshot["order"][-1] == "_insert_end1"

    service = SegmentApplicationService(
        registry=lambda: registry,
        store=store,
        refresher=SimpleNamespace(),
        mode=lambda: mode[0],
        runtime_snapshot=conductor.inspection_snapshot,
    )
    preview = service.cycle_preview()
    assert tuple(preview["order"]) == tuple(conductor._cycle_order)
    dynamic = {item["key"]: item for item in preview["segments"] if item["kind"] == "alert"}
    assert dynamic["_alert_a1"]["selected"] and dynamic["_alert_a1"]["eligible_to_air"]
    assert not dynamic["_alert_a2"]["selected"] and not dynamic["_alert_a2"]["eligible_to_air"]
    assert conductor._push_tracker_alert("a1") > 0.0
    assert conductor._push_tracker_alert("a2") == 0.0
    assert service.cycle_preview() == preview
    unsafe_target = tmp_path / "unsafe-alert.wav"
    unsafe_target.write_bytes(b"unsafe target")
    alert_audio.unlink()
    alert_audio.symlink_to(unsafe_target)
    unsafe_preview = service.cycle_preview()
    unsafe_item = next(item for item in unsafe_preview["segments"] if item["key"] == "_alert_a1")
    assert not unsafe_item["selected"] and not unsafe_item["eligible_to_air"]
    assert conductor._push_tracker_alert("a1") == 0.0
    assert [item["kind"] for item in preview["segments"] if item["key"].startswith("_")] == [
        "alert",
        "alert",
        "scheduled_insert",
        "scheduled_insert",
        "scheduled_insert",
    ]

    mode[0] = "heightened"
    entering = conductor.inspection_snapshot()
    assert entering["focus"] is True
    assert entering["deferred_due_keys"] == ()
    conductor._rebuild_cycle_order()
    assert list(entering["order"]) == conductor._cycle_order
    for key in registry.deferred_focus_keys():
        conductor._last_pushed_at[key] = time.time() - 10_000
    stable = conductor.inspection_snapshot()
    conductor._rebuild_cycle_order()
    assert list(stable["order"]) == conductor._cycle_order
    assert stable["deferred_due_keys"]
    mode[0] = "normal"
    returning = conductor.inspection_snapshot()
    conductor._rebuild_cycle_order()
    assert not returning["focus"]
    assert list(returning["order"]) == conductor._cycle_order


def test_inspection_uses_one_registry_snapshot_per_response() -> None:
    old_registry = DEFAULT_SEGMENT_REGISTRY.resolve(_config())
    new_config = _config()
    new_config.spc = SimpleNamespace(enabled=False)
    new_registry = DEFAULT_SEGMENT_REGISTRY.resolve(new_config)
    calls = 0

    def registry_provider():
        nonlocal calls
        calls += 1
        return old_registry if calls == 1 else new_registry

    class Store:
        def get(self, _key):
            return None

        def is_ready(self, _key):
            return False

    service = SegmentApplicationService(
        registry=registry_provider,
        store=Store(),  # type: ignore[arg-type]
        refresher=SimpleNamespace(),
        mode=lambda: "normal",
        runtime_snapshot=lambda: {"mode": "normal", "focus": False},
    )
    listing = service.list_segments()
    assert calls == 1
    assert next(item for item in listing["segments"] if item["key"] == "spc")["enabled"] is True

    calls = 0
    detail = service.get_segment("spc")
    assert calls == 1
    assert detail["enabled"] is True

    calls = 0
    preview = service.cycle_preview()
    assert calls == 1
    assert preview["read_only"] is True


def test_committed_refresh_receipt_repairs_exact_running_command_after_reopen(tmp_path: Path) -> None:
    from seasonalweather.database.core import SeasonalDatabase

    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        database = SeasonalDatabase(path=str(tmp_path / "commands.sqlite3"))
        command_store = CommandStore(database=database, lifecycle=lifecycle)
        command, _ = await command_store.create_or_replay(
            command_type="segment.refresh",
            idempotency_key="receipt-recovery",
            actor="tester",
            payload={"segment_key": "obs"},
            reason="segment-refresh:obs",
        )
        await command_store.mark_running(command.command_id)
        store = SegmentStore(tmp_path / "work", tmp_path / "audio", database=database)
        candidate = tmp_path / "audio" / ".candidate.wav"
        write_silence_wav(candidate, 0.1, 8000)
        store.commit_candidate(
            key="obs",
            title="Observations",
            text="committed before terminal acknowledgement",
            candidate_path=candidate,
            duration_s=0.1,
            refresh_interval_s=900,
            command_id=command.command_id,
        )

        reopened = SegmentStore(tmp_path / "work", tmp_path / "audio", database=database)
        assert reopened.load() == 1
        assert len(reopened.committed_refresh_receipts()) == 1
        repaired_store = CommandStore(database=database, lifecycle=lifecycle)
        assert await reopened.reconcile_committed_refresh_commands(repaired_store) == 1
        assert (await repaired_store.get(command.command_id)).status is CommandStatus.SUCCEEDED
        assert reopened.committed_refresh_receipts() == ()

    asyncio.run(scenario())


def test_successful_refresh_acknowledges_receipt_and_three_successes_do_not_accumulate(tmp_path: Path) -> None:
    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        command_store = CommandStore(lifecycle=lifecycle)
        store = SegmentStore(tmp_path / "work", tmp_path / "audio")

        class Refresher:
            async def refresh_one(self, key, **kwargs):
                candidate = tmp_path / "audio" / f".{kwargs['commit_identity']}.wav"
                write_silence_wav(candidate, 0.1, 8000)
                kwargs["commit_guard"]()
                result = store.commit_candidate(
                    key=key,
                    title=key,
                    text=key,
                    candidate_path=candidate,
                    duration_s=0.1,
                    refresh_interval_s=900,
                    command_id=kwargs["commit_identity"],
                )
                kwargs["commit_won"](result)
                return result

        service = SegmentApplicationService(
            registry=lambda: DEFAULT_SEGMENT_REGISTRY.resolve(_config()),
            store=store,
            refresher=Refresher(),
            mode=lambda: "normal",
            supervisor=TaskSupervisor(lifecycle),
        )
        for index in range(3):
            record, replayed = await service.accept_refresh(
                key="obs",
                actor="tester",
                idempotency_key=f"receipt-success-{index}",
                command_store=command_store,
            )
            assert not replayed
            await asyncio.sleep(0.05)
            assert (await command_store.get(record.command_id)).status is CommandStatus.SUCCEEDED
            assert store.committed_refresh_receipts() == ()
            assert list((tmp_path / "work").glob(".segment-commit-receipt-*.json")) == []

    asyncio.run(scenario())


def test_receipt_cleanup_failure_is_retryable_without_weakening_command_truth(tmp_path: Path, monkeypatch) -> None:
    store = SegmentStore(tmp_path / "work", tmp_path / "audio")
    candidate = tmp_path / "audio" / ".candidate.wav"
    write_silence_wav(candidate, 0.1, 8000)
    store.commit_candidate(
        key="obs",
        title="Observations",
        text="observations",
        candidate_path=candidate,
        duration_s=0.1,
        refresh_interval_s=900,
        command_id="cmd_receipt_retry",
    )
    receipt_path = store._receipt_path("obs", "cmd_receipt_retry")
    original_unlink = Path.unlink

    def fail_receipt_unlink(path, *args, **kwargs):
        if path == receipt_path:
            raise OSError("injected receipt cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_receipt_unlink)
    assert not store.acknowledge_refresh_command("cmd_receipt_retry", "obs")
    assert len(store.committed_refresh_receipts()) == 1
    monkeypatch.undo()
    assert store.acknowledge_refresh_command("cmd_receipt_retry", "obs")
    assert store.committed_refresh_receipts() == ()


def test_succeeded_receipt_for_superseded_generation_is_repairable_on_reopen(tmp_path: Path) -> None:
    from seasonalweather.database.core import SeasonalDatabase

    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        database = SeasonalDatabase(path=str(tmp_path / "commands.sqlite3"))
        command_store = CommandStore(database=database, lifecycle=lifecycle)
        first, _ = await command_store.create_or_replay(
            command_type="segment.refresh",
            idempotency_key="receipt-superseded-1",
            actor="tester",
            payload={"segment_key": "obs"},
            reason="segment-refresh:obs",
        )
        second, _ = await command_store.create_or_replay(
            command_type="segment.refresh",
            idempotency_key="receipt-superseded-2",
            actor="tester",
            payload={"segment_key": "obs"},
            reason="segment-refresh:obs",
        )
        await command_store.mark_running(first.command_id)
        await command_store.mark_running(second.command_id)
        store = SegmentStore(tmp_path / "work", tmp_path / "audio", database=database)
        for command, text in ((first, "first"), (second, "second")):
            candidate = tmp_path / "audio" / f".{command.command_id}.wav"
            write_silence_wav(candidate, 0.1, 8000)
            store.commit_candidate(
                key="obs",
                title="Observations",
                text=text,
                candidate_path=candidate,
                duration_s=0.1,
                refresh_interval_s=900,
                command_id=command.command_id,
            )
            await command_store.mark_succeeded(
                command.command_id,
                {"code": "segment_refresh_completed", "segment_key": "obs"},
            )

        reopened = SegmentStore(tmp_path / "work", tmp_path / "audio", database=database)
        assert reopened.load() == 1
        assert {item.command_id for item in reopened.committed_refresh_receipts()} == {
            first.command_id,
            second.command_id,
        }
        assert await reopened.reconcile_committed_refresh_commands(command_store) == 2
        assert reopened.committed_refresh_receipts() == ()

    asyncio.run(scenario())


def test_same_key_commits_retain_each_operation_and_encode_dynamic_keys_injectively(
    tmp_path: Path, monkeypatch
) -> None:
    store = SegmentStore(tmp_path / "work", tmp_path / "audio")
    dynamic_keys = ("_alert_a-b", "_alert_a:b", "_alert_a.b", "_alert_ab")
    assert len({store.audio_path_for(key).name for key in dynamic_keys}) == len(dynamic_keys)
    assert len({store._journal_path(key, "background-" + "a" * 32).name for key in dynamic_keys}) == len(dynamic_keys)
    assert len({store._receipt_path(key, "cmd_same_key").name for key in dynamic_keys}) == len(dynamic_keys)

    original_write = store._write_commit_receipt
    attempts = 0

    def fail_first_receipt(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("injected first-operation receipt failure")
        return original_write(**kwargs)

    monkeypatch.setattr(store, "_write_commit_receipt", fail_first_receipt)
    first = "cmd_same_key_one"
    second = "cmd_same_key_two"
    for command_id, text in ((first, "first"), (second, "second")):
        candidate = tmp_path / "audio" / f".{command_id}.wav"
        write_silence_wav(candidate, 0.1, 8000)
        store.commit_candidate(
            key="_alert_a-b",
            title="Alert",
            text=text,
            candidate_path=candidate,
            duration_s=0.1,
            refresh_interval_s=0,
            command_id=command_id,
        )

    journals = store._journal_paths_for_key("_alert_a-b")
    assert len(journals) == 1
    assert first in journals[0].read_text(encoding="utf-8")
    reopened = SegmentStore(tmp_path / "work", tmp_path / "audio")
    assert reopened.load() == 1
    assert {item.command_id for item in reopened.committed_refresh_receipts()} == {first, second}


def test_post_commit_success_terminalization_failure_does_not_mark_refresh_failed(tmp_path: Path, monkeypatch) -> None:
    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        command_store = CommandStore(lifecycle=lifecycle)
        store = SegmentStore(tmp_path / "work", tmp_path / "audio")

        class Refresher:
            async def refresh_one(self, key, **kwargs):
                candidate = tmp_path / "audio" / ".terminalization.wav"
                write_silence_wav(candidate, 0.1, 8000)
                kwargs["commit_guard"]()
                result = store.commit_candidate(
                    key=key,
                    title=key,
                    text="committed",
                    candidate_path=candidate,
                    duration_s=0.1,
                    refresh_interval_s=900,
                    command_id=kwargs["commit_identity"],
                )
                kwargs["commit_won"](result)
                return result

        service = SegmentApplicationService(
            registry=lambda: DEFAULT_SEGMENT_REGISTRY.resolve(_config()),
            store=store,
            refresher=Refresher(),
            mode=lambda: "normal",
        )
        command, _ = await command_store.create_or_replay(
            command_type="segment.refresh",
            idempotency_key="terminalization-failure",
            actor="tester",
            payload={"segment_key": "obs"},
            reason="segment-refresh:obs",
        )
        original_mark_succeeded = command_store.mark_succeeded

        async def fail_mark_succeeded(*_args, **_kwargs):
            raise OSError("injected durable success failure")

        monkeypatch.setattr(command_store, "mark_succeeded", fail_mark_succeeded)
        await service._run_refresh(command_store, command.command_id, "obs")
        assert (await command_store.get(command.command_id)).status is CommandStatus.SUCCEEDED
        assert store.committed_refresh_receipts() == ()

        monkeypatch.setattr(command_store, "mark_succeeded", original_mark_succeeded)
        assert (await command_store.get(command.command_id)).status is CommandStatus.SUCCEEDED

    asyncio.run(scenario())


def test_late_cancellation_after_publication_repairs_to_success(tmp_path: Path, monkeypatch) -> None:
    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        command_store = CommandStore(lifecycle=lifecycle)
        store = SegmentStore(tmp_path / "work", tmp_path / "audio")
        terminalization_started = asyncio.Event()
        release_terminalization = asyncio.Event()

        class Refresher:
            async def refresh_one(self, key, **kwargs):
                candidate = tmp_path / "audio" / ".late-cancel.wav"
                write_silence_wav(candidate, 0.1, 8000)
                kwargs["commit_guard"]()
                result = store.commit_candidate(
                    key=key,
                    title=key,
                    text="committed",
                    candidate_path=candidate,
                    duration_s=0.1,
                    refresh_interval_s=900,
                    command_id=kwargs["commit_identity"],
                )
                kwargs["commit_won"](result)
                return result

        async def fail_after_cancellation(*_args, **_kwargs):
            terminalization_started.set()
            await release_terminalization.wait()
            raise OSError("injected terminalization failure")

        monkeypatch.setattr(command_store, "mark_succeeded", fail_after_cancellation)
        service = SegmentApplicationService(
            registry=lambda: DEFAULT_SEGMENT_REGISTRY.resolve(_config()),
            store=store,
            refresher=Refresher(),
            mode=lambda: "normal",
        )
        command, _ = await command_store.create_or_replay(
            command_type="segment.refresh",
            idempotency_key="late-cancel-repair",
            actor="tester",
            payload={"segment_key": "obs"},
            reason="segment-refresh:obs",
        )
        task = asyncio.create_task(service._run_refresh(command_store, command.command_id, "obs"))
        await asyncio.wait_for(terminalization_started.wait(), timeout=1)
        await command_store.request_cancellation(command.command_id)
        release_terminalization.set()
        await task
        assert (await command_store.get(command.command_id)).status is CommandStatus.SUCCEEDED
        assert store.committed_refresh_receipts() == ()

    asyncio.run(scenario())


def test_persistent_post_commit_terminalization_failure_leaves_recovery_evidence(tmp_path: Path, monkeypatch) -> None:
    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        command_store = CommandStore(lifecycle=lifecycle)
        original_persist = command_store._persist_record

        def fail_success_persist(record, **kwargs):
            if record.status is CommandStatus.SUCCEEDED:
                raise OSError("persistent terminalization outage")
            return original_persist(record, **kwargs)

        monkeypatch.setattr(command_store, "_persist_record", fail_success_persist)
        store = SegmentStore(tmp_path / "work", tmp_path / "audio")

        class Refresher:
            async def refresh_one(self, key, **kwargs):
                candidate = tmp_path / "audio" / ".persistent-terminalization.wav"
                write_silence_wav(candidate, 0.1, 8000)
                kwargs["commit_guard"]()
                result = store.commit_candidate(
                    key=key,
                    title=key,
                    text="committed",
                    candidate_path=candidate,
                    duration_s=0.1,
                    refresh_interval_s=900,
                    command_id=kwargs["commit_identity"],
                )
                kwargs["commit_won"](result)
                return result

        service = SegmentApplicationService(
            registry=lambda: DEFAULT_SEGMENT_REGISTRY.resolve(_config()),
            store=store,
            refresher=Refresher(),
            mode=lambda: "normal",
        )
        command, _ = await command_store.create_or_replay(
            command_type="segment.refresh",
            idempotency_key="persistent-terminalization-failure",
            actor="tester",
            payload={"segment_key": "obs"},
            reason="segment-refresh:obs",
        )
        task = asyncio.create_task(service._run_refresh(command_store, command.command_id, "obs"))
        await task
        assert task.done()
        assert (await command_store.get(command.command_id)).status is CommandStatus.RUNNING
        assert len(store.committed_refresh_receipts()) == 1
        assert store.get("obs").text == "committed"

    asyncio.run(scenario())


def test_receipts_for_missing_wrong_cancelled_and_failed_commands_never_rewrite_success(tmp_path: Path) -> None:
    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        command_store = CommandStore(lifecycle=lifecycle)
        store = SegmentStore(tmp_path / "work", tmp_path / "audio")

        async def commit(key: str, command_id: str | None, *, command_type: str = "segment.refresh") -> object:
            record, _ = await command_store.create_or_replay(
                command_type=command_type,
                idempotency_key=f"receipt-state-{key}",
                actor="tester",
                payload={"segment_key": key},
                reason=f"segment-refresh:{key}",
            )
            await command_store.mark_running(record.command_id)
            candidate = tmp_path / "audio" / f".{key}.wav"
            write_silence_wav(candidate, 0.1, 8000)
            store.commit_candidate(
                key=key,
                title=key,
                text=key,
                candidate_path=candidate,
                duration_s=0.1,
                refresh_interval_s=900,
                command_id=command_id or record.command_id,
            )
            return record

        missing_candidate = tmp_path / "audio" / ".missing.wav"
        write_silence_wav(missing_candidate, 0.1, 8000)
        store.commit_candidate(
            key="obs",
            title="obs",
            text="obs",
            candidate_path=missing_candidate,
            duration_s=0.1,
            refresh_interval_s=900,
            command_id="cmd_missing",
        )
        wrong = await commit("hwo", None, command_type="inserts.cancel")
        cancelled = await commit("spc", None)
        failed = await commit("zfp", None)
        await command_store.request_cancellation(cancelled.command_id)
        await command_store.mark_cancelled(cancelled.command_id)
        await command_store.mark_failed(failed.command_id, {"code": "failed", "message": "failed"})

        assert await store.reconcile_committed_refresh_commands(command_store) == 0
        assert (await command_store.get(wrong.command_id)).status is CommandStatus.RUNNING
        assert (await command_store.get(cancelled.command_id)).status is CommandStatus.CANCELLED
        assert (await command_store.get(failed.command_id)).status is CommandStatus.FAILED

    asyncio.run(scenario())


def test_receipt_write_failure_keeps_committed_truth_and_repairs_after_reopen(tmp_path: Path, monkeypatch) -> None:
    from seasonalweather.database.core import SeasonalDatabase

    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        database = SeasonalDatabase(path=str(tmp_path / "commands.sqlite3"))
        command_store = CommandStore(database=database, lifecycle=lifecycle)
        command, _ = await command_store.create_or_replay(
            command_type="segment.refresh",
            idempotency_key="receipt-write-failure",
            actor="tester",
            payload={"segment_key": "obs"},
            reason="segment-refresh:obs",
        )
        await command_store.mark_running(command.command_id)
        store = SegmentStore(tmp_path / "work", tmp_path / "audio", database=database)
        candidate = tmp_path / "audio" / ".candidate.wav"
        write_silence_wav(candidate, 0.1, 8000)

        def fail_receipt(**_kwargs):
            raise OSError("injected receipt fsync failure")

        monkeypatch.setattr(store, "_write_commit_receipt", fail_receipt)
        result = store.commit_candidate(
            key="obs",
            title="Observations",
            text="committed despite receipt failure",
            candidate_path=candidate,
            duration_s=0.1,
            refresh_interval_s=900,
            command_id=command.command_id,
        )
        assert result.committed
        assert store.get("obs").text == "committed despite receipt failure"
        with database.connect() as conn:
            assert (
                conn.execute(
                    "SELECT 1 FROM segment_commit_journals WHERE segment_key = ? AND command_id = ?",
                    ("obs", command.command_id),
                ).fetchone()
                is not None
            )
        assert (await command_store.get(command.command_id)).status is CommandStatus.RUNNING

        reopened = SegmentStore(tmp_path / "work", tmp_path / "audio", database=database)
        assert reopened.load() == 1
        assert len(reopened.committed_refresh_receipts()) == 1
        assert await reopened.reconcile_committed_refresh_commands(command_store) == 1
        assert (await command_store.get(command.command_id)).status is CommandStatus.SUCCEEDED
        assert reopened.committed_refresh_receipts() == ()
        with database.connect() as conn:
            assert (
                conn.execute(
                    "SELECT 1 FROM segment_commit_journals WHERE segment_key = ? AND command_id = ?",
                    ("obs", command.command_id),
                ).fetchone()
                is None
            )

    asyncio.run(scenario())


def test_receipt_write_failure_does_not_fail_the_refresh_command(tmp_path: Path, monkeypatch) -> None:
    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        command_store = CommandStore(lifecycle=lifecycle)
        registry = DEFAULT_SEGMENT_REGISTRY.resolve(_config())
        store = SegmentStore(tmp_path / "work", tmp_path / "audio")
        original_write = store._write_commit_receipt
        attempts = 0

        def fail_once(**kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("injected receipt write failure")
            return original_write(**kwargs)

        monkeypatch.setattr(store, "_write_commit_receipt", fail_once)

        class Refresher:
            async def refresh_one(self, key, **kwargs):
                candidate = tmp_path / "audio" / ".candidate.wav"
                write_silence_wav(candidate, 0.1, 8000)
                kwargs["commit_guard"]()
                result = store.commit_candidate(
                    key=key,
                    title=key,
                    text="committed",
                    candidate_path=candidate,
                    duration_s=0.1,
                    refresh_interval_s=900,
                    command_id=kwargs["commit_identity"],
                )
                kwargs["commit_won"](result)

        service = SegmentApplicationService(
            registry=lambda: registry,
            store=store,
            refresher=Refresher(),
            mode=lambda: "normal",
            supervisor=TaskSupervisor(lifecycle),
        )
        record, _ = await service.accept_refresh(
            key="obs",
            actor="tester",
            idempotency_key="receipt-command-truth",
            command_store=command_store,
        )
        await asyncio.sleep(0.05)
        assert (await command_store.get(record.command_id)).status is CommandStatus.SUCCEEDED
        assert store.get("obs").text == "committed"
        assert store.committed_refresh_receipts() == ()

    asyncio.run(scenario())


def test_refresh_admission_failure_terminalizes_durable_command(tmp_path: Path) -> None:
    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        command_store = CommandStore(lifecycle=lifecycle)
        registry = DEFAULT_SEGMENT_REGISTRY.resolve(_config())
        store = SegmentStore(tmp_path / "work", tmp_path / "audio")

        class RejectingSupervisor:
            def __init__(self) -> None:
                self.lifecycle = lifecycle

            def create_task(self, coroutine, **_kwargs):
                coroutine.close()
                raise RuntimeError("injected task admission failure")

        service = SegmentApplicationService(
            registry=lambda: registry,
            store=store,
            refresher=SimpleNamespace(),
            mode=lambda: "normal",
            supervisor=RejectingSupervisor(),  # type: ignore[arg-type]
        )
        with pytest.raises(RuntimeError, match="task admission failure"):
            await service.accept_refresh(
                key="obs",
                actor="tester",
                idempotency_key="admission-failure",
                command_store=command_store,
            )
        command = await command_store.list_nonterminal(CommandType.SEGMENT_REFRESH)
        assert command == ()

    asyncio.run(scenario())


def test_restart_reconciles_orphaned_accepted_refresh_and_replay_is_terminal(tmp_path: Path) -> None:
    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        database = SeasonalDatabase(path=str(tmp_path / "commands.sqlite3"))
        original_store = CommandStore(database=database, lifecycle=lifecycle)
        command, replayed = await original_store.create_or_replay(
            command_type="segment.refresh",
            idempotency_key="orphaned-accepted",
            actor="tester",
            payload={"segment_key": "obs"},
            reason="segment-refresh:obs",
        )
        assert not replayed

        store = SegmentStore(tmp_path / "work", tmp_path / "audio", database=database)
        store.load()
        restarted_store = CommandStore(database=database, lifecycle=lifecycle)
        service = SegmentApplicationService(
            registry=lambda: DEFAULT_SEGMENT_REGISTRY.resolve(_config()),
            store=store,
            refresher=SimpleNamespace(),
            mode=lambda: "normal",
            supervisor=TaskSupervisor(lifecycle),
        )
        assert await service.reconcile_orphaned_refreshes(restarted_store) == 1
        assert (await restarted_store.get(command.command_id)).status is CommandStatus.CANCELLED
        replay, replayed = await service.accept_refresh(
            key="obs",
            actor="tester",
            idempotency_key="orphaned-accepted",
            command_store=restarted_store,
        )
        assert replayed
        assert replay.status is CommandStatus.CANCELLED

    asyncio.run(scenario())


def test_supervisor_shutdown_terminalizes_running_prepublication_refresh(tmp_path: Path) -> None:
    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        command_store = CommandStore(lifecycle=lifecycle)
        store = SegmentStore(tmp_path / "work", tmp_path / "audio")
        unrelated, _ = await command_store.create_or_replay(
            command_type="segment.refresh",
            idempotency_key="unrelated-prepublication-evidence",
            actor="tester",
            payload={"segment_key": "fcst"},
            reason="segment-refresh:fcst",
        )
        store._work_dir.mkdir(parents=True, exist_ok=True)
        store._receipt_path("fcst", unrelated.command_id).write_text("{not-json", encoding="utf-8")
        started = asyncio.Event()
        release = asyncio.Event()

        class Refresher:
            async def refresh_one(self, _key, **_kwargs):
                started.set()
                await release.wait()

        supervisor = TaskSupervisor(lifecycle)
        service = SegmentApplicationService(
            registry=lambda: DEFAULT_SEGMENT_REGISTRY.resolve(_config()),
            store=store,
            refresher=Refresher(),
            mode=lambda: "normal",
            supervisor=supervisor,
        )
        command, _ = await service.accept_refresh(
            key="obs",
            actor="tester",
            idempotency_key="shutdown-running",
            command_store=command_store,
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        lifecycle.request_shutdown()
        await supervisor.stop()
        assert (await command_store.get(command.command_id)).status is CommandStatus.CANCELLED
        assert (await command_store.get(unrelated.command_id)).status is CommandStatus.ACCEPTED

    asyncio.run(scenario())


def test_supervisor_cancellation_defers_to_durable_publication_evidence(tmp_path: Path) -> None:
    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        command_store = CommandStore(lifecycle=lifecycle)
        store = SegmentStore(tmp_path / "work", tmp_path / "audio")
        started = asyncio.Event()

        class Refresher:
            async def refresh_one(self, key, **kwargs):
                candidate = tmp_path / "audio" / ".cancel-after-commit.wav"
                write_silence_wav(candidate, 0.1, 8000)
                kwargs["commit_guard"]()
                store.commit_candidate(
                    key=key,
                    title="Observations",
                    text="committed before cancellation callback",
                    candidate_path=candidate,
                    duration_s=0.1,
                    refresh_interval_s=900,
                    command_id=kwargs["commit_identity"],
                )
                started.set()
                await asyncio.Event().wait()

        supervisor = TaskSupervisor(lifecycle)
        service = SegmentApplicationService(
            registry=lambda: DEFAULT_SEGMENT_REGISTRY.resolve(_config()),
            store=store,
            refresher=Refresher(),
            mode=lambda: "normal",
            supervisor=supervisor,
        )
        command, _ = await service.accept_refresh(
            key="obs",
            actor="tester",
            idempotency_key="shutdown-after-durable-commit",
            command_store=command_store,
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        lifecycle.request_shutdown()
        await supervisor.stop()
        assert (await command_store.get(command.command_id)).status is CommandStatus.SUCCEEDED
        assert store.committed_refresh_receipts() == ()

    asyncio.run(scenario())


def test_startup_publication_recovery_precedes_orphan_terminalization(tmp_path: Path) -> None:
    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        database = SeasonalDatabase(path=str(tmp_path / "commands.sqlite3"))
        command_store = CommandStore(database=database, lifecycle=lifecycle)
        command, _ = await command_store.create_or_replay(
            command_type="segment.refresh",
            idempotency_key="publication-wins-before-crash",
            actor="tester",
            payload={"segment_key": "obs"},
            reason="segment-refresh:obs",
        )
        store = SegmentStore(tmp_path / "work", tmp_path / "audio", database=database)
        candidate = tmp_path / "audio" / ".publication-wins.wav"
        write_silence_wav(candidate, 0.1, 8000)
        store.commit_candidate(
            key="obs",
            title="Observations",
            text="committed before crash",
            candidate_path=candidate,
            duration_s=0.1,
            refresh_interval_s=900,
            command_id=command.command_id,
        )
        reopened = SegmentStore(tmp_path / "work", tmp_path / "audio", database=database)
        reopened.load()
        restarted_store = CommandStore(database=database, lifecycle=lifecycle)
        assert await reopened.reconcile_committed_refresh_commands(restarted_store) == 1
        service = SegmentApplicationService(
            registry=lambda: DEFAULT_SEGMENT_REGISTRY.resolve(_config()),
            store=reopened,
            refresher=SimpleNamespace(),
            mode=lambda: "normal",
        )
        assert await service.reconcile_orphaned_refreshes(restarted_store) == 0
        assert (await restarted_store.get(command.command_id)).status is CommandStatus.SUCCEEDED

    asyncio.run(scenario())


def test_unresolved_publication_evidence_does_not_false_terminalize_refresh(tmp_path: Path) -> None:
    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        database = SeasonalDatabase(path=str(tmp_path / "commands.sqlite3"))
        command_store = CommandStore(database=database, lifecycle=lifecycle)
        command, _ = await command_store.create_or_replay(
            command_type="segment.refresh",
            idempotency_key="unresolved-publication",
            actor="tester",
            payload={"segment_key": "obs"},
            reason="segment-refresh:obs",
        )
        store = SegmentStore(tmp_path / "work", tmp_path / "audio", database=database)
        store._work_dir.mkdir(parents=True, exist_ok=True)
        store._receipt_path("obs", command.command_id).write_text("{not-json", encoding="utf-8")
        store.load()
        service = SegmentApplicationService(
            registry=lambda: DEFAULT_SEGMENT_REGISTRY.resolve(_config()),
            store=store,
            refresher=SimpleNamespace(),
            mode=lambda: "normal",
        )
        assert await service.reconcile_orphaned_refreshes(command_store) == 0
        assert (await command_store.get(command.command_id)).status is CommandStatus.ACCEPTED

    asyncio.run(scenario())


def test_live_refresh_replay_does_not_duplicate_in_process_execution(tmp_path: Path) -> None:
    async def scenario() -> None:
        lifecycle = Lifecycle()
        lifecycle.mark_running()
        command_store = CommandStore(lifecycle=lifecycle)
        store = SegmentStore(tmp_path / "work", tmp_path / "audio")
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        class Refresher:
            async def refresh_one(self, _key, **_kwargs):
                nonlocal calls
                calls += 1
                started.set()
                await release.wait()

        service = SegmentApplicationService(
            registry=lambda: DEFAULT_SEGMENT_REGISTRY.resolve(_config()),
            store=store,
            refresher=Refresher(),
            mode=lambda: "normal",
            supervisor=TaskSupervisor(lifecycle),
        )
        command, _ = await service.accept_refresh(
            key="obs",
            actor="tester",
            idempotency_key="live-replay",
            command_store=command_store,
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        replay, replayed = await service.accept_refresh(
            key="obs",
            actor="tester",
            idempotency_key="live-replay",
            command_store=command_store,
        )
        assert replayed
        assert replay.command_id == command.command_id
        assert calls == 1
        release.set()
        await asyncio.sleep(0.05)

    asyncio.run(scenario())


def test_stale_lkg_preview_matches_conductor_airability_and_placeholders_do_not(tmp_path: Path) -> None:
    from seasonalweather.broadcast.conductor import CycleConductor

    registry = DEFAULT_SEGMENT_REGISTRY.resolve(_config())
    store = SegmentStore(tmp_path / "work", tmp_path / "audio")
    audio = store.audio_path_for("obs")
    audio.parent.mkdir(parents=True, exist_ok=True)
    write_silence_wav(audio, 0.1, 8000)
    asyncio.run(store.update("obs", "Observations", "old", audio, 0.1, 1, 1))
    store.get("obs").last_updated_ts = time.time() - 1000
    pushed: list[str] = []
    conductor = CycleConductor(
        store=store,
        telnet=SimpleNamespace(push_cycle=lambda path, meta: pushed.append(path)),
        tts=SimpleNamespace(),
        alert_tracker=SimpleNamespace(get_cycle_alerts=lambda: ()),
        tz=ZoneInfo("UTC"),
        audio_dir=tmp_path / "audio",
        sample_rate=8000,
        np_meta_fn=lambda **_kwargs: {},
        registry=registry,
        mode_fn=lambda: "normal",
    )
    service = SegmentApplicationService(
        registry=lambda: registry,
        store=store,
        refresher=SimpleNamespace(),
        mode=lambda: "normal",
        runtime_snapshot=lambda: {
            "_registry": registry,
            "mode": "normal",
            "focus": False,
            "runtime_items": ({"key": "obs", "kind": "static"},),
        },
    )
    preview = service.cycle_preview()
    item = preview["segments"][0]
    assert item["freshness"] == "stale"
    assert item["selected"] and item["eligible_to_air"]
    assert conductor._push_cached("obs") > 0.0
    assert pushed

    store.get("obs").is_placeholder = True
    placeholder = service.cycle_preview()["segments"][0]
    assert not placeholder["selected"] and not placeholder["eligible_to_air"]
    store.get("obs").is_placeholder = False
    audio.unlink()
    missing = service.cycle_preview()["segments"][0]
    assert not missing["selected"] and not missing["eligible_to_air"]


def test_preview_uses_runtime_registry_generation_and_fails_closed_on_unknown_order_key(tmp_path: Path) -> None:
    old_registry = DEFAULT_SEGMENT_REGISTRY.resolve(_config())
    new_config = _config()
    new_config.spc = SimpleNamespace(enabled=False)
    new_registry = DEFAULT_SEGMENT_REGISTRY.resolve(new_config)
    store = SegmentStore(tmp_path / "work", tmp_path / "audio")
    audio = store.audio_path_for("spc")
    audio.parent.mkdir(parents=True, exist_ok=True)
    write_silence_wav(audio, 0.1, 8000)
    asyncio.run(store.update("spc", old_registry.title_for("spc"), "spc", audio, 0.1, 900, 1800))

    service = SegmentApplicationService(
        registry=lambda: old_registry,
        store=store,
        refresher=SimpleNamespace(),
        mode=lambda: "normal",
        runtime_snapshot=lambda: {
            "_registry": new_registry,
            "mode": "normal",
            "focus": False,
            "runtime_items": (
                {"key": "spc", "kind": "static"},
                {"key": "removed-by-generation", "kind": "static"},
            ),
        },
    )
    preview = service.cycle_preview()
    spc = next(item for item in preview["segments"] if item["key"] == "spc")
    assert not spc["selected"] and not spc["eligible_to_air"]
    assert "_registry" not in preview
    assert "removed-by-generation" not in preview["order"]


def _orphan_service(store: SegmentStore) -> SegmentApplicationService:
    return SegmentApplicationService(
        registry=lambda: DEFAULT_SEGMENT_REGISTRY.resolve(_config()),
        store=store,
        refresher=SimpleNamespace(),
        mode=lambda: "normal",
    )


async def _create_refresh_commands(command_store: CommandStore, keys: list[str]) -> list[CommandRecord]:
    commands: list[CommandRecord] = []
    for index, key in enumerate(keys):
        command, _ = await command_store.create_or_replay(
            command_type="segment.refresh",
            idempotency_key=f"attempt11-{index}-{key}",
            actor="tester",
            payload={"segment_key": key},
            reason=f"segment-refresh:{key}",
        )
        commands.append(command)
    return commands


def _write_durable_receipt(store: SegmentStore, key: str, command_id: str) -> None:
    target = store._versioned_audio_path(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    write_silence_wav(target, 0.1, 8000)
    store._write_commit_receipt(key=key, target=target, command_id=command_id)


async def _seed_file_orphan_store(tmp_path: Path, ambiguity: str) -> SegmentStore:
    seed = SegmentStore(tmp_path / "work", tmp_path / "audio")
    target = seed.audio_path_for("obs")
    target.parent.mkdir(parents=True, exist_ok=True)
    write_silence_wav(target, 0.1, 8000)
    await seed.update("obs", "Observations", "ready", target, 0.1, 900, 1800)
    metadata_path = tmp_path / "work" / "segment_store.json"
    raw = json.loads(metadata_path.read_text(encoding="utf-8"))
    if ambiguity == "unrelated":
        raw["entries"].append(
            {"key": "fcst", "audio_path": str(tmp_path / "audio" / "cycle_seg_fcst.wav"), "max_age_s": "bad"}
        )
    elif ambiguity == "same_key":
        raw["entries"].append({"key": "obs", "audio_path": str(target), "max_age_s": "bad"})
    elif ambiguity == "unknown":
        raw["entries"].append({"audio_path": str(target), "max_age_s": "bad"})
    else:
        raw["entries"].append({"key": "fcst", "audio_path": str(target), "max_age_s": "bad"})
    metadata_path.write_text(json.dumps(raw), encoding="utf-8")
    restarted = SegmentStore(tmp_path / "work", tmp_path / "audio")
    assert restarted.load() == (1 if ambiguity == "unrelated" else 0)
    return restarted


@pytest.mark.parametrize(
    "ambiguity, expected",
    [("unrelated", "cancelled"), ("same_key", "accepted"), ("unknown", "accepted"), ("alias", "accepted")],
)
def test_file_restart_orphan_reconciliation_is_exact_key_safe(tmp_path: Path, ambiguity: str, expected: str) -> None:
    async def scenario() -> None:
        command_store = CommandStore()
        command = (await _create_refresh_commands(command_store, ["obs"]))[0]
        store = await _seed_file_orphan_store(tmp_path, ambiguity)
        outcome = await store.reconcile_committed_refresh_command(command_store, command.command_id, "obs")
        if expected == "cancelled":
            assert outcome is RefreshReconciliationOutcome.PUBLICATION_NOT_PROVEN
            assert await _orphan_service(store).reconcile_orphaned_refreshes(command_store) == 1
            assert (await command_store.get(command.command_id)).status is CommandStatus.CANCELLED
            assert (
                await store.reconcile_committed_refresh_command(command_store, command.command_id, "obs")
            ) is RefreshReconciliationOutcome.PUBLICATION_NOT_PROVEN
            assert await _orphan_service(store).reconcile_orphaned_refreshes(command_store) == 0
        else:
            assert outcome is RefreshReconciliationOutcome.STILL_UNRESOLVED
            assert await _orphan_service(store).reconcile_orphaned_refreshes(command_store) == 0
            assert (await command_store.get(command.command_id)).status is CommandStatus.ACCEPTED
            assert (
                await store.reconcile_committed_refresh_command(command_store, command.command_id, "obs")
            ) is RefreshReconciliationOutcome.STILL_UNRESOLVED

    asyncio.run(scenario())


def test_attempt11_a_exact_command_and_key_scope_does_not_cross_keys(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = SeasonalDatabase(path=str(tmp_path / "commands.sqlite3"))
        command_store = CommandStore(database=database)
        commands = await _create_refresh_commands(command_store, ["obs", "fcst"])
        store = SegmentStore(tmp_path / "work", tmp_path / "audio", database=database)
        store._work_dir.mkdir(parents=True, exist_ok=True)
        store._receipt_path("fcst", commands[1].command_id).write_text("{not-json", encoding="utf-8")
        store.load()

        assert await _orphan_service(store).reconcile_orphaned_refreshes(command_store) == 1
        assert (await command_store.get(commands[0].command_id)).status is CommandStatus.CANCELLED
        assert (await command_store.get(commands[1].command_id)).status is CommandStatus.ACCEPTED

    asyncio.run(scenario())


def test_attempt11_b_same_key_other_command_evidence_does_not_block(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = SeasonalDatabase(path=str(tmp_path / "commands.sqlite3"))
        command_store = CommandStore(database=database)
        commands = await _create_refresh_commands(command_store, ["obs", "obs"])
        store = SegmentStore(tmp_path / "work", tmp_path / "audio", database=database)
        store._work_dir.mkdir(parents=True, exist_ok=True)
        store._receipt_path("obs", commands[1].command_id).write_text("{not-json", encoding="utf-8")
        store.load()

        assert await _orphan_service(store).reconcile_orphaned_refreshes(command_store) == 1
        assert (await command_store.get(commands[0].command_id)).status is CommandStatus.CANCELLED
        assert (await command_store.get(commands[1].command_id)).status is CommandStatus.ACCEPTED

    asyncio.run(scenario())


def test_attempt11_c_exact_malformed_receipt_remains_nonterminal(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = SeasonalDatabase(path=str(tmp_path / "commands.sqlite3"))
        command_store = CommandStore(database=database)
        commands = await _create_refresh_commands(command_store, ["obs", "fcst"])
        store = SegmentStore(tmp_path / "work", tmp_path / "audio", database=database)
        store._work_dir.mkdir(parents=True, exist_ok=True)
        store._receipt_path("obs", commands[0].command_id).write_text("{not-json", encoding="utf-8")
        store.load()

        assert await _orphan_service(store).reconcile_orphaned_refreshes(command_store) == 1
        assert (await command_store.get(commands[0].command_id)).status is CommandStatus.ACCEPTED
        assert (await command_store.get(commands[1].command_id)).status is CommandStatus.CANCELLED

    asyncio.run(scenario())


def test_attempt11_d_exact_committed_receipt_repairs_success(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = SeasonalDatabase(path=str(tmp_path / "commands.sqlite3"))
        command_store = CommandStore(database=database)
        command = (await _create_refresh_commands(command_store, ["obs"]))[0]
        store = SegmentStore(tmp_path / "work", tmp_path / "audio", database=database)
        _write_durable_receipt(store, "obs", command.command_id)
        store.load()
        restarted_store = CommandStore(database=database)

        assert await store.reconcile_committed_refresh_command(
            restarted_store,
            command.command_id,
            "obs",
        )
        assert (await restarted_store.get(command.command_id)).status is CommandStatus.SUCCEEDED

    asyncio.run(scenario())


def test_attempt11_f_startup_reconciles_all_durable_orphans_across_pages(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = SeasonalDatabase(path=str(tmp_path / "commands.sqlite3"))
        command_store = CommandStore(database=database)
        commands = await _create_refresh_commands(command_store, ["obs"] * 257)
        service = _orphan_service(SegmentStore(tmp_path / "work", tmp_path / "audio", database=database))

        assert await service.reconcile_orphaned_refreshes(command_store) == 257
        for command in commands:
            assert (await command_store.get(command.command_id)).status is CommandStatus.CANCELLED

    asyncio.run(scenario())


def test_attempt11_g_unresolved_first_page_does_not_starve_later_cleanup(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = SeasonalDatabase(path=str(tmp_path / "commands.sqlite3"))
        command_store = CommandStore(database=database)
        commands = await _create_refresh_commands(command_store, ["obs"] * 257)
        store = SegmentStore(tmp_path / "work", tmp_path / "audio", database=database)
        store._work_dir.mkdir(parents=True, exist_ok=True)
        store._receipt_path("obs", commands[0].command_id).write_text("{not-json", encoding="utf-8")
        store.load()

        assert await _orphan_service(store).reconcile_orphaned_refreshes(command_store) == 256
        assert (await command_store.get(commands[0].command_id)).status is CommandStatus.ACCEPTED
        assert (await command_store.get(commands[-1].command_id)).status is CommandStatus.CANCELLED

    asyncio.run(scenario())


def test_attempt11_h_accepted_and_running_commands_are_reconciled_across_pages(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = SeasonalDatabase(path=str(tmp_path / "commands.sqlite3"))
        command_store = CommandStore(database=database)
        commands = await _create_refresh_commands(command_store, ["obs"] * 257)
        await command_store.mark_running(commands[0].command_id)
        await command_store.mark_running(commands[-1].command_id)

        assert (
            await _orphan_service(
                SegmentStore(tmp_path / "work", tmp_path / "audio", database=database)
            ).reconcile_orphaned_refreshes(command_store)
            == 257
        )
        for command in commands:
            assert (await command_store.get(command.command_id)).status is CommandStatus.CANCELLED

    asyncio.run(scenario())


def test_attempt11_i_terminal_and_non_segment_commands_are_untouched(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = SeasonalDatabase(path=str(tmp_path / "commands.sqlite3"))
        command_store = CommandStore(database=database)
        terminal, _ = await command_store.create_or_replay(
            command_type="segment.refresh",
            idempotency_key="attempt11-terminal",
            actor="tester",
            payload={"segment_key": "obs"},
            reason="segment-refresh:obs",
        )
        await command_store.mark_running(terminal.command_id)
        await command_store.mark_succeeded(terminal.command_id, {"code": "already-done"})
        non_segment, _ = await command_store.create_or_replay(
            command_type="cycle.rebuild",
            idempotency_key="attempt11-non-segment",
            actor="tester",
            payload={},
        )

        assert (
            await _orphan_service(
                SegmentStore(tmp_path / "work", tmp_path / "audio", database=database)
            ).reconcile_orphaned_refreshes(command_store)
            == 0
        )
        assert (await command_store.get(terminal.command_id)).status is CommandStatus.SUCCEEDED
        assert (await command_store.get(non_segment.command_id)).status is CommandStatus.ACCEPTED

    asyncio.run(scenario())


def test_attempt11_j_committed_recovery_wins_independent_of_orphan_page(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = SeasonalDatabase(path=str(tmp_path / "commands.sqlite3"))
        command_store = CommandStore(database=database)
        commands = await _create_refresh_commands(command_store, ["obs"] * 257)
        store = SegmentStore(tmp_path / "work", tmp_path / "audio", database=database)
        _write_durable_receipt(store, "obs", commands[-1].command_id)
        store.load()
        restarted_store = CommandStore(database=database)

        assert await store.reconcile_committed_refresh_commands(restarted_store) == 1
        assert (await restarted_store.get(commands[-1].command_id)).status is CommandStatus.SUCCEEDED
        assert await _orphan_service(store).reconcile_orphaned_refreshes(restarted_store) == 256
        for command in commands[:-1]:
            assert (await restarted_store.get(command.command_id)).status is CommandStatus.CANCELLED

    asyncio.run(scenario())
