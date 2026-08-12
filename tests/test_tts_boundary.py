from __future__ import annotations

import asyncio
import datetime as dt
import shutil
import sys
import textwrap
import threading
from types import SimpleNamespace
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from seasonalweather.tts.admission import validate_synthesis_request, transitional_local_qualification
from seasonalweather.tts.admission import (
    ControllerLocalPublicationFence,
    ControllerLocalQualificationSource,
    P109TtsQualificationAdapter,
)
from seasonalweather.tts.async_bridge import EmbeddedExecutionPort, synthesize_async, synthesize_completed_wav_async
from seasonalweather.tts.audio import write_silence_wav
from seasonalweather.tts.local import (
    DecTalkHandler,
    FestivalHandler,
    LocalEngineHandler,
    LocalEngineRegistry,
    LocalHandlerResult,
    PiperHandler,
    EspeakHandler,
    VoiceTextPaulHandler,
)
from seasonalweather.tts.local import LocalCapabilityEvidence
from seasonalweather.tts.models import (
    AcceptedArtifactReference,
    ArtifactEvidence,
    BackendId,
    LastKnownGoodCandidate,
    LocalEngineOptions,
    SynthesisDisposition,
    SynthesisFailure,
    SynthesisOutputPolicy,
    SynthesisPurpose,
    SynthesisRequest,
)
from seasonalweather.tts.policy import policy_for
from seasonalweather.tts.service import SynthesisService
from seasonalweather.tts.subprocess import ProcessFailure, run_bounded
from seasonalweather.tts.tts import TTS, TTSCompatibilityError


def request(**updates) -> SynthesisRequest:
    values = {
        "purpose": SynthesisPurpose.ROUTINE,
        "backend": BackendId.LOCAL,
        "text": "A bounded forecast message.",
        "deadline_at": dt.datetime.now(dt.UTC) + dt.timedelta(seconds=10),
        "configuration_generation": 4,
    }
    values.update(updates)
    return SynthesisRequest(**values)


def transitional_service(**kwargs) -> SynthesisService:
    """Tests that model the pre-registry controller must opt in explicitly."""

    kwargs.setdefault("capability_check", transitional_local_qualification)
    return SynthesisService(**kwargs)


def test_bare_synthesis_service_fails_closed_without_p109_qualification(tmp_path: Path) -> None:
    result = SynthesisService().synthesize(request(), tmp_path / "unqualified.wav")
    assert result.failure is SynthesisFailure.CAPABILITY_REJECTED


def test_transitional_qualification_is_explicit_test_behavior(monkeypatch, tmp_path: Path) -> None:
    del tmp_path
    monkeypatch.setattr("seasonalweather.tts.service._resource_available", lambda _resource: True)
    monkeypatch.setattr("seasonalweather.tts.service.resolve_trusted_executable", lambda _name: "ffmpeg")
    monkeypatch.setattr("seasonalweather.tts.service.shutil.which", lambda _name: "/fake/ffmpeg")
    assert transitional_service().availability(request()) == (True, "tts_available")


def _production_config(tmp_path: Path, monkeypatch):
    from seasonalweather.config import load_config

    monkeypatch.setenv("ICECAST_SOURCE_PASSWORD", "test-source")
    monkeypatch.setenv("NWWS_JID", "changeme@nwws-oi.weather.gov")
    monkeypatch.setenv("NWWS_PASSWORD", "CHANGEME")
    cfg = load_config("config/config.yaml")
    return replace(
        cfg,
        paths=replace(
            cfg.paths,
            work_dir=str(tmp_path / "work"),
            audio_dir=str(tmp_path / "audio"),
            cache_dir=str(tmp_path / "cache"),
            config_dir=str(tmp_path / "config"),
            log_dir=str(tmp_path / "log"),
        ),
        database=replace(cfg.database, enabled=False),
        station_feed=replace(cfg.station_feed, enabled=False),
    )


def _install_fake_controller_tts(
    monkeypatch, *, release: threading.Event | None = None, started: threading.Event | None = None
):
    from seasonalweather.artifacts.media import WavPolicy, inspect_wav

    class FakeHandler(LocalEngineHandler):
        engine_id = "espeak-ng"

        def synthesize(self, text, *, options, output_dir, deadline, cancellation, volume=1.0):
            del text, options, deadline, cancellation, volume
            if started is not None:
                started.set()
            if release is not None:
                assert release.wait(5)
            output = output_dir / "engine.wav"
            write_silence_wav(output, 0.1, 48_000)
            return LocalHandlerResult(output, self.engine_id)

    params = {
        "format": "wav",
        "profiles": "espeak-ng",
        "voices": "9",
        "sample_rates": 48_000,
        "max_input_bytes": 65_536,
    }
    monkeypatch.setattr(LocalEngineRegistry, "handler", classmethod(lambda cls, _engine: FakeHandler()))
    monkeypatch.setattr(
        LocalEngineRegistry,
        "qualification_evidence",
        classmethod(lambda cls, _engine, _options: LocalCapabilityEvidence(True, "healthy", True, 1, 1, params)),
    )
    monkeypatch.setattr("seasonalweather.tts.service.shutil.which", lambda name: f"/fake/{name}")
    monkeypatch.setattr(
        SynthesisService,
        "_normalize_local_audio",
        lambda self, source, request, raw_dir, deadline, cancellation: (
            source,
            inspect_wav(source, policy=WavPolicy(maximum_duration_seconds=request.output.maximum_duration_seconds)),
        ),
    )


def test_request_is_immutable_and_deterministically_serialized() -> None:
    deadline = __import__("datetime").datetime.now(dt.UTC) + dt.timedelta(seconds=10)
    first = request(text="seven hundred PM", deadline_at=deadline)
    second = request(text="seven hundred PM", deadline_at=deadline)
    assert first.canonical_json() == second.canonical_json()
    assert first.content_identity == second.content_identity
    with pytest.raises((AttributeError, TypeError, ValueError)):
        first.text = "changed"  # type: ignore[misc]
    with pytest.raises(ValueError):
        SynthesisRequest.model_validate({**first.model_dump(), "unknown": True})


def test_facade_captures_live_generation_once_per_request() -> None:
    generation = [4]
    facade = TTS(
        backend="local",
        local_engine="espeak-ng",
        voice="9",
        rate_wpm=165,
        volume=1.0,
        sample_rate=48_000,
        configuration_generation=4,
        generation_provider=lambda: generation[0],
    )
    first = facade._request("first")
    generation[0] = 5
    second = facade._request("second")
    assert first.configuration_generation == 4
    assert second.configuration_generation == 5


def test_result_purpose_policy_maps_to_existing_job_semantics() -> None:
    alert = policy_for(SynthesisPurpose.ALERT)
    routine = policy_for(SynthesisPurpose.ROUTINE)
    optional = policy_for(SynthesisPurpose.OPTIONAL)
    administrative = policy_for(SynthesisPurpose.ADMINISTRATIVE)
    assert alert.priority.value < routine.priority.value
    assert alert.max_attempts == 1 and alert.replay_policy.value != "idempotent_all_fences"
    assert optional.suppress_on_failure and not optional.fallback_allowed
    assert administrative.priority is routine.priority


@pytest.mark.parametrize("engine", ("espeak-ng", "piper", "festival", "dectalk", "voicetext_paul"))
def test_every_supported_local_engine_has_one_registry_handler(engine: str) -> None:
    assert isinstance(LocalEngineRegistry.handler(engine), LocalEngineHandler)


def test_local_engine_aliases_are_canonical() -> None:
    assert LocalEngineRegistry.normalize("espeak") == "espeak-ng"
    assert LocalEngineRegistry.normalize("espeak_ng") == "espeak-ng"
    with pytest.raises(ProcessFailure, match="unsupported"):
        LocalEngineRegistry.normalize("not-an-engine")


@pytest.mark.parametrize(
    ("handler_type", "voice"),
    [
        (EspeakHandler, "9"),
        (PiperHandler, "en_US-lessac-medium"),
        (FestivalHandler, "kal_diphone"),
        (DecTalkHandler, "2"),
    ],
)
def test_local_handlers_construct_bounded_commands_with_fake_executables(
    handler_type, voice: str, monkeypatch, tmp_path: Path
) -> None:
    executable = tmp_path / "fake-engine"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.setattr("seasonalweather.tts.local.resolve_trusted_executable", lambda _name: str(executable))
    if handler_type is DecTalkHandler:
        say = tmp_path / "say"
        say.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        say.chmod(0o700)
        monkeypatch.setattr(DecTalkHandler, "say_path", say)

    commands: list[list[str]] = []
    handler = handler_type()

    def fake_run(argv, *, input_bytes, deadline, cancellation, cwd=None):
        del input_bytes, deadline, cancellation, cwd
        commands.append(argv)
        output = next(Path(argv[index + 1]) for index, item in enumerate(argv) if item in {"-w", "-f", "-o", "-fo"})
        write_silence_wav(output, 0.1, 22050)

    monkeypatch.setattr(handler, "_run", fake_run)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    result = handler.synthesize(
        "bounded test",
        options=LocalEngineOptions(engine=handler.engine_id, voice=voice),
        output_dir=output_dir,
        deadline=__import__("time").monotonic() + 10,
        cancellation=None,
    )
    assert result.output_path.is_file()
    assert commands and commands[0][0] == str(executable)
    if handler_type is EspeakHandler:
        assert commands[0][1:] == ["-v", voice, "-s", "165", "-w", str(result.output_path), "-f", "-"]
    elif handler_type is PiperHandler:
        assert commands[0][1:] == ["-m", voice, "-f", str(result.output_path), "-r", "48000"]
    elif handler_type is FestivalHandler:
        assert commands[0][1] == "-eval" and "Duration_Stretch" in commands[0][2]
    else:
        assert commands[0][1:] == [
            str(DecTalkHandler.say_path),
            "-l",
            "us",
            "-s",
            "2",
            "-r",
            "165",
            "-v",
            "100",
            "-e",
            "1",
            "-fo",
            str(result.output_path),
            "-c",
            "-",
        ]


