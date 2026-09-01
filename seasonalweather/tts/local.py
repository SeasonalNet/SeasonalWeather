"""Local-engine registry and handlers.

Only this module owns engine-specific argv, markup, and executable behavior.
"""

from __future__ import annotations

import os
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import ClassVar

from seasonalweather.capabilities.models import ParameterValue

from .cancellation import deadline_expired, explicit_cancellation
from .models import LocalEngineOptions, VoiceTextOptions
from .subprocess import ProcessFailure, resolve_trusted_executable, run_bounded


def _fence(deadline: float, cancellation: Event | None, stage: str) -> None:
    if deadline_expired(cancellation) or time.monotonic() >= deadline:
        raise ProcessFailure("timed_out", f"synthesis deadline expired during {stage}")
    if explicit_cancellation(cancellation):
        raise ProcessFailure("cancelled", f"synthesis was cancelled during {stage}")


def _data_base(value: str = "") -> Path:
    """Resolve the configured engine state base with legacy standalone fallback."""

    configured = value.strip()
    return Path(configured or os.getenv("SEASONALWEATHER_DATA_BASE", "/var/lib/seasonalweather"))


@contextmanager
def _process_lock(lock_path: Path, *, deadline: float, cancellation: Event | None):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        while True:
            _fence(deadline, cancellation, "VoiceText process lock")
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(min(0.05, max(0.001, deadline - time.monotonic())))
        try:
            yield
        finally:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class LocalHandlerResult:
    output_path: Path
    engine: str


@dataclass(frozen=True)
class LocalCapabilityEvidence:
    """Implementation/resource evidence, not a capability authority."""

    implemented: bool
    operational_state: str
    accepting_new_jobs: bool
    total_capacity: int
    reported_available: int
    parameters: dict[str, ParameterValue]
    evidence: tuple[str, ...] = ()


@dataclass
class _InvocationCounter:
    value: int = 0

    def next(self) -> int:
        self.value += 1
        return self.value


class LocalEngineHandler:
    engine_id: ClassVar[str]

    def synthesize(
        self,
        text: str,
        *,
        options: LocalEngineOptions,
        output_dir: Path,
        deadline: float,
        cancellation: Event | None,
        volume: float = 1.0,
    ) -> LocalHandlerResult:
        raise NotImplementedError


class _SubprocessHandler(LocalEngineHandler):
    def _run(
        self,
        argv: list[str],
        *,
        input_bytes: bytes | None,
        deadline: float,
        cancellation: Event | None,
        cwd: Path | None = None,
        environment: dict[str, str] | None = None,
    ) -> None:
        if environment is None:
            run_bounded(
                argv,
                input_bytes=input_bytes,
                deadline=deadline,
                cancellation=cancellation,
                cwd=cwd,
            )
        else:
            run_bounded(
                argv,
                input_bytes=input_bytes,
                deadline=deadline,
                cancellation=cancellation,
                cwd=cwd,
                environment=environment,
            )


class EspeakHandler(_SubprocessHandler):
    engine_id = "espeak-ng"

    def synthesize(
        self,
        text: str,
        *,
        options: LocalEngineOptions,
        output_dir: Path,
        deadline: float,
        cancellation: Event | None,
        volume: float = 1.0,
    ) -> LocalHandlerResult:
        output = output_dir / "engine.wav"
        executable = resolve_trusted_executable("espeak-ng")
        self._run(
            [executable, "-v", options.voice, "-s", str(options.rate_wpm), "-w", str(output), "-f", "-"],
            input_bytes=(text + "\n").encode("utf-8"),
            deadline=deadline,
            cancellation=cancellation,
        )
        return LocalHandlerResult(output, self.engine_id)


class PiperHandler(_SubprocessHandler):
    engine_id = "piper"

    def synthesize(
        self,
        text: str,
        *,
        options: LocalEngineOptions,
        output_dir: Path,
        deadline: float,
        cancellation: Event | None,
        volume: float = 1.0,
    ) -> LocalHandlerResult:
        output = output_dir / "engine.wav"
        executable = resolve_trusted_executable("piper")
        model = _piper_model_path(options.voice)
        self._run(
            [executable, "-m", str(model), "-f", str(output)],
            input_bytes=(text + "\n").encode("utf-8"),
            deadline=deadline,
            cancellation=cancellation,
        )
        return LocalHandlerResult(output, self.engine_id)


