"""Backend-neutral synthesis service and common local finalization."""

from __future__ import annotations

import datetime as dt
import hashlib
import inspect
import json
import os
import re
import shutil
import struct
import tempfile
import time
import wave
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import cast

from ..artifacts.hashing import ContentIdentity
from ..artifacts.media import WavPolicy, inspect_wav
from ..artifacts.models import MediaMetadata
from .adapters.base import ProviderAdapter
from .adapters.models import ProviderAudio
from .admission import LocalQualification, LocalQualificationDisposition, validate_synthesis_request
from .cancellation import deadline_expired, explicit_cancellation
from .local import LocalEngineRegistry, VoiceTextPaulHandler, _InvocationCounter
from .models import (
    AcceptedArtifactReference,
    ArtifactEvidence,
    BackendId,
    FallbackMetadata,
    FinalizationCallbackError,
    FinalizationContext,
    LastKnownGoodCandidate,
    SynthesisDisposition,
    SynthesisFailure,
    SynthesisRequest,
    SynthesisResult,
)
from .policy import SynthesisPurposePolicy, policy_for
from .preprocess import PREPROCESSING_VERSION, preprocess_text
from .subprocess import ProcessFailure, resolve_trusted_executable, run_bounded

CapabilityCheck = Callable[[SynthesisRequest, str], object]
GenerationCheck = Callable[[int | None], bool]
LkgResolver = Callable[[SynthesisRequest], AcceptedArtifactReference | None]


@dataclass(frozen=True)
class FinalizationAuthorityEvidence:
    """Bounded live evidence returned by the complete final authority check."""

    configuration_generation: int | None
    capability: str
    qualified: bool = True


def _unbound_p109_qualification(request: SynthesisRequest, capability: str) -> LocalQualification:
    del request
    return LocalQualification(
        disposition=LocalQualificationDisposition.UNKNOWN,
        capability=capability,
        evidence=("p1_09_qualification_port_unbound",),
        effective_capacity=0,
    )


def _resource_available(resource: str) -> bool:
    if resource.startswith("/"):
        return Path(resource).is_file() and os.access(resource, os.X_OK)
    return shutil.which(resource) is not None


def _voicetext_resources_available(request: SynthesisRequest) -> bool:
    state_base = Path(
        request.local.voicetext_paul.data_base or os.getenv("SEASONALWEATHER_DATA_BASE", "/var/lib/seasonalweather")
    )
    engine_root = Path(
        os.getenv(
            "VOICETEXT_PAUL_ENGINE_ROOT",
            str(state_base / "voices/voicetext_paul/WeatherRadioSuite-LIB"),
        )
    )
    engine_dir = Path(os.getenv("VOICETEXT_PAUL_BIN_DIR", str(engine_root / "binary")))
    reset_required = (
        request.local.voicetext_paul.retries > 0
        or request.local.voicetext_paul.kill_before
        or request.local.voicetext_paul.reset_every > 0
    )
    return (
        (engine_dir / "voicetext_paul.exe").is_file()
        and VoiceTextPaulHandler.wrapper_path.is_file()
        and (not reset_required or VoiceTextPaulHandler.reset_path.is_file())
    )