def test_voicetext_paul_retries_after_reset_with_fake_resources(monkeypatch, tmp_path: Path) -> None:
    state_base = tmp_path / "state"
    engine_dir = tmp_path / "engine"
    engine_dir.mkdir()
    (engine_dir / "voicetext_paul.exe").write_bytes(b"fake")
    wrapper = tmp_path / "wrapper"
    wrapper.write_bytes(b"fake")
    reset = tmp_path / "reset"
    reset.write_bytes(b"fake")
    monkeypatch.setenv("SEASONALWEATHER_DATA_BASE", str(state_base))
    monkeypatch.setenv("VOICETEXT_PAUL_BIN_DIR", str(engine_dir))
    monkeypatch.setattr(VoiceTextPaulHandler, "wrapper_path", wrapper)
    monkeypatch.setattr(VoiceTextPaulHandler, "reset_path", reset)
    monkeypatch.setattr("seasonalweather.tts.local.resolve_trusted_executable", lambda _name: "/fake/sudo")

    attempts = 0
    source = engine_dir / "output.wav"
    handler = VoiceTextPaulHandler()

    def fake_run(argv, *, input_bytes, deadline, cancellation, cwd=None):
        nonlocal attempts
        del argv, input_bytes, deadline, cancellation, cwd
        attempts += 1
        if attempts == 1:
            raise ProcessFailure("nonzero_exit", "fake wrapper failed")
        write_silence_wav(source, 0.2, 22050)

    reset_calls = 0

    def fake_reset(argv, **kwargs):
        nonlocal reset_calls
        del argv, kwargs
        reset_calls += 1

    monkeypatch.setattr(handler, "_run", fake_run)
    monkeypatch.setattr("seasonalweather.tts.local.run_bounded", fake_reset)
    (tmp_path / "out").mkdir()
    options = LocalEngineOptions(
        engine="voicetext_paul",
        voice="9",
        voicetext_paul={"retries": 1, "retry_sleep_ms": 0},
    )
    result = handler.synthesize(
        "bounded test",
        options=options,
        output_dir=tmp_path / "out",
        deadline=__import__("time").monotonic() + 10,
        cancellation=None,
    )
    assert result.output_path.is_file()
    assert attempts == 2
    assert reset_calls == 1


def test_voicetext_paul_preserves_primary_failure_when_reset_fails(monkeypatch, tmp_path: Path) -> None:
    state_base = tmp_path / "state"
    engine_dir = tmp_path / "engine"
    engine_dir.mkdir()
    (engine_dir / "voicetext_paul.exe").write_bytes(b"fake")
    wrapper = tmp_path / "wrapper"
    wrapper.write_bytes(b"fake")
    reset = tmp_path / "reset"
    reset.write_bytes(b"fake")
    monkeypatch.setenv("SEASONALWEATHER_DATA_BASE", str(state_base))
    monkeypatch.setenv("VOICETEXT_PAUL_BIN_DIR", str(engine_dir))
    monkeypatch.setattr(VoiceTextPaulHandler, "wrapper_path", wrapper)
    monkeypatch.setattr(VoiceTextPaulHandler, "reset_path", reset)
    monkeypatch.setattr("seasonalweather.tts.local.resolve_trusted_executable", lambda _name: "/fake/sudo")
    handler = VoiceTextPaulHandler()
    monkeypatch.setattr(
        handler,
        "_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(ProcessFailure("nonzero_exit", "primary")),
    )
    monkeypatch.setattr(
        "seasonalweather.tts.local.run_bounded",
        lambda *args, **kwargs: (_ for _ in ()).throw(ProcessFailure("reset_failed", "secondary")),
    )
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    with pytest.raises(ProcessFailure) as error:
        handler.synthesize(
            "bounded test",
            options=LocalEngineOptions(engine="voicetext_paul", voicetext_paul={"retries": 0}),
            output_dir=output_dir,
            deadline=__import__("time").monotonic() + 10,
            cancellation=None,
        )
    assert error.value.classification == "nonzero_exit"
    assert getattr(error.value, "secondary_evidence") == "reset:reset_failed"


def test_voicetext_reset_every_requires_reset_resource_and_counter_is_instance_scoped(
    monkeypatch, tmp_path: Path
) -> None:
    state_base = tmp_path / "state"
    engine_dir = tmp_path / "engine"
    engine_dir.mkdir()
    (engine_dir / "voicetext_paul.exe").write_bytes(b"fake")
    wrapper = tmp_path / "wrapper"
    wrapper.write_bytes(b"fake")
    reset = tmp_path / "reset"
    reset.write_bytes(b"fake")
    monkeypatch.setenv("SEASONALWEATHER_DATA_BASE", str(state_base))
    monkeypatch.setenv("VOICETEXT_PAUL_BIN_DIR", str(engine_dir))
    monkeypatch.setattr(VoiceTextPaulHandler, "wrapper_path", wrapper)
    monkeypatch.setattr(VoiceTextPaulHandler, "reset_path", reset)
    monkeypatch.setattr("seasonalweather.tts.service._resource_available", lambda resource: True)
    monkeypatch.setattr("seasonalweather.tts.service.shutil.which", lambda _name: "/fake/ffmpeg")
    assert transitional_service().availability(
        request(local=LocalEngineOptions(engine="voicetext_paul", voicetext_paul={"reset_every": 2}))
    )[0]
    reset.unlink()
    assert not transitional_service().availability(
        request(local=LocalEngineOptions(engine="voicetext_paul", voicetext_paul={"reset_every": 2}))
    )[0]

    first = VoiceTextPaulHandler()
    second = VoiceTextPaulHandler()
    assert first._invocations is not second._invocations


def test_facade_preserves_voicetext_reset_every_across_serialized_calls(monkeypatch, tmp_path: Path) -> None:
    state_base = tmp_path / "state"
    engine_dir = tmp_path / "engine"
    engine_dir.mkdir()
    (engine_dir / "voicetext_paul.exe").write_bytes(b"fake")
    wrapper = tmp_path / "wrapper"
    wrapper.write_bytes(b"fake")
    reset = tmp_path / "reset"
    reset.write_bytes(b"fake")
    monkeypatch.setenv("SEASONALWEATHER_DATA_BASE", str(state_base))
    monkeypatch.setenv("VOICETEXT_PAUL_BIN_DIR", str(engine_dir))
    monkeypatch.setattr(VoiceTextPaulHandler, "wrapper_path", wrapper)
    monkeypatch.setattr(VoiceTextPaulHandler, "reset_path", reset)
    monkeypatch.setattr("seasonalweather.tts.local.resolve_trusted_executable", lambda _name: "/fake/sudo")
    reset_calls = 0

    def fake_run(argv, *, input_bytes, deadline, cancellation, cwd=None):
        nonlocal reset_calls
        del input_bytes, deadline, cancellation
        if str(reset) in argv:
            reset_calls += 1
        else:
            write_silence_wav(engine_dir / "output.wav", 0.2, 48_000)

    monkeypatch.setattr("seasonalweather.tts.local.run_bounded", fake_run)
    monkeypatch.setattr(
        SynthesisService,
        "_normalize_local_audio",
        lambda self, source, request, raw_dir, deadline, cancellation: (
            source,
            __import__("seasonalweather.artifacts.media", fromlist=["inspect_wav"]).inspect_wav(source),
        ),
    )
    facade = TTS(
        backend="local",
        local_engine="voicetext_paul",
        voice="9",
        rate_wpm=165,
        volume=1.0,
        sample_rate=48_000,
        vtp_cfg=SimpleNamespace(retries=0, reset_every=2, retry_sleep_ms=0, kill_before=False, vtml_lexicon=False),
        allow_transitional_qualification=True,
    )
    facade.synth_to_wav("first", tmp_path / "first.wav")
    facade.synth_to_wav("second", tmp_path / "second.wav")
    facade.synth_to_wav("third", tmp_path / "third.wav")
    assert reset_calls == 1
    assert facade._service() is facade._service()


@pytest.mark.parametrize("engine", LocalEngineRegistry.supported_engines())
def test_remote_fallback_preserves_explicit_local_engine_profile(engine: str, monkeypatch, tmp_path: Path) -> None:
    captured: list[tuple[str, str, int]] = []
    facade = TTS(
        backend="seasonal_ttsd",
        fallback_backend="local",
        local_engine=engine,
        voice=("2" if engine == "dectalk" else "kal_diphone" if engine == "festival" else "custom-voice"),
        rate_wpm=193,
        volume=1.0,
        sample_rate=44_100,
        capability_check=lambda _request, _capability: SimpleNamespace(disposition="satisfied", effective_capacity=1),
    )

    def fake_local(req, output, selected_engine, deadline, cancellation):
        del deadline, cancellation
        captured.append((selected_engine, req.local.voice, req.local.rate_wpm))
        write_silence_wav(output, 0.1, req.output.sample_rate_hz)
        from seasonalweather.artifacts.hashing import hash_file
        from seasonalweather.artifacts.media import inspect_wav
        from seasonalweather.tts.service import _artifact_evidence

        identity = hash_file(output, maximum_bytes=req.output.maximum_bytes)
        return _artifact_evidence(identity.sha256, identity.size_bytes, inspect_wav(output))

    monkeypatch.setattr(facade._service(), "_run_local", fake_local)
    result = facade.synthesize("fallback", tmp_path / f"{engine}.wav")
    assert result.failure is None
    assert captured == [(engine, facade.voice, facade.rate_wpm)]


def test_tts_admission_uses_typed_tts_path() -> None:
    rejection = validate_synthesis_request(request(output=SynthesisOutputPolicy(format="mp3")))
    assert rejection is not None
    assert rejection.issue.path.kind.value == "tts"
    assert rejection.issue.path.to_pointer() == "/output/format"


@pytest.mark.parametrize(
    ("decision", "reason"),
    [
        (SimpleNamespace(disposition="unavailable", effective_capacity=0), "capability_unavailable"),
        (SimpleNamespace(disposition="incompatible", effective_capacity=0), "capability_unavailable"),
        (SimpleNamespace(disposition="stale_or_unknown", effective_capacity=0), "capability_unavailable"),
        (SimpleNamespace(disposition="no_capacity", effective_capacity=0), "capability_unavailable"),
        (SimpleNamespace(disposition="satisfied", effective_capacity=1), None),
        (SimpleNamespace(disposition="degraded", effective_capacity=1), None),
    ],
)
def test_p1_14_tts_admission_translates_current_p1_09_evidence(decision, reason) -> None:
    rejection = validate_synthesis_request(request(), qualification=decision)
    assert (rejection.reason_code if rejection else None) == reason


def test_p1_14_tts_admission_reports_fallback_and_deadline() -> None:
    fallback = validate_synthesis_request(
        request(backend=BackendId.SEASONAL_TTSD, fallback_backend=BackendId.LOCAL),
        fallback_viability=False,
    )
    assert fallback is not None and fallback.reason_code == "fallback_unavailable"
    expired = validate_synthesis_request(request(deadline_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)))
    assert expired is not None and expired.reason_code == "invalid_deadline"


def test_unbound_production_tts_does_not_promote_local_resources_to_p1_09_health(tmp_path: Path) -> None:
    facade = TTS(backend="espeak-ng", voice="9", rate_wpm=165, volume=1.0, sample_rate=48_000)
    result = facade.synthesize("bounded", tmp_path / "unbound.wav")
    assert result.failure is SynthesisFailure.CAPABILITY_REJECTED


