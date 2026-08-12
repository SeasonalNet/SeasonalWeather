"""Provider-owned configuration and bounded adapter result contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SeasonalTtsdConfig:
    base_url: str = ""
    client_credential_file: str = ""
    voice: str = "voicetext-paul"
    profile: str = "wav-48k-stereo"
    token_ttl_seconds: int = 900
    refresh_margin_seconds: int = 120
    connect_timeout_seconds: float = 5.0
    token_timeout_seconds: float = 10.0
    synthesis_timeout_seconds: float = 180.0
    max_input_bytes: int = 65_536
    max_response_bytes: int = 67_108_864
    max_error_bytes: int = 16_384
    verify_tls: bool = True


@dataclass(frozen=True, slots=True)
class OpenAICompatibleConfig:
    base_url: str = ""
    api_key_file: str = ""
    model: str = ""
    voice: str = ""
    response_format: str = "wav"
    speed: float = 1.0
    connect_timeout_seconds: float = 5.0
    synthesis_timeout_seconds: float = 180.0
    max_input_bytes: int = 65_536
    max_response_bytes: int = 67_108_864
    max_error_bytes: int = 16_384
    verify_tls: bool = True


@dataclass(frozen=True, slots=True)
class ProviderAudio:
    """A provider-owned staged response, never a caller-visible target."""

    path: Path
    media_type: str
    format: str


@dataclass(frozen=True, slots=True)
class _AccessToken:
    value: str = field(repr=False)
    expires_at: float