def _piper_model_path(voice: str) -> Path | str:
    """Resolve a Piper voice ID against the deployment's model directory."""

    model_dir = os.getenv("PIPER_MODEL_DIR", "").strip()
    if not model_dir:
        return voice
    if not voice or Path(voice).name != voice or voice in {".", ".."}:
        raise ProcessFailure("invalid_input", "Piper voice must be a model filename")
    filename = voice if voice.endswith(".onnx") else f"{voice}.onnx"
    model = Path(model_dir) / filename
    if not model.is_file() or not model.with_name(f"{model.name}.json").is_file():
        raise ProcessFailure("executable_unavailable", "Piper voice model or sidecar is unavailable")
    return model


class FestivalHandler(_SubprocessHandler):
    engine_id = "festival"

    def synthesize(
        self,
        text: str,
        *,
        options: LocalEngineOptions,
        output_dir: Path,
        deadline: float,
        cancellation: Event | None,
        volume: float = 1.0,
    ) -> LocalHandlerResult:
        output = output_dir / "engine.wav"
        text_path = output_dir / "input.txt"
        text_path.write_text(text + "\n", encoding="utf-8")
        os.chmod(text_path, 0o600)
        executable = resolve_trusted_executable("text2wave")
        stretch = max(0.5, min(2.0, 175.0 / float(options.rate_wpm)))
        self._run(
            [
                executable,
                "-eval",
                f"(Parameter.set 'Duration_Stretch {stretch})",
                "-eval",
                _festival_voice(options.voice),
                "-o",
                str(output),
                str(text_path),
            ],
            input_bytes=None,
            deadline=deadline,
            cancellation=cancellation,
        )
        return LocalHandlerResult(output, self.engine_id)


class DecTalkHandler(_SubprocessHandler):
    engine_id = "dectalk"
    say_path = Path("/opt/dectalk/dectalk/dist/say")

    def synthesize(
        self,
        text: str,
        *,
        options: LocalEngineOptions,
        output_dir: Path,
        deadline: float,
        cancellation: Event | None,
        volume: float = 1.0,
    ) -> LocalHandlerResult:
        output = output_dir / "engine.wav"
        env_wrapper = resolve_trusted_executable("dectalk-env")
        say = self.say_path
        if not say.is_file() or not os.access(say, os.X_OK):
            raise ProcessFailure("executable_unavailable", "DECtalk engine is unavailable")
        speaker = max(0, min(9, int(options.voice) if str(options.voice).isdigit() else 0))
        self._run(
            [
                env_wrapper,
                str(say),
                "-l",
                "us",
                "-s",
                str(speaker),
                "-r",
                str(max(75, min(600, int(options.rate_wpm)))),
                "-v",
                str(max(0, min(100, round(volume * 100)))),
                "-e",
                "1",
                "-fo",
                str(output),
                "-c",
                "-",
            ],
            input_bytes=(text + "\n").encode("utf-8"),
            deadline=deadline,
            cancellation=cancellation,
        )
        return LocalHandlerResult(output, self.engine_id)


class VoiceTextPaulHandler(_SubprocessHandler):
    engine_id = "voicetext_paul"
    wrapper_path = Path("/usr/local/bin/voicetext_paul_synth")
    reset_path = Path("/usr/local/bin/voicetext_paul_wineserver_kill")

    def __init__(self) -> None:
        self._invocations = _InvocationCounter()

    def set_invocation_counter(self, counter: _InvocationCounter) -> None:
        self._invocations = counter

    def synthesize(
        self,
        text: str,
        *,
        options: LocalEngineOptions,
        output_dir: Path,
        deadline: float,
        cancellation: Event | None,
        volume: float = 1.0,
    ) -> LocalHandlerResult:
        return _synthesize_voicetext_paul(
            self,
            text,
            options=options,
            output_dir=output_dir,
            deadline=deadline,
            cancellation=cancellation,
        )


