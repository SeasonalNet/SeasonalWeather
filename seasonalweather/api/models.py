from __future__ import annotations

import datetime as dt
import re
from enum import Enum
from typing import Any, Literal

from ..commands.contracts import CommandStatus, CommandType
from ..validation.modeling import BaseModel, ConfigDict, Field, field_validator, model_validator

ALLOWED_MANUAL_EVENT_CODES = {"ADR", "DMO", "RWT", "RMT", "SPS"}
_SAME_RE = re.compile(r"^\d{6}$")
_SENDER_RE = re.compile(r"^[A-Z0-9_-]{3,16}$")
_PRINTABLE_RE = re.compile(r"^[\x20-\x7E\n\r\t]+$")


class VoiceMode(str, Enum):
    VOICE_ONLY = "voice_only"
    FULL_EAS = "full_eas"


class InterruptPolicy(str, Enum):
    INTERRUPT_THEN_REFILL = "interrupt_then_refill"
    QUEUE_AFTER_CURRENT_ALERT = "queue_after_current_alert"


class InsertKind(str, Enum):
    TEXT = "text"
    AUDIO = "audio"


class InsertPlacement(str, Enum):
    AFTER_TIME = "after_time"
    AFTER_STATUS = "after_status"
    END_OF_ROTATION = "end_of_rotation"


class InsertRepeatMode(str, Enum):
    ONCE = "once"
    EVERY_N_ROTATIONS = "every_n_rotations"


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, use_enum_values=True)


class TokenExchangeRequest(ApiModel):
    scopes: list[str] | None = Field(default=None, min_length=1, max_length=32)
    ttl_seconds: int | None = Field(default=None)


class TokenExchangeResponse(ApiModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int
    scopes: list[str]


class TokenRevocationRequest(ApiModel):
    token: str


class TokenRevocationResponse(ApiModel):
    revoked: bool = True


class RebuildCycleRequest(ApiModel):
    reason: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _PRINTABLE_RE.fullmatch(value):
            raise ValueError("reason contains unsupported characters")
        return value


class SetHeightenedModeRequest(ApiModel):
    minutes: int = Field(ge=1, le=240)
    reason: str = Field(min_length=3, max_length=160)

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, value: str) -> str:
        if not _PRINTABLE_RE.fullmatch(value):
            raise ValueError("reason contains unsupported characters")
        return value


class ClearHeightenedModeRequest(ApiModel):
    reason: str | None = Field(default=None, min_length=3, max_length=160)

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _PRINTABLE_RE.fullmatch(value):
            raise ValueError("reason contains unsupported characters")
        return value


class OriginateTestRequest(ApiModel):
    event_code: Literal["RWT", "RMT"]