class SynthesisService:
    """One controller-facing boundary for local and configured remote backends."""

    def __init__(
        self,
        *,
        admission_check: Callable[[], None] | None = None,
        activity_context: Callable[[], AbstractContextManager[None]] | None = None,
        capability_check: CapabilityCheck | None = None,
        current_generation: GenerationCheck | None = None,
        lkg_resolver: LkgResolver | None = None,
        provider_adapters: dict[BackendId, ProviderAdapter] | None = None,
    ) -> None:
        self._admission_check = admission_check
        self._activity_context = activity_context
        # A direct service has no authority to assert local capability. Tests
        # may explicitly inject ``transitional_local_qualification``; normal
        # composition injects the controller-owned P1-09 port.
        self._capability_check = capability_check or _unbound_p109_qualification
        self._current_generation = current_generation
        self._lkg_resolver = lkg_resolver
        self._voicetext_counters: dict[int | None, _InvocationCounter] = {}
        self._provider_adapters = dict(provider_adapters or {})

    def availability(self, request: SynthesisRequest) -> tuple[bool, str]:
        if request.backend is not BackendId.LOCAL:
            return self._remote_availability(request)
        return self._local_availability(request)

    def _remote_availability(self, request: SynthesisRequest) -> tuple[bool, str]:
        adapter = self._provider_adapters.get(request.backend)
        if adapter is None or not self._remote_adapter_configured(adapter):
            return False, "remote_backend_unconfigured"
        return True, "tts_available"

    def _local_availability(self, request: SynthesisRequest) -> tuple[bool, str]:
        local_request = self._local_capacity_request(request)
        if local_request is None:
            return False, "backend_unavailable"
        request = local_request
        if not shutil.which("ffmpeg"):
            return False, "ffmpeg_unavailable"
        try:
            engine = LocalEngineRegistry.normalize(request.local.engine)
            LocalEngineRegistry.validate_voice(engine, request.local.voice)
        except ProcessFailure as exc:
            return False, exc.classification
        except ValueError:
            return False, "invalid_input"
        if any(not _resource_available(resource) for resource in LocalEngineRegistry.required_resources(engine)):
            return False, "backend_unavailable"
        if engine == "voicetext_paul" and not _voicetext_resources_available(request):
            return False, "backend_unavailable"
        try:
            self._require_capability(self._capability_check(request, LocalEngineRegistry.capability_for(engine)))
        except ProcessFailure as exc:
            return False, f"capability_{exc.classification}"
        return True, "tts_available"

    @property
    def provider_adapters(self) -> tuple[ProviderAdapter, ...]:
        return tuple(self._provider_adapters.values())

    @staticmethod
    def _local_capacity_request(request: SynthesisRequest) -> SynthesisRequest | None:
        if request.backend is BackendId.LOCAL:
            return request
        return None

    @staticmethod
    def _local_fallback_request(request: SynthesisRequest) -> SynthesisRequest | None:
        if (
            request.backend is not BackendId.LOCAL
            and request.fallback_backend is BackendId.LOCAL
            and policy_for(request.purpose).fallback_allowed
        ):
            return request.model_copy(
                update={"backend": BackendId.LOCAL, "fallback_backend": None, "backend_profile_identity": None}
            )
        return None

    @staticmethod
    def _remote_adapter_configured(adapter: ProviderAdapter) -> bool:
        config = getattr(adapter, "config", None)
        if config is None or not getattr(config, "base_url", ""):
            return False
        required = (
            ("client_credential_file", "voice", "profile")
            if hasattr(config, "client_credential_file")
            else ("api_key_file", "model", "voice")
        )
        return all(bool(getattr(config, name, "")) for name in required)

    def capacity_is_relevant(self, request: SynthesisRequest) -> bool:
        """Return whether the selected policy can execute a local path."""

        return self._local_capacity_request(request) is not None

    def synthesize(
        self,
        request: SynthesisRequest,
        output_path: Path,
        *,
        cancellation: Event | None = None,
        last_known_good: LastKnownGoodCandidate | None = None,
        finalize: Callable[[Path, object, Callable[[], None]], None] | None = None,
        capacity_reservation: object | None = None,
    ) -> SynthesisResult:
        started = time.monotonic()
        cancellation = cancellation or Event()
        if self._admission_check is not None:
            self._admission_check()
        # Let the operation-owned deadline path produce TIMED_OUT rather than
        # converting an already-expired request into a generic input error.
        static_admission = validate_synthesis_request(request)
        if static_admission is not None and static_admission.reason_code != "invalid_deadline":
            return self._failed(request, SynthesisFailure.INVALID_INPUT, started)
        reservation_box: list[object] = [capacity_reservation, False]
        try:
            initial_failure = self._reserve_initial_capacity(request, cancellation, reservation_box, started)
            if initial_failure is not None:
                return initial_failure
            return self._run_with_activity(
                request,
                output_path,
                cancellation,
                last_known_good,
                started,
                finalize,
                reservation_box,
            )
        finally:
            if reservation_box[1] and reservation_box[0] is not None:
                self._release_capacity(reservation_box[0])

    def _reserve_initial_capacity(
        self, request: SynthesisRequest, cancellation: Event, reservation_box: list[object], started: float
    ) -> SynthesisResult | None:
        capacity_request = self._local_capacity_request(request)
        if reservation_box[0] is not None or capacity_request is None:
            return None
        try:
            reservation_box[0] = self._reserve_local_capacity(capacity_request, cancellation)
        except ProcessFailure as error:
            disposition = {
                "cancelled": SynthesisDisposition.CANCELLED,
                "timed_out": SynthesisDisposition.TIMED_OUT,
            }.get(error.classification, SynthesisDisposition.FAILED)
            return self._failed(request, self._failure_class(error), started, disposition=disposition)
        reservation_box[1] = reservation_box[0] is not None
        return None

    def _reserve_local_capacity(self, request: SynthesisRequest | None, cancellation: object) -> object | None:
        if request is None:
            return None
        return self._reserve_capacity(request, cancellation)

    def _run_with_activity(
        self,
        request: SynthesisRequest,
        output_path: Path,
        cancellation: Event,
        last_known_good: LastKnownGoodCandidate | None,
        started: float,
        finalize: Callable[[Path, object, Callable[[], None]], None] | None,
        reservation_box: list[object],
    ) -> SynthesisResult:
        if self._activity_context is not None:
            with self._activity_context():
                return self._synthesize_and_finalize(
                    request, output_path, cancellation, last_known_good, started, finalize, reservation_box
                )
        return self._synthesize_and_finalize(
            request, output_path, cancellation, last_known_good, started, finalize, reservation_box
        )

    def _synthesize_and_finalize(
        self,
        request: SynthesisRequest,
        output_path: Path,
        cancellation: Event,
        last_known_good: LastKnownGoodCandidate | None,
        started: float,
        finalize: Callable[[Path, object, Callable[[], None]], None] | None,
        reservation_box: list[object],
    ) -> SynthesisResult:
        result = self._synthesize(
            request, output_path, cancellation, last_known_good, started, reservation_box[0], reservation_box
        )
        capacity_reservation = reservation_box[0]
        if finalize is not None and result.disposition in {
            SynthesisDisposition.SUCCEEDED,
            SynthesisDisposition.LKG_REUSED,
        }:
            effective_request = request
            if result.fallback is not None and result.fallback.succeeded:
                effective_request = request.model_copy(update={"backend": result.backend, "fallback_backend": None})
            finalization_context = FinalizationContext(
                request=effective_request,
                cancellation=cancellation,
                capacity_reservation=capacity_reservation,
            )
            try:
                finalize(
                    output_path,
                    finalization_context,
                    cast(
                        Callable[[], None],
                        lambda: self.finalization_fence(effective_request, cancellation, capacity_reservation),
                    ),
                )
            except TimeoutError:
                return self._failed(
                    request,
                    SynthesisFailure.DEADLINE_EXPIRED,
                    started,
                    disposition=SynthesisDisposition.TIMED_OUT,
                )
            except ProcessFailure as error:
                disposition = {
                    "cancelled": SynthesisDisposition.CANCELLED,
                    "timed_out": SynthesisDisposition.TIMED_OUT,
                }.get(error.classification, SynthesisDisposition.FAILED)
                return self._failed(
                    request,
                    self._failure_class(error),
                    started,
                    disposition=disposition,
                )
            except FinalizationCallbackError:
                # Controller publication callbacks may carry exact durable
                # ambiguity evidence. Preserve that callback-owned result
                # across the generic media/output translation boundary.
                raise
            except (IndexError, OSError, RuntimeError, ValueError):
                return self._failed(
                    request,
                    SynthesisFailure.OUTPUT_INVALID,
                    started,
                    disposition=SynthesisDisposition.FAILED,
                )
        return result

    def _reserve_capacity(self, request: SynthesisRequest, cancellation: object) -> object | None:
        reserve = getattr(self._capability_check, "reserve", None)
        if reserve is None:
            return None
        reservation_id = f"tts-sync-{id(request):x}"
        while True:
            if explicit_cancellation(cancellation):
                raise ProcessFailure("cancelled", "TTS capacity wait was cancelled")
            deadline = self._operation_deadline(request)
            if deadline_expired(cancellation) or time.monotonic() >= deadline:
                raise ProcessFailure("timed_out", "TTS capacity wait expired")
            try:
                return cast(Callable[..., object | None], reserve)(
                    request,
                    reservation_id,
                    expires_at=request.deadline_at,
                )
            except ProcessFailure:
                raise
            except (RuntimeError, TimeoutError):
                time.sleep(min(0.01, max(0.001, deadline - time.monotonic())))

    def _release_capacity(self, reservation: object | None) -> None:
        release = getattr(self._capability_check, "release", None)
        if reservation is not None and release is not None:
            release(reservation)

    def _operation_deadline(self, request: SynthesisRequest) -> float:
        if request.operation_deadline is not None:
            return request.operation_deadline
        return time.monotonic() + max(0.0, (request.deadline_at - dt.datetime.now(dt.UTC)).total_seconds())

    def finalization_fence(
        self, request: SynthesisRequest, cancellation: object, capacity_reservation: object | None = None
    ) -> FinalizationAuthorityEvidence:
        deadline = self._operation_deadline(request)
        engine = (
            LocalEngineRegistry.normalize(request.local.engine)
            if request.backend is BackendId.LOCAL
            else request.backend.value
        )
        return self._final_acceptance_fence(
            request,
            engine,
            deadline,
            cast(Event, cancellation),
            capacity_reservation,
        )

    def _synthesize(
        self,
        request: SynthesisRequest,
        output_path: Path,
        cancellation: Event,
        last_known_good: LastKnownGoodCandidate | None,
        started: float,
        capacity_reservation: object | None = None,
        reservation_box: list[object] | None = None,
    ) -> SynthesisResult:
        policy = policy_for(request.purpose)
        deadline, early_result = self._deadline_and_early_result(request, cancellation, started)
        if early_result is not None:
            return early_result

        prepared_text: str | None = None
        try:
            primary_engine = (
                LocalEngineRegistry.normalize(request.local.engine) if request.backend is BackendId.LOCAL else None
            )
            capability = (
                LocalEngineRegistry.capability_for(primary_engine)
                if primary_engine is not None
                else request.backend.value
            )
            if request.backend is BackendId.LOCAL:
                self._admit_capability(request, capability, capacity_reservation)
            reused = self._reuse_lkg(
                request,
                output_path,
                cancellation,
                deadline,
                primary_engine,
                last_known_good,
                started,
                policy.last_known_good_allowed,
                capacity_reservation,
            )
            if reused is not None:
                return reused
            prepared_text = self._prepare_text(request, deadline, cancellation)
            if request.backend is BackendId.LOCAL:
                evidence = self._call_local(
                    request,
                    output_path,
                    primary_engine or "",
                    deadline,
                    cancellation,
                    capacity_reservation,
                    prepared_text=prepared_text,
                )
                return self._success(request, evidence, BackendId.LOCAL, primary_engine, started)
            evidence = self._run_remote(request, prepared_text, output_path, deadline, cancellation)
            return self._success(request, evidence, request.backend, request.backend.value, started)
        except ProcessFailure as primary_error:
            return self._handle_failure(
                request,
                primary_error,
                started,
                deadline,
                output_path,
                cancellation,
                policy,
                capacity_reservation,
                prepared_text,
                reservation_box,
            )
        except (ValueError, OSError):
            return self._failed(request, SynthesisFailure.INVALID_INPUT, started)

    def _deadline_and_early_result(
        self, request: SynthesisRequest, cancellation: Event, started: float
    ) -> tuple[float, SynthesisResult | None]:
        deadline = self._operation_deadline(request)
        if deadline <= time.monotonic() or deadline_expired(cancellation):
            return deadline, self._failed(
                request, SynthesisFailure.DEADLINE_EXPIRED, started, disposition=SynthesisDisposition.TIMED_OUT
            )
        if explicit_cancellation(cancellation):
            return deadline, self._failed(
                request, SynthesisFailure.CANCELLED, started, disposition=SynthesisDisposition.CANCELLED
            )
        return deadline, None

    def _admit_capability(
        self, request: SynthesisRequest, capability: str, capacity_reservation: object | None = None
    ) -> object:
        reserved_check = getattr(self._capability_check, "for_reservation", None)
        decision = (
            reserved_check(request, capability, capacity_reservation)
            if capacity_reservation is not None and reserved_check is not None
            else self._capability_check(request, capability)
        )
        admission = validate_synthesis_request(request, qualification=decision)
        if admission is not None and admission.reason_code == "capability_unavailable":
            raise ProcessFailure("capability_rejected", admission.message)
        self._require_capability(decision)
        return decision

    def _reuse_lkg(
        self,
        request: SynthesisRequest,
        output_path: Path,
        cancellation: Event,
        deadline: float,
        engine: str | None,
        candidate: LastKnownGoodCandidate | None,
        started: float,
        allowed: bool,
        capacity_reservation: object | None = None,
    ) -> SynthesisResult | None:
        if candidate is None or not allowed or not self._lkg_matches(request, candidate):
            return None
        evidence = self._copy_lkg(candidate, request, output_path, deadline, cancellation, capacity_reservation)
        return SynthesisResult(
            disposition=SynthesisDisposition.LKG_REUSED,
            purpose=request.purpose,
            backend=request.backend,
            engine=engine,
            configuration_generation=request.configuration_generation,
            content_identity=request.content_identity or "unknown_identity",
            preprocessing_version=PREPROCESSING_VERSION,
            artifact=evidence,
            last_known_good_reused=True,
            source_identity=request.source_identity,
            event_identity=request.event_identity,
            segment_identity=request.segment_identity,
            output_profile_identity=self._output_profile(request),
            freshness_deadline_at=request.deadline_at,
            elapsed_ms=self._elapsed(started),
        )

    def _success(
        self,
        request: SynthesisRequest,
        evidence: ArtifactEvidence,
        backend: BackendId,
        engine: str | None,
        started: float,
    ) -> SynthesisResult:
        return SynthesisResult(
            disposition=SynthesisDisposition.SUCCEEDED,
            purpose=request.purpose,
            backend=backend,
            engine=engine,
            configuration_generation=request.configuration_generation,
            content_identity=request.content_identity or "unknown_identity",
            preprocessing_version=PREPROCESSING_VERSION,
            artifact=evidence,
            source_identity=request.source_identity,
            event_identity=request.event_identity,
            segment_identity=request.segment_identity,
            output_profile_identity=self._output_profile(request),
            freshness_deadline_at=request.deadline_at,
            elapsed_ms=self._elapsed(started),
        )

    def _handle_failure(
        self,
        request: SynthesisRequest,
        primary_error: ProcessFailure,
        started: float,
        deadline: float,
        output_path: Path,
        cancellation: Event,
        policy: SynthesisPurposePolicy,
        capacity_reservation: object | None = None,
        prepared_text: str | None = None,
        reservation_box: list[object] | None = None,
    ) -> SynthesisResult:
        fallback_result, fallback_metadata = self._try_fallback(
            request,
            primary_error,
            started,
            deadline,
            output_path,
            cancellation,
            policy,
            capacity_reservation,
            prepared_text,
            reservation_box,
        )
        if fallback_result is not None:
            return fallback_result
        failure = self._failure_class(primary_error)
        if policy.suppress_on_failure:
            return self._failed(
                request, failure, started, disposition=SynthesisDisposition.SUPPRESSED, fallback=fallback_metadata
            )
        disposition = {
            "cancelled": SynthesisDisposition.CANCELLED,
            "timed_out": SynthesisDisposition.TIMED_OUT,
        }.get(primary_error.classification, SynthesisDisposition.FAILED)
        return self._failed(request, failure, started, disposition=disposition, fallback=fallback_metadata)

    def _try_fallback(
        self,
        request: SynthesisRequest,
        primary_error: ProcessFailure,
        started: float,
        deadline: float,
        output_path: Path,
        cancellation: Event,
        policy: SynthesisPurposePolicy,
        capacity_reservation: object | None = None,
        prepared_text: str | None = None,
        reservation_box: list[object] | None = None,
    ) -> tuple[SynthesisResult | None, FallbackMetadata | None]:
        if not self._fallback_permitted(request, primary_error, policy, cancellation, deadline):
            return None, None
        try:
            engine = LocalEngineRegistry.normalize(request.local.engine)
            fallback_request = self._local_fallback_request(request)
            if fallback_request is None:
                return None, None
            capability = LocalEngineRegistry.capability_for(engine)
            if capacity_reservation is None:
                capacity_reservation = self._reserve_capacity(fallback_request, cancellation)
                if reservation_box is not None:
                    reservation_box[0] = capacity_reservation
                    reservation_box[1] = capacity_reservation is not None
            if capacity_reservation is None and getattr(self._capability_check, "reserve", None) is not None:
                raise ProcessFailure("capability_rejected", "local fallback capacity is unavailable")
            self._admit_capability(fallback_request, capability, capacity_reservation)
            evidence = self._call_local(
                fallback_request,
                output_path,
                engine,
                deadline,
                cancellation,
                capacity_reservation,
                prepared_text=prepared_text,
            )
            metadata = self._fallback_metadata(request, primary_error, True, "explicit_local_fallback", deadline)
            result = self._success(fallback_request, evidence, BackendId.LOCAL, engine, started)
            return result.model_copy(update={"fallback": metadata}), None
        except ProcessFailure as fallback_error:
            return None, self._fallback_metadata(
                request, primary_error, False, self._fallback_evidence(fallback_error), deadline
            )

    @staticmethod
    def _fallback_permitted(
        request: SynthesisRequest,
        primary_error: ProcessFailure,
        policy: SynthesisPurposePolicy,
        cancellation: Event,
        deadline: float,
    ) -> bool:
        return (
            request.fallback_backend is BackendId.LOCAL
            and request.backend is not BackendId.LOCAL
            and policy.fallback_allowed
            and primary_error.classification not in {"cancelled", "timed_out"}
            and not explicit_cancellation(cancellation)
            and time.monotonic() < deadline
        )

    @staticmethod
    def _fallback_metadata(
        request: SynthesisRequest,
        primary_error: ProcessFailure,
        succeeded: bool,
        evidence: str,
        deadline: float,
    ) -> FallbackMetadata:
        return FallbackMetadata(
            primary_backend=request.backend,
            fallback_backend=BackendId.LOCAL,
            reason=SynthesisService._failure_class(primary_error),
            attempted=True,
            succeeded=succeeded,
            deadline_remaining_ms=max(0, min(86_400_000, int((deadline - time.monotonic()) * 1000))),
            capability_evidence=evidence,
        )

    def _run_local(
        self,
        request: SynthesisRequest,
        output_path: Path,
        engine: str,
        deadline: float,
        cancellation: Event,
        capacity_reservation: object | None = None,
        *,
        prepared_text: str | None = None,
    ) -> ArtifactEvidence:
        self._fence(request, deadline, cancellation, "local synthesis admission")
        if self._current_generation is not None and not self._current_generation(request.configuration_generation):
            raise ProcessFailure("stale_result", "configuration generation changed before synthesis")
        text = prepared_text if prepared_text is not None else self._prepare_text(request, deadline, cancellation)
        output_path = Path(output_path)
        if output_path.is_symlink():
            raise ProcessFailure("output_invalid", "TTS output target must not be a symlink")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".tts-", dir=str(output_path.parent)) as raw_root:
            raw_dir = Path(raw_root)
            result = self._invoke_local_handler(request, text, engine, raw_dir, deadline, cancellation)
            normalized, media = self._normalize_local_audio(
                result.output_path, request, raw_dir, deadline, cancellation
            )
            return self._accept_local_audio(
                request,
                engine,
                output_path,
                normalized,
                media,
                raw_dir,
                deadline,
                cancellation,
                capacity_reservation,
            )

    def _call_local(
        self,
        request: SynthesisRequest,
        output_path: Path,
        engine: str,
        deadline: float,
        cancellation: Event,
        capacity_reservation: object | None,
        *,
        prepared_text: str | None,
    ) -> ArtifactEvidence:
        """Call the handler boundary while retaining the P1-16 test facade."""

        parameters = inspect.signature(self._run_local).parameters
        kwargs: dict[str, object] = {}
        if "capacity_reservation" in parameters:
            kwargs["capacity_reservation"] = capacity_reservation
        if "prepared_text" in parameters:
            kwargs["prepared_text"] = prepared_text
        runner = cast(object, self._run_local)
        return cast(Callable[..., ArtifactEvidence], runner)(
            request, output_path, engine, deadline, cancellation, **kwargs
        )

    @staticmethod
    def _prepare_text(request: SynthesisRequest, deadline: float, cancellation: Event) -> str:
        try:
            return preprocess_text(
                request.text,
                request.text_overrides,
                deadline=deadline,
                cancellation=cancellation,
            )
        except ProcessFailure:
            raise
        except (ValueError, re.error) as exc:
            raise ProcessFailure("invalid_input", "common preprocessing rejected the synthesis input") from exc

    def _run_remote(
        self,
        request: SynthesisRequest,
        text: str,
        output_path: Path,
        deadline: float,
        cancellation: Event,
    ) -> ArtifactEvidence:
        self._fence(request, deadline, cancellation, "remote synthesis admission")
        if self._current_generation is not None and not self._current_generation(request.configuration_generation):
            raise ProcessFailure("stale_result", "configuration generation changed before remote synthesis")
        adapter = self._provider_adapters.get(request.backend)
        if adapter is None:
            raise ProcessFailure("backend_unavailable", "selected remote TTS backend is not configured")
        output_path = Path(output_path)
        if output_path.is_symlink():
            raise ProcessFailure("output_invalid", "TTS output target must not be a symlink")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".tts-", dir=str(output_path.parent)) as raw_root:
            raw_dir = Path(raw_root)
            remote = adapter.synthesize(
                request,
                text,
                output_dir=raw_dir,
                deadline=deadline,
                cancellation=cancellation,
            )
            self._validate_remote_audio(request, remote)
            normalized, media = self._normalize_local_audio(Path(remote.path), request, raw_dir, deadline, cancellation)
            return self._accept_local_audio(
                request,
                request.backend.value,
                output_path,
                normalized,
                media,
                raw_dir,
                deadline,
                cancellation,
                None,
            )

    @staticmethod
    def _validate_remote_audio(request: SynthesisRequest, remote: ProviderAudio) -> None:
        """Apply common media authority before any remote result is normalized."""

        if request.backend is not BackendId.SEASONAL_TTSD:
            return
        if remote.format != "wav" or remote.media_type != "audio/wav":
            raise ProcessFailure("unsupported_audio_format", "seasonal_ttsd returned unsupported audio")
        try:
            media = inspect_wav(
                remote.path,
                policy=WavPolicy(
                    maximum_duration_seconds=request.output.maximum_duration_seconds,
                    allowed_sample_widths=(2,),
                    allowed_channels=(2,),
                ),
            )
        except (OSError, ValueError) as error:
            raise ProcessFailure(
                "unsupported_audio_format", "seasonal_ttsd returned nonconforming WAV audio"
            ) from error
        if (
            media.sample_rate_hz != 48_000
            or media.channels != 2
            or media.sample_width_bytes != 2
            or media.frame_count is None
            or media.frame_count < 1
        ):
            raise ProcessFailure("unsupported_audio_format", "seasonal_ttsd returned nonconforming WAV audio")

    def _invoke_local_handler(
        self,
        request: SynthesisRequest,
        text: str,
        engine: str,
        raw_dir: Path,
        deadline: float,
        cancellation: Event,
        capacity_reservation: object | None = None,
    ):
        handler = LocalEngineRegistry.handler(engine)
        if isinstance(handler, VoiceTextPaulHandler):
            counter = self._voicetext_counters.setdefault(
                request.configuration_generation,
                _InvocationCounter(),
            )
            handler.set_invocation_counter(counter)
        options = request.local.model_copy(
            update={"volume": request.output.volume, "sample_rate_hz": request.output.sample_rate_hz}
        )
        try:
            return handler.synthesize(
                text,
                options=options,
                output_dir=raw_dir,
                deadline=deadline,
                cancellation=cancellation,
                volume=request.output.volume,
            )
        except TypeError as exc:
            if "volume" not in str(exc):
                raise
            return handler.synthesize(
                text,
                options=options,
                output_dir=raw_dir,
                deadline=deadline,
                cancellation=cancellation,
            )

    def _accept_local_audio(
        self,
        request: SynthesisRequest,
        engine: str,
        output_path: Path,
        normalized: Path,
        media: MediaMetadata,
        raw_dir: Path,
        deadline: float,
        cancellation: Event,
        capacity_reservation: object | None = None,
    ) -> ArtifactEvidence:
        if self._current_generation is not None and not self._current_generation(request.configuration_generation):
            raise ProcessFailure("stale_result", "configuration generation changed before result completion")
        self._fence(request, deadline, cancellation, "local output hashing")
        try:
            digest = self._hash_bounded(normalized, request.output.maximum_bytes, deadline, cancellation)
        except (ValueError, OSError) as exc:
            raise ProcessFailure("output_invalid", "local engine output failed content validation") from exc
        completed = raw_dir / "completed.wav"
        self._copy_bounded(normalized, completed, deadline, cancellation, "local output staging")
        os.chmod(completed, 0o640)
        # Exactly one authoritative final acceptance fence sits immediately
        # before atomic completion. No failure-producing authority check runs
        # after the replacement.
        self._final_acceptance_fence(request, engine, deadline, cancellation, capacity_reservation)
        os.replace(completed, output_path)
        return _artifact_evidence(digest.sha256, digest.size_bytes, media)

    def _normalize_local_audio(
        self,
        source: Path,
        request: SynthesisRequest,
        raw_dir: Path,
        deadline: float,
        cancellation: Event,
    ) -> tuple[Path, MediaMetadata]:
        self._fence(request, deadline, cancellation, "normalization")
        normalized = raw_dir / "normalized.wav"
        ffmpeg = resolve_trusted_executable("ffmpeg")
        run_bounded(
            [
                ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-ar",
                str(request.output.sample_rate_hz),
                "-ac",
                "2",
                "-c:a",
                "pcm_s16le",
                str(normalized),
            ],
            input_bytes=None,
            deadline=deadline,
            cancellation=cancellation,
        )
        policy = WavPolicy(maximum_duration_seconds=request.output.maximum_duration_seconds)
        try:
            self._fence(request, deadline, cancellation, "WAV validation")
            media = inspect_wav(normalized, policy=policy)
            self._fence(request, deadline, cancellation, "WAV validation")
            if normalized.stat().st_size > request.output.maximum_bytes:
                raise ProcessFailure("output_invalid", "synthesized WAV exceeds its configured size bound")
            if request.output.volume != 1.0:
                adjusted = raw_dir / "volume.wav"
                self._adjust_volume(normalized, adjusted, request.output.volume, deadline, cancellation)
                normalized = adjusted
                self._fence(request, deadline, cancellation, "volume validation")
                media = inspect_wav(normalized, policy=policy)
                self._fence(request, deadline, cancellation, "volume validation")
                if normalized.stat().st_size > request.output.maximum_bytes:
                    raise ProcessFailure("output_invalid", "volume-adjusted WAV exceeds its configured size bound")
        except (ValueError, OSError) as exc:
            raise ProcessFailure("output_invalid", "local engine output failed WAV validation") from exc
        return normalized, media

    @staticmethod
    def _adjust_volume(source: Path, target: Path, volume: float, deadline: float, cancellation: Event) -> None:
        if not 0.0 <= volume <= 2.0:
            raise ProcessFailure("invalid_input", "volume is outside the bounded local policy")
        with wave.open(str(source), "rb") as reader:
            params = reader.getparams()
            if params.sampwidth != 2:
                raise ProcessFailure("output_invalid", "volume adjustment requires PCM16 output")
            with wave.open(str(target), "wb") as writer:
                writer.setparams(params)
                while frames := reader.readframes(8192):
                    if deadline_expired(cancellation) or time.monotonic() >= deadline:
                        raise ProcessFailure("timed_out", "synthesis expired during volume adjustment")
                    if explicit_cancellation(cancellation):
                        raise ProcessFailure("cancelled", "synthesis was cancelled during volume adjustment")
                    samples = bytearray()
                    for (sample,) in struct.iter_unpack("<h", frames):
                        scaled = max(-32768, min(32767, round(sample * volume)))
                        samples.extend(struct.pack("<h", scaled))
                    writer.writeframes(bytes(samples))

    def _copy_lkg(
        self,
        candidate: LastKnownGoodCandidate,
        request: SynthesisRequest,
        output_path: Path,
        deadline: float,
        cancellation: Event,
        capacity_reservation: object | None = None,
    ):
        if self._lkg_resolver is None:
            raise ProcessFailure("lkg_rejected", "last-known-good reuse requires the controller artifact resolver")
        accepted = self._lkg_resolver(request)
        if accepted is None:
            raise ProcessFailure("lkg_rejected", "controller artifact resolver did not return accepted evidence")
        source = Path(accepted.path)
        self._verify_accepted_lkg(accepted, request, source, deadline, cancellation)
        if output_path.is_symlink():
            raise ProcessFailure("output_invalid", "TTS output target must not be a symlink")
        if deadline_expired(cancellation) or time.monotonic() >= deadline:
            raise ProcessFailure("timed_out", "synthesis deadline expired before last-known-good reuse")
        if explicit_cancellation(cancellation):
            raise ProcessFailure("cancelled", "synthesis was cancelled before last-known-good reuse")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=".tts-lkg-", suffix=".wav", dir=output_path.parent, delete=False
        ) as temp:
            temporary = Path(temp.name)
        try:
            self._copy_bounded(source, temporary, deadline, cancellation, "last-known-good reuse")
            self._fence(request, deadline, cancellation, "last-known-good validation")
            media = inspect_wav(
                temporary, policy=WavPolicy(maximum_duration_seconds=request.output.maximum_duration_seconds)
            )
            self._fence(request, deadline, cancellation, "last-known-good validation")
            identity = self._hash_bounded(temporary, request.output.maximum_bytes, deadline, cancellation)
            self._final_acceptance_fence(
                request,
                LocalEngineRegistry.normalize(request.local.engine)
                if request.backend is BackendId.LOCAL
                else request.backend.value,
                deadline,
                cancellation,
                capacity_reservation,
            )
            os.replace(temporary, output_path)
            return _artifact_evidence(identity.sha256, identity.size_bytes, media)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _lkg_matches(request: SynthesisRequest, candidate: LastKnownGoodCandidate) -> bool:
        return bool(
            candidate.validated
            and candidate.content_identity == request.content_identity
            and candidate.purpose is request.purpose
            and candidate.backend is request.backend
            and candidate.preprocessing_version == PREPROCESSING_VERSION
            and candidate.configuration_generation == request.configuration_generation
            and candidate.source_identity == request.source_identity
            and candidate.event_identity == request.event_identity
        )

    def _verify_accepted_lkg(
        self,
        accepted: AcceptedArtifactReference,
        request: SynthesisRequest,
        source: Path,
        deadline: float,
        cancellation: Event,
    ) -> None:
        self._fence(request, deadline, cancellation, "last-known-good admission")
        if source.is_symlink() or not source.is_file():
            raise ProcessFailure("lkg_rejected", "accepted artifact is missing or unsafe")
        self._verify_lkg_metadata(accepted, request)
        self._verify_lkg_evidence(accepted, request, source, deadline, cancellation)

    def _verify_lkg_metadata(self, accepted: AcceptedArtifactReference, request: SynthesisRequest) -> None:
        fences = (
            accepted.content_identity == request.content_identity,
            accepted.purpose is request.purpose,
            accepted.backend is request.backend,
            accepted.preprocessing_version == PREPROCESSING_VERSION,
            accepted.configuration_generation == request.configuration_generation,
            accepted.source_identity == request.source_identity,
            accepted.event_identity == request.event_identity,
            accepted.segment_identity == request.segment_identity,
            accepted.output_profile_identity == self._output_profile(request),
            accepted.freshness_deadline_at >= request.deadline_at,
        )
        if not all(fences):
            raise ProcessFailure("lkg_rejected", "accepted artifact fences do not match the synthesis request")
        if accepted.freshness_deadline_at <= dt.datetime.now(dt.UTC):
            raise ProcessFailure("lkg_rejected", "accepted artifact evidence is stale")

    def _verify_lkg_evidence(
        self,
        accepted: AcceptedArtifactReference,
        request: SynthesisRequest,
        source: Path,
        deadline: float,
        cancellation: Event,
    ) -> None:
        try:
            self._fence(request, deadline, cancellation, "last-known-good validation")
            media = inspect_wav(
                source, policy=WavPolicy(maximum_duration_seconds=request.output.maximum_duration_seconds)
            )
            self._fence(request, deadline, cancellation, "last-known-good validation")
            digest = self._hash_bounded(source, request.output.maximum_bytes, deadline, cancellation)
        except (ValueError, OSError) as exc:
            raise ProcessFailure("lkg_rejected", "accepted artifact evidence could not be verified") from exc
        if digest.sha256 != accepted.artifact.sha256 or digest.size_bytes != accepted.artifact.size_bytes:
            raise ProcessFailure("lkg_rejected", "accepted artifact digest does not match controller evidence")
        if _artifact_evidence(digest.sha256, digest.size_bytes, media) != accepted.artifact:
            raise ProcessFailure("lkg_rejected", "accepted artifact media evidence does not match controller evidence")

    @staticmethod
    def _output_profile(request: SynthesisRequest) -> str:
        if request.backend is BackendId.LOCAL:
            try:
                engine = LocalEngineRegistry.normalize(request.local.engine)
            except (ProcessFailure, ValueError):
                engine = request.local.engine.strip().lower()[:64]
        else:
            engine = "remote"
        profile = {
            "backend": request.backend.value,
            "engine": engine,
            "voice": request.local.voice,
            "rate_wpm": request.local.rate_wpm,
            "sample_rate_hz": request.output.sample_rate_hz,
            "output": {
                "format": request.output.format,
                "maximum_bytes": request.output.maximum_bytes,
                "maximum_duration_seconds": request.output.maximum_duration_seconds,
                "volume": request.output.volume,
            },
            "preprocessing_version": request.preprocessing_version,
            "voicetext": request.local.voicetext_paul.model_dump(mode="json"),
        }
        if request.backend is not BackendId.LOCAL:
            profile["backend_profile_identity"] = request.backend_profile_identity or "unconfigured"
        raw = json.dumps(profile, sort_keys=True, separators=(",", ":"))
        return f"sha256:{hashlib.sha256(raw.encode()).hexdigest()[:32]}"

    def _final_acceptance_fence(
        self,
        request: SynthesisRequest,
        engine: str,
        deadline: float,
        cancellation: Event,
        capacity_reservation: object | None = None,
    ) -> FinalizationAuthorityEvidence:
        """Check the immutable request and live authorities before replace."""

        self._fence(request, deadline, cancellation, "final result acceptance")
        if self._current_generation is not None and not self._current_generation(request.configuration_generation):
            raise ProcessFailure("stale_result", "configuration generation changed before result acceptance")
        capability = (
            LocalEngineRegistry.capability_for(engine) if request.backend is BackendId.LOCAL else request.backend.value
        )
        if request.backend is BackendId.LOCAL:
            reserved_check = getattr(self._capability_check, "for_reservation", None)
            decision = (
                reserved_check(request, capability, capacity_reservation)
                if capacity_reservation is not None and reserved_check is not None
                else self._capability_check(request, capability)
            )
            self._require_capability(decision)
        if request.content_identity is None:
            raise ProcessFailure("stale_result", "synthesis request content identity is missing")
        return FinalizationAuthorityEvidence(
            configuration_generation=request.configuration_generation,
            capability=capability,
        )

    @staticmethod
    def _fence(request: SynthesisRequest, deadline: float, cancellation: Event, stage: str) -> None:
        del request
        if deadline_expired(cancellation) or time.monotonic() >= deadline:
            raise ProcessFailure("timed_out", f"synthesis deadline expired during {stage}")
        if explicit_cancellation(cancellation):
            raise ProcessFailure("cancelled", f"synthesis was cancelled during {stage}")

    @staticmethod
    def _copy_bounded(
        source: Path,
        target: Path,
        deadline: float,
        cancellation: Event,
        stage: str,
    ) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as reader, target.open("wb") as writer:
            while block := reader.read(65_536):
                if deadline_expired(cancellation) or time.monotonic() >= deadline:
                    raise ProcessFailure("timed_out", f"synthesis deadline expired during {stage}")
                if explicit_cancellation(cancellation):
                    raise ProcessFailure("cancelled", f"synthesis was cancelled during {stage}")
                writer.write(block)

    @staticmethod
    def _hash_bounded(
        path: Path,
        maximum_bytes: int,
        deadline: float,
        cancellation: Event,
    ) -> ContentIdentity:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as reader:
            while block := reader.read(65_536):
                if deadline_expired(cancellation) or time.monotonic() >= deadline:
                    raise ProcessFailure("timed_out", "synthesis deadline expired during hashing")
                if explicit_cancellation(cancellation):
                    raise ProcessFailure("cancelled", "synthesis was cancelled during hashing")
                size += len(block)
                if size > maximum_bytes:
                    raise ValueError("artifact exceeds configured size limit")
                digest.update(block)
        if size < 1:
            raise ValueError("artifact must not be empty")
        return ContentIdentity(f"sha256:{digest.hexdigest()}", size)

    @staticmethod
    def _require_capability(decision: object) -> None:
        if decision is False or decision is None:
            raise ProcessFailure("capability_rejected", "TTS capability admission was not satisfied")
        qualified = getattr(decision, "qualified", None)
        if qualified is False:
            raise ProcessFailure("capability_rejected", "TTS capability admission was not satisfied")
        disposition = getattr(decision, "disposition", None)
        if disposition is not None and str(getattr(disposition, "value", disposition)) not in {
            "satisfied",
            "degraded",
            "degraded_fallback",
        }:
            raise ProcessFailure("capability_rejected", "TTS capability admission was not satisfied")
        if getattr(decision, "effective_capacity", 1) <= 0:
            raise ProcessFailure("capability_rejected", "TTS capability has no available capacity")

    @staticmethod
    def _failure_class(error: ProcessFailure) -> SynthesisFailure:
        return {
            "cancelled": SynthesisFailure.CANCELLED,
            "timed_out": SynthesisFailure.DEADLINE_EXPIRED,
            "provider_timed_out": SynthesisFailure.PROVIDER_TIMEOUT,
            "stale_result": SynthesisFailure.STALE_RESULT,
            "unsupported_engine": SynthesisFailure.UNSUPPORTED_ENGINE,
            "unsupported_backend": SynthesisFailure.UNSUPPORTED_BACKEND,
            "backend_unavailable": SynthesisFailure.BACKEND_UNAVAILABLE,
            "capability_rejected": SynthesisFailure.CAPABILITY_REJECTED,
            "input_limit": SynthesisFailure.INVALID_INPUT,
            "invalid_input": SynthesisFailure.INVALID_INPUT,
            "request_rejected": SynthesisFailure.REQUEST_REJECTED,
            "output_limit": SynthesisFailure.PROCESS_OUTPUT_LIMIT,
            "output_invalid": SynthesisFailure.OUTPUT_INVALID,
            "lkg_rejected": SynthesisFailure.LKG_REJECTED,
            "authentication_failed": SynthesisFailure.AUTHENTICATION_FAILED,
            "authorization_failed": SynthesisFailure.AUTHORIZATION_FAILED,
            "rate_limited": SynthesisFailure.RATE_LIMITED,
            "tls_failed": SynthesisFailure.TLS_FAILED,
            "transport_failed": SynthesisFailure.TRANSPORT_FAILED,
            "malformed_response": SynthesisFailure.RESPONSE_MALFORMED,
            "response_too_large": SynthesisFailure.RESPONSE_TOO_LARGE,
            "unsupported_audio_format": SynthesisFailure.UNSUPPORTED_AUDIO_FORMAT,
            "provider_failed": SynthesisFailure.PROVIDER_FAILED,
            "redirect_rejected": SynthesisFailure.REDIRECT_REJECTED,
        }.get(error.classification, SynthesisFailure.PROCESS_FAILED)

    @staticmethod
    def _elapsed(started: float) -> int:
        return max(0, min(86_400_000, int((time.monotonic() - started) * 1000)))

    def _failed(
        self,
        request: SynthesisRequest,
        failure: SynthesisFailure,
        started: float,
        *,
        disposition: SynthesisDisposition = SynthesisDisposition.FAILED,
        fallback: FallbackMetadata | None = None,
    ) -> SynthesisResult:
        return SynthesisResult(
            disposition=disposition,
            purpose=request.purpose,
            backend=request.backend,
            engine=None,
            configuration_generation=request.configuration_generation,
            content_identity=request.content_identity or "unknown_identity",
            preprocessing_version=PREPROCESSING_VERSION,
            failure=failure,
            fallback=fallback,
            elapsed_ms=self._elapsed(started),
            source_identity=request.source_identity,
            event_identity=request.event_identity,
            segment_identity=request.segment_identity,
            output_profile_identity=self._output_profile(request),
            freshness_deadline_at=request.deadline_at,
        )

    @staticmethod
    def _fallback_evidence(error: ProcessFailure) -> str:
        return f"fallback_{error.classification}"[:128]


def _artifact_evidence(sha256: str, size_bytes: int, media: MediaMetadata) -> ArtifactEvidence:
    return ArtifactEvidence(
        sha256=sha256,
        size_bytes=size_bytes,
        media_type=media.media_type,
        sample_rate_hz=cast(int, media.sample_rate_hz),
        channels=cast(int, media.channels),
        frame_count=cast(int, media.frame_count),
        duration_seconds=cast(float, media.duration_seconds),
    )