class SpfyHandler(_SubprocessHandler):
    """Run the optional local ``spfy`` worker and return its native WAV."""

    engine_id = "spfy"

    def synthesize(
        self,
        text: str,
        *,
        options: LocalEngineOptions,
        output_dir: Path,
        deadline: float,
        cancellation: Event | None,
        volume: float = 1.0,
    ) -> LocalHandlerResult:
        del volume
        executable = resolve_trusted_executable(options.spfy.executable)
        voice_dir = Path(options.spfy.voice_dir)
        if not voice_dir.is_dir():
            raise ProcessFailure("executable_unavailable", "spfy voice directory is unavailable")
        input_path = output_dir / "spfy-input.txt"
        output_path = output_dir / "engine.wav"
        input_path.write_text(text + "\n", encoding="utf-8")
        os.chmod(input_path, 0o600)
        self._run(
            [executable, options.voice, "--no-update-check", "--file", str(input_path), str(output_path)],
            input_bytes=None,
            deadline=deadline,
            cancellation=cancellation,
            environment={"SPFY_VOICE_DIR": str(voice_dir), "SPFY_NO_UPDATE_CHECK": "1"},
        )
        if not output_path.is_file() or output_path.stat().st_size < 44:
            raise ProcessFailure("nonzero_exit", "spfy produced no bounded output")
        return LocalHandlerResult(output_path, self.engine_id)


def _synthesize_voicetext_paul(
    handler: VoiceTextPaulHandler,
    text: str,
    *,
    options: LocalEngineOptions,
    output_dir: Path,
    deadline: float,
    cancellation: Event | None,
) -> LocalHandlerResult:
    from .voicetext_paul_vtml import apply_voicetext_paul_vtml

    vtp = options.voicetext_paul
    prepared = apply_voicetext_paul_vtml(
        text,
        vtml_lexicon=vtp.vtml_lexicon,
        alias_overrides=[
            {"match": item.match, "alias": item.replace, "regex": item.regex, "ignore_case": item.ignore_case}
            for item in vtp.alias_overrides
        ],
        phoneme_overrides_x_cmu=[
            {"match": item.match, "ph": item.replace, "regex": item.regex, "ignore_case": item.ignore_case}
            for item in vtp.phoneme_overrides_x_cmu
        ],
    )
    _fence(deadline, cancellation, "VoiceText preprocessing")
    state_base = _data_base(options.voicetext_paul.data_base)
    engine_root = Path(
        os.getenv("VOICETEXT_PAUL_ENGINE_ROOT", str(state_base / "voices/voicetext_paul/WeatherRadioSuite-LIB"))
    )
    engine_dir = Path(os.getenv("VOICETEXT_PAUL_BIN_DIR", str(engine_root / "binary")))
    exe = engine_dir / "voicetext_paul.exe"
    wrapper = handler.wrapper_path
    if not exe.is_file() or not wrapper.is_file():
        raise ProcessFailure("executable_unavailable", "VoiceText Paul engine is unavailable")
    source = engine_dir / "output.wav"
    reset_tool = handler.reset_path
    command = [str(wrapper)]
    lock_path = Path(os.getenv("VOICETEXT_PAUL_LOCK_PATH", str(state_base / ".voicetext_paul_tts.lock")))
    with _process_lock(lock_path, deadline=deadline, cancellation=cancellation):
        call_number = handler._invocations.next()
        if vtp.kill_before or (vtp.reset_every > 0 and call_number % vtp.reset_every == 0):
            _reset_voicetext(reset_tool, engine_dir, deadline, cancellation)
        return _run_voicetext_attempts(
            handler,
            prepared,
            command,
            source,
            reset_tool,
            vtp,
            output_dir,
            engine_dir,
            deadline,
            cancellation,
        )


def _reset_voicetext(
    reset_tool: Path,
    engine_dir: Path,
    deadline: float,
    cancellation: Event | None,
) -> None:
    if not reset_tool.is_file():
        raise ProcessFailure("executable_unavailable", "VoiceText reset utility is unavailable")
    _fence(deadline, cancellation, "VoiceText wineserver reset")
    run_bounded(
        [str(reset_tool)],
        input_bytes=None,
        deadline=deadline,
        cancellation=cancellation,
        cwd=engine_dir,
    )


