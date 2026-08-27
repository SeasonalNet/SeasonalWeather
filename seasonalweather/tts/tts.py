"""Compatibility facade over the backend-neutral P1-16 TTS boundary."""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import time
from collections.abc import Callable
from concurrent.futures import Executor
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Any, cast

from .preprocess import clean_for_tts, normalize_nws_spoken_times, verbalize_url
from .models import FinalizationCallbackError

if TYPE_CHECKING:
    from .service import SynthesisService

__all__ = ["TTS", "TTSCompatibilityError", "clean_for_tts", "normalize_nws_spoken_times", "verbalize_url"]


TTSCompatibilityError = FinalizationCallbackError


@contextmanager
def _flock_path(lock_path: Path, timeout_s: float = 90.0, poll_s: float = 0.1):
    """Process-safe advisory lock retained for compatibility inspection."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with lock_path.open("a+") as handle:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() - started > timeout_s:
                    raise RuntimeError(f"Timed out waiting for lock {lock_path}") from None
                time.sleep(poll_s)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass
class TTS:
    """Preserve the historical facade while delegating policy and execution."""

    backend: str
    voice: str
    rate_wpm: int
    volume: float
    sample_rate: int
    text_overrides: list[dict] | None = None
    vtp_cfg: object = None
    admission_check: Callable[[], None] | None = None
    activity_context: Callable[[], AbstractContextManager[None]] | None = None
    fallback_backend: str | None = None
    local_engine: str | None = None
    configuration_generation: int | None = None
    # Construction/target generation remains explicit for prepared resource
    # plans. Requests capture the controller's live generation through this
    # provider when the facade is retained across unrelated reloads.
    generation_provider: Callable[[], int | None] | None = None
    current_generation: Callable[[int | None], bool] | None = None
    capability_check: Callable[[object, str], object] | None = None
    lkg_resolver: Callable[[Any], Any] | None = None
    allow_transitional_qualification: bool = False
    execution_executor: Executor | None = None
    seasonal_ttsd_config: object | None = None
    openai_compatible_config: object | None = None
    tts_data_base: str | None = None
    diagnostic_sink: object | None = None
    _synthesis_service: SynthesisService | None = field(default=None, init=False, repr=False)

    def _request(self, text: str, *, purpose: str = "routine", deadline_at: dt.datetime | None = None):
        from .models import BackendId, LocalEngineOptions, SynthesisOutputPolicy, SynthesisPurpose, SynthesisRequest
        from .policy import deadline_for

        local_engine = self._selected_local_engine()
        backend = self._selected_backend()
        configuration_generation = (
            self.generation_provider() if self.generation_provider is not None else self.configuration_generation
        )
        return SynthesisRequest(
            purpose=SynthesisPurpose(purpose),
            backend=backend,
            fallback_backend=None if self.fallback_backend is None else BackendId(self.fallback_backend),
            text=text,
            backend_profile_identity=self._backend_profile_identity(backend),
            configuration_generation=configuration_generation,
            deadline_at=deadline_at or deadline_for(SynthesisPurpose(purpose)),
            local=LocalEngineOptions(
                engine=local_engine,
                voice=self.voice,
                rate_wpm=self.rate_wpm,
                sample_rate_hz=self.sample_rate,
                voicetext_paul=self._voice_options(),
            ),
            output=SynthesisOutputPolicy(sample_rate_hz=self.sample_rate, volume=self.volume),
            text_overrides=self._text_overrides(),
        )

    def _backend_profile_identity(self, backend: object) -> str | None:
        from .models import BackendId

        if backend is BackendId.LOCAL:
            return None
        config = self.seasonal_ttsd_config if backend is BackendId.SEASONAL_TTSD else self.openai_compatible_config
        if config is None:
            return None
        fields = (
            "voice",
            "profile",
            "model",
            "response_format",
            "speed",
        )
        public = {name: getattr(config, name) for name in fields if hasattr(config, name)}
        raw = json.dumps(public, sort_keys=True, separators=(",", ":"))
        return f"sha256:{hashlib.sha256(raw.encode()).hexdigest()[:32]}"

    def _voice_options(self):
        from .models import VoiceTextOptions

        cfg = self.vtp_cfg
        return VoiceTextOptions(
            run_as=str(getattr(cfg, "run_as", "voicetext")),
            data_base=self.tts_data_base or "",
            retries=int(getattr(cfg, "retries", 1)),
            retry_sleep_ms=int(getattr(cfg, "retry_sleep_ms", 150)),
            reset_every=int(getattr(cfg, "reset_every", 0)),
            kill_before=bool(getattr(cfg, "kill_before", False)),
            vtml_lexicon=bool(getattr(cfg, "vtml_lexicon", True)),
            alias_overrides=self._override_values("alias_overrides", "alias"),
            phoneme_overrides_x_cmu=self._override_values("phoneme_overrides_x_cmu", "ph"),
        )

    def _override_values(self, name: str, replacement: str):
        from .models import TextOverride

        return tuple(
            TextOverride(
                match=str(item.get("match", "")),
                replace=str(item.get(replacement, "")),
                regex=bool(item.get("regex", False)),
                ignore_case=bool(item.get("ignore_case", False)),
            )
            for item in (getattr(self.vtp_cfg, name, []) or [])
        )

    def _text_overrides(self):
        from .models import TextOverride

        return tuple(
            TextOverride(
                match=str(item.get("match", "")),
                replace=str(item.get("replace", "")),
                regex=bool(item.get("regex", False)),
                ignore_case=bool(item.get("ignore_case", False)),
            )
            for item in (self.text_overrides or [])
        )

    def _service(self) -> SynthesisService:
        from .adapters import (
            OpenAICompatibleAdapter,
            OpenAICompatibleConfig,
            SeasonalTtsdAdapter,
            SeasonalTtsdConfig,
        )
        from .adapters.base import ProviderAdapter
        from .admission import LocalQualification, LocalQualificationDisposition, transitional_local_qualification
        from .models import BackendId, SynthesisRequest
        from .service import SynthesisService

        def qualify(request: SynthesisRequest, capability: str) -> object:
            if self.capability_check is not None:
                return self.capability_check(request, capability)
            if self.allow_transitional_qualification:
                return transitional_local_qualification(request, capability)
            return LocalQualification(
                disposition=LocalQualificationDisposition.UNKNOWN,
                capability=capability,
                evidence=("p1_09_qualification_port_unbound",),
                effective_capacity=0,
            )

        # Preserve the P1-09 reservation port when the qualification callback
        # is wrapped for the service; the wrapper remains policy-free.
        if self.capability_check is not None:
            for name in ("reserve", "release", "for_reservation"):
                operation = getattr(self.capability_check, name, None)
                if operation is not None:
                    setattr(qualify, name, operation)

        if self._synthesis_service is None:
            providers = cast(
                dict[BackendId, ProviderAdapter],
                self._provider_adapters(
                    BackendId,
                    OpenAICompatibleAdapter,
                    OpenAICompatibleConfig,
                    SeasonalTtsdAdapter,
                    SeasonalTtsdConfig,
                ),
            )
            self._synthesis_service = SynthesisService(
                activity_context=self.activity_context,
                current_generation=self.current_generation,
                capability_check=qualify,
                lkg_resolver=self.lkg_resolver,  # controller-owned P1-10 evidence port
                provider_adapters=providers,
                diagnostic_sink=self.diagnostic_sink,
            )
        return self._synthesis_service

    def _provider_adapters(
        self,
        backend_enum: Any,
        openai_adapter: Any,
        openai_config: Any,
        seasonal_adapter: Any,
        seasonal_config: Any,
    ) -> dict[object, Any]:
        selected = self._selected_backend()
        providers: dict[object, Any] = {}
        definitions = (
            (backend_enum.SEASONAL_TTSD, self.seasonal_ttsd_config, seasonal_adapter, seasonal_config),
            (backend_enum.OPENAI_COMPATIBLE, self.openai_compatible_config, openai_adapter, openai_config),
        )
        for provider, config, adapter_type, config_type in definitions:
            if config is None or (selected is not provider and self.fallback_backend != provider.value):
                continue
            providers[provider] = cast(Any, adapter_type)(cast(Any, config_type)(**_config_values(config)))
        return providers

    def _selected_local_engine(self) -> str:
        from .models import BackendId

        if self.local_engine:
            return self.local_engine
        if self.backend not in {item.value for item in BackendId}:
            return self.backend
        return "espeak-ng"

    def _selected_backend(self):
        from .models import BackendId

        if self.backend in {item.value for item in BackendId}:
            return BackendId(self.backend)
        # Legacy backend=<local-engine> is normalized at the facade boundary.
        return BackendId.LOCAL

    def _voicetext_lock(self):
        if self._selected_local_engine() != "voicetext_paul":
            return nullcontext()
        state_base_value: str = self.tts_data_base or cast(
            str,
            os.getenv("SEASONALWEATHER_DATA_BASE", "/var/lib/seasonalweather"),
        )
        state_base = Path(state_base_value)
        return _flock_path(state_base / ".voicetext_paul_tts.lock")

    def availability(self) -> tuple[bool, str]:
        return self._service().availability(self._request("availability probe"))

    def synthesize(
        self,
        text: str,
        out_wav: Path,
        *,
        purpose: str = "routine",
        deadline_at: dt.datetime | None = None,
        cancellation: Event | None = None,
    ):
        if self.admission_check is not None:
            self.admission_check()
        return self._service().synthesize(
            self._request(text, purpose=purpose, deadline_at=deadline_at),
            Path(out_wav),
            cancellation=cancellation,
        )

    def request_for(self, text: str, *, purpose: str = "routine", deadline_at: dt.datetime | None = None):
        """Capture an immutable request before an async executor submission."""

        return self._request(text, purpose=purpose, deadline_at=deadline_at)

    def synthesize_request(
        self,
        request: object,
        out_wav: Path,
        *,
        cancellation: object | None = None,
        finalize: Callable[[Path, object, Callable[[], None]], None] | None = None,
        capacity_reservation: object | None = None,
    ):
        """Execute a request already created by the async TTS bridge."""

        from .models import SynthesisRequest

        if self.admission_check is not None:
            self.admission_check()
        if not isinstance(request, SynthesisRequest):
            raise TypeError("TTS bridge request must be a SynthesisRequest")
        return self._service().synthesize(
            request,
            Path(out_wav),
            cancellation=cast(Event | None, cancellation),
            finalize=finalize,
            capacity_reservation=capacity_reservation,
        )

    def try_reserve_capacity(self, request: object, reservation_id: str, *, expires_at: dt.datetime) -> object | None:
        """Try one P1-09-owned embedded reservation without creating a job."""

        from .models import SynthesisRequest

        if not isinstance(request, SynthesisRequest):
            raise TypeError("TTS capacity request must be a SynthesisRequest")
        request = self._service()._local_capacity_request(request)
        if request is None:
            return None
        checker = self.capability_check
        reserve = getattr(checker, "reserve", None)
        if reserve is None:
            return None
        return cast(Callable[..., object | None], reserve)(request, reservation_id, expires_at=expires_at)

    def capacity_is_relevant(self, request: object) -> bool:
        from .models import SynthesisRequest

        return isinstance(request, SynthesisRequest) and self._service().capacity_is_relevant(request)

    def release_capacity(self, reservation: object) -> None:
        release = getattr(self.capability_check, "release", None)
        if release is not None:
            release(reservation)

    def finalization_fence(
        self, request: object, cancellation: object, capacity_reservation: object | None = None
    ) -> None:
        """Recheck generation/admission immediately before caller replacement."""

        from .models import SynthesisRequest

        if not isinstance(request, SynthesisRequest):
            raise TypeError("TTS finalization request must be a SynthesisRequest")
        self._service().finalization_fence(request, cancellation, capacity_reservation)

    def synth_to_wav(
        self,
        text: str,
        out_wav: Path,
        *,
        purpose: str = "routine",
        deadline_at: dt.datetime | None = None,
        cancellation: Event | None = None,
    ) -> None:
        result = self.synthesize(
            text,
            out_wav,
            purpose=purpose,
            deadline_at=deadline_at,
            cancellation=cancellation,
        )
        from .models import SynthesisDisposition

        if result.disposition not in {SynthesisDisposition.SUCCEEDED, SynthesisDisposition.LKG_REUSED}:
            raise TTSCompatibilityError(result)
        if not Path(out_wav).is_file():
            raise TTSCompatibilityError(result)

    def close(self) -> None:
        """Close provider resources owned by this prepared TTS instance."""

        if self._synthesis_service is None:
            return
        for adapter in self._synthesis_service.provider_adapters:
            adapter.close()


def _config_values(config: object) -> dict[str, Any]:
    """Copy only provider configuration leaves; never retain mutable config."""

    names = (
        "base_url",
        "client_credential_file",
        "api_key_file",
        "voice",
        "profile",
        "model",
        "response_format",
        "speed",
        "token_ttl_seconds",
        "refresh_margin_seconds",
        "connect_timeout_seconds",
        "token_timeout_seconds",
        "synthesis_timeout_seconds",
        "max_input_bytes",
        "max_response_bytes",
        "max_error_bytes",
        "verify_tls",
    )
    return {name: getattr(config, name) for name in names if hasattr(config, name)}