def test_compatibility_facade_raises_on_non_output_without_rendering_source_text(monkeypatch, tmp_path: Path) -> None:
    facade = TTS(backend="espeak-ng", voice="9", rate_wpm=165, volume=1.0, sample_rate=48_000)
    failed = SimpleNamespace(disposition=SynthesisDisposition.FAILED, failure=SynthesisFailure.PROCESS_FAILED)
    monkeypatch.setattr(facade, "synthesize", lambda *args, **kwargs: failed)
    with pytest.raises(TTSCompatibilityError) as error:
        facade.synth_to_wav("secret source text", tmp_path / "missing.wav", purpose="alert")
    assert "secret source text" not in str(error.value)
    assert "process_failed" in str(error.value)

    output = tmp_path / "success.wav"
    output.write_bytes(b"wav")
    succeeded = SimpleNamespace(disposition=SynthesisDisposition.SUCCEEDED, failure=None)
    monkeypatch.setattr(facade, "synthesize", lambda *args, **kwargs: succeeded)
    facade.synth_to_wav("safe", output, purpose="routine")


def test_remote_backend_fails_without_network_and_explicit_local_fallback_works(monkeypatch, tmp_path: Path) -> None:
    service = transitional_service()
    calls: list[str] = []

    def fake_local(req, output, engine, deadline, cancellation):
        calls.append(engine)
        write_silence_wav(output, 0.1, req.output.sample_rate_hz)
        from seasonalweather.artifacts.hashing import hash_file
        from seasonalweather.artifacts.media import inspect_wav
        from seasonalweather.tts.service import _artifact_evidence

        identity = hash_file(output, maximum_bytes=req.output.maximum_bytes)
        return _artifact_evidence(identity.sha256, identity.size_bytes, inspect_wav(output))

    monkeypatch.setattr(service, "_run_local", fake_local)
    result = service.synthesize(
        request(backend=BackendId.SEASONAL_TTSD, fallback_backend=BackendId.LOCAL),
        tmp_path / "fallback.wav",
    )
    assert result.disposition is SynthesisDisposition.SUCCEEDED
    assert result.fallback is not None and result.fallback.succeeded
    assert calls == ["espeak-ng"]

    failed = service.synthesize(request(backend=BackendId.SEASONAL_TTSD), tmp_path / "remote.wav")
    assert failed.failure is SynthesisFailure.BACKEND_UNAVAILABLE


def test_capability_gate_accepts_degraded_and_rejects_unknown(tmp_path: Path) -> None:
    allowed = SynthesisService(
        capability_check=lambda _request, _engine: type("Decision", (), {"disposition": "degraded"})()
    )
    rejected = SynthesisService(capability_check=lambda _request, _engine: False)
    assert (
        allowed.synthesize(request(backend=BackendId.SEASONAL_TTSD), tmp_path / "a.wav").failure
        is SynthesisFailure.BACKEND_UNAVAILABLE
    )
    assert rejected.synthesize(request(), tmp_path / "b.wav").failure is SynthesisFailure.CAPABILITY_REJECTED


def test_past_deadline_and_pre_spawn_cancellation_do_not_run_engine(monkeypatch, tmp_path: Path) -> None:
    called = False
    service = transitional_service()

    def should_not_run(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("engine started")

    monkeypatch.setattr(service, "_run_local", should_not_run)
    past = request(deadline_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1))
    assert service.synthesize(past, tmp_path / "past.wav").disposition is SynthesisDisposition.TIMED_OUT
    import threading

    cancelled = threading.Event()
    cancelled.set()
    assert (
        service.synthesize(request(), tmp_path / "cancelled.wav", cancellation=cancelled).disposition
        is SynthesisDisposition.CANCELLED
    )
    assert not called


class _FakeHandler(LocalEngineHandler):
    engine_id = "espeak-ng"

    def synthesize(self, text, *, options, output_dir, deadline, cancellation):
        output = output_dir / "engine.wav"
        write_silence_wav(output, 0.1, 22050)
        return LocalHandlerResult(output, self.engine_id)


def test_common_finalization_validates_and_atomically_replaces_output(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(LocalEngineRegistry, "handler", classmethod(lambda cls, engine: _FakeHandler()))

    def fake_ffmpeg(argv, **kwargs):
        source = Path(argv[argv.index("-i") + 1])
        target = Path(argv[-1])
        shutil.copyfile(source, target)

    monkeypatch.setattr("seasonalweather.tts.service.resolve_trusted_executable", lambda _name: "ffmpeg")
    monkeypatch.setattr("seasonalweather.tts.service.run_bounded", fake_ffmpeg)
    output = tmp_path / "result.wav"
    result = transitional_service().synthesize(request(), output)
    assert result.disposition is SynthesisDisposition.SUCCEEDED
    assert result.artifact is not None and output.is_file()
    assert result.artifact.channels == 2


def test_invalid_wav_never_becomes_completed_output(monkeypatch, tmp_path: Path) -> None:
    class BadHandler(_FakeHandler):
        def synthesize(self, text, *, options, output_dir, deadline, cancellation):
            output = output_dir / "engine.wav"
            output.write_bytes(b"not wav")
            return LocalHandlerResult(output, self.engine_id)

    monkeypatch.setattr(LocalEngineRegistry, "handler", classmethod(lambda cls, engine: BadHandler()))
    monkeypatch.setattr("seasonalweather.tts.service.resolve_trusted_executable", lambda _name: "ffmpeg")
    monkeypatch.setattr("seasonalweather.tts.service.run_bounded", lambda argv, **kwargs: None)
    output = tmp_path / "result.wav"
    result = transitional_service().synthesize(request(), output)
    assert result.failure is SynthesisFailure.OUTPUT_INVALID
    assert not output.exists()


def test_exact_fenced_last_known_good_reuse_and_mismatch_rejection(tmp_path: Path) -> None:
    candidate_path = tmp_path / "known.wav"
    write_silence_wav(candidate_path, 0.1, 48000)
    original = request()
    candidate = LastKnownGoodCandidate(
        path=str(candidate_path),
        content_identity=original.content_identity,
        purpose=original.purpose,
        backend=original.backend,
        preprocessing_version="tts-preprocess-v1",
        configuration_generation=original.configuration_generation,
        validated=True,
    )
    # A path plus caller metadata is no longer trusted evidence. Reuse
    # requires a controller-owned P1-10 resolver.
    rejected = transitional_service().synthesize(original, tmp_path / "reuse.wav", last_known_good=candidate)
    assert rejected.failure is SynthesisFailure.LKG_REJECTED
    mismatch = transitional_service().synthesize(
        request(text="different"), tmp_path / "mismatch.wav", last_known_good=candidate
    )
    assert mismatch.failure is not None


def test_controller_owned_lkg_resolver_reuses_only_exact_profile_and_evidence(tmp_path: Path) -> None:
    from seasonalweather.artifacts.hashing import hash_file
    from seasonalweather.artifacts.media import inspect_wav
    from seasonalweather.tts.service import _artifact_evidence

    source = tmp_path / "controller-owned.wav"
    write_silence_wav(source, 0.1, 48_000)
    original = request(source_identity="source-a", event_identity="event-a", segment_identity="segment-a")
    service = transitional_service()
    digest = hash_file(source, maximum_bytes=original.output.maximum_bytes)
    media = inspect_wav(source)
    evidence = _artifact_evidence(digest.sha256, digest.size_bytes, media)
    accepted = AcceptedArtifactReference(
        artifact_ref="artifact:tts:lkg-1",
        path=str(source),
        content_identity=original.content_identity,
        purpose=original.purpose,
        backend=original.backend,
        preprocessing_version=original.preprocessing_version,
        configuration_generation=original.configuration_generation,
        source_identity=original.source_identity,
        event_identity=original.event_identity,
        segment_identity=original.segment_identity,
        output_profile_identity=service._output_profile(original),
        artifact=evidence,
        freshness_deadline_at=original.deadline_at + dt.timedelta(seconds=30),
    )
    legacy = LastKnownGoodCandidate(
        path=str(source),
        content_identity=original.content_identity,
        purpose=original.purpose,
        backend=original.backend,
        preprocessing_version=original.preprocessing_version,
        configuration_generation=original.configuration_generation,
        source_identity=original.source_identity,
        event_identity=original.event_identity,
        validated=True,
    )
    resolver = lambda _request: accepted

    result = transitional_service(lkg_resolver=resolver).synthesize(
        original, tmp_path / "reused.wav", last_known_good=legacy
    )
    assert result.disposition is SynthesisDisposition.LKG_REUSED
    assert result.artifact == evidence

    for changed in (
        {"local": original.local.model_copy(update={"engine": "piper"})},
        {"local": original.local.model_copy(update={"voice": "different"})},
        {"local": original.local.model_copy(update={"rate_wpm": 210})},
        {"source_identity": "source-b"},
        {"event_identity": "event-b"},
        {"segment_identity": "segment-b"},
        {"configuration_generation": 5},
    ):
        altered = original.model_copy(update=changed)
        output = tmp_path / f"reject-{len(list(tmp_path.glob('reject-*.wav')))}.wav"
        rejected = transitional_service(lkg_resolver=resolver).synthesize(altered, output, last_known_good=legacy)
        assert rejected.disposition is not SynthesisDisposition.LKG_REUSED
        assert not output.exists()

    stale = accepted.model_copy(update={"freshness_deadline_at": dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)})
    stale_result = transitional_service(lkg_resolver=lambda _request: stale).synthesize(
        original, tmp_path / "stale.wav", last_known_good=legacy
    )
    assert stale_result.failure is SynthesisFailure.LKG_REJECTED
    assert not (tmp_path / "stale.wav").exists()

    corrupt = tmp_path / "corrupt.wav"
    corrupt.write_bytes(b"not wav")
    corrupt_ref = accepted.model_copy(update={"path": str(corrupt)})
    corrupt_result = transitional_service(lkg_resolver=lambda _request: corrupt_ref).synthesize(
        original, tmp_path / "corrupt-out.wav", last_known_good=legacy
    )
    assert corrupt_result.failure is SynthesisFailure.LKG_REJECTED
    assert not (tmp_path / "corrupt-out.wav").exists()


def test_shared_activity_context_is_released_on_failure(monkeypatch, tmp_path: Path) -> None:
    active = 0

    @contextmanager
    def activity():
        nonlocal active
        active += 1
        try:
            yield
        finally:
            active -= 1

    service = transitional_service(activity_context=activity)
    result = service.synthesize(request(backend=BackendId.SEASONAL_TTSD), tmp_path / "no.wav")
    assert result.failure is SynthesisFailure.BACKEND_UNAVAILABLE
    assert active == 0


