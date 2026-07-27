"""Strict, bounded wire-neutral artifact records.  They contain no paths or bytes."""

from __future__ import annotations

import datetime as dt
import re
from enum import StrEnum

from ..validation.modeling import BaseModel, ConfigDict, Field, field_validator

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")
_TOKEN_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class ArtifactClass(StrEnum):
    BLOB = "blob"
    WAV = "wav"


class ArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


def _identity(value: str, name: str) -> str:
    if not _IDENTITY.fullmatch(value):
        raise ValueError(f"{name} must be a bounded opaque identity")
    return value


class MediaMetadata(ArtifactModel):
    media_type: str = Field(min_length=3, max_length=64)
    encoding: str | None = Field(default=None, min_length=2, max_length=32)
    sample_width_bytes: int | None = Field(default=None, ge=1, le=8)
    sample_rate_hz: int | None = Field(default=None, ge=8000, le=192000)
    channels: int | None = Field(default=None, ge=1, le=8)
    frame_count: int | None = Field(default=None, ge=1, le=2_000_000_000)
    duration_seconds: float | None = Field(default=None, gt=0, le=86400)

    @field_validator("media_type")
    @classmethod
    def media_type_is_token(cls, value: str) -> str:
        if not re.fullmatch(r"^[a-z0-9][a-z0-9+.-]{1,31}/[a-z0-9][a-z0-9+.-]{1,31}$", value):
            raise ValueError("media type must be a bounded lower-case MIME token")
        return value

    @field_validator("encoding")
    @classmethod
    def encoding_is_token(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"^[a-z0-9][a-z0-9_.-]{1,31}$", value):
            raise ValueError("encoding must be a bounded lower-case token")
        return value


class ArtifactReference(ArtifactModel):
    artifact_class: ArtifactClass
    staging_namespace: str = Field(min_length=3, max_length=64)
    staging_token: str = Field(min_length=1, max_length=256)
    claimed_sha256: str
    claimed_size_bytes: int = Field(ge=1, le=1_073_741_824)
    media: MediaMetadata

    @field_validator("staging_namespace")
    @classmethod
    def namespace_is_token(cls, value: str) -> str:
        return _identity(value, "staging namespace")

    @field_validator("staging_token")
    @classmethod
    def token_is_safe_relative_path(cls, value: str) -> str:
        if "\\" in value or value.startswith("/") or ":" in value or "//" in value:
            raise ValueError("staging token must be a portable relative token")
        parts = value.split("/")
        if len(parts) > 8 or any(part in {"", ".", ".."} or not _TOKEN_COMPONENT.fullmatch(part) for part in parts):
            raise ValueError("staging token has invalid components")
        return "/".join(parts)

    @field_validator("claimed_sha256")
    @classmethod
    def digest_is_canonical(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("SHA-256 digest must be canonical")
        return value


class ArtifactResult(ArtifactModel):
    job_id: str
    job_type: str
    lease_id: str
    attempt_id: str
    result_schema_version: int = Field(ge=1, le=64)
    configuration_generation: int | None = Field(default=None, ge=0)
    source_identity: str | None = None
    event_identity: str | None = None
    content_identity: str | None = None
    artifact: ArtifactReference
    completed_at: dt.datetime
    provenance: str | None = Field(default=None, min_length=2, max_length=128)

    @field_validator("job_id", "job_type", "lease_id", "attempt_id")
    @classmethod
    def required_identity(cls, value: str, info: object) -> str:
        return _identity(value, getattr(info, "field_name", "identity"))

    @field_validator("source_identity", "event_identity", "content_identity")
    @classmethod
    def optional_identity(cls, value: str | None, info: object) -> str | None:
        return _identity(value, getattr(info, "field_name", "identity")) if value else None

    @field_validator("completed_at")
    @classmethod
    def timestamp_is_utc_aware(cls, value: dt.datetime) -> dt.datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("completed timestamp must be timezone aware")
        return value.astimezone(dt.UTC)
