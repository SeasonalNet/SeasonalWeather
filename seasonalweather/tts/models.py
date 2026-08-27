"""Immutable backend-neutral synthesis contracts.

These models deliberately contain intent and bounded evidence only.  They do
not carry credentials, subprocess details, or controller-owned publication
paths.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..validation.modeling import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_SYNTHESIS_TEXT = 65_536
MAX_OVERRIDE_COUNT = 32
MAX_OVERRIDE_LENGTH = 512
_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{2,127}$")


class SynthesisPurpose(StrEnum):
    ALERT = "alert"
    ROUTINE = "routine"
    OPTIONAL = "optional"
    ADMINISTRATIVE = "administrative"


class BackendId(StrEnum):
    LOCAL = "local"
    SEASONAL_TTSD = "seasonal_ttsd"
    OPENAI_COMPATIBLE = "openai_compatible"


class SynthesisDisposition(StrEnum):
    SUCCEEDED = "succeeded"
    SUPPRESSED = "suppressed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    LKG_REUSED = "lkg_reused"


class SynthesisFailure(StrEnum):
    INVALID_INPUT = "invalid_input"
    REQUEST_REJECTED = "request_rejected"
    UNSUPPORTED_BACKEND = "unsupported_backend"
    UNSUPPORTED_ENGINE = "unsupported_engine"
    CAPABILITY_REJECTED = "capability_rejected"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    OUTPUT_INVALID = "output_invalid"
    PROCESS_FAILED = "process_failed"
    PROCESS_OUTPUT_LIMIT = "process_output_limit"
    DEADLINE_EXPIRED = "deadline_expired"
    PROVIDER_TIMEOUT = "provider_timeout"
    CANCELLED = "cancelled"
    STALE_RESULT = "stale_result"
    FALLBACK_UNAVAILABLE = "fallback_unavailable"
    LKG_REJECTED = "lkg_rejected"
    AUTHENTICATION_FAILED = "authentication_failed"
    AUTHORIZATION_FAILED = "authorization_failed"
    RATE_LIMITED = "rate_limited"
    TLS_FAILED = "tls_failed"
    TRANSPORT_FAILED = "transport_failed"
    RESPONSE_MALFORMED = "malformed_response"
    RESPONSE_TOO_LARGE = "response_too_large"
    UNSUPPORTED_AUDIO_FORMAT = "unsupported_audio_format"
    PROVIDER_FAILED = "provider_failed"
    REDIRECT_REJECTED = "redirect_rejected"


class FinalizationCallbackError(RuntimeError):
    """A finalization callback owns the exact meaning of this exception."""


class TtsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


def _bounded_identity(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    if "\x00" in value or not _IDENTITY_RE.fullmatch(value):
        raise ValueError(f"{name} must be a bounded opaque identity")
    return value


def _utc(value: dt.datetime, name: str) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(dt.UTC)


class TextOverride(TtsModel):
    match: str = Field(min_length=1, max_length=MAX_OVERRIDE_LENGTH)
    replace: str = Field(max_length=MAX_OVERRIDE_LENGTH)
    regex: bool = False
    ignore_case: bool = False

    @field_validator("match", "replace")
    @classmethod
    def no_nul(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("text overrides cannot contain NUL")
        return value


class VoiceTextOptions(TtsModel):
    run_as: str = Field(default="voicetext", min_length=1, max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
    data_base: str = Field(default="", max_length=4096)
    retries: int = Field(default=1, ge=0, le=3)
    retry_sleep_ms: int = Field(default=150, ge=0, le=5_000)
    reset_every: int = Field(default=0, ge=0, le=10_000)
    kill_before: bool = False
    vtml_lexicon: bool = True
    alias_overrides: tuple[TextOverride, ...] = Field(default_factory=tuple, max_length=32)
    phoneme_overrides_x_cmu: tuple[TextOverride, ...] = Field(default_factory=tuple, max_length=32)


class SpfyOptions(TtsModel):
    """Bounded paths for the optional Speechify-compatible local worker."""

    executable: str = Field(default="/opt/spfy/bin/spfy_synth", min_length=1, max_length=4096)
    voice_dir: str = Field(default="/opt/spfy", min_length=1, max_length=4096)

    @field_validator("executable", "voice_dir")
    @classmethod
    def no_nul(cls, value: str) -> str:
        if "\x00" in value or "\r" in value or "\n" in value:
            raise ValueError("spfy paths cannot contain control characters")
        return value


class LocalEngineOptions(TtsModel):
    engine: str = Field(default="espeak-ng", min_length=2, max_length=64)
    voice: str = Field(default="9", min_length=1, max_length=128)
    rate_wpm: int = Field(default=165, ge=40, le=600)
    # Piper's accepted P1-06/P1-16 argv contract uses ``-r`` for the output
    # sample rate.  Keep that value explicit instead of making the handler
    # infer it from an engine-specific global.
    sample_rate_hz: int = Field(default=48_000, ge=8_000, le=192_000)
    # Kept in the backend-neutral local options because DECtalk historically
    # consumed the configured gain itself. Common finalization still applies
    # the authoritative output policy after the handler returns.
    volume: float = Field(default=1.0, ge=0.0, le=2.0)
    voicetext_paul: VoiceTextOptions = Field(default_factory=VoiceTextOptions)
    spfy: SpfyOptions = Field(default_factory=SpfyOptions)

    @field_validator("engine", "voice")
    @classmethod
    def bounded_values(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("local TTS options cannot contain NUL")
        return value


class SynthesisOutputPolicy(TtsModel):
    format: str = Field(default="wav", pattern=r"^[a-z][a-z0-9_.-]{1,15}$")
    sample_rate_hz: int = Field(default=48_000, ge=8_000, le=192_000)
    maximum_bytes: int = Field(default=20 * 1024 * 1024, ge=1, le=1_073_741_824)
    maximum_duration_seconds: float = Field(default=3_600.0, gt=0, le=86_400)
    volume: float = Field(default=1.0, ge=0.0, le=2.0)


class SynthesisRequest(TtsModel):
    purpose: SynthesisPurpose
    backend: BackendId
    fallback_backend: BackendId | None = None
    text: str = Field(min_length=1, max_length=MAX_SYNTHESIS_TEXT)
    content_identity: str | None = None
    backend_profile_identity: str | None = None
    source_identity: str | None = None
    event_identity: str | None = None
    segment_identity: str | None = None
    configuration_generation: int | None = Field(default=None, ge=0)
    deadline_at: dt.datetime
    # Internal monotonic fence populated by the async bridge.  It is excluded
    # from the serialized request and content identity so wall-clock contract
    # identity remains stable while one operation budget crosses the bridge.
    operation_deadline: float | None = Field(default=None, exclude=True, repr=False)
    job_id: str | None = None
    attempt_id: str | None = None
    cancellation_id: str | None = None
    local: LocalEngineOptions = Field(default_factory=LocalEngineOptions)
    output: SynthesisOutputPolicy = Field(default_factory=SynthesisOutputPolicy)
    text_overrides: tuple[TextOverride, ...] = Field(default_factory=tuple, max_length=MAX_OVERRIDE_COUNT)
    preprocessing_version: str = Field(default="tts-preprocess-v1", min_length=3, max_length=32)

    @field_validator("text")
    @classmethod
    def bounded_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("synthesis text must not be empty")
        if "\x00" in value:
            raise ValueError("synthesis text cannot contain NUL")
        return value

    @field_validator(
        "content_identity",
        "backend_profile_identity",
        "source_identity",
        "event_identity",
        "segment_identity",
        "job_id",
        "attempt_id",
        "cancellation_id",
    )
    @classmethod
    def bounded_ids(cls, value: str | None, info: Any) -> str | None:
        return _bounded_identity(value, info.field_name)

    @field_validator("deadline_at")
    @classmethod
    def deadline_is_utc(cls, value: dt.datetime) -> dt.datetime:
        return _utc(value, "deadline_at")

    @model_validator(mode="after")
    def derive_content_identity(self) -> SynthesisRequest:
        if self.fallback_backend is self.backend:
            raise ValueError("fallback backend cannot equal primary backend")
        # Identity is controller-derived from the exact common preprocessing
        # contract. A caller may repeat it for compatibility, but cannot make
        # arbitrary metadata authoritative.
        from .preprocess import preprocess_text

        # Model construction has no operation-owned cancellation token yet.
        # The conservative override policy is the primary bound; this short
        # monotonic budget is a second fence for identity construction.
        normalized = preprocess_text(
            self.text,
            self.text_overrides,
            deadline=time.monotonic() + 1.0,
        )
        identity = content_identity_for(normalized, self.preprocessing_version)
        if self.content_identity is not None and self.content_identity != identity:
            raise ValueError("content_identity does not match the synthesis input")
        object.__setattr__(self, "content_identity", identity)
        return self

    def canonical_json(self) -> str:
        """Return stable JSON with sorted map keys and no caller-owned data."""
        return self.model_dump_json(exclude_none=True, by_alias=False)

    @property
    def purpose_policy(self) -> object:
        """Expose the P1-06-compatible policy without creating a job owner."""

        from .policy import policy_for

        return policy_for(self.purpose)


@dataclass(frozen=True)
class FinalizationContext:
    """Private typed context binding finalization to effective execution."""

    request: SynthesisRequest
    cancellation: object
    capacity_reservation: object | None = None

    def is_set(self) -> bool:
        return bool(getattr(self.cancellation, "is_set", lambda: False)())

    @property
    def cause(self) -> object | None:
        return getattr(self.cancellation, "cause", None)

    def deadline_expired(self) -> bool:
        return bool(getattr(self.cancellation, "deadline_expired", lambda: False)())


class ArtifactEvidence(TtsModel):
    sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1, le=1_073_741_824)
    media_type: str = Field(default="audio/wav", min_length=3, max_length=64)
    sample_rate_hz: int = Field(ge=8_000, le=192_000)
    channels: int = Field(ge=1, le=8)
    frame_count: int = Field(ge=1, le=2_000_000_000)
    duration_seconds: float = Field(gt=0, le=86_400)


class FallbackMetadata(TtsModel):
    primary_backend: BackendId
    fallback_backend: BackendId
    reason: SynthesisFailure
    attempted: bool
    succeeded: bool
    deadline_remaining_ms: int = Field(ge=0, le=86_400_000)
    capability_evidence: str = Field(min_length=1, max_length=128)


class SynthesisResult(TtsModel):
    disposition: SynthesisDisposition
    purpose: SynthesisPurpose
    backend: BackendId
    engine: str | None = Field(default=None, max_length=64)
    configuration_generation: int | None = Field(default=None, ge=0)
    content_identity: str
    preprocessing_version: str = Field(min_length=3, max_length=32)
    artifact: ArtifactEvidence | None = None
    failure: SynthesisFailure | None = None
    fallback: FallbackMetadata | None = None
    last_known_good_reused: bool = False
    source_identity: str | None = None
    event_identity: str | None = None
    segment_identity: str | None = None
    output_profile_identity: str | None = None
    freshness_deadline_at: dt.datetime | None = None
    elapsed_ms: int = Field(ge=0, le=86_400_000)

    @field_validator("content_identity", "source_identity", "event_identity", "segment_identity")
    @classmethod
    def result_ids(cls, value: str | None, info: Any) -> str | None:
        return _bounded_identity(value, info.field_name)

    @field_validator("freshness_deadline_at")
    @classmethod
    def result_deadline_is_utc(cls, value: dt.datetime | None) -> dt.datetime | None:
        return _utc(value, "freshness_deadline_at") if value is not None else None

    @model_validator(mode="after")
    def disposition_invariants(self) -> SynthesisResult:
        successful = self.disposition in {SynthesisDisposition.SUCCEEDED, SynthesisDisposition.LKG_REUSED}
        if successful != (self.artifact is not None):
            raise ValueError("successful synthesis results require exactly one artifact evidence")
        if self.disposition is SynthesisDisposition.FAILED and self.failure is None:
            raise ValueError("failed synthesis results require a bounded failure")
        if self.last_known_good_reused and self.disposition is not SynthesisDisposition.LKG_REUSED:
            raise ValueError("last-known-good reuse must be explicit in disposition")
        return self

    def canonical_json(self) -> str:
        return self.model_dump_json(exclude_none=True, by_alias=False)


class LastKnownGoodCandidate(TtsModel):
    """Legacy metadata shape accepted only as resolver input, never as trust."""

    path: str = Field(min_length=1, max_length=512)
    content_identity: str
    purpose: SynthesisPurpose
    backend: BackendId
    preprocessing_version: str = Field(min_length=3, max_length=32)
    configuration_generation: int | None = Field(default=None, ge=0)
    source_identity: str | None = None
    event_identity: str | None = None
    validated: bool = False

    @field_validator("content_identity", "source_identity", "event_identity")
    @classmethod
    def candidate_ids(cls, value: str | None, info: Any) -> str | None:
        return _bounded_identity(value, info.field_name)


class AcceptedArtifactReference(TtsModel):
    """Controller-owned evidence returned by the P1-10 artifact resolver.

    ``path`` is deliberately absent from the request-side LKG contract. The
    resolver may return a controller-local path, but the service rehashes and
    reparses it before reuse and checks every synthesis fence again.
    """

    artifact_ref: str = Field(min_length=3, max_length=256)
    path: str = Field(min_length=1, max_length=512)
    content_identity: str
    purpose: SynthesisPurpose
    backend: BackendId
    preprocessing_version: str = Field(min_length=3, max_length=32)
    configuration_generation: int | None = Field(default=None, ge=0)
    source_identity: str | None = None
    event_identity: str | None = None
    segment_identity: str | None = None
    output_profile_identity: str = Field(min_length=3, max_length=128)
    artifact: ArtifactEvidence
    freshness_deadline_at: dt.datetime

    @field_validator("content_identity", "source_identity", "event_identity", "segment_identity")
    @classmethod
    def accepted_ids(cls, value: str | None, info: Any) -> str | None:
        return _bounded_identity(value, info.field_name)

    @field_validator("freshness_deadline_at")
    @classmethod
    def accepted_deadline_is_utc(cls, value: dt.datetime) -> dt.datetime:
        return _utc(value, "freshness_deadline_at")


def content_identity_for(normalized_text: str, preprocessing_version: str) -> str:
    """Hash normalized content and the exact preprocessing contract."""

    payload = json.dumps(
        {"preprocessing_version": preprocessing_version, "text": normalized_text},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"
