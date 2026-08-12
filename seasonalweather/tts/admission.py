"""P1-14 reusable admission bridge for backend-neutral TTS requests."""

from __future__ import annotations

import datetime as dt
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, cast

from seasonalweather.capabilities.qualification import QualificationReason, qualify
from seasonalweather.capabilities.models import CapabilityRecord, CompatibilityState, OperationalState
from seasonalweather.capabilities.manifest import CapabilityManifest
from seasonalweather.capabilities.registry import CapabilityRegistry
from seasonalweather.jobs.registry import policy_for as job_policy_for
from seasonalweather.jobs.policies import JobType
from seasonalweather.validation.admission import AdmissionRejection, admission_error, tts_field

from .local import LocalEngineRegistry
from .models import (
    BackendId,
    LocalEngineOptions,
    MAX_SYNTHESIS_TEXT,
    SynthesisRequest,
    TextOverride,
    VoiceTextOptions,
)
from .subprocess import ProcessFailure


class LocalQualificationDisposition(StrEnum):
    SATISFIED = "satisfied"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    INCOMPATIBLE = "incompatible"
    UNKNOWN = "stale_or_unknown"
    NO_CAPACITY = "no_capacity"


@dataclass(frozen=True)
class LocalQualification:
    disposition: LocalQualificationDisposition
    capability: str
    evidence: tuple[str, ...] = ()
    effective_capacity: int = 1