def test_subprocess_uses_deadline_and_reaps_timeout(tmp_path: Path) -> None:
    script = tmp_path / "sleep.py"
    script.write_text(
        textwrap.dedent("""
        import time
        time.sleep(10)
    """),
        encoding="utf-8",
    )
    with pytest.raises(ProcessFailure) as error:
        run_bounded([sys.executable, str(script)], input_bytes=None, deadline=__import__("time").monotonic() + 0.1)
    assert error.value.classification == "timed_out"


def test_content_identity_is_controller_derived_and_preprocessed() -> None:
    first = request(text="A 700 PM update.")
    second = request(text="A 7:00 PM update.")
    assert first.content_identity == second.content_identity
    with pytest.raises(ValueError, match="content_identity"):
        request(content_identity="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")


@pytest.mark.parametrize(
    ("disposition", "allowed"),
    [
        ("satisfied", True),
        ("degraded", True),
        ("unavailable", False),
        ("incompatible", False),
        ("stale_or_unknown", False),
        ("no_capacity", False),
    ],
)
def test_p1_09_qualification_states_are_admitted_fail_closed(
    disposition: str, allowed: bool, monkeypatch, tmp_path: Path
) -> None:
    calls: list[str] = []

    def capability(_request, capability):
        calls.append(capability)
        return SimpleNamespace(disposition=disposition, effective_capacity=1 if allowed else 0)

    service = SynthesisService(capability_check=capability)
    monkeypatch.setattr(
        service,
        "_run_local",
        lambda *args: __import__("seasonalweather.tts.service", fromlist=["ArtifactEvidence"]).ArtifactEvidence(
            sha256="sha256:" + "a" * 64,
            size_bytes=1,
            media_type="audio/wav",
            sample_rate_hz=48000,
            channels=2,
            frame_count=1,
            duration_seconds=1 / 48000,
        ),
    )
    result = service.synthesize(request(), tmp_path / "state.wav")
    assert (result.failure is None) is allowed
    assert calls and set(calls) == {LocalEngineRegistry.capability_for("espeak-ng")}


def test_qualification_is_rechecked_before_result_acceptance(monkeypatch, tmp_path: Path) -> None:
    states = iter(("satisfied",))
    output = tmp_path / "race.wav"

    def capability(_request, _capability):
        state = next(states)
        return SimpleNamespace(disposition=state, effective_capacity=1 if state == "satisfied" else 0)

    service = SynthesisService(capability_check=capability)

    def fake_local(*args):
        return __import__("seasonalweather.tts.service", fromlist=["ArtifactEvidence"]).ArtifactEvidence(
            sha256="sha256:" + "b" * 64,
            size_bytes=1,
            media_type="audio/wav",
            sample_rate_hz=48000,
            channels=2,
            frame_count=1,
            duration_seconds=1 / 48000,
        )

    monkeypatch.setattr(service, "_run_local", fake_local)
    result = service.synthesize(request(), output)
    # The final fence owns the last qualification decision.  There is no
    # failure-producing post-replace check that can invalidate a completed
    # atomic result.
    assert result.failure is None
    assert next(states, None) is None
    assert not output.exists()