def _run_voicetext_attempts(
    handler: VoiceTextPaulHandler,
    prepared: str,
    command: list[str],
    source: Path,
    reset_tool: Path,
    vtp: VoiceTextOptions,
    output_dir: Path,
    engine_dir: Path,
    deadline: float,
    cancellation: Event | None,
) -> LocalHandlerResult:
    primary_error: ProcessFailure | None = None
    for attempt in range(vtp.retries + 1):
        _fence(deadline, cancellation, "VoiceText synthesis")
        source.unlink(missing_ok=True)
        try:
            handler._run(
                command,
                input_bytes=(prepared + "\n").encode("utf-8"),
                deadline=deadline,
                cancellation=cancellation,
                cwd=engine_dir,
            )
            if not source.is_file() or source.stat().st_size < 2_000:
                raise ProcessFailure("nonzero_exit", "VoiceText Paul produced no bounded output")
            target = output_dir / "engine.wav"
            _copy_voicetext_output(source, target, deadline, cancellation)
            source.unlink(missing_ok=True)
            return LocalHandlerResult(target, handler.engine_id)
        except ProcessFailure as error:
            primary_error = error
            source.unlink(missing_ok=True)
            try:
                if reset_tool.is_file():
                    _reset_voicetext(reset_tool, engine_dir, deadline, cancellation)
                elif attempt < vtp.retries:
                    raise ProcessFailure("executable_unavailable", "VoiceText reset utility is unavailable")
            except ProcessFailure as reset_error:
                setattr(error, "secondary_evidence", f"reset:{reset_error.classification}")
            if attempt < vtp.retries:
                _sleep_bounded(vtp.retry_sleep_ms / 1000.0, deadline, cancellation)
    if primary_error is not None:
        raise primary_error
    raise ProcessFailure("nonzero_exit", "VoiceText Paul produced no bounded output")


def _copy_voicetext_output(source: Path, target: Path, deadline: float, cancellation: Event | None) -> None:
    _fence(deadline, cancellation, "VoiceText output copy")
    with source.open("rb") as src, target.open("wb") as dst:
        while block := src.read(65_536):
            _fence(deadline, cancellation, "VoiceText output copy")
            dst.write(block)
    os.chmod(target, 0o640)


def _sleep_bounded(duration: float, deadline: float, cancellation: Event | None) -> None:
    end = min(deadline, time.monotonic() + max(0.0, duration))
    while time.monotonic() < end:
        _fence(deadline, cancellation, "VoiceText retry delay")
        time.sleep(min(0.05, max(0.001, end - time.monotonic())))


def _festival_voice(voice: str) -> str:
    value = voice.strip() or "kal_diphone"
    if value.startswith("(") and value.endswith(")"):
        value = value[1:-1].strip()
    if not value.startswith("voice_"):
        value = "voice_" + value
    if not value.replace("_", "").isalnum():
        raise ProcessFailure("invalid_input", "Festival voice name is invalid")
    return f"({value})"