class ControllerLocalPublicationFence:
    """Shared lock for controller-local source replacement and publication."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    @contextmanager
    def generation_publication(self, expected: int | None, current: Callable[[], int | None] | None):
        with self._lock:
            yield current is None or expected == current()

    @contextmanager
    def hold(self):
        with self._lock:
            yield


class P109TtsQualificationAdapter:
    """Narrow read-only TTS port over the controller P1-09 registry.

    The registry remains the sole owner of freshness, compatibility, health,
    and effective capacity.  A missing registry is represented as stale/
    unknown; it is never silently promoted to a satisfied local capability.
    """

    def __init__(
        self,
        registry: CapabilityRegistry | None,
        clock: Callable[[], dt.datetime],
        *,
        local_source: ControllerLocalQualificationSource | None = None,
    ) -> None:
        self.registry = registry
        self.clock = clock
        self.local_source = local_source

    def __call__(self, request: object, capability: str) -> LocalQualification:
        context = self._qualification_context(request, capability)
        if isinstance(context, LocalQualification):
            return context
        engine = context
        typed_request = cast(SynthesisRequest, request)
        if self.registry is None:
            return LocalQualification(
                LocalQualificationDisposition.UNKNOWN,
                capability,
                ("p1_09_registry_unbound",),
                0,
            )
        if self.local_source is None:
            return LocalQualification(
                LocalQualificationDisposition.UNKNOWN,
                capability,
                ("controller_local_qualification_source_unbound",),
                0,
            )
        local_snapshot = self.local_source.refresh(engine)
        if local_snapshot is None:
            return LocalQualification(
                LocalQualificationDisposition.INCOMPATIBLE,
                capability,
                ("controller_local_profile_not_configured_for_request",),
                0,
            )
        now = self.clock()
        self.registry.tick(now)
        policy = job_policy_for(JobType.TTS_SYNTHESIZE)
        results = self._qualification_results(request, capability, engine, policy, now, (local_snapshot,))
        return self._select_qualification(results, capability)

    def reserve(self, request: object, reservation_id: str, *, expires_at: dt.datetime) -> object | None:
        """Atomically reserve the already-qualified controller-local profile."""

        if not isinstance(request, SynthesisRequest):
            raise ProcessFailure("capability_rejected", "invalid TTS request")
        try:
            requested_engine = LocalEngineRegistry.normalize(request.local.engine)
            requested_capability = LocalEngineRegistry.capability_for(requested_engine)
        except (ProcessFailure, ValueError) as exc:
            raise ProcessFailure("capability_rejected", "invalid local TTS profile") from exc
        context = self._qualification_context(request, requested_capability)
        if isinstance(context, LocalQualification):
            if context.disposition is LocalQualificationDisposition.NO_CAPACITY:
                return None
            raise ProcessFailure("capability_rejected", "controller-local TTS profile is not qualified")
        return self._reserve_qualified(request, context, reservation_id, expires_at)

    def _reserve_qualified(
        self, request: SynthesisRequest, engine: str, reservation_id: str, expires_at: dt.datetime
    ) -> object | None:
        capability = LocalEngineRegistry.capability_for(engine)
        if self.registry is None or self.local_source is None:
            raise ProcessFailure("capability_rejected", "controller-local TTS authority is unavailable")
        snapshot = self.local_source.refresh(engine)
        if snapshot is None:
            raise ProcessFailure("capability_rejected", "controller-local TTS profile is stale")
        now = self.clock()
        self.registry.tick(now)
        policy = job_policy_for(JobType.TTS_SYNTHESIZE)
        selected = self._qualification_results(
            request,
            capability,
            engine,
            policy,
            now,
            (snapshot,),
        )
        if not selected:
            raise ProcessFailure("capability_rejected", "controller-local TTS profile is unavailable")
        _snapshot, qualified = selected[0]
        if not qualified.qualified:
            if qualified.reason is QualificationReason.NO_CAPACITY:
                return None
            raise ProcessFailure("capability_rejected", "controller-local TTS profile is not qualified")
        try:
            return self.registry.reserve_controller_local(
                worker_id=self.local_source.worker_id,
                reservation_id=reservation_id,
                job_id=request.job_id or reservation_id,
                capability_names=(capability,),
                now=now,
                expires_at=expires_at,
            )
        except RuntimeError as exc:
            if "capacity" in str(exc):
                return None
            raise ProcessFailure("capability_rejected", "controller-local TTS reservation is stale") from exc

    def release(self, reservation: object) -> None:
        worker_id = getattr(reservation, "worker_id", None)
        reservation_id = getattr(reservation, "reservation_id", None)
        if worker_id and reservation_id and self.registry is not None:
            self.registry.release_reservation(str(worker_id), str(reservation_id))

    def for_reservation(self, request: object, capability: str, reservation: object) -> LocalQualification:
        """Re-fence a request while crediting its own P1-09 reservation."""

        if not isinstance(request, SynthesisRequest) or self.registry is None or self.local_source is None:
            raise ProcessFailure("capability_rejected", "controller-local reservation authority is unavailable")
        engine = self._qualification_context(request, capability)
        if not isinstance(engine, str):
            raise ProcessFailure("capability_rejected", "controller-local reservation does not match the request")
        source = self.local_source
        worker_id = getattr(reservation, "worker_id", None)
        reservation_id = getattr(reservation, "reservation_id", None)
        if source is None or worker_id != source.worker_id or not reservation_id:
            raise ProcessFailure("capability_rejected", "controller-local reservation identity is invalid")
        return self._reservation_qualification(request, capability, engine, str(reservation_id))

    def _reservation_qualification(
        self, request: SynthesisRequest, capability: str, engine: str, reservation_id: str
    ) -> LocalQualification:
        registry = self.registry
        source = self.local_source
        if registry is None or source is None:
            raise ProcessFailure("capability_rejected", "controller-local reservation authority is unavailable")
        if source.current_generation is not None:
            if source.configuration_generation != source.current_generation():
                raise ProcessFailure("stale_result", "controller-local reservation generation is stale")
        now = self.clock()
        # Capacity ownership is not a health lease.  Advance authoritative
        # freshness first, then refresh/re-publish controller-local evidence
        # when the configured executor can provide it.  Both operations retain
        # the existing reservation and its accounting.
        registry.tick(now)
        source.refresh(engine)
        now = self.clock()
        snapshot = registry.controller_local_reservation_snapshot(
            worker_id=source.worker_id,
            reservation_id=reservation_id,
            capability=capability,
            now=now,
        )
        policy = job_policy_for(JobType.TTS_SYNTHESIZE)
        selected = self._qualification_results(request, capability, engine, policy, now, (snapshot,))
        if not selected or not selected[0][1].qualified:
            raise ProcessFailure("capability_rejected", "controller-local reservation is no longer qualified")
        return self._select_qualification(selected, capability)

    @staticmethod
    def _select_qualification(results, capability: str) -> LocalQualification:
        if not results:
            return LocalQualification(LocalQualificationDisposition.UNKNOWN, capability, ("p1_09_no_snapshot",), 0)
        snapshot, selected = sorted(
            results,
            key=lambda item: (-int(item[1].qualified), -item[1].effective_capacity, item[1].worker_id),
        )[0]
        disposition = {
            QualificationReason.QUALIFIED: LocalQualificationDisposition.SATISFIED,
            QualificationReason.DEGRADED_NOT_ACCEPTING: LocalQualificationDisposition.DEGRADED,
            QualificationReason.NO_CAPACITY: LocalQualificationDisposition.NO_CAPACITY,
            QualificationReason.INCOMPATIBLE: LocalQualificationDisposition.INCOMPATIBLE,
            QualificationReason.UNKNOWN_OR_STALE: LocalQualificationDisposition.UNKNOWN,
            QualificationReason.SCHEMA_MISMATCH: LocalQualificationDisposition.INCOMPATIBLE,
            QualificationReason.PARAMETER_MISMATCH: LocalQualificationDisposition.INCOMPATIBLE,
        }.get(selected.reason, LocalQualificationDisposition.UNAVAILABLE)
        record = next((item for item in snapshot.records if item.name == capability), None)
        if selected.qualified and record is not None and record.operational_state is OperationalState.DEGRADED:
            disposition = LocalQualificationDisposition.DEGRADED
        return LocalQualification(
            disposition,
            capability,
            selected.evidence + (f"reason={selected.reason.value}",),
            selected.effective_capacity if selected.qualified else 0,
        )

    @staticmethod
    def _qualification_context(
        request: object,
        capability: str,
    ) -> str | LocalQualification:
        if not isinstance(request, SynthesisRequest):
            return LocalQualification(LocalQualificationDisposition.UNKNOWN, capability, ("invalid_request",), 0)
        try:
            engine = LocalEngineRegistry.normalize(request.local.engine)
            expected = LocalEngineRegistry.capability_for(engine)
        except (ProcessFailure, ValueError):
            return LocalQualification(
                LocalQualificationDisposition.INCOMPATIBLE,
                capability,
                ("invalid_local_profile",),
                0,
            )
        if request.backend is not BackendId.LOCAL and request.fallback_backend is not BackendId.LOCAL:
            return LocalQualification(LocalQualificationDisposition.UNKNOWN, capability, ("remote_deferred",), 0)
        if capability != expected:
            return LocalQualification(
                LocalQualificationDisposition.INCOMPATIBLE,
                capability,
                (f"expected_capability={expected}",),
                0,
            )
        return engine

    def _qualification_results(self, request, capability, engine, policy, now, snapshots):
        requirement = policy.capabilities[0].model_copy(
            update={
                "name": capability,
                "parameters": {
                    "format": request.output.format,
                    "profiles": engine,
                    "voices": request.local.voice,
                    "sample_rates": request.output.sample_rate_hz,
                    "max_input_bytes": MAX_SYNTHESIS_TEXT,
                },
            }
        )
        return tuple(
            (
                snapshot,
                qualify(
                    snapshot.qualification_view(),
                    job_type=JobType.TTS_SYNTHESIZE,
                    payload_schema_version=policy.payload_schema_version,
                    result_schema_version=policy.result_schema_version,
                    requirements=(requirement,),
                ),
            )
            for snapshot in snapshots
        )


class ControllerLocalQualificationSource:
    """Ephemeral P1-09 snapshot publisher for the embedded local executor."""

    worker_id = "controller-local-tts"

    def __init__(
        self,
        registry: CapabilityRegistry,
        clock: Callable[[], dt.datetime],
        *,
        configured_options: LocalEngineOptions | None = None,
        configuration_generation: int | None = None,
        current_generation: Callable[[], int | None] | None = None,
        publication_fence: ControllerLocalPublicationFence | None = None,
    ) -> None:
        self.registry = registry
        self.clock = clock
        self._epoch = 0
        self._epoch_lock = threading.Lock()
        # This snapshot is controller-composed configuration, never request
        # data.  A generation guard prevents an overtaken resource from
        # republishing its old profile after a selective reload.
        self.configured_options = configured_options or LocalEngineOptions()
        self.configuration_generation = configuration_generation
        self.current_generation = current_generation
        self.publication_fence = publication_fence or ControllerLocalPublicationFence()

    def refresh(self, engine: str | SynthesisRequest, *_legacy_request: object):
        if isinstance(engine, SynthesisRequest) and _legacy_request:
            engine_name = str(_legacy_request[0])
        elif isinstance(engine, str):
            engine_name = engine
        else:
            return None
        if self.current_generation is not None and self.configuration_generation != self.current_generation():
            return None
        configured_engine = LocalEngineRegistry.normalize(self.configured_options.engine)
        if LocalEngineRegistry.normalize(engine_name) != configured_engine:
            return None
        with self._epoch_lock:
            with self.publication_fence.generation_publication(
                self.configuration_generation, self.current_generation
            ) as eligible:
                if not eligible:
                    return None
                prior = self.registry.snapshot(self.worker_id, self.clock())
                self._epoch = max(self._epoch, prior.epoch if prior is not None else 0) + 1
                capability = LocalEngineRegistry.capability_for(engine_name)
                evidence = LocalEngineRegistry.qualification_evidence(engine_name, self.configured_options)
                now = self.clock()
                record = CapabilityRecord(
                    name=capability,
                    implemented=evidence.implemented,
                    compatibility=CompatibilityState.COMPATIBLE,
                    operational_state=OperationalState(evidence.operational_state),
                    accepting_new_jobs=evidence.accepting_new_jobs,
                    total_capacity=evidence.total_capacity,
                    reported_available=evidence.reported_available,
                    job_restrictions=(JobType.TTS_SYNTHESIZE.value,),
                    parameters=evidence.parameters,
                    validity_seconds=60,
                    observed_at=now,
                    published_at=now,
                )
                manifest = CapabilityManifest.create(epoch=self._epoch, records=(record,))
                policy = job_policy_for(JobType.TTS_SYNTHESIZE)
                return self.registry.publish_controller_local(
                    worker_id=self.worker_id,
                    manifest=manifest,
                    authorized_capabilities=frozenset({capability}),
                    authorized_job_types=frozenset({JobType.TTS_SYNTHESIZE}),
                    payload_versions={JobType.TTS_SYNTHESIZE: policy.payload_schema_version},
                    result_versions={JobType.TTS_SYNTHESIZE: policy.result_schema_version},
                    now=now,
                )


def local_options_from_configuration(configuration: object) -> LocalEngineOptions:
    """Build the embedded executor profile from controller configuration."""

    tts = getattr(configuration, "tts")
    local = getattr(tts, "local")
    vtp = getattr(local, "voicetext_paul")

    def overrides(name: str, replacement: str) -> tuple[TextOverride, ...]:
        return tuple(
            TextOverride(
                match=str(item.get("match", "")),
                replace=str(item.get(replacement, "")),
                regex=bool(item.get("regex", False)),
                ignore_case=bool(item.get("ignore_case", False)),
            )
            for item in (getattr(vtp, name, []) or [])
        )

    return LocalEngineOptions(
        engine=str(getattr(local, "engine")),
        voice=str(getattr(local, "voice")),
        rate_wpm=int(getattr(local, "rate_wpm")),
        sample_rate_hz=int(getattr(getattr(configuration, "audio"), "sample_rate")),
        volume=float(getattr(tts, "volume", 1.0)),
        voicetext_paul=VoiceTextOptions(
            run_as=str(getattr(vtp, "run_as", "voicetext")),
            retries=int(getattr(vtp, "retries", 1)),
            retry_sleep_ms=int(getattr(vtp, "retry_sleep_ms", 150)),
            reset_every=int(getattr(vtp, "reset_every", 0)),
            kill_before=bool(getattr(vtp, "kill_before", False)),
            vtml_lexicon=bool(getattr(vtp, "vtml_lexicon", True)),
            alias_overrides=overrides("alias_overrides", "alias"),
            phoneme_overrides_x_cmu=overrides("phoneme_overrides_x_cmu", "ph"),
        ),
    )


def transitional_local_qualification(request: SynthesisRequest, capability: str) -> LocalQualification:
    """Explicit direct-controller qualification using P1-09 vocabulary.

    Production composition supplies a resource/health probe at this same
    port. This default is only the accepted transitional in-process model; it
    is not a competing registry and retains no health state.
    """

    del request
    return LocalQualification(
        disposition=LocalQualificationDisposition.SATISFIED,
        capability=capability,
        evidence=("controller_local_in_process",),
        effective_capacity=1,
    )


def qualify_with_probe(
    request: SynthesisRequest,
    capability: str,
    probe: Callable[[SynthesisRequest, str], LocalQualification],
) -> LocalQualification:
    """Use the injected P1-09 qualification port and mapped identity."""

    decision = probe(request, capability)
    if decision.capability != capability:
        raise ValueError("capability qualification identity does not match the registry mapping")
    return decision


def _validate_tts_shape(request: SynthesisRequest, now: dt.datetime | None) -> AdmissionRejection | None:
    if request.deadline_at <= (now or dt.datetime.now(dt.UTC)):
        return _rejection("invalid_deadline", ("deadline_at",), "TTS synthesis deadline has already expired.")
    if request.output.format != "wav":
        return _rejection(
            "unsupported_output_format", ("output", "format"), "TTS synthesis currently admits WAV output only."
        )
    if len(request.text.encode("utf-8")) > MAX_SYNTHESIS_TEXT:
        return _rejection("input_limit", ("text",), "TTS input exceeds its bounded UTF-8 size limit.")
    return None


def _validate_tts_fallback(request: SynthesisRequest, fallback_viability: bool | None) -> AdmissionRejection | None:
    if request.backend is BackendId.LOCAL and request.fallback_backend is not None:
        return _rejection(
            "unsupported_fallback_direction",
            ("fallback_backend",),
            "Local synthesis cannot select a remote fallback.",
        )
    if fallback_viability is False:
        return _rejection(
            "fallback_unavailable", ("fallback_backend",), "The configured TTS fallback is not currently viable."
        )
    if request.backend is not BackendId.LOCAL and request.fallback_backend not in {None, BackendId.LOCAL}:
        return _rejection(
            "unsupported_fallback",
            ("fallback_backend",),
            "The current fallback policy permits only remote primary to local fallback.",
        )
    return None


def _validate_local_tts_profile(request: SynthesisRequest) -> AdmissionRejection | None:
    if request.backend is not BackendId.LOCAL and request.fallback_backend is not BackendId.LOCAL:
        return None
    try:
        engine = LocalEngineRegistry.normalize(request.local.engine)
        LocalEngineRegistry.validate_voice(engine, request.local.voice)
    except (ValueError, ProcessFailure):
        return _rejection(
            "unsupported_engine",
            ("local", "engine"),
            "The local engine or voice profile is not supported.",
        )
    return None


def _validate_tts_qualification(request: SynthesisRequest, qualification: object | None) -> AdmissionRejection | None:
    if qualification is None or (request.backend is not BackendId.LOCAL and request.fallback_backend is not None):
        return None
    raw_disposition = getattr(qualification, "disposition", None)
    disposition = str(getattr(raw_disposition, "value", raw_disposition) or "")
    qualified = getattr(qualification, "qualified", None)
    capacity = getattr(qualification, "effective_capacity", 1)
    if disposition in {"satisfied", "degraded", "degraded_fallback"} or qualified is True:
        if capacity > 0:
            return None
    return _rejection(
        "capability_unavailable",
        ("backend",),
        "The current P1-09 capability qualification cannot admit TTS synthesis.",
    )


def validate_synthesis_request(
    request: SynthesisRequest,
    *,
    qualification: object | None = None,
    now: dt.datetime | None = None,
    fallback_viability: bool | None = None,
) -> AdmissionRejection | None:
    """Return one bounded typed diagnostic before engine execution.

    ``qualification`` is evidence supplied by P1-09; this function only
    translates it into P1-14 admission diagnostics and never owns its state.
    """

    for check in (
        _validate_tts_shape(request, now),
        _validate_tts_fallback(request, fallback_viability),
        _validate_local_tts_profile(request),
        _validate_tts_qualification(request, qualification),
    ):
        if check is not None:
            return check
    return None


def _rejection(reason: str, field: tuple[str, ...], message: str) -> AdmissionRejection:
    return AdmissionRejection(
        reason_code=reason,
        message=message,
        issue=admission_error(
            tts_field(*field),
            message=message,
            help_text="Correct the bounded TTS request and select a supported local profile.",
        ),
    )