def test_fallback_viability_is_explicit(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []

    def capability(_request, capability):
        calls.append(capability)
        return SimpleNamespace(disposition="unavailable", effective_capacity=0)

    service = SynthesisService(capability_check=capability)
    result = service.synthesize(
        request(backend=BackendId.SEASONAL_TTSD, fallback_backend=BackendId.LOCAL),
        tmp_path / "fallback-no-capacity.wav",
    )
    assert result.failure is SynthesisFailure.BACKEND_UNAVAILABLE
    assert result.fallback is not None and not result.fallback.succeeded
    assert calls == [LocalEngineRegistry.capability_for("espeak-ng")]


def test_regex_overrides_are_bounded_and_ordered() -> None:
    from seasonalweather.tts.preprocess import preprocess_text
    from seasonalweather.tts.models import TextOverride

    ordered = preprocess_text(
        "NWS 700 PM",
        (
            TextOverride(match="NWS", replace="National Weather Service"),
            TextOverride(match="National Weather Service", replace="NWS"),
        ),
    )
    assert ordered == "Nws 7:00 PM"
    with pytest.raises(ValueError, match="unsafe|overlong"):
        preprocess_text("a" * 65_000, (TextOverride(match="(?=a)", replace="x", regex=True),))


@pytest.mark.parametrize("pattern", ("(a+)+$", "(?:a|aa)+$", "(?:a?)*$"))
def test_regex_pathological_repetition_is_rejected_before_catastrophic_work(pattern: str) -> None:
    from seasonalweather.tts.preprocess import preprocess_text
    from seasonalweather.tts.models import TextOverride

    with pytest.raises(ValueError, match="unsafe|ambiguous|nested"):
        preprocess_text(
            "a" * 65_000,
            (TextOverride(match=pattern, replace="x", regex=True),),
        )


@pytest.mark.parametrize(
    "pattern",
    (
        "a*a*a*a*a*b",
        "a+a+a+a+a+b",
        "a{1,}a{1,}c",
        "a{0,256}a{0,256}a{0,256}a{0,256}a{0,256}b",
        "[ab]{0,256}[bc]{0,256}",
        "a{0,256}a+",
        "(?:a{0,256}){0,256}b",
        "(?:ab){0,256}(?:bc){0,256}",
    ),
)
def test_regex_sequential_unbounded_repetition_is_rejected_before_compile(pattern: str, monkeypatch) -> None:
    from seasonalweather.tts import preprocess
    from seasonalweather.tts.models import TextOverride

    compiled = False

    def fail_compile(*args, **kwargs):
        nonlocal compiled
        compiled = True
        raise AssertionError("unsafe regex reached the regex engine")

    monkeypatch.setattr(preprocess.re, "compile", fail_compile)
    with pytest.raises(ValueError) as error:
        preprocess.preprocess_text(
            "a" * 65_536,
            (TextOverride(match=pattern, replace="x", regex=True),),
        )
    assert any(word in str(error.value) for word in ("unbounded", "competing", "nested"))
    assert not compiled


def test_exact_capability_does_not_accept_unrelated_worker() -> None:
    from seasonalweather.capabilities.manifest import CapabilityManifest
    from seasonalweather.capabilities.models import (
        CapabilityRecord,
        CompatibilityState,
        OperationalState,
    )
    from seasonalweather.capabilities.registry import CapabilityRegistry
    from seasonalweather.jobs.policies import JobType
    from seasonalweather.jobs.registry import policy_for as job_policy_for

    now = dt.datetime.now(dt.UTC)
    policy = job_policy_for(JobType.TTS_SYNTHESIZE)
    unrelated = CapabilityRecord(
        name="tts.synthesis.v1",
        implemented=True,
        compatibility=CompatibilityState.UNKNOWN,
        operational_state=OperationalState.HEALTHY,
        accepting_new_jobs=True,
        total_capacity=1,
        reported_available=1,
        parameters={"format": "wav"},
        validity_seconds=60,
        observed_at=now,
        published_at=now,
    )
    exact = LocalEngineRegistry.capability_for("espeak-ng")
    registry = CapabilityRegistry(allowed_capabilities=frozenset({unrelated.name, exact}))
    registry.register(
        worker_id="unrelated-worker",
        worker_instance_id="instance",
        session_id="session",
        manifest=CapabilityManifest.create(epoch=1, records=(unrelated,)),
        authorized_capabilities=frozenset({unrelated.name}),
        authorized_job_types=frozenset({JobType.TTS_SYNTHESIZE}),
        payload_versions={JobType.TTS_SYNTHESIZE: policy.payload_schema_version},
        result_versions={JobType.TTS_SYNTHESIZE: policy.result_schema_version},
        now=now,
    )
    decision = P109TtsQualificationAdapter(registry, lambda: now)(request(), exact)
    assert decision.disposition.value != "satisfied"
    assert decision.effective_capacity == 0


@pytest.mark.parametrize(
    ("operational_state", "accepting", "capacity", "expected"),
    (
        ("unavailable", False, 0, "unavailable"),
        ("healthy", True, 0, "no_capacity"),
        ("unknown", False, 0, "stale_or_unknown"),
        ("degraded", True, 1, "degraded"),
        ("healthy", True, 1, "satisfied"),
    ),
)
def test_controller_local_snapshot_cannot_be_masked_by_healthy_same_capability_worker(
    operational_state: str, accepting: bool, capacity: int, expected: str, monkeypatch
) -> None:
    from seasonalweather.capabilities.manifest import CapabilityManifest
    from seasonalweather.capabilities.models import CapabilityRecord, CompatibilityState, OperationalState
    from seasonalweather.capabilities.registry import CapabilityRegistry
    from seasonalweather.jobs.policies import JobType
    from seasonalweather.jobs.registry import policy_for as job_policy_for

    now = dt.datetime.now(dt.UTC)
    policy = job_policy_for(JobType.TTS_SYNTHESIZE)
    capability = LocalEngineRegistry.capability_for("espeak-ng")
    parameters = {
        "format": "wav",
        "profiles": "espeak-ng",
        "voices": "9",
        "sample_rates": 48_000,
        "max_input_bytes": 65_536,
    }
    registry = CapabilityRegistry(allowed_capabilities=frozenset({capability}))
    worker_record = CapabilityRecord(
        name=capability,
        implemented=True,
        compatibility=CompatibilityState.UNKNOWN,
        operational_state=OperationalState.HEALTHY,
        accepting_new_jobs=True,
        total_capacity=1,
        reported_available=1,
        job_restrictions=(JobType.TTS_SYNTHESIZE.value,),
        parameters=parameters,
        validity_seconds=60,
        observed_at=now,
        published_at=now,
    )
    registry.register(
        worker_id="same-capability-worker",
        worker_instance_id="instance",
        session_id="session",
        manifest=CapabilityManifest.create(epoch=1, records=(worker_record,)),
        authorized_capabilities=frozenset({capability}),
        authorized_job_types=frozenset({JobType.TTS_SYNTHESIZE}),
        payload_versions={JobType.TTS_SYNTHESIZE: policy.payload_schema_version},
        result_versions={JobType.TTS_SYNTHESIZE: policy.result_schema_version},
        now=now,
    )
    source = ControllerLocalQualificationSource(registry, lambda: now)
    from seasonalweather.tts.local import LocalCapabilityEvidence

    evidence = LocalCapabilityEvidence(True, operational_state, accepting, 1, capacity, parameters)
    monkeypatch.setattr(
        LocalEngineRegistry, "qualification_evidence", classmethod(lambda cls, _engine, _options: evidence)
    )
    decision = P109TtsQualificationAdapter(registry, lambda: now, local_source=source)(request(), capability)
    assert decision.disposition.value == expected
    assert decision.effective_capacity == (capacity if expected in {"satisfied", "degraded"} else 0)


def test_controller_local_incompatible_snapshot_cannot_be_masked_by_worker() -> None:
    from seasonalweather.capabilities.manifest import CapabilityManifest
    from seasonalweather.capabilities.models import CapabilityRecord, CompatibilityState, OperationalState
    from seasonalweather.capabilities.registry import CapabilityRegistry
    from seasonalweather.jobs.policies import JobType
    from seasonalweather.jobs.registry import policy_for as job_policy_for

    now = dt.datetime.now(dt.UTC)
    policy = job_policy_for(JobType.TTS_SYNTHESIZE)
    capability = LocalEngineRegistry.capability_for("espeak-ng")
    params = {
        "format": "wav",
        "profiles": "espeak-ng",
        "voices": "9",
        "sample_rates": 48_000,
        "max_input_bytes": 65_536,
    }
    registry = CapabilityRegistry(allowed_capabilities=frozenset({capability}))
    local = registry.publish_controller_local(
        worker_id="controller-local-tts",
        manifest=CapabilityManifest.create(
            epoch=1,
            records=(
                CapabilityRecord(
                    name=capability,
                    implemented=True,
                    compatibility=CompatibilityState.INCOMPATIBLE,
                    operational_state=OperationalState.HEALTHY,
                    accepting_new_jobs=True,
                    total_capacity=1,
                    reported_available=1,
                    job_restrictions=(JobType.TTS_SYNTHESIZE.value,),
                    parameters=params,
                    validity_seconds=60,
                    observed_at=now,
                    published_at=now,
                ),
            ),
        ),
        authorized_capabilities=frozenset({capability}),
        authorized_job_types=frozenset({JobType.TTS_SYNTHESIZE}),
        payload_versions={JobType.TTS_SYNTHESIZE: policy.payload_schema_version},
        result_versions={JobType.TTS_SYNTHESIZE: policy.result_schema_version},
        now=now,
    )
    source = SimpleNamespace(refresh=lambda _engine: local)
    decision = P109TtsQualificationAdapter(registry, lambda: now, local_source=source)(request(), capability)
    assert decision.disposition.value == "incompatible"
    assert decision.effective_capacity == 0


def test_controller_local_source_uses_one_registry_authority(monkeypatch) -> None:
    from seasonalweather.capabilities.registry import CapabilityRegistry

    now = dt.datetime.now(dt.UTC)
    engine = LocalEngineRegistry.capability_for("espeak-ng")
    registry = CapabilityRegistry(allowed_capabilities=frozenset({engine}))
    monkeypatch.setattr(
        LocalEngineRegistry,
        "qualification_evidence",
        classmethod(
            lambda cls, _engine, _options: LocalCapabilityEvidence(
                True,
                "healthy",
                True,
                1,
                1,
                {
                    "format": "wav",
                    "profiles": "espeak-ng",
                    "voices": "9",
                    "sample_rates": 48_000,
                    "max_input_bytes": 65_536,
                },
            )
        ),
    )
    monkeypatch.setattr("seasonalweather.tts.service.shutil.which", lambda name: f"/fake/{name}")
    source = ControllerLocalQualificationSource(registry, lambda: now)
    source.refresh(request(), "espeak-ng")
    assert len(registry.snapshots(now)) == 1
    assert registry.snapshots(now)[0].worker_id == source.worker_id


def test_controller_local_capacity_one_serializes_embedded_reservations(monkeypatch) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from seasonalweather.capabilities.registry import CapabilityRegistry

    now = dt.datetime.now(dt.UTC)
    capability = LocalEngineRegistry.capability_for("espeak-ng")
    registry = CapabilityRegistry(allowed_capabilities=frozenset({capability}))
    evidence = LocalCapabilityEvidence(
        True,
        "healthy",
        True,
        1,
        1,
        {
            "format": "wav",
            "profiles": "espeak-ng",
            "voices": "9",
            "sample_rates": 48_000,
            "max_input_bytes": 65_536,
        },
    )
    monkeypatch.setattr(LocalEngineRegistry, "qualification_evidence", classmethod(lambda cls, _e, _o: evidence))
    source = ControllerLocalQualificationSource(registry, lambda: now)
    adapter = P109TtsQualificationAdapter(registry, lambda: now, local_source=source)
    requests = [request(job_id=f"embedded-{index}") for index in range(2)]

    with ThreadPoolExecutor(max_workers=2) as executor:
        reservations = list(
            executor.map(
                lambda item: adapter.reserve(
                    item,
                    item.job_id or "missing",
                    expires_at=now + dt.timedelta(seconds=10),
                ),
                requests,
            )
        )
    assert sum(reservation is not None for reservation in reservations) == 1
    winner = next(reservation for reservation in reservations if reservation is not None)
    snapshot = registry.snapshot(source.worker_id, now)
    assert snapshot is not None
    assert snapshot.pending_reservations == 1
    assert snapshot.effective_capacity[capability] == 0
    adapter.release(winner)
    adapter.release(winner)
    restored = registry.snapshot(source.worker_id, now)
    assert restored is not None
    assert restored.pending_reservations == 0
    assert restored.effective_capacity[capability] == 1


def test_controller_local_epoch_publication_rejects_stale_and_conflicting_refresh() -> None:
    from seasonalweather.capabilities.manifest import CapabilityManifest
    from seasonalweather.capabilities.models import CapabilityRecord, CompatibilityState, OperationalState
    from seasonalweather.capabilities.registry import CapabilityRegistry
    from seasonalweather.jobs.policies import JobType
    from seasonalweather.jobs.registry import policy_for as job_policy_for

    now = dt.datetime.now(dt.UTC)
    capability = LocalEngineRegistry.capability_for("espeak-ng")
    policy = job_policy_for(JobType.TTS_SYNTHESIZE)
    registry = CapabilityRegistry(allowed_capabilities=frozenset({capability}))
    record = CapabilityRecord(
        name=capability,
        implemented=True,
        compatibility=CompatibilityState.COMPATIBLE,
        operational_state=OperationalState.HEALTHY,
        accepting_new_jobs=True,
        total_capacity=1,
        reported_available=1,
        job_restrictions=(JobType.TTS_SYNTHESIZE.value,),
        parameters={
            "format": "wav",
            "profiles": "espeak-ng",
            "voices": "9",
            "sample_rates": 48_000,
            "max_input_bytes": 65_536,
        },
        validity_seconds=60,
        observed_at=now,
        published_at=now,
    )

    def publish(epoch: int, current_record: CapabilityRecord = record):
        return registry.publish_controller_local(
            worker_id="controller-local-tts",
            manifest=CapabilityManifest.create(epoch=epoch, records=(current_record,)),
            authorized_capabilities=frozenset({capability}),
            authorized_job_types=frozenset({JobType.TTS_SYNTHESIZE}),
            payload_versions={JobType.TTS_SYNTHESIZE: policy.payload_schema_version},
            result_versions={JobType.TTS_SYNTHESIZE: policy.result_schema_version},
            now=now,
        )

    first = publish(1)
    publish(2)
    with pytest.raises(ValueError, match="stale"):
        publish(1)
    conflicting = record.model_copy(update={"parameters": {**record.parameters, "voices": "8"}})
    with pytest.raises(ValueError, match="conflicts"):
        registry.publish_controller_local(
            worker_id="controller-local-tts",
            manifest=CapabilityManifest.create(epoch=2, records=(conflicting,)),
            authorized_capabilities=frozenset({capability}),
            authorized_job_types=frozenset({JobType.TTS_SYNTHESIZE}),
            payload_versions={JobType.TTS_SYNTHESIZE: policy.payload_schema_version},
            result_versions={JobType.TTS_SYNTHESIZE: policy.result_schema_version},
            now=now,
        )
    assert first.epoch == 1


def test_controller_local_profile_is_configuration_owned_not_request_owned(monkeypatch) -> None:
    from seasonalweather.capabilities.registry import CapabilityRegistry

    now = dt.datetime.now(dt.UTC)
    capability = LocalEngineRegistry.capability_for("espeak-ng")
    registry = CapabilityRegistry(allowed_capabilities=frozenset({capability}))
    monkeypatch.setattr("seasonalweather.tts.service.shutil.which", lambda _name: "/fake/ffmpeg")
    configured = LocalEngineOptions(engine="espeak-ng", voice="Y", sample_rate_hz=48_000)
    source = ControllerLocalQualificationSource(registry, lambda: now, configured_options=configured)
    adapter = P109TtsQualificationAdapter(registry, lambda: now, local_source=source)
    decision = adapter(request(local=request().local.model_copy(update={"voice": "X"})), capability)
    assert decision.disposition.value == "incompatible"
    snapshot = registry.snapshots(now)[0]
    record = next(item for item in snapshot.records if item.name == capability)
    assert record.parameters["voices"] == "Y"


def test_old_controller_local_profile_cannot_republish_after_generation_change(monkeypatch) -> None:
    from seasonalweather.capabilities.registry import CapabilityRegistry

    now = dt.datetime.now(dt.UTC)
    capability = LocalEngineRegistry.capability_for("espeak-ng")
    registry = CapabilityRegistry(allowed_capabilities=frozenset({capability}))
    generation = [4]
    monkeypatch.setattr("seasonalweather.tts.service.shutil.which", lambda _name: "/fake/ffmpeg")
    source = ControllerLocalQualificationSource(
        registry,
        lambda: now,
        configured_options=LocalEngineOptions(voice="Y"),
        configuration_generation=4,
        current_generation=lambda: generation[0],
    )
    assert source.refresh("espeak-ng") is not None
    generation[0] = 5
    assert source.refresh("espeak-ng") is None
    assert len(registry.snapshots(now)) == 1


def test_controller_local_source_replacement_fence_blocks_overtaken_publication(monkeypatch) -> None:
    from seasonalweather.capabilities.registry import CapabilityRegistry

    now = dt.datetime.now(dt.UTC)
    capability = LocalEngineRegistry.capability_for("espeak-ng")
    registry = CapabilityRegistry(allowed_capabilities=frozenset({capability}))
    generation = [1]
    publication_fence = ControllerLocalPublicationFence()

    def evidence(cls, _engine, options):
        del cls
        return LocalCapabilityEvidence(
            True,
            "healthy",
            True,
            1,
            1,
            {
                "format": "wav",
                "profiles": "espeak-ng",
                "voices": options.voice,
                "sample_rates": 48_000,
                "max_input_bytes": 65_536,
            },
        )

    monkeypatch.setattr(LocalEngineRegistry, "qualification_evidence", classmethod(evidence))
    old_source = ControllerLocalQualificationSource(
        registry,
        lambda: now,
        configured_options=LocalEngineOptions(voice="OLD"),
        configuration_generation=1,
        current_generation=lambda: generation[0],
        publication_fence=publication_fence,
    )
    new_source = ControllerLocalQualificationSource(
        registry,
        lambda: now,
        configured_options=LocalEngineOptions(voice="NEW"),
        configuration_generation=2,
        current_generation=lambda: generation[0],
        publication_fence=publication_fence,
    )

    entered = threading.Event()
    release = threading.Event()
    original_fence = old_source.publication_fence

    class GatedFence:
        def generation_publication(self, expected, current):
            entered.set()
            assert release.wait(1)
            return original_fence.generation_publication(expected, current)

        def hold(self):
            return original_fence.hold()

    old_source.publication_fence = GatedFence()
    old_result: list[object] = []
    thread = threading.Thread(target=lambda: old_result.append(old_source.refresh("espeak-ng")))
    thread.start()
    assert entered.wait(1)

    generation[0] = 2
    assert new_source.refresh("espeak-ng") is not None
    release.set()
    thread.join(1)
    assert not thread.is_alive()
    assert old_result == [None]
    snapshot = registry.snapshot(old_source.worker_id, now)
    assert snapshot is not None
    assert next(record for record in snapshot.records if record.name == capability).parameters["voices"] == "NEW"


def test_real_p109_reservation_is_reused_by_remote_to_local_fallback(monkeypatch, tmp_path: Path) -> None:
    from seasonalweather.capabilities.registry import CapabilityRegistry

    _install_fake_controller_tts(monkeypatch)
    now = dt.datetime.now(dt.UTC)
    capability = LocalEngineRegistry.capability_for("espeak-ng")
    registry = CapabilityRegistry(allowed_capabilities=frozenset({capability}))
    source = ControllerLocalQualificationSource(
        registry,
        lambda: now,
        configured_options=LocalEngineOptions(engine="espeak-ng", voice="9"),
        configuration_generation=4,
    )
    adapter = P109TtsQualificationAdapter(registry, lambda: now, local_source=source)
    observed_remote_reservations: list[int] = []

    class FailingRemote:
        backend_id = "seasonal_ttsd"

        def synthesize(self, request, text, *, output_dir, deadline, cancellation):
            del request, text, output_dir, deadline, cancellation
            snapshot = registry.snapshot(source.worker_id, now)
            observed_remote_reservations.append(0 if snapshot is None else snapshot.pending_reservations)
            raise ProcessFailure("provider_timed_out", "remote fixture failure")

        def close(self):
            pass

    reserve_count = 0
    release_count = 0
    reserve = registry.reserve_controller_local
    release = registry.release_reservation

    def counted_reserve(**kwargs):
        nonlocal reserve_count
        reserve_count += 1
        return reserve(**kwargs)

    def counted_release(*args, **kwargs):
        nonlocal release_count
        release_count += 1
        return release(*args, **kwargs)

    monkeypatch.setattr(registry, "reserve_controller_local", counted_reserve)
    monkeypatch.setattr(registry, "release_reservation", counted_release)
    service = SynthesisService(
        capability_check=adapter,
        provider_adapters={BackendId.SEASONAL_TTSD: FailingRemote()},
    )
    result = service.synthesize(
        request(backend=BackendId.SEASONAL_TTSD, fallback_backend=BackendId.LOCAL),
        tmp_path / "reserved-fallback.wav",
    )
    assert result.disposition is SynthesisDisposition.SUCCEEDED
    assert result.fallback is not None and result.fallback.succeeded
    assert (tmp_path / "reserved-fallback.wav").is_file()
    assert reserve_count == 1
    assert release_count == 1
    assert observed_remote_reservations == [0]
    snapshot = registry.snapshot(source.worker_id, now)
    assert snapshot is not None
    assert snapshot.pending_reservations == 0
    assert snapshot.active_assignments == 0


def test_real_p109_remote_success_ignores_occupied_local_lane(monkeypatch, tmp_path: Path) -> None:
    from seasonalweather.capabilities.registry import CapabilityRegistry
    from seasonalweather.tts.adapters.models import ProviderAudio

    _install_fake_controller_tts(monkeypatch)
    now = dt.datetime.now(dt.UTC)
    capability = LocalEngineRegistry.capability_for("espeak-ng")
    registry = CapabilityRegistry(allowed_capabilities=frozenset({capability}))
    source = ControllerLocalQualificationSource(
        registry,
        lambda: now,
        configured_options=LocalEngineOptions(engine="espeak-ng", voice="9"),
        configuration_generation=4,
    )
    adapter = P109TtsQualificationAdapter(registry, lambda: now, local_source=source)
    held = adapter.reserve(
        request(job_id="already-running"), "already-running", expires_at=now + dt.timedelta(minutes=1)
    )
    assert held is not None

    class SuccessfulRemote:
        backend_id = "seasonal_ttsd"

        def synthesize(self, request, text, *, output_dir, deadline, cancellation):
            del request, text, deadline, cancellation
            output = output_dir / "remote.wav"
            write_silence_wav(output, 0.1, 48_000)
            return ProviderAudio(output, "audio/wav", "wav")

        def close(self):
            pass

    reserve_count = 0
    reserve = registry.reserve_controller_local

    def counted_reserve(**kwargs):
        nonlocal reserve_count
        reserve_count += 1
        return reserve(**kwargs)

    monkeypatch.setattr(registry, "reserve_controller_local", counted_reserve)
    service = SynthesisService(
        capability_check=adapter,
        provider_adapters={BackendId.SEASONAL_TTSD: SuccessfulRemote()},
    )
    result = service.synthesize(
        request(backend=BackendId.SEASONAL_TTSD),
        tmp_path / "remote-success.wav",
    )
    snapshot = registry.snapshot(source.worker_id, now)
    assert result.disposition is SynthesisDisposition.SUCCEEDED
    assert result.backend is BackendId.SEASONAL_TTSD
    assert reserve_count == 0
    assert snapshot is not None and snapshot.pending_reservations == 1
    adapter.release(held)


def test_real_async_p109_reservation_fences_effective_local_fallback(monkeypatch, tmp_path: Path) -> None:
    from seasonalweather.capabilities.registry import CapabilityRegistry
    from seasonalweather.tts.adapters import SeasonalTtsdAdapter, SeasonalTtsdConfig

    _install_fake_controller_tts(monkeypatch)
    now = dt.datetime.now(dt.UTC)
    capability = LocalEngineRegistry.capability_for("espeak-ng")
    registry = CapabilityRegistry(allowed_capabilities=frozenset({capability}))
    source = ControllerLocalQualificationSource(
        registry,
        lambda: now,
        configured_options=LocalEngineOptions(engine="espeak-ng", voice="9"),
        configuration_generation=4,
    )
    adapter = P109TtsQualificationAdapter(registry, lambda: now, local_source=source)
    observed_remote_reservations: list[int] = []

    def fail_remote(self, request, text, *, output_dir, deadline, cancellation):
        del self, request, text, output_dir, deadline, cancellation
        snapshot = registry.snapshot(source.worker_id, now)
        observed_remote_reservations.append(0 if snapshot is None else snapshot.pending_reservations)
        raise ProcessFailure("rate_limited", "remote async fixture failure")

    monkeypatch.setattr(SeasonalTtsdAdapter, "synthesize", fail_remote)
    reserve_count = 0
    release_count = 0
    reserve = registry.reserve_controller_local
    release = registry.release_reservation

    def counted_reserve(**kwargs):
        nonlocal reserve_count
        reserve_count += 1
        return reserve(**kwargs)

    def counted_release(*args, **kwargs):
        nonlocal release_count
        release_count += 1
        return release(*args, **kwargs)

    monkeypatch.setattr(registry, "reserve_controller_local", counted_reserve)
    monkeypatch.setattr(registry, "release_reservation", counted_release)
    executor = EmbeddedExecutionPort()
    facade = TTS(
        backend="seasonal_ttsd",
        fallback_backend="local",
        local_engine="espeak-ng",
        voice="9",
        rate_wpm=165,
        volume=1.0,
        sample_rate=48_000,
        configuration_generation=4,
        capability_check=adapter,
        execution_executor=executor,
        seasonal_ttsd_config=SeasonalTtsdConfig(
            base_url="https://tts.example.test", client_credential_file="/tmp/client"
        ),
    )
    output = tmp_path / "async-effective-local.wav"
    try:
        result = asyncio.run(
            synthesize_completed_wav_async(
                facade,
                "remote primary with local fallback",
                output,
                purpose="routine",
                executor=executor,
            )
        )
    finally:
        executor.shutdown(wait=True)
    assert result.disposition is SynthesisDisposition.SUCCEEDED
    assert result.backend is BackendId.LOCAL
    assert result.fallback is not None
    assert result.fallback.primary_backend is BackendId.SEASONAL_TTSD
    assert result.fallback.succeeded
    assert output.is_file()
    assert reserve_count == 1
    assert release_count == 1
    assert observed_remote_reservations == [0]
    snapshot = registry.snapshot(source.worker_id, now)
    assert snapshot is not None
    assert snapshot.pending_reservations == 0
    assert snapshot.active_assignments == 0


def _p109_controller_adapter(monkeypatch, *, available: bool):
    from seasonalweather.capabilities.registry import CapabilityRegistry

    _install_fake_controller_tts(monkeypatch)
    now = dt.datetime.now(dt.UTC)
    capability = LocalEngineRegistry.capability_for("espeak-ng")
    parameters = {
        "format": "wav",
        "profiles": "espeak-ng",
        "voices": "9",
        "sample_rates": 48_000,
        "max_input_bytes": 65_536,
    }
    evidence = LocalCapabilityEvidence(
        True,
        "healthy" if available else "unavailable",
        available,
        1,
        1 if available else 0,
        parameters,
    )
    monkeypatch.setattr(
        LocalEngineRegistry,
        "qualification_evidence",
        classmethod(lambda cls, _engine, _options: evidence),
    )
    registry = CapabilityRegistry(allowed_capabilities=frozenset({capability}))
    source = ControllerLocalQualificationSource(
        registry,
        lambda: now,
        configured_options=LocalEngineOptions(engine="espeak-ng", voice="9"),
        configuration_generation=4,
    )
    return registry, source, P109TtsQualificationAdapter(registry, lambda: now, local_source=source), now


def test_remote_only_p109_request_skips_unavailable_local_capacity(monkeypatch, tmp_path: Path) -> None:
    registry, source, adapter, now = _p109_controller_adapter(monkeypatch, available=False)
    service = SynthesisService(capability_check=adapter)
    remote = request(backend=BackendId.SEASONAL_TTSD, fallback_backend=None)
    local_capability = LocalEngineRegistry.capability_for("espeak-ng")
    assert adapter(request(), local_capability).disposition.value == "unavailable"

    available, reason = service.availability(remote)
    assert (available, reason) == (False, "remote_backend_unconfigured")
    result = service.synthesize(remote, tmp_path / "remote-only.wav")
    assert result.failure is SynthesisFailure.BACKEND_UNAVAILABLE
    snapshot = registry.snapshot(source.worker_id, now)
    assert snapshot is None or snapshot.pending_reservations == 0


def test_real_async_p109_remote_only_request_skips_local_capacity_and_stays_deferred(
    monkeypatch, tmp_path: Path
) -> None:
    registry, source, adapter, now = _p109_controller_adapter(monkeypatch, available=False)
    reserve_count = 0
    reserve = registry.reserve_controller_local

    def counted_reserve(**kwargs):
        nonlocal reserve_count
        reserve_count += 1
        return reserve(**kwargs)

    monkeypatch.setattr(registry, "reserve_controller_local", counted_reserve)
    executor = EmbeddedExecutionPort()
    facade = TTS(
        backend="seasonal_ttsd",
        fallback_backend=None,
        local_engine="espeak-ng",
        voice="9",
        rate_wpm=165,
        volume=1.0,
        sample_rate=48_000,
        configuration_generation=4,
        capability_check=adapter,
        execution_executor=executor,
    )
    output = tmp_path / "async-remote-only.wav"
    try:
        result = asyncio.run(
            synthesize_async(
                facade,
                "remote only",
                output,
                purpose="routine",
                executor=executor,
            )
        )
    finally:
        executor.shutdown(wait=True)

    assert result.failure is SynthesisFailure.BACKEND_UNAVAILABLE
    assert result.failure is not SynthesisFailure.CAPABILITY_REJECTED
    assert reserve_count == 0
    snapshot = registry.snapshot(source.worker_id, now)
    assert snapshot is None or snapshot.pending_reservations == 0
    assert not output.exists()


def test_real_async_p109_remote_only_request_does_not_wait_on_consumed_local_capacity(
    monkeypatch, tmp_path: Path
) -> None:
    registry, source, adapter, now = _p109_controller_adapter(monkeypatch, available=True)
    held = adapter.reserve(
        request(job_id="held-async-local"), "held-async-local", expires_at=now + dt.timedelta(minutes=1)
    )
    assert held is not None
    executor = EmbeddedExecutionPort()
    facade = TTS(
        backend="seasonal_ttsd",
        fallback_backend=None,
        local_engine="espeak-ng",
        voice="9",
        rate_wpm=165,
        volume=1.0,
        sample_rate=48_000,
        configuration_generation=4,
        capability_check=adapter,
        execution_executor=executor,
    )
    output = tmp_path / "async-remote-not-delayed.wav"
    try:
        result = asyncio.run(
            asyncio.wait_for(
                synthesize_async(
                    facade,
                    "remote does not wait",
                    output,
                    purpose="routine",
                    executor=executor,
                ),
                timeout=1.0,
            )
        )
    finally:
        executor.shutdown(wait=True)
        adapter.release(held)

    assert result.failure is SynthesisFailure.BACKEND_UNAVAILABLE
    snapshot = registry.snapshot(source.worker_id, now)
    assert snapshot is not None and snapshot.pending_reservations == 0
    assert not output.exists()


def test_remote_only_p109_request_is_not_delayed_by_held_local_reservation(monkeypatch, tmp_path: Path) -> None:
    registry, source, adapter, now = _p109_controller_adapter(monkeypatch, available=True)
    held = adapter.reserve(request(job_id="held-local"), "held-local", expires_at=now + dt.timedelta(minutes=1))
    assert held is not None
    service = SynthesisService(capability_check=adapter)
    result = service.synthesize(
        request(backend=BackendId.SEASONAL_TTSD, fallback_backend=None), tmp_path / "not-delayed.wav"
    )
    assert result.failure is SynthesisFailure.BACKEND_UNAVAILABLE
    snapshot = registry.snapshot(source.worker_id, now)
    assert snapshot is not None and snapshot.pending_reservations == 1
    adapter.release(held)


def test_local_primary_p109_capacity_is_reserved_once(monkeypatch, tmp_path: Path) -> None:
    registry, source, adapter, now = _p109_controller_adapter(monkeypatch, available=True)
    reserve_count = 0
    release_count = 0
    reserve = registry.reserve_controller_local
    release = registry.release_reservation

    def counted_reserve(**kwargs):
        nonlocal reserve_count
        reserve_count += 1
        return reserve(**kwargs)

    def counted_release(*args, **kwargs):
        nonlocal release_count
        release_count += 1
        return release(*args, **kwargs)

    monkeypatch.setattr(registry, "reserve_controller_local", counted_reserve)
    monkeypatch.setattr(registry, "release_reservation", counted_release)
    result = SynthesisService(capability_check=adapter).synthesize(request(), tmp_path / "local.wav")
    assert result.disposition is SynthesisDisposition.SUCCEEDED
    assert reserve_count == 1
    assert release_count == 1
    snapshot = registry.snapshot(source.worker_id, now)
    assert snapshot is not None and snapshot.pending_reservations == 0


def test_fallback_disallowed_purpose_does_not_reserve_local_capacity(monkeypatch, tmp_path: Path) -> None:
    registry, source, adapter, now = _p109_controller_adapter(monkeypatch, available=True)
    local_capability = LocalEngineRegistry.capability_for("espeak-ng")
    assert adapter(request(), local_capability).disposition.value == "satisfied"

    class ProviderTimeout:
        backend_id = "seasonal_ttsd"

        def synthesize(self, request, text, *, output_dir, deadline, cancellation):
            del request, text, output_dir, deadline, cancellation
            raise ProcessFailure("provider_timed_out", "provider timeout")

        def close(self):
            pass

    reserve_count = 0
    reserve = registry.reserve_controller_local

    def counted_reserve(**kwargs):
        nonlocal reserve_count
        reserve_count += 1
        return reserve(**kwargs)

    monkeypatch.setattr(registry, "reserve_controller_local", counted_reserve)
    result = SynthesisService(
        capability_check=adapter,
        provider_adapters={BackendId.SEASONAL_TTSD: ProviderTimeout()},
    ).synthesize(
        request(
            backend=BackendId.SEASONAL_TTSD,
            fallback_backend=BackendId.LOCAL,
            purpose=SynthesisPurpose.OPTIONAL,
        ),
        tmp_path / "optional-no-fallback.wav",
    )
    assert result.disposition is SynthesisDisposition.SUPPRESSED
    assert reserve_count == 0
    snapshot = registry.snapshot(source.worker_id, now)
    assert snapshot is None or snapshot.pending_reservations == 0


@pytest.mark.parametrize("fence", ["deadline", "cancellation"])
def test_global_timeout_or_cancellation_does_not_reserve_fallback_capacity(
    monkeypatch, tmp_path: Path, fence: str
) -> None:
    registry, source, adapter, now = _p109_controller_adapter(monkeypatch, available=True)
    reserve_count = 0
    reserve = registry.reserve_controller_local

    def counted_reserve(**kwargs):
        nonlocal reserve_count
        reserve_count += 1
        return reserve(**kwargs)

    monkeypatch.setattr(registry, "reserve_controller_local", counted_reserve)
    cancellation = threading.Event()
    request_kwargs: dict[str, object] = {"backend": BackendId.SEASONAL_TTSD, "fallback_backend": BackendId.LOCAL}
    if fence == "deadline":
        request_kwargs["deadline_at"] = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
    else:
        cancellation.set()
    result = SynthesisService(capability_check=adapter).synthesize(
        request(**request_kwargs),
        tmp_path / f"no-fallback-reservation-{fence}.wav",
        cancellation=cancellation,
    )
    assert result.disposition in {SynthesisDisposition.TIMED_OUT, SynthesisDisposition.CANCELLED}
    assert reserve_count == 0
    snapshot = registry.snapshot(source.worker_id, now)
    assert snapshot is None or snapshot.pending_reservations == 0


def test_reservation_requalification_refreshes_p109_freshness_without_reacquiring(monkeypatch) -> None:
    from seasonalweather.capabilities.registry import CapabilityRegistry

    capability = LocalEngineRegistry.capability_for("espeak-ng")
    t0 = dt.datetime(2026, 8, 11, tzinfo=dt.UTC)
    current = [t0]
    evidence = LocalCapabilityEvidence(
        True,
        "healthy",
        True,
        1,
        1,
        {
            "format": "wav",
            "profiles": "espeak-ng",
            "voices": "9",
            "sample_rates": 48_000,
            "max_input_bytes": 65_536,
        },
    )
    monkeypatch.setattr(LocalEngineRegistry, "qualification_evidence", classmethod(lambda cls, _e, _o: evidence))
    registry = CapabilityRegistry(allowed_capabilities=frozenset({capability}))
    source = ControllerLocalQualificationSource(
        registry,
        lambda: current[0],
        configured_options=LocalEngineOptions(engine="espeak-ng", voice="9"),
    )
    adapter = P109TtsQualificationAdapter(registry, lambda: current[0], local_source=source)
    owned_request = request(job_id="freshness-reservation")
    reservation = adapter.reserve(
        owned_request,
        "freshness-reservation",
        expires_at=t0 + dt.timedelta(seconds=120),
    )
    assert reservation is not None
    current[0] = t0 + dt.timedelta(seconds=61)
    original_refresh = source.refresh
    source.refresh = lambda _engine: None  # type: ignore[method-assign]
    with pytest.raises(ProcessFailure, match="no longer qualified"):
        adapter.for_reservation(owned_request, capability, reservation)
    source.refresh = original_refresh  # type: ignore[method-assign]
    refreshed = adapter.for_reservation(owned_request, capability, reservation)
    assert refreshed.disposition.value == "satisfied"
    snapshot = registry.snapshot(source.worker_id, current[0])
    assert snapshot is not None
    assert snapshot.pending_reservations == 1
    adapter.release(reservation)
    adapter.release(reservation)
    released = registry.snapshot(source.worker_id, current[0])
    assert released is not None
    assert released.pending_reservations == 0
    assert released.active_assignments == 0


def test_real_orchestrator_local_composition_uses_controller_qualification_authority(
    monkeypatch, tmp_path: Path
) -> None:
    from seasonalweather.artifacts.media import WavPolicy, inspect_wav
    from seasonalweather.main import Orchestrator

    cfg = _production_config(tmp_path, monkeypatch)
    called: list[str] = []

    class FakeHandler(LocalEngineHandler):
        engine_id = "espeak-ng"

        def synthesize(self, text, *, options, output_dir, deadline, cancellation, volume=1.0):
            del text, options, deadline, cancellation, volume
            called.append(self.engine_id)
            output = output_dir / "engine.wav"
            write_silence_wav(output, 0.1, 48_000)
            return LocalHandlerResult(output, self.engine_id)

    monkeypatch.setattr(LocalEngineRegistry, "handler", classmethod(lambda cls, _engine: FakeHandler()))
    monkeypatch.setattr(
        LocalEngineRegistry,
        "qualification_evidence",
        classmethod(
            lambda cls, _engine, _options: LocalCapabilityEvidence(
                True,
                "healthy",
                True,
                1,
                1,
                {
                    "format": "wav",
                    "profiles": "espeak-ng",
                    "voices": "9",
                    "sample_rates": 48_000,
                    "max_input_bytes": 65_536,
                },
            )
        ),
    )
    monkeypatch.setattr(
        SynthesisService,
        "_normalize_local_audio",
        lambda self, source, request, raw_dir, deadline, cancellation: (
            source,
            inspect_wav(source, policy=WavPolicy(maximum_duration_seconds=request.output.maximum_duration_seconds)),
        ),
    )
    monkeypatch.setattr("seasonalweather.tts.service.shutil.which", lambda name: f"/fake/{name}")
    orch = Orchestrator(cfg)
    orch.lifecycle.mark_running()
    result = orch.tts.synthesize("controller-local", tmp_path / "qualified.wav")
    assert result.failure is None
    assert called == ["espeak-ng"]
    assert orch.tts.availability() == (True, "tts_available")
    assert orch.tts_capability_check.registry is orch.capability_registry
    assert orch.tts_capability_check.local_source is orch.tts_capability_source
    assert any(
        snapshot.worker_id == "controller-local-tts"
        for snapshot in orch.capability_registry.snapshots(dt.datetime.now(dt.UTC))
    )

    from seasonalweather.configuration_reload.models import ReloadDisposition
    from seasonalweather.configuration_reload.resources import OrchestratorResourcePreparer

    path = SimpleNamespace(
        segments=("tts", "local", "voice"),
        to_pointer=lambda: "/tts/local/voice",
    )
    diff = SimpleNamespace(
        entries=(SimpleNamespace(path=path),),
        disposition=ReloadDisposition.QUIESCENT,
        digest="sha256:" + "a" * 64,
    )
    replacement_cfg = replace(cfg, tts=replace(cfg.tts, local=replace(cfg.tts.local, voice="8")))
    replacement = __import__("asyncio").run(
        OrchestratorResourcePreparer(orch, orch.reload_activities).prepare(
            replacement_cfg,
            diff=diff,
            expected_generation=0,
            target_generation=1,
            candidate_identity_sha256="b" * 64,
        )
    )
    assert replacement.tts is not None
    assert replacement.tts.capability_check is replacement.tts_capability_check
    assert replacement.tts.capability_check is not orch.tts_capability_check
    assert replacement.tts.capability_check.local_source is replacement.tts_capability_source
    assert replacement.tts_capability_source is not None
    assert replacement.tts_capability_source.configured_options.voice == "8"
    assert replacement.tts_capability_source.configuration_generation == 1


@pytest.mark.parametrize(
    "evidence",
    [
        LocalCapabilityEvidence(True, "unavailable", False, 1, 0, {}),
        LocalCapabilityEvidence(True, "healthy", True, 1, 0, {}),
    ],
)
def test_real_orchestrator_local_composition_fails_when_controller_capability_is_unusable(
    monkeypatch, tmp_path: Path, evidence: LocalCapabilityEvidence
) -> None:
    from seasonalweather.main import Orchestrator

    cfg = _production_config(tmp_path, monkeypatch)
    monkeypatch.setattr(
        LocalEngineRegistry, "qualification_evidence", classmethod(lambda cls, _engine, _options: evidence)
    )
    orch = Orchestrator(cfg)
    orch.lifecycle.mark_running()
    result = orch.tts.synthesize("controller-local", tmp_path / "rejected.wav")
    assert result.failure is SynthesisFailure.CAPABILITY_REJECTED
    assert orch.tts.availability()[0] is False


def _reload_diff_for_tts(path_segments: tuple[str, ...], disposition):
    path = SimpleNamespace(segments=path_segments, to_pointer=lambda: "/" + "/".join(path_segments))
    return SimpleNamespace(
        entries=(SimpleNamespace(path=path),),
        disposition=disposition,
        digest="sha256:" + "d" * 64,
    )


@pytest.mark.parametrize(
    ("path", "disposition", "safe_point"),
    (
        (
            ("dedupe", "ttl_seconds"),
            __import__(
                "seasonalweather.configuration_reload.models", fromlist=["ReloadDisposition"]
            ).ReloadDisposition.LIVE,
            False,
        ),
        (
            ("nwws", "allowed_wfos"),
            __import__(
                "seasonalweather.configuration_reload.models", fromlist=["ReloadDisposition"]
            ).ReloadDisposition.QUIESCENT,
            True,
        ),
    ),
)
def test_production_retained_tts_captures_generation_after_non_tts_reload(
    monkeypatch, tmp_path: Path, path: tuple[str, ...], disposition, safe_point: bool
) -> None:
    from seasonalweather.configuration_reload.resources import OrchestratorResourcePreparer
    from seasonalweather.main import Orchestrator

    _install_fake_controller_tts(monkeypatch)
    cfg = _production_config(tmp_path, monkeypatch)
    orch = Orchestrator(cfg)
    orch.lifecycle.mark_running()
    retained = orch.tts
    replacement_cfg = (
        replace(cfg, dedupe=replace(cfg.dedupe, ttl_seconds=cfg.dedupe.ttl_seconds + 1))
        if path[0] == "dedupe"
        else replace(cfg, nwws=replace(cfg.nwws, allowed_wfos=["KXXX"]))
    )
    plan = __import__("asyncio").run(
        OrchestratorResourcePreparer(orch, orch.reload_activities).prepare(
            replacement_cfg,
            diff=_reload_diff_for_tts(path, disposition),
            expected_generation=0,
            target_generation=1,
            candidate_identity_sha256="e" * 64,
        )
    )
    assert plan.tts is None
    if safe_point:
        plan.activate(safe_point_acquired=True)
    else:
        plan.activate()
    assert orch.tts is retained
    assert orch.configuration_generation == 1
    result = orch.tts.synthesize("retained facade", tmp_path / f"{path[0]}.wav")
    assert result.failure is None
    assert result.configuration_generation == 1


def test_production_running_synthesis_keeps_old_generation_and_fails_closed_on_reload(
    monkeypatch, tmp_path: Path
) -> None:
    from seasonalweather.configuration_reload.models import ReloadDisposition
    from seasonalweather.configuration_reload.resources import OrchestratorResourcePreparer
    from seasonalweather.main import Orchestrator

    started = threading.Event()
    release = threading.Event()
    _install_fake_controller_tts(monkeypatch, release=release, started=started)
    cfg = _production_config(tmp_path, monkeypatch)
    orch = Orchestrator(cfg)
    orch.lifecycle.mark_running()
    output = tmp_path / "overtaken.wav"
    holder: dict[str, object] = {}

    def synthesize() -> None:
        holder["result"] = orch.tts.synthesize("old generation", output)

    worker = threading.Thread(target=synthesize)
    worker.start()
    assert started.wait(5)
    plan = __import__("asyncio").run(
        OrchestratorResourcePreparer(orch, orch.reload_activities).prepare(
            replace(cfg, dedupe=replace(cfg.dedupe, ttl_seconds=cfg.dedupe.ttl_seconds + 1)),
            diff=_reload_diff_for_tts(("dedupe", "ttl_seconds"), ReloadDisposition.LIVE),
            expected_generation=0,
            target_generation=1,
            candidate_identity_sha256="f" * 64,
        )
    )
    plan.activate()
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert getattr(holder["result"], "failure", None) is SynthesisFailure.STALE_RESULT
    assert not output.exists()


def test_tts_changing_reload_installs_explicit_target_generation_facade(monkeypatch, tmp_path: Path) -> None:
    from seasonalweather.configuration_reload.models import ReloadDisposition
    from seasonalweather.configuration_reload.resources import OrchestratorResourcePreparer
    from seasonalweather.main import Orchestrator

    _install_fake_controller_tts(monkeypatch)
    cfg = _production_config(tmp_path, monkeypatch)
    orch = Orchestrator(cfg)
    replacement_cfg = replace(cfg, tts=replace(cfg.tts, local=replace(cfg.tts.local, voice="8")))
    plan = __import__("asyncio").run(
        OrchestratorResourcePreparer(orch, orch.reload_activities).prepare(
            replacement_cfg,
            diff=_reload_diff_for_tts(("tts", "voice"), ReloadDisposition.QUIESCENT),
            expected_generation=0,
            target_generation=1,
            candidate_identity_sha256="1" * 64,
        )
    )
    assert plan.tts is not None
    assert plan.tts.configuration_generation == 1
    plan.activate(safe_point_acquired=True)
    assert orch.tts is plan.tts
    assert orch.tts._request("target generation").configuration_generation == 1


def test_voicetext_counter_starts_new_generation_on_retained_facade(monkeypatch, tmp_path: Path) -> None:
    from seasonalweather.artifacts.media import WavPolicy, inspect_wav

    generation = [0]
    handler = VoiceTextPaulHandler()
    invocation_numbers: list[int] = []

    def fake_synthesize(text, *, options, output_dir, deadline, cancellation, volume=1.0):
        del text, options, deadline, cancellation, volume
        invocation_numbers.append(handler._invocations.next())
        output = output_dir / "engine.wav"
        write_silence_wav(output, 0.1, 48_000)
        return LocalHandlerResult(output, handler.engine_id)

    monkeypatch.setattr(LocalEngineRegistry, "handler", classmethod(lambda cls, _engine: handler))
    monkeypatch.setattr("seasonalweather.tts.service.shutil.which", lambda name: f"/fake/{name}")
    monkeypatch.setattr(
        SynthesisService,
        "_normalize_local_audio",
        lambda self, source, request, raw_dir, deadline, cancellation: (
            source,
            inspect_wav(source, policy=WavPolicy(maximum_duration_seconds=request.output.maximum_duration_seconds)),
        ),
    )
    monkeypatch.setattr(handler, "synthesize", fake_synthesize)
    facade = TTS(
        backend="local",
        local_engine="voicetext_paul",
        voice="9",
        rate_wpm=165,
        volume=1.0,
        sample_rate=48_000,
        generation_provider=lambda: generation[0],
        allow_transitional_qualification=True,
        vtp_cfg=SimpleNamespace(retries=0, reset_every=2, vtml_lexicon=False),
    )
    facade.synthesize("generation zero", tmp_path / "zero.wav")
    generation[0] = 1
    facade.synthesize("generation one", tmp_path / "one.wav")
    assert invocation_numbers == [1, 1]