class LocalEngineRegistry:
    """Static implementation metadata; it is not a health or capacity registry."""

    _handlers = {
        "espeak-ng": EspeakHandler,
        "piper": PiperHandler,
        "festival": FestivalHandler,
        "dectalk": DecTalkHandler,
        "voicetext_paul": VoiceTextPaulHandler,
        "spfy": SpfyHandler,
    }
    _aliases = {"espeak": "espeak-ng", "espeak_ng": "espeak-ng"}

    @classmethod
    def normalize(cls, engine: str) -> str:
        canonical = cls._aliases.get(engine.strip().lower(), engine.strip().lower())
        if canonical not in cls._handlers:
            raise ProcessFailure("unsupported_engine", "configured local TTS engine is unsupported")
        return canonical

    @classmethod
    def handler(cls, engine: str) -> LocalEngineHandler:
        canonical = cls.normalize(engine)
        return cls._handlers[canonical]()

    @classmethod
    def supported_engines(cls) -> tuple[str, ...]:
        return tuple(sorted(cls._handlers))

    @classmethod
    def capability_for(cls, engine: str) -> str:
        return f"tts.local.{cls.normalize(engine)}.v1"

    @classmethod
    def capability_names(cls) -> frozenset[str]:
        return frozenset(cls.capability_for(engine) for engine in cls.supported_engines())

    @classmethod
    def qualification_evidence(cls, engine: str, options: LocalEngineOptions) -> LocalCapabilityEvidence:
        """Probe implementation/resources for the embedded executor.

        The returned evidence is converted into a P1-09 record by the
        controller-local qualification source. This registry never stores or
        publishes health/capacity state.
        """

        canonical = cls.normalize(engine)
        parameters, profile_ok = cls._qualification_profile(canonical, options)
        resources_ok = cls._qualification_resources(canonical, options)
        ffmpeg_ok = shutil.which("ffmpeg") is not None
        available = profile_ok and resources_ok and ffmpeg_ok
        return LocalCapabilityEvidence(
            implemented=True,
            operational_state="healthy" if available else "unavailable",
            accepting_new_jobs=available,
            total_capacity=1,
            reported_available=1 if available else 0,
            parameters=parameters,
            evidence=tuple(
                item
                for item, present in (
                    ("local_engine_resources", resources_ok),
                    ("ffmpeg_resource", ffmpeg_ok),
                )
                if present
            ),
        )

    @classmethod
    def _qualification_profile(
        cls,
        canonical: str,
        options: LocalEngineOptions,
    ) -> tuple[dict[str, ParameterValue], bool]:
        try:
            cls.validate_voice(canonical, options.voice)
        except ValueError:
            return (
                {"format": "wav", "profiles": canonical, "voices": options.voice},
                False,
            )
        return (
            {
                "format": "wav",
                "profiles": canonical,
                "voices": options.voice,
                "sample_rates": options.sample_rate_hz,
                "max_input_bytes": 65_536,
            },
            True,
        )

    @classmethod
    def _qualification_resources(cls, canonical: str, options: LocalEngineOptions) -> bool:
        resources_ok = all(
            (
                Path(resource).is_file() and os.access(resource, os.X_OK)
                if resource.startswith("/")
                else shutil.which(resource)
            )
            for resource in cls.required_resources(canonical)
        )
        specialized = {
            "piper": cls._piper_resources_available,
            "voicetext_paul": cls._voicetext_resources_available,
            "spfy": cls._spfy_resources_available,
        }.get(canonical)
        return resources_ok if specialized is None else specialized(options, resources_ok)

    @staticmethod
    def _piper_resources_available(options: LocalEngineOptions, resources_ok: bool) -> bool:
        if not resources_ok:
            return False
        try:
            _piper_model_path(options.voice)
        except ProcessFailure:
            return False
        return True

    @staticmethod
    def _spfy_resources_available(options: LocalEngineOptions, resources_ok: bool) -> bool:
        return resources_ok and Path(options.spfy.voice_dir).is_dir()

    @staticmethod
    def _voicetext_resources_available(options: LocalEngineOptions, resources_ok: bool) -> bool:
        state_base = _data_base(options.voicetext_paul.data_base)
        engine_root = Path(
            os.getenv(
                "VOICETEXT_PAUL_ENGINE_ROOT",
                str(state_base / "voices/voicetext_paul/WeatherRadioSuite-LIB"),
            )
        )
        engine_dir = Path(os.getenv("VOICETEXT_PAUL_BIN_DIR", str(engine_root / "binary")))
        reset_required = (
            options.voicetext_paul.retries > 0
            or options.voicetext_paul.kill_before
            or options.voicetext_paul.reset_every > 0
        )
        return (
            resources_ok
            and (engine_dir / "voicetext_paul.exe").is_file()
            and (not reset_required or VoiceTextPaulHandler.reset_path.is_file())
        )

    @classmethod
    def validate_voice(cls, engine: str, voice: str) -> None:
        canonical = cls.normalize(engine)
        value = voice.strip()
        if not value or any(char in value for char in "\x00\r\n"):
            raise ValueError("local voice is invalid")
        if canonical == "dectalk" and not value.isdigit():
            raise ValueError("DECtalk voice must be a numeric speaker")
        if canonical == "festival" and not value.replace("_", "").replace("(", "").replace(")", "").isalnum():
            raise ValueError("Festival voice is invalid")

    @classmethod
    def required_resources(cls, engine: str) -> tuple[str, ...]:
        canonical = cls.normalize(engine)
        if canonical == "dectalk":
            return ("dectalk-env", "/opt/dectalk/dectalk/dist/say")
        if canonical == "voicetext_paul":
            return ("voicetext_paul_synth",)
        if canonical == "spfy":
            return ("/opt/spfy/bin/spfy_synth",)
        return {
            "espeak-ng": ("espeak-ng",),
            "piper": ("piper",),
            "festival": ("text2wave",),
        }[canonical]