class OriginateBaseRequest(ApiModel):
    event_code: str = Field(min_length=3, max_length=3)
    headline: str = Field(min_length=1, max_length=160)
    voice_mode: VoiceMode = Field(default=VoiceMode.VOICE_ONLY)
    same_codes: list[str] = Field(default_factory=list, max_length=31)
    sender: str | None = Field(default=None, min_length=3, max_length=16)
    expires_in_minutes: int = Field(default=30, ge=1, le=360)
    interrupt_policy: InterruptPolicy = Field(default=InterruptPolicy.INTERRUPT_THEN_REFILL)
    heightened: bool | None = Field(
        default=None,
        description=(
            "Override heightened-mode behaviour for this origination. "
            "True forces heightened mode on; False suppresses it even if the "
            "station config would normally enable it. "
            "Omit (null) to use the station default (manual_full_eas_heightens)."
        ),
    )

    @field_validator("event_code")
    @classmethod
    def _validate_event_code(cls, value: str) -> str:
        code = "".join(ch for ch in value.upper() if ch.isalnum())
        if len(code) != 3:
            raise ValueError("event_code must be exactly three alphanumeric characters")
        if code not in ALLOWED_MANUAL_EVENT_CODES:
            raise ValueError(f"event_code must be one of: {', '.join(sorted(ALLOWED_MANUAL_EVENT_CODES))}")
        return code

    @field_validator("headline")
    @classmethod
    def _validate_headline(cls, value: str) -> str:
        if not _PRINTABLE_RE.fullmatch(value):
            raise ValueError("headline contains unsupported characters")
        return value

    @field_validator("same_codes")
    @classmethod
    def _validate_same_codes(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for raw in value:
            code = str(raw).strip()
            if not _SAME_RE.fullmatch(code):
                raise ValueError("same_codes must contain only 6-digit SAME/FIPS codes")
            if code in seen:
                continue
            seen.add(code)
            out.append(code)
        return out

    @field_validator("sender")
    @classmethod
    def _validate_sender(cls, value: str | None) -> str | None:
        if value is None:
            return None
        sender = value.strip().upper()
        if not _SENDER_RE.fullmatch(sender):
            raise ValueError("sender must match [A-Z0-9_-]{3,16}")
        return sender

    @model_validator(mode="after")
    def _validate_voice_mode_targeting(self) -> OriginateBaseRequest:
        if self.voice_mode == VoiceMode.FULL_EAS and not self.same_codes:
            raise ValueError("same_codes must be provided for full_eas origination")
        if self.voice_mode == VoiceMode.VOICE_ONLY and self.same_codes:
            raise ValueError("same_codes are only valid for full_eas origination")
        return self


class OriginateTextRequest(OriginateBaseRequest):
    text: str = Field(min_length=1, max_length=4000)

    @field_validator("text")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        if not _PRINTABLE_RE.fullmatch(value):
            raise ValueError("text contains unsupported characters")
        return value.strip()


class OriginateAudioRequest(OriginateBaseRequest):
    audio_asset_id: str = Field(min_length=8, max_length=64)

    @field_validator("audio_asset_id")
    @classmethod
    def _validate_asset_id(cls, value: str) -> str:
        v = value.strip()
        if not re.fullmatch(r"^[A-Za-z0-9_-]{8,64}$", v):
            raise ValueError("audio_asset_id contains unsupported characters")
        return v


class ConfigReloadRequest(ApiModel):
    reason: str | None = Field(default=None, min_length=3, max_length=160)
    dry_run: bool = False
    expected_generation: int | None = Field(default=None, ge=0)
    safe_point_timeout_seconds: float = Field(default=30.0, ge=0.1, le=120.0)
    acknowledgment: ConfigWarningAcknowledgment | None = None

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _PRINTABLE_RE.fullmatch(value):
            raise ValueError("reason contains unsupported characters")
        return value


class ConfigWarningAcknowledgment(ApiModel):
    schema_version: Literal[1] = 1
    actor: str = Field(min_length=1, max_length=128)
    candidate_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate_identity_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    report_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    active_generation: int = Field(ge=0)
    warning_identities: list[str] = Field(min_length=1, max_length=128)
    acknowledged_at: dt.datetime
    validator_completed_at: dt.datetime
    expires_at: dt.datetime
    maximum_age_seconds: Literal[300]
    clock_skew_seconds: Literal[5]

    @field_validator("warning_identities")
    @classmethod
    def _validate_warning_identities(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)) or any(not re.fullmatch(r"^warning:[a-f0-9]{24}$", item) for item in value):
            raise ValueError("warning identities must be an exact sorted set")
        return value

    @field_validator("acknowledged_at", "validator_completed_at", "expires_at")
    @classmethod
    def _validate_acknowledged_at(cls, value: dt.datetime) -> dt.datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("acknowledged_at must include a timezone offset")
        return value


class CommandAccepted(ApiModel):
    command_id: str
    command_type: CommandType
    status: CommandStatus
    accepted_at: dt.datetime
    idempotent_replay: bool = False
    request_id: str
    status_url: str


class CommandSnapshot(ApiModel):
    command_id: str
    command_type: CommandType
    status: CommandStatus
    created_at: dt.datetime
    accepted_at: dt.datetime
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    cancel_requested_at: dt.datetime | None = None
    idempotency_key: str
    actor: str
    reason: str | None = None
    request_id: str
    correlation_id: str | None = None
    idempotent_replay_count: int = 0
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class SegmentSnapshot(ApiModel):
    key: str
    title: str
    enabled: bool
    build_role: str
    builder: str
    refreshable: bool
    refresh_cadence_seconds: int
    maximum_age_seconds: int
    minimum_air_interval_seconds: int
    freshness: Literal["fresh", "stale", "placeholder"]
    stale: bool
    placeholder: bool
    ready: bool
    failure: dict[str, Any]
    provenance: dict[str, Any]


