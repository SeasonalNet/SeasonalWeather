from __future__ import annotations

import re
from enum import StrEnum
from typing import Self

from ..validation.modeling import BaseModel, ConfigDict, Field, field_validator, model_validator

_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")
_KEY_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")


class JobSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


def _validate_ref(value: str, name: str) -> str:
    if not _REF_RE.fullmatch(value):
        raise ValueError(f"{name} must be a bounded opaque reference")
    return value


class SegmentKey(StrEnum):
    STATION_ID = "id"
    STATUS = "status"
    TIME = "time"
    OBSERVATIONS = "observations"
    FORECAST = "forecast"
    HAZARDS = "hazards"


class AudioFormat(StrEnum):
    WAV = "wav"


class AlertMode(StrEnum):
    FULL = "full"
    VOICE = "voice"


class ReconcileTarget(StrEnum):
    STATION_FEED = "station_feed"
    ACTIVE_ALERTS = "active_alerts"
    CYCLE_SEGMENTS = "cycle_segments"
    AUDIO_HOUSEKEEPING = "audio_housekeeping"


class SegmentBuildPayloadV1(JobSchema):
    segment_key: SegmentKey
    context_ref: str
    config_generation: int = Field(ge=0)

    _context_ref = field_validator("context_ref")(lambda value: _validate_ref(value, "context_ref"))


class TtsSynthesisPayloadV1(JobSchema):
    content_ref: str
    voice_profile_ref: str
    output_format: AudioFormat = AudioFormat.WAV
    config_generation: int = Field(ge=0)

    _content_ref = field_validator("content_ref")(lambda value: _validate_ref(value, "content_ref"))
    _profile_ref = field_validator("voice_profile_ref")(lambda value: _validate_ref(value, "voice_profile_ref"))


class AudioConversionPayloadV1(JobSchema):
    input_artifact_ref: str
    target_sample_rate_hz: int = Field(ge=8000, le=192000)
    output_format: AudioFormat = AudioFormat.WAV
    config_generation: int = Field(ge=0)

    _artifact_ref = field_validator("input_artifact_ref")(lambda value: _validate_ref(value, "input_artifact_ref"))


class CycleRegenerationPayloadV1(JobSchema):
    cycle_ref: str
    reason_code: str = Field(min_length=2, max_length=64)
    config_generation: int = Field(ge=0)

    _cycle_ref = field_validator("cycle_ref")(lambda value: _validate_ref(value, "cycle_ref"))

    @field_validator("reason_code")
    @classmethod
    def validate_reason_code(cls, value: str) -> str:
        if not _KEY_RE.fullmatch(value):
            raise ValueError("reason_code must be a bounded declared key")
        return value


class MaintenanceReconcilePayloadV1(JobSchema):
    target: ReconcileTarget
    cursor_ref: str | None = None
    config_generation: int | None = Field(default=None, ge=0)

    @field_validator("cursor_ref")
    @classmethod
    def validate_cursor(cls, value: str | None) -> str | None:
        return _validate_ref(value, "cursor_ref") if value is not None else None


class ConfigValidationPayloadV1(JobSchema):
    candidate_ref: str
    candidate_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate_identity_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    current_generation: int = Field(ge=0)
    preflight_required: bool = True

    _candidate_ref = field_validator("candidate_ref")(lambda value: _validate_ref(value, "candidate_ref"))


class ConfigCommitPayloadV1(JobSchema):
    candidate_ref: str
    validated_result_ref: str
    expected_generation: int = Field(ge=0)

    _candidate_ref = field_validator("candidate_ref")(lambda value: _validate_ref(value, "candidate_ref"))
    _result_ref = field_validator("validated_result_ref")(lambda value: _validate_ref(value, "validated_result_ref"))


class AlertArtifactPayloadV1(JobSchema):
    source_identity: str
    event_identity: str
    content_identity: str
    content_ref: str
    mode: AlertMode
    config_generation: int = Field(ge=0)

    @field_validator("source_identity", "event_identity", "content_identity", "content_ref")
    @classmethod
    def validate_identity(cls, value: str, info: object) -> str:
        return _validate_ref(value, getattr(info, "field_name", "identity"))


class ArtifactResultV1(JobSchema):
    artifact_ref: str
    content_identity: str
    duration_seconds: float | None = Field(default=None, gt=0, le=86400)

    _artifact_ref = field_validator("artifact_ref")(lambda value: _validate_ref(value, "artifact_ref"))
    _content_identity = field_validator("content_identity")(lambda value: _validate_ref(value, "content_identity"))


class SegmentResultV1(JobSchema):
    segment_ref: str
    content_identity: str
    duration_seconds: float = Field(gt=0, le=86400)

    _segment_ref = field_validator("segment_ref")(lambda value: _validate_ref(value, "segment_ref"))
    _content_identity = field_validator("content_identity")(lambda value: _validate_ref(value, "content_identity"))


class ReconcileResultV1(JobSchema):
    target: ReconcileTarget
    inspected_count: int = Field(ge=0, le=1_000_000)
    changed_count: int = Field(ge=0, le=1_000_000)
    continuation_ref: str | None = None

    @field_validator("continuation_ref")
    @classmethod
    def validate_continuation(cls, value: str | None) -> str | None:
        return _validate_ref(value, "continuation_ref") if value is not None else None

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.changed_count > self.inspected_count:
            raise ValueError("changed_count cannot exceed inspected_count")
        return self


class ConfigValidationResultV1(JobSchema):
    candidate_ref: str
    candidate_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate_identity_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    report_ref: str
    report_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    valid: bool
    preflight_ready: bool
    issue_codes: tuple[str, ...] = Field(default_factory=tuple, max_length=64)

    _candidate_ref = field_validator("candidate_ref")(lambda value: _validate_ref(value, "candidate_ref"))
    _report_ref = field_validator("report_ref")(lambda value: _validate_ref(value, "report_ref"))

    @field_validator("issue_codes")
    @classmethod
    def validate_issue_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not (_KEY_RE.fullmatch(item) or re.fullmatch(r"^[A-Z]{4,8}[0-9]{4}$", item)) for item in value):
            raise ValueError("issue codes must be bounded declared keys")
        return value


class ConfigCommitResultV1(JobSchema):
    committed_generation: int = Field(ge=0)
    candidate_ref: str

    _candidate_ref = field_validator("candidate_ref")(lambda value: _validate_ref(value, "candidate_ref"))