class SegmentListResponse(ApiModel):
    segments: list[SegmentSnapshot]


class CyclePlanResponse(ApiModel):
    mode: str
    normal: list[dict[str, str]]
    focus: list[dict[str, str]]
    dynamic: dict[str, bool]


class CyclePreviewResponse(ApiModel):
    mode: str
    focus: bool
    segments: list[dict[str, Any]]
    read_only: Literal[True] = True


class AudioUploadAccepted(ApiModel):
    asset_id: str
    filename: str
    content_type: str
    duration_seconds: float
    sample_rate_hz: int
    target_sample_rate_hz: int
    channels: int
    sample_width_bytes: int
    frames: int
    normalized: bool = True
    sha256: str
    uploaded_at: str
    expires_at: str


class InsertRepeatRequest(ApiModel):
    mode: InsertRepeatMode = Field(default=InsertRepeatMode.ONCE)
    every_n_rotations: int = Field(default=1, ge=1, le=1440)
    max_airings: int = Field(default=1, ge=1, le=100)

    @model_validator(mode="after")
    def _validate_repeat(self) -> InsertRepeatRequest:
        if self.mode == InsertRepeatMode.ONCE:
            self.every_n_rotations = 1
            self.max_airings = 1
        return self


class CreateInsertBaseRequest(ApiModel):
    title: str = Field(min_length=1, max_length=160)
    placement: InsertPlacement = Field(default=InsertPlacement.AFTER_TIME)
    start_after: dt.datetime | None = None
    expires_at: dt.datetime
    repeat: InsertRepeatRequest = Field(default_factory=InsertRepeatRequest)
    defer_during_active_alerts: bool = True

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        if not _PRINTABLE_RE.fullmatch(value):
            raise ValueError("title contains unsupported characters")
        return value

    @field_validator("start_after", "expires_at")
    @classmethod
    def _validate_datetime(cls, value: dt.datetime | None) -> dt.datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime values must include a timezone offset")
        return value

    @model_validator(mode="after")
    def _validate_window(self) -> CreateInsertBaseRequest:
        if self.start_after is not None and self.expires_at <= self.start_after:
            raise ValueError("expires_at must be after start_after")
        return self


class CreateTextInsertRequest(CreateInsertBaseRequest):
    text: str = Field(min_length=1, max_length=2000)

    @field_validator("text")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        if not _PRINTABLE_RE.fullmatch(value):
            raise ValueError("text contains unsupported characters")
        return value.strip()


class CreateAudioInsertRequest(CreateInsertBaseRequest):
    audio_asset_id: str = Field(min_length=8, max_length=64)

    @field_validator("audio_asset_id")
    @classmethod
    def _validate_asset_id(cls, value: str) -> str:
        v = value.strip()
        if not re.fullmatch(r"^[A-Za-z0-9_-]{8,64}$", v):
            raise ValueError("audio_asset_id contains unsupported characters")
        return v


class CycleInsertSnapshot(ApiModel):
    insert_id: str
    kind: InsertKind
    title: str
    placement: InsertPlacement
    start_after: str | None = None
    expires_at: str
    repeat: dict[str, Any]
    defer_during_active_alerts: bool
    status: str
    actor: str
    created_at: str
    updated_at: str
    last_aired_at: str | None = None
    airing_count: int
    max_airings: int
    duration_seconds: float
    estimated_next_air_at: str | None = None
    estimate_confidence: str | None = None
    estimate_window_seconds: int | None = None
    audio_asset_id: str | None = None


class CycleInsertList(ApiModel):
    inserts: list[CycleInsertSnapshot]


class ProblemDetails(BaseModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    type: str
    title: str
    status: int = Field(ge=100, le=599)
    detail: str | None = None
    instance: str | None = None
    code: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    errors: list[dict[str, Any]] | None = None
    request_id: str
