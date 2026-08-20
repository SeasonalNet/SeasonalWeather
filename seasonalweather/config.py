"""
SeasonalWeather configuration loader.

config.yaml is the single source of truth for all behaviour.

The only things that belong in the environment (seasonalweather.env) are:
  - NWWS_JID / NWWS_PASSWORD          — XMPP credentials
  - ICECAST_SOURCE_PASSWORD            — Icecast source secret
  - ICECAST_ADMIN_PASSWORD             — Icecast admin secret   (optional)
  - ICECAST_RELAY_PASSWORD             — Icecast relay secret   (optional)
  - SEASONAL_API_TOKEN                 — API bearer token       (optional)
  - SEASONAL_API_TOKENS_JSON           — multi-token JSON blob  (optional)
  - LIQUIDSOAP_TELNET_HOST             — legacy deployment override (optional)
  - LIQUIDSOAP_TELNET_PORT             — legacy deployment override (optional)

Everything else lives in config.yaml.
"""

from __future__ import annotations

import ipaddress
import json
import math
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, cast
from urllib.parse import urlsplit

from .alerts.focus import (
    DEFAULT_EXCLUDED_SOURCES,
    DEFAULT_HOLD_VTEC_SIGNIFICANCE,
    DEFAULT_MARINE_EVENT_CODES,
    DEFAULT_MARINE_HOLD_EVENT_CODES,
    DEFAULT_TEST_EVENT_CODES,
    AlertFocusPolicy,
)
from .auth.policy import KNOWN_API_SCOPES as _KNOWN_API_SCOPES
from .auth.policy import SCOPE_RE as _AUTH_SCOPE_RE
from .broadcast.tests import normalize_postpone_policy
from .configuration.environment import EnvironmentValues
from .configuration.semantic_rules import (
    REMOTE_CONNECT_TIMEOUT_MAX,
    REMOTE_ERROR_BYTES_MAX,
    REMOTE_INPUT_BYTES_MAX,
    REMOTE_RESPONSE_BYTES_MAX,
    REMOTE_SYNTHESIS_TIMEOUT_MAX,
    REMOTE_TOKEN_TIMEOUT_MAX,
    remote_tts_configuration_errors,
)
from .lifecycle import LifecycleTimeouts

_DEFAULT_SWWP_BIND_HOST = str(ipaddress.IPv4Address(0))
_DEFAULT_TEMPORARY_DIR = tempfile.gettempdir()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _nwws_credentials_are_default(jid: str, password: str) -> bool:
    """
    Treat the repo/example NWWS credentials as intentionally disabled.

    Fresh installs start from config/example.env.  Keeping the example values
    should not create an endless XMPP authentication loop.
    """
    jid_norm = (jid or "").strip().lower()
    password_norm = (password or "").strip()
    return jid_norm in {"", "changeme", "changeme@nwws-oi.weather.gov"} or password_norm in {"", "CHANGEME", "changeme"}


def _normalize_ern_decoder_backend(value: Any) -> str:
    raw = str(value or "auto").strip().lower().replace("-", "_")
    if raw in {"auto", "default"}:
        return "auto"
    if raw in {"samedec", "same_dec", "rust"}:
        return "samedec"
    if raw in {"native", "python", "legacy", "internal"}:
        return "native"
    return "auto"


# ---------------------------------------------------------------------------
# Dataclasses — one per logical subsystem
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StationConfig:
    name: str
    service_area_name: str
    timezone: str
    disclaimer: str
    # Governs which broadcast segments are generated.
    # land         - ZFP land zones only, no marine segment
    # coastal      - CWF marine segment only (pure marine station)
    # land_coastal - ZFP + CWF (default for Canonical SeasonalWeather)
    # land_marine  - ZFP + CWF for inland/bay waters (non-ocean)
    # marine       - CWF / marine products only
    deployment_type: str = "land"  # land|coastal|land_coastal|land_marine|marine


@dataclass(frozen=True)
class StreamConfig:
    icecast_host: str
    icecast_port: int
    icecast_mount: str


@dataclass(frozen=True)
class ApiNetworkConfig:
    bind_host: str = "127.0.0.1"
    port: int = 9080


@dataclass(frozen=True)
class LiquidsoapNetworkConfig:
    host: str = "127.0.0.1"
    port: int = 1234
    timeout_seconds: float = 3.0


@dataclass(frozen=True)
class SwwpNetworkConfig:
    bind_host: str = _DEFAULT_SWWP_BIND_HOST
    controller_path: str = "/v1/workers/connect"
    worker_controller_url: str = ""
    verify_tls: bool = True
    heartbeat_interval_seconds: int = 15
    heartbeat_timeout_seconds: int = 45
    lease_seconds: int = 60
    assignment_ack_seconds: int = 10
    max_message_bytes: int = 65536
    capability_validity_seconds: int = 60


@dataclass(frozen=True)
class OptionalServiceNetworkConfig:
    enabled: bool = False
    address: str = ""
    port: int = 0
    database: str = ""
    tls: bool = True
    connect_timeout_seconds: float = 5.0


@dataclass(frozen=True)
class NetworkConfig:
    api: ApiNetworkConfig = field(default_factory=ApiNetworkConfig)
    liquidsoap: LiquidsoapNetworkConfig = field(default_factory=LiquidsoapNetworkConfig)
    swwp: SwwpNetworkConfig = field(default_factory=SwwpNetworkConfig)
    postgresql: OptionalServiceNetworkConfig = field(default_factory=lambda: OptionalServiceNetworkConfig(port=5432))
    redis: OptionalServiceNetworkConfig = field(default_factory=lambda: OptionalServiceNetworkConfig(port=6379))


# --- cycle sub-sections ---


@dataclass(frozen=True)
class CycleSpcConfig:
    enabled: bool
    wfos: List[str]
    days: int
    min_dn: int
    timeout_s: float


@dataclass(frozen=True)
class CycleFcConfig:
    use_short: bool
    periods_normal: int
    periods_per_group: int
    max_points_normal: int
    max_points_7day: int
    point_max_chars: int
    line_max_chars: int
    rotate_period_s: int
    rotate_step: int
    # Ordered list of (zone_id, display_label) pairs.  When non-empty
    # the ZFP /zones/forecast/{zoneId}/forecast path is used instead of
    # gridpoint lat/lon fetches.  Falls back to gridpoint if empty.
    forecast_zones: List[Tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class CycleObsConfig:
    max_normal: int
    rotate_period_s: int
    rotate_step: int
    aliases: Dict[str, str]


@dataclass(frozen=True)
class CycleProductConfig:
    """Generic per-product character-limit config (AFD, SYN, etc.)."""

    max_chars_normal: int
    max_chars_heightened: int


@dataclass(frozen=True)
class CycleHwoConfig:
    max_chars_normal: int
    max_chars_heightened: int
    speak_unavailable: bool


@dataclass(frozen=True)
class CycleCwfConfig:
    # Coastal Waters Forecast segment configuration.
    enabled: bool
    offices: List[str]  # WFO offices to pull CWF from, e.g. ["LWX"]
    max_chars_normal: int  # 0 = unlimited
    max_chars_heightened: int


@dataclass(frozen=True)
class CycleOffnt2Config:
    # Mid-Atlantic Offshore Waters Forecast segment configuration.
    enabled: bool
    source_office: str
    product_type: str
    zones: list[tuple[str, str]]
    include_synopsis: bool
    max_chars_normal: int
    max_chars_heightened: int
    max_airtime_seconds: int
    rotate_period_s: int
    rotate_step: int
    defer_in_heightened: bool


@dataclass(frozen=True)
class CycleRwrConfig:
    # Regional Weather Roundup (RWR) observations segment configuration.
    enabled: bool
    office: str  # WFO to pull RWR from (e.g. 'LWX')
    staleness_minutes: int  # fall back to ASOS if RWR older than this
    # Station names matching the raw RWR product (upper-case) that get
    # full-detail treatment (wind, pressure, dew point, humidity).
    # Empty list = auto-derive from first station in first section.
    anchor_stations: List[str]
    # ASOS station IDs to use when RWR is stale/unavailable.
    # Empty = use observations.stations from the main config.
    fallback_stations: List[str]
    pressure_trend_threshold_inhg: float
    pressure_cache_hours: float
    max_compact_per_section: int
    # Optional spoken-name overrides for RWR station abbreviations.
    # {RAW_UPPER: spoken_name}  e.g. {'DULLES': 'Dulles Airport'}
    station_names: Dict[str, str]


@dataclass(frozen=True)
class CycleMarineObsConfig:
    # Marine observations segment — sourced from the MARINE OBSERVATIONS
    # section already present in the RWR product (no extra API call).
    enabled: bool
    max_stations: int  # 0 = read all available marine stations
    # Raw upper-case station names to always list first, e.g. "THOMAS PT LIGHT"
    anchor_stations: List[str]
    # Optional spoken-name overrides: {RAW_UPPER: spoken_name}
    station_names: Dict[str, str]


@dataclass(frozen=True)
class CycleConfig:
    normal_interval_seconds: int
    heightened_interval_seconds: int
    min_heightened_seconds: int
    lead_time_seconds: int
    alert_focus: AlertFocusPolicy
    reference_points: List[Tuple[float, float, str]]
    last_product_max_chars: int
    spc: CycleSpcConfig
    fc: CycleFcConfig
    obs: CycleObsConfig
    hwo: CycleHwoConfig
    afd: CycleProductConfig
    syn: CycleProductConfig
    cwf: CycleCwfConfig
    offnt2: CycleOffnt2Config
    rwr: CycleRwrConfig
    marine_obs: CycleMarineObsConfig


@dataclass(frozen=True)
class ObservationsConfig:
    stations: List[str]


# --- nwws ---


@dataclass(frozen=True)
class NWWSResiliencyConfig:
    stall_seconds: int
    muc_confirm_seconds: int
    start_wait_seconds: int
    join_wait_seconds: int
    backoff_max_seconds: int
    rx_log_first_n: int
    decision_log_first_n: int
    decision_log_every: int


@dataclass(frozen=True)
class NWWSConfig:
    enabled: bool
    server: str
    port: int
    room: str
    nick: str
    allowed_wfos: List[str]
    resiliency: NWWSResiliencyConfig
    credentials_defaulted: bool = False


# --- nws (api.weather.gov calls) ---


@dataclass(frozen=True)
class NwsConfig:
    user_agent: str


# --- pns ---


@dataclass(frozen=True)
class PnsSubtypeConfig:
    name: str
    enabled: bool
    audio: bool
    event: str
    code: str
    key_prefix: str
    intro: str
    headline_contains: List[str]
    body_contains_all: List[str]
    body_contains_any: List[str]
    reject_contains: List[str]
    max_fresh_hours: float
    require_same_day: bool
    max_chars: int


@dataclass(frozen=True)
class PnsConfig:
    enabled: bool
    default_expire_hours: float
    hard_stop_delimiter: str
    suppress_unknown_audio: bool
    reject_audio_keywords: List[str]
    subtypes: List[PnsSubtypeConfig]


# --- now / short-term forecast ---


@dataclass(frozen=True)
class NowBackfillConfig:
    enabled: bool
    initial_delay_seconds: int
    interval_seconds: int
    lookback_minutes: int
    max_products_per_office: int


@dataclass(frozen=True)
class NowConfig:
    enabled: bool
    intro: str
    default_expire_minutes: int
    api_backfill: NowBackfillConfig


# --- health state machine ---


@dataclass(frozen=True)
class HealthSourceConfig:
    name: str
    enabled: bool
    role: str
    stale_after_seconds: int
    failure_threshold: int
    critical: bool


@dataclass(frozen=True)
class HealthConfig:
    enabled: bool
    check_interval_seconds: int
    min_hold_seconds: int
    detached_loop_only: bool
    source_impaired_message: str
    degraded_message: str
    critical_message: str
    detached_message: str
    sources: List[HealthSourceConfig]


# --- policy ---


@dataclass(frozen=True)
class PolicyConfig:
    toneout_product_types: List[str]
    min_tone_gap_seconds: float


# --- same ---


@dataclass(frozen=True)
class SameNativeEncoderConfig:
    enabled: bool
    bin: str
    timeout_seconds: float
    fallback_to_python: bool


@dataclass(frozen=True)
class SameConfig:
    enabled: bool
    sender: str
    duration_minutes: int
    amplitude: float
    native_encoder: SameNativeEncoderConfig


# --- cap ---


@dataclass(frozen=True)
class CapFullConfig:
    enabled: bool
    severities: List[str]
    events: List[str]
    cooldown_seconds: int


@dataclass(frozen=True)
class CapVoiceConfig:
    enabled: bool
    events: List[str]
    cooldown_seconds: int


@dataclass(frozen=True)
class CapConfig:
    enabled: bool
    dryrun: bool
    poll_seconds: int
    user_agent: str
    url: str
    ledger_path: str
    ledger_max_age_days: int
    full: CapFullConfig
    voice: CapVoiceConfig


# --- ipaws ---


@dataclass(frozen=True)
class IpawsConfig:
    enabled: bool
    dryrun: bool
    poll_seconds: int
    user_agent: str
    url: str
    ledger_path: str
    ledger_max_age_days: int
    # SAME event codes to air with full SAME tones (must also be in policy.toneout_product_types)
    full_events: List[str]
    # SAME event codes to air voice-only (no tones)
    voice_events: List[str]
    # Functional dedupe TTL (seconds) shared with ERN to prevent double-air
    ern_dedup_ttl_seconds: int


# --- ern ---


@dataclass(frozen=True)
class ErnRelayConfig:
    enabled: bool
    events: List[str]
    min_confidence: float
    cooldown_seconds: int
    senders: List[str]


@dataclass(frozen=True)
class ErnConfig:
    enabled: bool
    dryrun: bool
    url: str
    name: str
    decoder_backend: str
    sample_rate: int
    tail_seconds: float
    trigger_ratio: float
    dedupe_seconds: float
    confidence_min: float
    relay: ErnRelayConfig


# --- samedec subprocess ---


@dataclass(frozen=True)
class SameDecConfig:
    bin: str
    confidence: float
    start_delay_s: float


# --- tests (RWT/RMT scheduling) ---


@dataclass(frozen=True)
class TestsGateConfig:
    block_heightened: bool
    block_recent_toneout: bool
    block_recent_severe_cap: bool
    block_recent_ern: bool


@dataclass(frozen=True)
class TestsScheduleConfig:
    weekday: int
    hour: int
    minute: int
    script_lines: tuple[str, ...]  # spoken text lines; empty = use built-in default
    postpone_policy: str
    postpone_minutes: int
    max_postpone_hours: int
    max_postpone_days: int
    gate: TestsGateConfig


@dataclass(frozen=True)
class TestsRmtConfig:
    nth: int
    weekday: int
    hour: int
    minute: int
    script_lines: tuple[str, ...]  # spoken text lines; empty = use built-in default
    postpone_policy: str
    postpone_minutes: int
    max_postpone_hours: int
    max_postpone_days: int
    gate: TestsGateConfig


@dataclass(frozen=True)
class TestsPresentationConfig:
    """Presentation overrides for locally-originated test events."""

    headline_template: str
    area_text: str
    discord_area_text: str


@dataclass(frozen=True)
class TestsConfig:
    enabled: bool
    postpone_policy: str
    postpone_minutes: int
    max_postpone_hours: int
    max_postpone_days: int
    jitter_seconds: int
    toneout_cooldown_seconds: int
    cap_block_seconds: int
    ern_block_seconds: int
    presentation: TestsPresentationConfig
    rwt: TestsScheduleConfig
    rmt: TestsRmtConfig


# --- zonecounty ---


@dataclass(frozen=True)
class ZoneCountyConfig:
    enabled: bool
    dbx_url: str
    cache_days: int
    index_url: str
    base_url: str


# --- mareas ---


@dataclass(frozen=True)
class MareasConfig:
    enabled: bool
    url: str
    cache_days: int


# --- station_feed ---


@dataclass(frozen=True)
class StationFeedHousekeepingConfig:
    enabled: bool
    interval_sec: int
    grace_sec: int
    housekeep_seconds: int


@dataclass(frozen=True)
class StationFeedNwwsConfig:
    vtec_event_labels: dict[str, str]
    tz_abbrev_overrides: dict[str, str]


@dataclass(frozen=True)
class StationFeedConfig:
    enabled: bool
    station_id: str
    source: str
    max_items: int
    ttl_seconds: int
    fetch_nws: bool
    debug: bool
    ern_area_names: bool
    housekeeping: StationFeedHousekeepingConfig
    nwws: StationFeedNwwsConfig


# --- api ---


class AuthMode(str, Enum):
    STATIC = "static"
    EXCHANGE = "exchange"
    HYBRID = "hybrid"


class AuthConfigurationError(ValueError):
    """Bounded, redacted authentication configuration failure."""

    def __init__(
        self,
        *,
        kind: str,
        path: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.kind = kind
        self.path = path
        self.details = dict(details or {})
        super().__init__(message)


@dataclass(frozen=True)
class StaticCredentialConfig:
    token: str = field(repr=False)
    subject: str
    scopes: frozenset[str]


@dataclass(frozen=True)
class ExchangeAuthConfig:
    minimum_ttl_seconds: int = 60
    default_ttl_seconds: int = 900
    maximum_read_ttl_seconds: int = 3600
    maximum_write_ttl_seconds: int = 900


@dataclass(frozen=True)
class ApiAuthConfig:
    mode: AuthMode
    credentials: tuple[StaticCredentialConfig, ...] = field(repr=False)
    exchange: ExchangeAuthConfig
    legacy_mode_normalized: bool
    legacy_scope_normalized: bool


@dataclass(frozen=True)
class ApiConfig:
    auth: ApiAuthConfig
    allow_remote: bool
    audio_max_bytes: int
    audio_max_seconds: int
    audio_ttl_seconds: int
    ffmpeg_bin: str
    full_eas_heightened: bool
    manual_full_eas_heightens: bool


# --- dedupe ---


@dataclass(frozen=True)
class DedupeConfig:
    ttl_seconds: int


# --- tts ---


@dataclass(frozen=True)
class LogsRuntimeConfig:
    """Runtime/systemd logging policy knobs."""

    level: str = "INFO"
    color: str = "never"  # never|auto|always
    httpx_level: str = "WARNING"
    httpcore_level: str = "WARNING"
    uvicorn_access_level: str = "WARNING"
    uvicorn_error_level: str = "INFO"
    asyncio_level: str = "WARNING"
    slixmpp_level: str = "WARNING"
    slixmpp_xmlstream_level: str = "WARNING"
    logger_levels: Dict[str, str] = field(default_factory=dict)
    cap_poll_summary: bool = False
    ipaws_poll_summary: bool = False
    conductor_cycle_push: bool = False
    conductor_alert_push: bool = False
    conductor_live_time_push: bool = False
    segment_refresher_synth: bool = False
    segment_refresher_alert_lifecycle: bool = False


@dataclass(frozen=True)
class LogsDiscordConfig:
    """Discord webhook logging knobs. URLs come from .env, not config.yaml."""

    enabled: bool = False

    # Per-channel on/off toggles.  A channel only fires if both `enabled` AND
    # the corresponding env var is set.
    alerts_enabled: bool = True
    ops_enabled: bool = True
    api_enabled: bool = True
    errors_enabled: bool = True

    # Webhook URLs (populated from .env by load_config; kept as empty strings
    # here so the dataclass can be constructed without env vars in tests).
    alerts_url: str = ""
    ops_url: str = ""
    api_url: str = ""
    errors_url: str = ""

    # Rate limiting — max embeds per webhook per minute.
    # Discord allows ~30/min per webhook; stay conservative.
    rate_limit_per_minute: int = 20

    # Content knobs
    post_tests: bool = True  # Post RWT/RMT test originations to alerts channel
    post_voice_only: bool = True  # Post voice-only cut-ins (SPS, CAP voice, etc.)
    cycle_rebuild_log: bool = True  # Post cycle rebuild events to ops channel
    # AlertTracker lifecycle (load/purge on startup) — slightly noisy, off by default
    alerttracker_lifecycle_log: bool = False
    # Detailed ops/audit embeds (targeting, dedupe, audio, station-feed). Noisy, off by default.
    ops_detail_log: bool = False
    # Source health/startup source-state embeds. Low volume, on by default.
    source_health_log: bool = True

    # Base URL for the Lucide icon CDN, e.g. "https://cdn.seasonalnet.org"
    # Leave empty to omit thumbnails from all embeds.
    icon_cdn_url: str = ""


@dataclass(frozen=True)
class LogsConfig:
    runtime: LogsRuntimeConfig = field(default_factory=LogsRuntimeConfig)
    discord: LogsDiscordConfig = field(default_factory=LogsDiscordConfig)


@dataclass(frozen=True)
class VoiceTextPaulConfig:
    run_as: str
    retries: int
    retry_sleep_ms: int
    reset_every: int
    kill_before: bool
    vtml_lexicon: bool
    alias_overrides: List[Dict[str, Any]] = field(default_factory=list)
    phoneme_overrides_x_cmu: List[Dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class LocalTTSConfig:
    engine: str
    voice: str
    rate_wpm: int
    voicetext_paul: VoiceTextPaulConfig


@dataclass(frozen=True)
class SeasonalTtsdTTSConfig:
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


@dataclass(frozen=True)
class OpenAICompatibleTTSConfig:
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


@dataclass(frozen=True)
class TTSConfig:
    backend: str
    fallback_backend: str | None
    local: LocalTTSConfig
    volume: float
    text_overrides: List[Dict[str, Any]] = field(default_factory=list)
    seasonal_ttsd: SeasonalTtsdTTSConfig = field(default_factory=SeasonalTtsdTTSConfig)
    openai_compatible: OpenAICompatibleTTSConfig = field(default_factory=OpenAICompatibleTTSConfig)

    @property
    def voice(self) -> str:
        return self.local.voice

    @property
    def rate_wpm(self) -> int:
        return self.local.rate_wpm

    @property
    def voicetext_paul(self) -> VoiceTextPaulConfig:
        return self.local.voicetext_paul


@dataclass(frozen=True)
class AudioConfig:
    sample_rate: int
    attention_tone_hz: int
    attention_tone_seconds: float
    eom_beep_hz: int
    eom_beep_seconds: float
    inter_segment_silence_seconds: float
    post_alert_silence_seconds: float


@dataclass(frozen=True)
class PathsConfig:
    work_dir: str
    audio_dir: str
    cache_dir: str
    config_dir: str
    log_dir: str
    operational_state_dir: str = ""
    job_state_dir: str = ""
    artifact_dir: str = ""
    diagnostic_export_dir: str = "/usr/share/seasonalweather/diagnostics"
    temporary_dir: str = _DEFAULT_TEMPORARY_DIR
    runtime_dir: str = "/run/seasonalweather"
    secret_dir: str = "/run/secrets"


@dataclass(frozen=True)
class DatabaseHousekeepingConfig:
    enabled: bool
    interval_seconds: int
    startup_delay_seconds: int
    api_command_retention_days: int
    audio_asset_grace_seconds: int
    generated_audio_retention_seconds: int
    generated_audio_max_bytes: int
    tmp_file_grace_seconds: int
    wal_checkpoint: bool


@dataclass(frozen=True)
class DatabaseConfig:
    enabled: bool
    path: str
    busy_timeout_ms: int
    journal_mode: str
    housekeeping: DatabaseHousekeepingConfig


@dataclass(frozen=True)
class JobRepositoryConfig:
    enabled: bool
    required: bool
    path: str
    busy_timeout_ms: int
    lease_seconds: int
    assignment_ack_seconds: int
    progress_retention: int
    event_retention: int
    reconciliation_batch_size: int
    payload_max_bytes: int
    result_max_bytes: int
    shutdown_reconciliation_seconds: float

    def validate(self, *, operational_database_path: str) -> None:
        _validate_job_repository_identity(self, operational_database_path)
        _validate_job_repository_timing(self)
        _validate_job_repository_retention(self)
        _validate_job_repository_sizes(self)


def _validate_job_repository_identity(
    cfg: JobRepositoryConfig,
    operational_database_path: str,
) -> None:
    from .configuration.semantic_rules import job_repository_identity_errors

    errors = job_repository_identity_errors(
        enabled=cfg.enabled,
        required=cfg.required,
        path=cfg.path,
        operational_database_path=operational_database_path,
    )
    if errors:
        raise ValueError(errors[0])
    if cfg.enabled and Path(cfg.path).resolve(strict=False) == Path(operational_database_path).resolve(strict=False):
        raise ValueError("jobs.path must be separate from database.path")


def _validate_job_repository_timing(cfg: JobRepositoryConfig) -> None:
    from .configuration.semantic_rules import job_repository_timing_errors

    errors = job_repository_timing_errors(
        busy_timeout_ms=cfg.busy_timeout_ms,
        assignment_ack_seconds=cfg.assignment_ack_seconds,
        lease_seconds=cfg.lease_seconds,
        shutdown_reconciliation_seconds=cfg.shutdown_reconciliation_seconds,
    )
    if errors:
        raise ValueError(errors[0])


def _validate_job_repository_retention(cfg: JobRepositoryConfig) -> None:
    if not 1 <= cfg.progress_retention <= 1000 or not 1 <= cfg.event_retention <= 5000:
        raise ValueError("jobs progress and event retention values are out of bounds")
    if not 1 <= cfg.reconciliation_batch_size <= 1000:
        raise ValueError("jobs.reconciliation_batch_size must be between 1 and 1000")


def _validate_job_repository_sizes(cfg: JobRepositoryConfig) -> None:
    if not 256 <= cfg.payload_max_bytes <= 1_048_576:
        raise ValueError("jobs.payload_max_bytes must be between 256 and 1048576")
    if not 256 <= cfg.result_max_bytes <= 1_048_576:
        raise ValueError("jobs.result_max_bytes must be between 256 and 1048576")


@dataclass(frozen=True)
class ServiceAreaConfig:
    same_fips_all: List[str]
    transmitters: Dict[str, List[Dict[str, str]]]


# ---------------------------------------------------------------------------
# Secrets — sourced from environment, not yaml
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SecretsConfig:
    """
    The nine values that legitimately live in the environment file.
    Loaded once at startup by load_config() and kept here so no other
    module ever needs to call os.getenv() for credentials.
    """

    nwws_jid: str = field(repr=False)
    nwws_password: str = field(repr=False)
    icecast_source_password: str = field(repr=False)
    icecast_admin_password: str = field(repr=False)  # may be empty
    icecast_relay_password: str = field(repr=False)  # may be empty
    api_token: str = field(repr=False)  # may be empty
    api_tokens_json: str = field(repr=False)  # may be empty — JSON blob for multi-token
    liquidsoap_host: str
    liquidsoap_port: int


# ---------------------------------------------------------------------------
# Top-level AppConfig
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AppConfig:
    # core
    station: StationConfig
    stream: StreamConfig
    network: NetworkConfig
    cycle: CycleConfig
    observations: ObservationsConfig
    nwws: NWWSConfig
    nws: NwsConfig
    pns: PnsConfig
    now: NowConfig
    health: HealthConfig
    policy: PolicyConfig
    tts: TTSConfig
    audio: AudioConfig
    paths: PathsConfig
    lifecycle: LifecycleTimeouts
    database: DatabaseConfig
    jobs: JobRepositoryConfig
    service_area: ServiceAreaConfig

    # subsystems
    same: SameConfig
    cap: CapConfig
    ipaws: IpawsConfig
    ern: ErnConfig
    samedec: SameDecConfig
    tests: TestsConfig
    zonecounty: ZoneCountyConfig
    mareas: MareasConfig
    station_feed: StationFeedConfig
    api: ApiConfig
    dedupe: DedupeConfig
    logs: LogsConfig

    # secrets (from environment)
    secrets: SecretsConfig


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _get(d: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Safe nested get with a fallback default."""
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k, None)
        if cur is None:
            return default
    return cur


def _network_host(value: Any, name: str, default: str) -> str:
    result = str(value if value is not None else default).strip()
    if not result or any(ord(character) < 0x20 or ord(character) == 0x7F for character in result):
        raise ValueError(f"network.{name} must be a non-empty host or address")
    return result


def _network_port(
    value: Any,
    name: str,
    default: int,
    *,
    allow_zero: bool = False,
    maximum: int = 65_535,
) -> int:
    try:
        result = int(value if value is not None else default)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"network.{name} must be an integer port") from exc
    minimum = 0 if allow_zero else 1
    if not minimum <= result <= maximum:
        raise ValueError(f"network.{name} must be between {minimum} and {maximum}")
    return result


def _network_timeout(value: Any, name: str, default: float) -> float:
    try:
        result = float(value if value is not None else default)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"network.{name} must be a finite positive number") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"network.{name} must be a finite positive number")
    return result


def _load_optional_service_network_config(
    network_raw: dict[str, Any], name: str, default_port: int
) -> OptionalServiceNetworkConfig:
    service_raw = network_raw.get(name, {}) or {}
    address = str(service_raw.get("address", "") or "").strip()
    enabled = bool(service_raw.get("enabled", False))
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in address):
        raise ValueError(f"network.{name}.address contains control characters")
    if enabled and not address:
        raise ValueError(f"network.{name}.address is required when enabled")
    return OptionalServiceNetworkConfig(
        enabled=enabled,
        address=address,
        port=_network_port(service_raw.get("port", default_port), f"{name}.port", default_port),
        database=str(service_raw.get("database", "") or "").strip(),
        tls=bool(service_raw.get("tls", True)),
        connect_timeout_seconds=_network_timeout(
            service_raw.get("connect_timeout_seconds", 5.0),
            f"{name}.connect_timeout_seconds",
            5.0,
        ),
    )


def _load_swwp_network_config(swwp_raw: dict[str, Any]) -> SwwpNetworkConfig:
    worker_url = str(swwp_raw.get("worker_controller_url", "") or "").strip()
    if worker_url and not worker_url.startswith(("ws://", "wss://")):
        raise ValueError("network.swwp.worker_controller_url must use ws:// or wss://")
    controller_path = str(swwp_raw.get("controller_path", "/v1/workers/connect") or "").strip()
    if not controller_path.startswith("/") or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in controller_path
    ):
        raise ValueError("network.swwp.controller_path must be an absolute path")
    return SwwpNetworkConfig(
        bind_host=_network_host(swwp_raw.get("bind_host"), "swwp.bind_host", _DEFAULT_SWWP_BIND_HOST),
        controller_path=controller_path,
        worker_controller_url=worker_url,
        verify_tls=bool(swwp_raw.get("verify_tls", True)),
        heartbeat_interval_seconds=_network_port(
            swwp_raw.get("heartbeat_interval_seconds"),
            "swwp.heartbeat_interval_seconds",
            15,
            maximum=300,
        ),
        heartbeat_timeout_seconds=_network_port(
            swwp_raw.get("heartbeat_timeout_seconds"),
            "swwp.heartbeat_timeout_seconds",
            45,
            maximum=900,
        ),
        lease_seconds=_network_port(swwp_raw.get("lease_seconds"), "swwp.lease_seconds", 60, maximum=3600),
        assignment_ack_seconds=_network_port(
            swwp_raw.get("assignment_ack_seconds"), "swwp.assignment_ack_seconds", 10, maximum=300
        ),
        max_message_bytes=_network_port(
            swwp_raw.get("max_message_bytes"), "swwp.max_message_bytes", 65_536, maximum=1_048_576
        ),
        capability_validity_seconds=_network_port(
            swwp_raw.get("capability_validity_seconds"),
            "swwp.capability_validity_seconds",
            60,
            maximum=900,
        ),
    )


def _load_network_config(raw: dict[str, Any]) -> NetworkConfig:
    network_raw = raw.get("network", {}) or {}
    api_raw = network_raw.get("api", {}) or {}
    liquidsoap_raw = network_raw.get("liquidsoap", {}) or {}
    swwp_raw = network_raw.get("swwp", {}) or {}

    return NetworkConfig(
        api=ApiNetworkConfig(
            bind_host=_network_host(api_raw.get("bind_host"), "api.bind_host", "127.0.0.1"),
            port=_network_port(api_raw.get("port"), "api.port", 9080),
        ),
        liquidsoap=LiquidsoapNetworkConfig(
            host=_network_host(liquidsoap_raw.get("host"), "liquidsoap.host", "127.0.0.1"),
            port=_network_port(liquidsoap_raw.get("port"), "liquidsoap.port", 1234),
            timeout_seconds=_network_timeout(
                liquidsoap_raw.get("timeout_seconds", 3.0),
                "liquidsoap.timeout_seconds",
                3.0,
            ),
        ),
        swwp=_load_swwp_network_config(swwp_raw),
        postgresql=_load_optional_service_network_config(network_raw, "postgresql", 5432),
        redis=_load_optional_service_network_config(network_raw, "redis", 6379),
    )


def _upper_list(value: Any) -> List[str]:
    """Return unique non-empty strings upper-cased, preserving order."""
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    else:
        try:
            items = list(value)
        except TypeError:
            items = [value]
    out: List[str] = []
    seen: set[str] = set()
    for item in items:
        token = str(item).strip().upper()
        if not token or token in seen:
            continue
        out.append(token)
        seen.add(token)
    return out


_DEFAULT_ADMIN_SCOPES = _KNOWN_API_SCOPES - {"*"}


def _auth_error(
    kind: str,
    path: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> AuthConfigurationError:
    return AuthConfigurationError(
        kind=kind,
        path=path,
        message=message,
        details=details,
    )


def _parse_auth_mode(value: Any) -> AuthMode:
    if not isinstance(value, str):
        raise _auth_error(
            "invalid_type",
            "api.auth.mode",
            "Authentication mode must be a string.",
            details={"expected": "string"},
        )
    normalized = value.strip().lower()
    if not normalized:
        raise _auth_error(
            "empty_value",
            "api.auth.mode",
            "Authentication mode must not be empty.",
        )
    try:
        return AuthMode(normalized)
    except ValueError as exc:
        raise _auth_error(
            "unknown_value",
            "api.auth.mode",
            "Authentication mode is not supported.",
            details={"supported": [mode.value for mode in AuthMode]},
        ) from exc


def _validate_scope(value: Any, *, path: str) -> str:
    if not isinstance(value, str):
        raise _auth_error(
            "invalid_type",
            path,
            "API scopes must be strings.",
            details={"expected": "string"},
        )
    if value != value.strip() or not value or not _AUTH_SCOPE_RE.fullmatch(value) or value not in _KNOWN_API_SCOPES:
        raise _auth_error(
            "malformed_scope",
            path,
            "API scope value is malformed.",
        )
    return value


def _deduplicate_scopes(values: list[str], *, path: str) -> frozenset[str]:
    if not values:
        raise _auth_error("empty_scope", path, "API scope configuration must not be empty.")
    if len(values) != len(set(values)):
        raise _auth_error("duplicate_scope", path, "API scope configuration contains a duplicate.")
    return frozenset(values)


def _parse_scope_string(value: Any, *, path: str) -> tuple[frozenset[str], bool]:
    if not isinstance(value, str):
        raise _auth_error(
            "invalid_type",
            path,
            "API scopes must be a space-separated string.",
            details={"expected": "string"},
        )
    if not value or value != value.strip():
        raise _auth_error("empty_scope", path, "API scope configuration must not be empty.")

    legacy = "," in value
    if legacy:
        if any(char.isspace() for char in value):
            raise _auth_error(
                "mixed_scope_delimiters",
                path,
                "API scopes must not mix comma and whitespace delimiters.",
            )
        raw_scopes = value.split(",")
    else:
        if any(char.isspace() and char != " " for char in value):
            raise _auth_error(
                "malformed_scope",
                path,
                "API scopes must use a single ASCII space delimiter.",
            )
        raw_scopes = value.split(" ")

    if any(not scope for scope in raw_scopes):
        raise _auth_error("empty_scope", path, "API scope configuration contains an empty entry.")
    scopes = [_validate_scope(scope, path=path) for scope in raw_scopes]
    return _deduplicate_scopes(scopes, path=path), legacy


def _parse_scope_list(value: Any, *, path: str) -> frozenset[str]:
    if not isinstance(value, list):
        raise _auth_error(
            "invalid_type",
            path,
            "Multi-token API scopes must be a JSON array.",
            details={"expected": "array"},
        )
    scopes = [_validate_scope(scope, path=f"{path}[{index}]") for index, scope in enumerate(value)]
    return _deduplicate_scopes(scopes, path=path)


def _validate_static_token(value: Any, *, path: str) -> str:
    if not isinstance(value, str):
        raise _auth_error(
            "invalid_type",
            path,
            "Static API credential material must be a string.",
            details={"expected": "string"},
        )
    if (
        not value
        or value != value.strip()
        or any(char.isspace() for char in value)
        or value.casefold() in {"changeme", "yourtoken"}
    ):
        raise _auth_error(
            "invalid_credential",
            path,
            "Static API credential material is absent or malformed.",
        )
    return value


def _parse_static_credentials(
    *,
    single_token: str,
    tokens_json: str,
    subject: Any,
    scope_value: Any,
    scope_path: str,
) -> tuple[tuple[StaticCredentialConfig, ...], bool]:
    from .configuration.semantic_rules import static_credential_sources_conflict

    if static_credential_sources_conflict(single_token, tokens_json):
        raise _auth_error(
            "conflicting_credentials",
            "api.auth.credentials",
            "Configure exactly one static API credential source.",
        )

    if tokens_json:
        try:
            parsed = json.loads(tokens_json)
        except json.JSONDecodeError as exc:
            raise _auth_error(
                "invalid_credentials_json",
                "SEASONAL_API_TOKENS_JSON",
                "Static API credential JSON is invalid.",
            ) from exc
        if not isinstance(parsed, dict) or not parsed:
            raise _auth_error(
                "invalid_credentials_json",
                "SEASONAL_API_TOKENS_JSON",
                "Static API credential JSON must be a non-empty object.",
            )

        credentials: list[StaticCredentialConfig] = []
        for index, (token_value, metadata) in enumerate(parsed.items()):
            token = _validate_static_token(
                token_value,
                path=f"SEASONAL_API_TOKENS_JSON.credentials[{index}]",
            )
            if not isinstance(metadata, dict):
                raise _auth_error(
                    "invalid_credential_metadata",
                    f"SEASONAL_API_TOKENS_JSON.credentials[{index}]",
                    "Static API credential metadata must be an object.",
                )
            raw_subject = metadata.get("subject", "api-user")
            if not isinstance(raw_subject, str) or not raw_subject.strip():
                raise _auth_error(
                    "invalid_subject",
                    f"SEASONAL_API_TOKENS_JSON.credentials[{index}].subject",
                    "Static API credential subject must be a non-empty string.",
                )
            raw_scopes = metadata.get("scopes", ["*"])
            scopes = _parse_scope_list(
                raw_scopes,
                path=f"SEASONAL_API_TOKENS_JSON.credentials[{index}].scopes",
            )
            credentials.append(
                StaticCredentialConfig(
                    token=token,
                    subject=raw_subject.strip(),
                    scopes=scopes,
                )
            )
        return tuple(credentials), False

    token = _validate_static_token(single_token, path="SEASONAL_API_TOKEN")
    if not isinstance(subject, str) or not subject.strip():
        raise _auth_error(
            "invalid_subject",
            "api.auth.subject",
            "Static API credential subject must be a non-empty string.",
        )
    if scope_value is None:
        scopes = _DEFAULT_ADMIN_SCOPES
        legacy_scope = False
    else:
        scopes, legacy_scope = _parse_scope_string(scope_value, path=scope_path)
    return (
        (
            StaticCredentialConfig(
                token=token,
                subject=subject.strip(),
                scopes=scopes,
            ),
        ),
        legacy_scope,
    )


def _load_exchange_auth_config(auth_block: dict[str, Any] | None) -> ExchangeAuthConfig:
    exchange_raw = auth_block.get("exchange", {}) if auth_block is not None else {}
    if not isinstance(exchange_raw, dict):
        raise _auth_error(
            "invalid_type",
            "api.auth.exchange",
            "Exchange authentication configuration must be an object.",
        )
    try:
        exchange = ExchangeAuthConfig(
            minimum_ttl_seconds=int(exchange_raw.get("minimum_ttl_seconds", 60)),
            default_ttl_seconds=int(exchange_raw.get("default_ttl_seconds", 900)),
            maximum_read_ttl_seconds=int(exchange_raw.get("maximum_read_ttl_seconds", 3600)),
            maximum_write_ttl_seconds=int(exchange_raw.get("maximum_write_ttl_seconds", 900)),
        )
    except (TypeError, ValueError) as exc:
        raise _auth_error(
            "invalid_ttl_policy",
            "api.auth.exchange",
            "Exchange token TTL policy must contain integers.",
        ) from exc
    from .configuration.semantic_rules import exchange_ttls_are_ordered

    ordered = exchange_ttls_are_ordered(
        exchange.minimum_ttl_seconds,
        exchange.default_ttl_seconds,
        exchange.maximum_write_ttl_seconds,
        exchange.maximum_read_ttl_seconds,
    )
    if not ordered:
        raise _auth_error(
            "invalid_ttl_policy",
            "api.auth.exchange",
            "Exchange token TTL policy is invalid.",
        )
    return exchange


def _load_api_auth_config(
    api_raw: Any,
    environment: EnvironmentValues,
) -> ApiAuthConfig:
    if not isinstance(api_raw, dict):
        raise _auth_error(
            "invalid_type",
            "api",
            "API configuration must be an object.",
            details={"expected": "object"},
        )

    auth_present = "auth" in api_raw
    auth_raw = api_raw.get("auth")
    auth_block = auth_raw if isinstance(auth_raw, dict) else None
    if auth_present and auth_block is None:
        raise _auth_error(
            "invalid_type",
            "api.auth",
            "API authentication configuration must be an object.",
            details={"expected": "object"},
        )

    legacy_fields = {"scopes", "subject"}.intersection(api_raw)
    from .configuration.semantic_rules import current_and_legacy_auth_conflict

    if current_and_legacy_auth_conflict(auth_present=auth_present, legacy_fields=legacy_fields):
        raise _auth_error(
            "conflicting_fields",
            "api.auth",
            "Legacy and current API authentication fields must not be combined.",
            details={"legacy_fields": sorted(legacy_fields)},
        )

    single_token = environment.raw_optional("SEASONAL_API_TOKEN")
    tokens_json = environment.raw_optional("SEASONAL_API_TOKENS_JSON")

    if auth_block is not None:
        if "mode" not in auth_block:
            raise _auth_error(
                "missing_value",
                "api.auth.mode",
                "Authentication mode is required in the current API auth configuration.",
            )
        mode = _parse_auth_mode(auth_block["mode"])
        subject = auth_block.get("subject", "local-admin")
        scope_value = auth_block.get("scopes")
        scope_path = "api.auth.scopes"
        legacy_mode = False
    else:
        mode = AuthMode.STATIC
        subject = api_raw.get("subject", "local-admin")
        scope_value = api_raw.get("scopes")
        scope_path = "api.scopes"
        legacy_mode = True

    exchange = _load_exchange_auth_config(auth_block)

    if mode is AuthMode.EXCHANGE:
        credentials: tuple[StaticCredentialConfig, ...] = ()
        legacy_scope = False
    else:
        credentials, legacy_scope = _parse_static_credentials(
            single_token=single_token,
            tokens_json=tokens_json,
            subject=subject,
            scope_value=scope_value,
            scope_path=scope_path,
        )
    return ApiAuthConfig(
        mode=mode,
        credentials=credentials,
        exchange=exchange,
        legacy_mode_normalized=legacy_mode,
        legacy_scope_normalized=legacy_scope,
    )


def load_config(path: str) -> AppConfig:
    """Compile and load one configuration through the authoritative loader."""
    from .configuration.loader import load_runtime_config

    return load_runtime_config(path)


def _build_app_config(
    raw: Dict[str, Any],
    *,
    environment: EnvironmentValues,
) -> AppConfig:
    """Compatibility projection from one validated value tree to AppConfig."""

    nwws_jid = environment.optional("NWWS_JID")
    nwws_password = environment.optional("NWWS_PASSWORD")
    nwws_credentials_defaulted = _nwws_credentials_are_default(nwws_jid, nwws_password)

    # ------------------------------------------------------------------
    # station
    # ------------------------------------------------------------------
    st = raw["station"]
    station = StationConfig(
        name=str(st["name"]),
        service_area_name=str(st["service_area_name"]),
        timezone=str(st["timezone"]),
        disclaimer=str(st["disclaimer"]),
        deployment_type=str(st.get("deployment_type", "land")),
    )

    # ------------------------------------------------------------------
    # stream
    # ------------------------------------------------------------------
    stream = StreamConfig(**raw["stream"])
    network = _load_network_config(raw)
    paths_raw = raw["paths"]
    work_dir = str(paths_raw["work_dir"]).strip()
    state_dir = str(paths_raw.get("operational_state_dir", "") or "").strip() or str(Path(work_dir) / "state")
    job_state_dir = str(paths_raw.get("job_state_dir", "") or "").strip() or str(Path(work_dir) / "jobs")
    artifact_dir = str(paths_raw.get("artifact_dir", "") or "").strip() or str(Path(work_dir) / "artifacts")
    paths = PathsConfig(
        work_dir=work_dir,
        audio_dir=str(paths_raw["audio_dir"]),
        cache_dir=str(paths_raw["cache_dir"]),
        config_dir=str(paths_raw["config_dir"]),
        log_dir=str(paths_raw["log_dir"]),
        operational_state_dir=state_dir,
        job_state_dir=job_state_dir,
        artifact_dir=artifact_dir,
        diagnostic_export_dir=str(paths_raw.get("diagnostic_export_dir", "/usr/share/seasonalweather/diagnostics")),
        temporary_dir=str(paths_raw.get("temporary_dir", _DEFAULT_TEMPORARY_DIR)),
        runtime_dir=str(paths_raw.get("runtime_dir", "/run/seasonalweather")),
        secret_dir=str(paths_raw.get("secret_dir", "/run/secrets")),
    )

    # ------------------------------------------------------------------
    # cycle
    # ------------------------------------------------------------------
    cy = raw["cycle"]
    focus_raw = cy.get("alert_focus", {}) or {}
    policy_toneout_raw = raw.get("policy", {}).get("toneout_product_types", [])
    focus_test_codes = _upper_list(focus_raw.get("test_event_codes", DEFAULT_TEST_EVENT_CODES))
    focus_hold_raw = focus_raw.get("hold_event_codes", None)
    if focus_hold_raw:
        focus_hold_codes = _upper_list(focus_hold_raw)
    else:
        # Omitted/empty hold_event_codes inherits the operational tone-out
        # policy, then test codes are removed.  This keeps local high-value
        # tone-out additions from being silently blacklisted while still
        # preventing RWT/RMT/DMO-style tests from pinning focus.
        focus_hold_codes = _upper_list(policy_toneout_raw)
    focus_hold_codes = [c for c in focus_hold_codes if c not in set(focus_test_codes)]
    alert_focus = AlertFocusPolicy(
        hold_event_codes=tuple(focus_hold_codes),
        excluded_sources=tuple(_upper_list(focus_raw.get("excluded_sources", DEFAULT_EXCLUDED_SOURCES))),
        test_event_codes=tuple(focus_test_codes),
        hold_vtec_significance=tuple(
            _upper_list(focus_raw.get("hold_vtec_significance", DEFAULT_HOLD_VTEC_SIGNIFICANCE))
        ),
        marine_event_codes=tuple(_upper_list(focus_raw.get("marine_event_codes", DEFAULT_MARINE_EVENT_CODES))),
        marine_hold_event_codes=tuple(
            _upper_list(focus_raw.get("marine_hold_event_codes", DEFAULT_MARINE_HOLD_EVENT_CODES))
        ),
    )
    spc_raw = cy.get("spc", {})
    fc_raw = cy.get("fc", {})
    obs_raw = cy.get("obs", {})
    hwo_raw = cy.get("hwo", {})
    afd_raw = cy.get("afd", {})
    syn_raw = cy.get("syn", {})
    cwf_raw = cy.get("cwf", {})
    offnt2_raw = cy.get("offnt2", {})
    rwr_raw = cy.get("rwr", {})
    marine_obs_raw = cy.get("marine_obs", {})

    cycle = CycleConfig(
        normal_interval_seconds=int(cy["normal_interval_seconds"]),
        heightened_interval_seconds=int(cy["heightened_interval_seconds"]),
        min_heightened_seconds=int(cy["min_heightened_seconds"]),
        lead_time_seconds=int(cy.get("lead_time_seconds", 90)),
        alert_focus=alert_focus,
        reference_points=[(float(a), float(b), str(lbl)) for a, b, lbl in cy["reference_points"]],
        last_product_max_chars=int(cy.get("last_product_max_chars", 260)),
        spc=CycleSpcConfig(
            enabled=bool(spc_raw.get("enabled", False)),
            wfos=[str(w).upper() for w in spc_raw.get("wfos", ["LWX"])],
            days=int(spc_raw.get("days", 3)),
            min_dn=int(spc_raw.get("min_dn", 3)),
            timeout_s=float(spc_raw.get("timeout_s", 6.0)),
        ),
        fc=CycleFcConfig(
            use_short=bool(fc_raw.get("use_short", True)),
            periods_normal=int(fc_raw.get("periods_normal", 14)),
            periods_per_group=int(fc_raw.get("periods_per_group", 4)),
            max_points_normal=int(fc_raw.get("max_points_normal", 6)),
            max_points_7day=int(fc_raw.get("max_points_7day", 2)),
            point_max_chars=int(fc_raw.get("point_max_chars", 1600)),
            line_max_chars=int(fc_raw.get("line_max_chars", 1600)),
            rotate_period_s=int(fc_raw.get("rotate_period_s", 300)),
            rotate_step=int(fc_raw.get("rotate_step", 0)),
            forecast_zones=[
                (str(z["id"]).upper().strip(), str(z["label"]).strip())
                for z in (fc_raw.get("forecast_zones") or [])
                if isinstance(z, dict) and z.get("id") and z.get("label")
            ],
        ),
        obs=CycleObsConfig(
            max_normal=int(obs_raw.get("max_normal", 0)),
            rotate_period_s=int(obs_raw.get("rotate_period_s", 300)),
            rotate_step=int(obs_raw.get("rotate_step", 0)),
            aliases=dict(obs_raw.get("aliases", {})),
        ),
        hwo=CycleHwoConfig(
            max_chars_normal=int(hwo_raw.get("max_chars_normal", 0)),
            max_chars_heightened=int(hwo_raw.get("max_chars_heightened", 0)),
            speak_unavailable=bool(hwo_raw.get("speak_unavailable", True)),
        ),
        afd=CycleProductConfig(
            max_chars_normal=int(afd_raw.get("max_chars_normal", 0)),
            max_chars_heightened=int(afd_raw.get("max_chars_heightened", 0)),
        ),
        syn=CycleProductConfig(
            max_chars_normal=int(syn_raw.get("max_chars_normal", 1500)),
            max_chars_heightened=int(syn_raw.get("max_chars_heightened", 0)),
        ),
        cwf=CycleCwfConfig(
            enabled=bool(cwf_raw.get("enabled", False)),
            offices=[str(o).upper().strip() for o in (cwf_raw.get("offices") or [])],
            max_chars_normal=int(cwf_raw.get("max_chars_normal", 2000)),
            max_chars_heightened=int(cwf_raw.get("max_chars_heightened", 1200)),
        ),
        offnt2=CycleOffnt2Config(
            enabled=bool(offnt2_raw.get("enabled", False)),
            source_office=str(offnt2_raw.get("source_office", "KWBC")).upper().strip(),
            product_type=str(offnt2_raw.get("product_type", "OFF")).upper().strip(),
            zones=[
                (str(z["id"]).upper().strip(), str(z["label"]).strip())
                for z in (offnt2_raw.get("zones") or [])
                if isinstance(z, dict) and z.get("id") and z.get("label")
            ],
            include_synopsis=bool(offnt2_raw.get("include_synopsis", True)),
            max_chars_normal=int(offnt2_raw.get("max_chars_normal", 2400)),
            max_chars_heightened=int(offnt2_raw.get("max_chars_heightened", 1200)),
            max_airtime_seconds=int(offnt2_raw.get("max_airtime_seconds", 90)),
            rotate_period_s=int(offnt2_raw.get("rotate_period_s", 1800)),
            rotate_step=int(offnt2_raw.get("rotate_step", 1)),
            defer_in_heightened=bool(offnt2_raw.get("defer_in_heightened", True)),
        ),
        rwr=CycleRwrConfig(
            enabled=bool(rwr_raw.get("enabled", False)),
            office=str(rwr_raw.get("office", "LWX")).upper().strip(),
            staleness_minutes=int(rwr_raw.get("staleness_minutes", 75)),
            anchor_stations=[str(s).upper().strip() for s in (rwr_raw.get("anchor_stations") or []) if str(s).strip()],
            fallback_stations=[
                str(s).upper().strip() for s in (rwr_raw.get("fallback_stations") or []) if str(s).strip()
            ],
            pressure_trend_threshold_inhg=float(rwr_raw.get("pressure_trend_threshold_inhg", 0.02)),
            pressure_cache_hours=float(rwr_raw.get("pressure_cache_hours", 3.0)),
            max_compact_per_section=int(rwr_raw.get("max_compact_per_section", 8)),
            station_names={
                str(k).upper().strip(): str(v).strip()
                for k, v in (rwr_raw.get("station_names") or {}).items()
                if str(k).strip() and str(v).strip()
            },
        ),
        marine_obs=CycleMarineObsConfig(
            enabled=bool(marine_obs_raw.get("enabled", False)),
            max_stations=int(marine_obs_raw.get("max_stations", 0)),
            anchor_stations=[
                str(s).upper().strip() for s in (marine_obs_raw.get("anchor_stations") or []) if str(s).strip()
            ],
            station_names={
                str(k).upper().strip(): str(v).strip()
                for k, v in (marine_obs_raw.get("station_names") or {}).items()
                if str(k).strip() and str(v).strip()
            },
        ),
    )

    # ------------------------------------------------------------------
    # observations
    # ------------------------------------------------------------------
    observations = ObservationsConfig(stations=list(raw["observations"]["stations"]))

    # ------------------------------------------------------------------
    # nwws
    # ------------------------------------------------------------------
    nw = raw["nwws"]
    res_raw = nw.get("resiliency", {})
    nwws = NWWSConfig(
        enabled=bool(nw.get("enabled", True)) and not nwws_credentials_defaulted,
        server=str(nw.get("server", "nwws-oi.weather.gov")),
        port=int(nw.get("port", 5222)),
        room=str(nw.get("room", "NWWS@conference.nwws-oi.weather.gov")),
        nick=str(nw.get("nick", "SeasonalWeather")),
        allowed_wfos=list(nw.get("allowed_wfos", [])),
        credentials_defaulted=nwws_credentials_defaulted,
        resiliency=NWWSResiliencyConfig(
            stall_seconds=int(res_raw.get("stall_seconds", 60)),
            muc_confirm_seconds=int(res_raw.get("muc_confirm_seconds", 30)),
            start_wait_seconds=int(res_raw.get("start_wait_seconds", 25)),
            join_wait_seconds=int(res_raw.get("join_wait_seconds", 35)),
            backoff_max_seconds=int(res_raw.get("backoff_max_seconds", 90)),
            rx_log_first_n=int(res_raw.get("rx_log_first_n", 20)),
            decision_log_first_n=int(res_raw.get("decision_log_first_n", 20)),
            decision_log_every=int(res_raw.get("decision_log_every", 0)),
        ),
    )

    # ------------------------------------------------------------------
    # nws
    # ------------------------------------------------------------------
    nws_raw = raw.get("nws", {})
    nws = NwsConfig(user_agent=str(nws_raw.get("user_agent", "")))

    # ------------------------------------------------------------------
    # pns
    # ------------------------------------------------------------------
    pns_raw = raw.get("pns", {})
    default_pns_subtypes = [
        {
            "name": "severe_weather_safety_rules",
            "enabled": True,
            "audio": True,
            "event": "Severe Weather Safety Rules",
            "code": "SPS",
            "key_prefix": "PNS_SAFETY",
            "intro": "The National Weather Service has issued the following public information statement.",
            "headline_contains": ["...SEVERE WEATHER SAFETY RULES..."],
            "body_contains_all": [],
            "body_contains_any": [],
            "reject_contains": [],
            "max_fresh_hours": 18.0,
            "require_same_day": True,
            "max_chars": 2400,
        },
        {
            "name": "nwr_transmitter_outage",
            "enabled": True,
            "audio": True,
            "event": "NOAA Weather Radio Service Announcement",
            "code": "SPS",
            "key_prefix": "PNS_NWR_SERVICE",
            "intro": "This is a service announcement from the National Weather Service concerning NOAA Weather Radio transmitters in the service area.",
            "headline_contains": [],
            "body_contains_all": ["NOAA Weather Radio", "transmitter"],
            "body_contains_any": ["off the air", "offline", "out of service", "technical difficulties", "maintenance"],
            "reject_contains": [],
            "max_fresh_hours": 48.0,
            "require_same_day": False,
            "max_chars": 1400,
        },
        {
            "name": "nwr_transmitter_restoration",
            "enabled": True,
            "audio": True,
            "event": "NOAA Weather Radio Service Announcement",
            "code": "SPS",
            "key_prefix": "PNS_NWR_SERVICE",
            "intro": "This is a service announcement from the National Weather Service concerning NOAA Weather Radio transmitters in the service area.",
            "headline_contains": [],
            "body_contains_all": ["NOAA Weather Radio", "transmitter"],
            "body_contains_any": ["returned to service", "back on the air", "service has been restored", "restored"],
            "reject_contains": [],
            "max_fresh_hours": 24.0,
            "require_same_day": False,
            "max_chars": 1200,
        },
    ]

    def _str_list(value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        if isinstance(value, list):
            return [str(v) for v in value if str(v).strip()]
        return []

    pns_subtypes = []
    for item in pns_raw.get("subtypes") or default_pns_subtypes:
        if not isinstance(item, dict):
            continue
        pns_subtypes.append(
            PnsSubtypeConfig(
                name=str(item.get("name", "pns_subtype") or "pns_subtype"),
                enabled=bool(item.get("enabled", True)),
                audio=bool(item.get("audio", True)),
                event=str(item.get("event", "Public Information Statement") or "Public Information Statement"),
                code=str(item.get("code", "SPS") or "SPS").strip().upper()[:3] or "SPS",
                key_prefix=str(item.get("key_prefix", "PNS") or "PNS"),
                intro=str(
                    item.get(
                        "intro", "The National Weather Service has issued the following public information statement."
                    )
                    or ""
                ),
                headline_contains=_str_list(item.get("headline_contains")),
                body_contains_all=_str_list(item.get("body_contains_all")),
                body_contains_any=_str_list(item.get("body_contains_any")),
                reject_contains=_str_list(item.get("reject_contains")),
                max_fresh_hours=float(item.get("max_fresh_hours", 18.0)),
                require_same_day=bool(item.get("require_same_day", False)),
                max_chars=int(item.get("max_chars", 1800)),
            )
        )

    pns = PnsConfig(
        enabled=bool(pns_raw.get("enabled", True)),
        default_expire_hours=float(pns_raw.get("default_expire_hours", 4.0)),
        hard_stop_delimiter=str(pns_raw.get("hard_stop_delimiter", "&&") or "&&"),
        suppress_unknown_audio=bool(pns_raw.get("suppress_unknown_audio", True)),
        reject_audio_keywords=_str_list(pns_raw.get("reject_audio_keywords"))
        or [
            "spotter reports",
            "storm reports",
            "preliminary local storm report",
            "metadata",
        ],
        subtypes=pns_subtypes,
    )

    # ------------------------------------------------------------------
    # now / short-term forecast
    # ------------------------------------------------------------------
    now_raw = raw.get("now", {})
    now_backfill_raw = now_raw.get("api_backfill", {}) or {}
    now = NowConfig(
        enabled=bool(now_raw.get("enabled", True)),
        intro=str(
            now_raw.get(
                "intro",
                "A statement from the National Weather Service.",
            )
            or "A statement from the National Weather Service."
        ).strip(),
        default_expire_minutes=max(
            5,
            int(now_raw.get("default_expire_minutes", 60)),
        ),
        api_backfill=NowBackfillConfig(
            enabled=bool(now_backfill_raw.get("enabled", True)),
            initial_delay_seconds=max(
                0,
                int(now_backfill_raw.get("initial_delay_seconds", 15)),
            ),
            interval_seconds=max(
                30,
                int(now_backfill_raw.get("interval_seconds", 120)),
            ),
            lookback_minutes=max(
                15,
                int(now_backfill_raw.get("lookback_minutes", 120)),
            ),
            max_products_per_office=max(
                1,
                int(now_backfill_raw.get("max_products_per_office", 25)),
            ),
        ),
    )

    # ------------------------------------------------------------------
    # health state machine
    # ------------------------------------------------------------------
    health_raw = raw.get("health", {})
    health_sources_raw = health_raw.get("sources") or {
        "nwws_oi": {
            "enabled": True,
            "role": "alert_redundant",
            "stale_after_seconds": 600,
            "failure_threshold": 2,
            "critical": False,
        },
        "cap_api": {
            "enabled": True,
            "role": "alert",
            "stale_after_seconds": 300,
            "failure_threshold": 3,
            "critical": True,
        },
        "nws_api": {
            "enabled": True,
            "role": "forecast",
            "stale_after_seconds": 900,
            "failure_threshold": 3,
            "critical": False,
        },
    }
    health_sources: List[HealthSourceConfig] = []
    if isinstance(health_sources_raw, dict):
        iter_sources = health_sources_raw.items()
    else:
        iter_sources = (
            (str((item or {}).get("name", "")), item) for item in health_sources_raw if isinstance(item, dict)
        )
    for name, item in iter_sources:
        if not isinstance(item, dict):
            continue
        source_name = str(item.get("name", name) or name).strip()
        if not source_name:
            continue
        health_sources.append(
            HealthSourceConfig(
                name=source_name,
                enabled=bool(item.get("enabled", True)),
                role=str(item.get("role", "general") or "general"),
                stale_after_seconds=int(item.get("stale_after_seconds", 600)),
                failure_threshold=int(item.get("failure_threshold", 3)),
                critical=bool(item.get("critical", False)),
            )
        )

    health = HealthConfig(
        enabled=bool(health_raw.get("enabled", True)),
        check_interval_seconds=int(health_raw.get("check_interval_seconds", 30)),
        min_hold_seconds=int(health_raw.get("min_hold_seconds", 300)),
        detached_loop_only=bool(health_raw.get("detached_loop_only", True)),
        source_impaired_message=str(
            health_raw.get(
                "source_impaired_message",
                "SeasonalWeather is operating with reduced data-feed redundancy. Some information may be delayed.",
            )
        ),
        degraded_message=str(
            health_raw.get(
                "degraded_message",
                "SeasonalWeather is operating in a degraded mode. Some National Weather Service data may be delayed or unavailable.",
            )
        ),
        critical_message=str(
            health_raw.get(
                "critical_message",
                "SeasonalWeather is operating in a degraded mode. Current watches, warnings, and advisories may be delayed or unavailable.",
            )
        ),
        detached_message=str(
            health_raw.get(
                "detached_message",
                "SeasonalWeather is temporarily unable to receive current National Weather Service information. Please use another weather information source or visit weather.gov for the latest information.",
            )
        ),
        sources=health_sources,
    )

    # ------------------------------------------------------------------
    # policy
    # ------------------------------------------------------------------
    pol = raw["policy"]
    policy = PolicyConfig(
        toneout_product_types=list(pol["toneout_product_types"]),
        min_tone_gap_seconds=float(pol.get("min_tone_gap_seconds", 2.0)),
    )

    # ------------------------------------------------------------------
    # same
    # ------------------------------------------------------------------
    sa = raw.get("same", {})
    same_native_raw = sa.get("native_encoder", {}) or {}
    same = SameConfig(
        enabled=bool(sa.get("enabled", False)),
        sender=str(sa.get("sender", "SEASNWXR")),
        duration_minutes=int(sa.get("duration_minutes", 60)),
        amplitude=float(sa.get("amplitude", 0.35)),
        native_encoder=SameNativeEncoderConfig(
            enabled=bool(same_native_raw.get("enabled", False)),
            bin=str(same_native_raw.get("bin", "samegen") or "samegen"),
            timeout_seconds=float(same_native_raw.get("timeout_seconds", 5.0)),
            fallback_to_python=bool(same_native_raw.get("fallback_to_python", True)),
        ),
    )

    # ------------------------------------------------------------------
    # cap
    # ------------------------------------------------------------------
    cap_raw = raw.get("cap", {})
    cap_full_raw = cap_raw.get("full", {})
    cap_voice_raw = cap_raw.get("voice", {})
    cap = CapConfig(
        enabled=bool(cap_raw.get("enabled", False)),
        dryrun=bool(cap_raw.get("dryrun", True)),
        poll_seconds=int(cap_raw.get("poll_seconds", 60)),
        user_agent=str(cap_raw.get("user_agent", "SeasonalWeather (CAP monitor)")),
        url=str(cap_raw.get("url", "")),
        ledger_path=str(cap_raw.get("ledger_path", Path(paths.operational_state_dir) / "cap_ledger.json")),
        ledger_max_age_days=int(cap_raw.get("ledger_max_age_days", 14)),
        full=CapFullConfig(
            enabled=bool(cap_full_raw.get("enabled", True)),
            severities=[str(s) for s in cap_full_raw.get("severities", ["Severe", "Extreme"])],
            events=[str(e) for e in cap_full_raw.get("events", [])],
            cooldown_seconds=int(cap_full_raw.get("cooldown_seconds", 180)),
        ),
        voice=CapVoiceConfig(
            enabled=bool(cap_voice_raw.get("enabled", False)),
            events=[str(e) for e in cap_voice_raw.get("events", ["Special Weather Statement"])],
            cooldown_seconds=int(cap_voice_raw.get("cooldown_seconds", 600)),
        ),
    )

    # ------------------------------------------------------------------
    # ipaws
    # ------------------------------------------------------------------
    ipaws_raw = raw.get("ipaws", {})
    _ipaws_default_full = ["CEM", "LAE", "CDW", "EAN", "EVI", "NUW", "RHW", "LEW"]
    ipaws = IpawsConfig(
        enabled=bool(ipaws_raw.get("enabled", False)),
        dryrun=bool(ipaws_raw.get("dryrun", True)),
        poll_seconds=int(ipaws_raw.get("poll_seconds", 90)),
        user_agent=str(ipaws_raw.get("user_agent", "SeasonalWeather (IPAWS monitor)")),
        url=str(ipaws_raw.get("url", "")),
        ledger_path=str(ipaws_raw.get("ledger_path", Path(paths.operational_state_dir) / "ipaws_ledger.json")),
        ledger_max_age_days=int(ipaws_raw.get("ledger_max_age_days", 14)),
        full_events=[str(e) for e in ipaws_raw.get("full_events", _ipaws_default_full)],
        voice_events=[str(e) for e in ipaws_raw.get("voice_events", [])],
        ern_dedup_ttl_seconds=int(ipaws_raw.get("ern_dedup_ttl_seconds", 600)),
    )

    # ------------------------------------------------------------------
    # ern
    # ------------------------------------------------------------------
    ern_raw = raw.get("ern", {})
    ern_relay_raw = ern_raw.get("relay", {})
    ern = ErnConfig(
        enabled=bool(ern_raw.get("enabled", False)),
        dryrun=bool(ern_raw.get("dryrun", True)),
        url=str(ern_raw.get("url", "")),
        name=str(ern_raw.get("name", "ERN/JON")),
        decoder_backend=_normalize_ern_decoder_backend(ern_raw.get("decoder_backend", "auto")),
        sample_rate=int(ern_raw.get("sample_rate", 48000)),
        tail_seconds=float(ern_raw.get("tail_seconds", 10.0)),
        trigger_ratio=float(ern_raw.get("trigger_ratio", 8.0)),
        dedupe_seconds=float(ern_raw.get("dedupe_seconds", 20.0)),
        confidence_min=float(ern_raw.get("confidence_min", 0.25)),
        relay=ErnRelayConfig(
            enabled=bool(ern_relay_raw.get("enabled", False)),
            events=[str(e) for e in ern_relay_raw.get("events", ["RWT", "RMT"])],
            min_confidence=float(ern_relay_raw.get("min_confidence", 0.80)),
            cooldown_seconds=int(ern_relay_raw.get("cooldown_seconds", 300)),
            senders=[str(s) for s in ern_relay_raw.get("senders", [])],
        ),
    )

    # ------------------------------------------------------------------
    # samedec
    # ------------------------------------------------------------------
    sd_raw = raw.get("samedec", {})
    samedec = SameDecConfig(
        bin=str(sd_raw.get("bin", "/usr/local/bin/samedec")),
        confidence=float(sd_raw.get("confidence", 0.85)),
        start_delay_s=float(sd_raw.get("start_delay_s", 1.4)),
    )

    # ------------------------------------------------------------------
    # tests
    # ------------------------------------------------------------------
    tst_raw = raw.get("tests", {})
    rwt_raw = tst_raw.get("rwt", {})
    rmt_raw = tst_raw.get("rmt", {})
    tst_present_raw = tst_raw.get("presentation", {})

    global_postpone_policy = normalize_postpone_policy(
        tst_raw.get("postpone_policy", "delay_window"),
        "delay_window",
    )
    global_postpone_minutes = int(tst_raw.get("postpone_minutes", 15))
    global_max_postpone_hours = int(tst_raw.get("max_postpone_hours", 6))
    global_max_postpone_days = int(tst_raw.get("max_postpone_days", 2))
    rwt_default_postpone_policy = global_postpone_policy if "postpone_policy" in tst_raw else "next_day"

    def _tests_gate_config(section_raw: Dict[str, Any]) -> TestsGateConfig:
        gate_raw = section_raw.get("gate", {})
        if not isinstance(gate_raw, dict):
            gate_raw = {}
        return TestsGateConfig(
            block_heightened=bool(gate_raw.get("block_heightened", True)),
            block_recent_toneout=bool(gate_raw.get("block_recent_toneout", True)),
            block_recent_severe_cap=bool(gate_raw.get("block_recent_severe_cap", True)),
            block_recent_ern=bool(gate_raw.get("block_recent_ern", True)),
        )

    tests = TestsConfig(
        enabled=bool(tst_raw.get("enabled", False)),
        postpone_policy=global_postpone_policy,
        postpone_minutes=global_postpone_minutes,
        max_postpone_hours=global_max_postpone_hours,
        max_postpone_days=global_max_postpone_days,
        jitter_seconds=int(tst_raw.get("jitter_seconds", 60)),
        toneout_cooldown_seconds=int(tst_raw.get("toneout_cooldown_seconds", cycle.min_heightened_seconds)),
        cap_block_seconds=int(tst_raw.get("cap_block_seconds", 3600)),
        ern_block_seconds=int(tst_raw.get("ern_block_seconds", 3600)),
        presentation=TestsPresentationConfig(
            headline_template=str(
                tst_present_raw.get(
                    "headline_template",
                    "{event} for the {service_area_name}",
                )
                or "{event} for the {service_area_name}"
            ),
            area_text=str(tst_present_raw.get("area_text", "") or ""),
            discord_area_text=str(tst_present_raw.get("discord_area_text", "") or ""),
        ),
        rwt=TestsScheduleConfig(
            weekday=int(rwt_raw.get("weekday", 2)),
            hour=int(rwt_raw.get("hour", 11)),
            minute=int(rwt_raw.get("minute", 0)),
            script_lines=tuple(str(x) for x in rwt_raw.get("script_lines", []) if str(x).strip()),
            postpone_policy=normalize_postpone_policy(
                rwt_raw.get("postpone_policy", rwt_default_postpone_policy),
                "next_day",
            ),
            postpone_minutes=int(rwt_raw.get("postpone_minutes", global_postpone_minutes)),
            max_postpone_hours=int(rwt_raw.get("max_postpone_hours", global_max_postpone_hours)),
            max_postpone_days=int(rwt_raw.get("max_postpone_days", global_max_postpone_days)),
            gate=_tests_gate_config(rwt_raw),
        ),
        rmt=TestsRmtConfig(
            nth=int(rmt_raw.get("nth", 1)),
            weekday=int(rmt_raw.get("weekday", 2)),
            hour=int(rmt_raw.get("hour", 11)),
            minute=int(rmt_raw.get("minute", 0)),
            script_lines=tuple(str(x) for x in rmt_raw.get("script_lines", []) if str(x).strip()),
            postpone_policy=normalize_postpone_policy(
                rmt_raw.get("postpone_policy", global_postpone_policy),
                "delay_window",
            ),
            postpone_minutes=int(rmt_raw.get("postpone_minutes", global_postpone_minutes)),
            max_postpone_hours=int(rmt_raw.get("max_postpone_hours", global_max_postpone_hours)),
            max_postpone_days=int(rmt_raw.get("max_postpone_days", 0)),
            gate=_tests_gate_config(rmt_raw),
        ),
    )

    # ------------------------------------------------------------------
    # zonecounty
    # ------------------------------------------------------------------
    zc_raw = raw.get("zonecounty", {})
    zonecounty = ZoneCountyConfig(
        enabled=bool(zc_raw.get("enabled", True)),
        dbx_url=str(zc_raw.get("dbx_url", "")),
        cache_days=int(zc_raw.get("cache_days", 30)),
        index_url=str(zc_raw.get("index_url", "https://www.weather.gov/gis/ZoneCounty")),
        base_url=str(zc_raw.get("base_url", "https://www.weather.gov/source/gis/Shapefiles/County/")),
    )

    # ------------------------------------------------------------------
    # mareas
    # ------------------------------------------------------------------
    mr_raw = raw.get("mareas", {})
    mareas = MareasConfig(
        enabled=bool(mr_raw.get("enabled", True)),
        url=str(mr_raw.get("url", "")),
        cache_days=int(mr_raw.get("cache_days", 30)),
    )

    # ------------------------------------------------------------------
    # station_feed
    # ------------------------------------------------------------------
    sf_raw = raw.get("station_feed", {})
    sf_hk_raw = sf_raw.get("housekeeping", {})
    sf_nwws_raw = sf_raw.get("nwws", {}) if isinstance(sf_raw.get("nwws", {}), dict) else {}

    sf_nwws_labels_raw = sf_nwws_raw.get("vtec_event_labels", {})
    if not isinstance(sf_nwws_labels_raw, dict):
        sf_nwws_labels_raw = {}
    sf_nwws_labels = {
        str(k).strip().upper(): str(v).strip()
        for k, v in sf_nwws_labels_raw.items()
        if str(k).strip() and str(v).strip()
    }

    sf_nwws_tz_raw = sf_nwws_raw.get("tz_abbrev_overrides", {})
    if not isinstance(sf_nwws_tz_raw, dict):
        sf_nwws_tz_raw = {}
    sf_nwws_tz = {
        str(k).strip().upper(): str(v).strip() for k, v in sf_nwws_tz_raw.items() if str(k).strip() and str(v).strip()
    }

    station_feed = StationFeedConfig(
        enabled=bool(sf_raw.get("enabled", False)),
        station_id=str(sf_raw.get("station_id", "seasonalweather")),
        source=str(sf_raw.get("source", "seasonalweather")),
        max_items=int(sf_raw.get("max_items", 24)),
        ttl_seconds=int(sf_raw.get("ttl_seconds", 7200)),
        fetch_nws=bool(sf_raw.get("fetch_nws", False)),
        debug=bool(sf_raw.get("debug", False)),
        ern_area_names=bool(sf_raw.get("ern_area_names", True)),
        housekeeping=StationFeedHousekeepingConfig(
            enabled=bool(sf_hk_raw.get("enabled", True)),
            interval_sec=int(sf_hk_raw.get("interval_sec", 60)),
            grace_sec=int(sf_hk_raw.get("grace_sec", 5)),
            housekeep_seconds=int(sf_hk_raw.get("housekeep_seconds", 30)),
        ),
        nwws=StationFeedNwwsConfig(
            vtec_event_labels=sf_nwws_labels,
            tz_abbrev_overrides=sf_nwws_tz,
        ),
    )

    # ------------------------------------------------------------------
    # api
    # ------------------------------------------------------------------
    api_raw = raw.get("api", {})
    api = ApiConfig(
        auth=_load_api_auth_config(api_raw, environment),
        allow_remote=bool(api_raw.get("allow_remote", False)),
        audio_max_bytes=int(api_raw.get("audio_max_bytes", 20971520)),
        audio_max_seconds=int(api_raw.get("audio_max_seconds", 180)),
        audio_ttl_seconds=int(api_raw.get("audio_ttl_seconds", 86400)),
        ffmpeg_bin=str(api_raw.get("ffmpeg_bin", "ffmpeg")),
        full_eas_heightened=bool(api_raw.get("full_eas_heightened", False)),
        manual_full_eas_heightens=bool(api_raw.get("manual_full_eas_heightens", True)),
    )

    # ------------------------------------------------------------------
    # dedupe
    # ------------------------------------------------------------------
    dd_raw = raw.get("dedupe", {})
    dedupe = DedupeConfig(ttl_seconds=int(dd_raw.get("ttl_seconds", 900)))

    # ------------------------------------------------------------------
    # tts
    # ------------------------------------------------------------------
    tts_raw = raw["tts"]
    local_raw = tts_raw.get("local", {}) or {}
    backend_raw = str(tts_raw["backend"]).strip().lower()
    local_aliases = {
        "espeak-ng": "espeak-ng",
        "espeak_ng": "espeak-ng",
        "espeak": "espeak-ng",
        "piper": "piper",
        "festival": "festival",
        "dectalk": "dectalk",
        "voicetext_paul": "voicetext_paul",
    }
    legacy_local = backend_raw in local_aliases
    backend = "local" if legacy_local else str(tts_raw["backend"])
    local_engine = local_aliases.get(backend_raw, str(local_raw.get("engine", "espeak-ng")))
    local_voice = tts_raw.get("voice", local_raw.get("voice", "9")) if legacy_local else local_raw.get("voice", "9")
    local_rate = (
        tts_raw.get("rate_wpm", local_raw.get("rate_wpm", 165)) if legacy_local else local_raw.get("rate_wpm", 165)
    )
    vtp_raw = (
        tts_raw.get("voicetext_paul", local_raw.get("voicetext_paul", {}))
        if legacy_local
        else local_raw.get("voicetext_paul", tts_raw.get("voicetext_paul", {}))
    ) or {}
    fallback_raw = tts_raw.get("fallback_backend")
    fallback_backend = None
    if fallback_raw is not None:
        fallback_value = str(fallback_raw).strip().lower()
        fallback_backend = "local" if fallback_value in local_aliases else str(fallback_raw)
    seasonal_raw = tts_raw.get("seasonal_ttsd", {}) or {}
    openai_raw = tts_raw.get("openai_compatible", {}) or {}

    def remote_url(value: object, label: str, *, required: bool, openai: bool = False) -> str:
        url = str(value or "").strip()
        if not url and not required:
            return ""
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(f"{label} must be an HTTPS origin without credentials, query, or fragment")
        path = parsed.path.rstrip("/")
        if openai and not path.endswith("/v1"):
            raise ValueError(f"{label} must include the /v1 API path")
        if not openai and path.endswith("/v1"):
            raise ValueError(f"{label} must not include /v1; the adapter supplies that route")
        return url.rstrip("/")

    def bounded_remote_number(
        value: object, label: str, *, integer: bool = False, maximum: int | float | None = None
    ) -> int | float:
        number = int(cast(Any, value)) if integer else float(cast(Any, value))
        if number <= 0 or (isinstance(number, float) and not math.isfinite(number)):
            raise ValueError(f"{label} must be positive")
        if maximum is not None and number > maximum:
            raise ValueError(f"{label} exceeds its bound")
        return number

    remote_selected = {backend, fallback_backend}
    seasonal_required = "seasonal_ttsd" in remote_selected
    openai_required = "openai_compatible" in remote_selected
    for provider, provider_raw, selected in (
        ("seasonal_ttsd", seasonal_raw, seasonal_required),
        ("openai_compatible", openai_raw, openai_required),
    ):
        errors = remote_tts_configuration_errors(provider, provider_raw, selected=selected)
        if errors:
            field, message = errors[0]
            raise ValueError(f"tts.{provider}.{field} {message}")
    tts = TTSConfig(
        backend=backend,
        fallback_backend=fallback_backend,
        local=LocalTTSConfig(
            engine=local_engine,
            voice=str(local_voice),
            rate_wpm=int(local_rate),
            voicetext_paul=VoiceTextPaulConfig(
                run_as=str(vtp_raw.get("run_as", "voicetext")),
                retries=int(vtp_raw.get("retries", 1)),
                retry_sleep_ms=int(vtp_raw.get("retry_sleep_ms", 150)),
                reset_every=int(vtp_raw.get("reset_every", 0)),
                kill_before=bool(vtp_raw.get("kill_before", False)),
                vtml_lexicon=bool(vtp_raw.get("vtml_lexicon", True)),
                alias_overrides=list(vtp_raw.get("alias_overrides", []) or []),
                phoneme_overrides_x_cmu=list(vtp_raw.get("phoneme_overrides_x_cmu", []) or []),
            ),
        ),
        volume=float(tts_raw.get("volume", 1.0)),
        text_overrides=list(tts_raw.get("text_overrides", []) or []),
        seasonal_ttsd=SeasonalTtsdTTSConfig(
            base_url=remote_url(seasonal_raw.get("base_url"), "tts.seasonal_ttsd.base_url", required=seasonal_required),
            client_credential_file=str(seasonal_raw.get("client_credential_file", "") or "").strip(),
            voice=str(seasonal_raw.get("voice", "voicetext-paul")).strip(),
            profile=str(seasonal_raw.get("profile", "wav-48k-stereo")).strip(),
            token_ttl_seconds=int(
                bounded_remote_number(
                    seasonal_raw.get("token_ttl_seconds", 900), "tts.seasonal_ttsd.token_ttl_seconds", integer=True
                )
            ),
            refresh_margin_seconds=int(seasonal_raw.get("refresh_margin_seconds", 120)),
            connect_timeout_seconds=float(
                bounded_remote_number(
                    seasonal_raw.get("connect_timeout_seconds", 5.0),
                    "tts.seasonal_ttsd.connect_timeout_seconds",
                    maximum=REMOTE_CONNECT_TIMEOUT_MAX,
                )
            ),
            token_timeout_seconds=float(
                bounded_remote_number(
                    seasonal_raw.get("token_timeout_seconds", 10.0),
                    "tts.seasonal_ttsd.token_timeout_seconds",
                    maximum=REMOTE_TOKEN_TIMEOUT_MAX,
                )
            ),
            synthesis_timeout_seconds=float(
                bounded_remote_number(
                    seasonal_raw.get("synthesis_timeout_seconds", 180.0),
                    "tts.seasonal_ttsd.synthesis_timeout_seconds",
                    maximum=REMOTE_SYNTHESIS_TIMEOUT_MAX,
                )
            ),
            max_input_bytes=int(
                bounded_remote_number(
                    seasonal_raw.get("max_input_bytes", 65_536),
                    "tts.seasonal_ttsd.max_input_bytes",
                    integer=True,
                    maximum=REMOTE_INPUT_BYTES_MAX,
                )
            ),
            max_response_bytes=int(
                bounded_remote_number(
                    seasonal_raw.get("max_response_bytes", 67_108_864),
                    "tts.seasonal_ttsd.max_response_bytes",
                    integer=True,
                    maximum=REMOTE_RESPONSE_BYTES_MAX,
                )
            ),
            max_error_bytes=int(
                bounded_remote_number(
                    seasonal_raw.get("max_error_bytes", 16_384),
                    "tts.seasonal_ttsd.max_error_bytes",
                    integer=True,
                    maximum=REMOTE_ERROR_BYTES_MAX,
                )
            ),
            verify_tls=bool(seasonal_raw.get("verify_tls", True)),
        ),
        openai_compatible=OpenAICompatibleTTSConfig(
            base_url=remote_url(
                openai_raw.get("base_url"), "tts.openai_compatible.base_url", required=openai_required, openai=True
            ),
            api_key_file=str(openai_raw.get("api_key_file", "") or "").strip(),
            model=str(openai_raw.get("model", "")).strip(),
            voice=str(openai_raw.get("voice", "")).strip(),
            response_format=str(openai_raw.get("response_format", "wav")).strip().lower(),
            speed=float(openai_raw.get("speed", 1.0)),
            connect_timeout_seconds=float(
                bounded_remote_number(
                    openai_raw.get("connect_timeout_seconds", 5.0),
                    "tts.openai_compatible.connect_timeout_seconds",
                    maximum=REMOTE_CONNECT_TIMEOUT_MAX,
                )
            ),
            synthesis_timeout_seconds=float(
                bounded_remote_number(
                    openai_raw.get("synthesis_timeout_seconds", 180.0),
                    "tts.openai_compatible.synthesis_timeout_seconds",
                    maximum=REMOTE_SYNTHESIS_TIMEOUT_MAX,
                )
            ),
            max_input_bytes=int(
                bounded_remote_number(
                    openai_raw.get("max_input_bytes", 65_536),
                    "tts.openai_compatible.max_input_bytes",
                    integer=True,
                    maximum=REMOTE_INPUT_BYTES_MAX,
                )
            ),
            max_response_bytes=int(
                bounded_remote_number(
                    openai_raw.get("max_response_bytes", 67_108_864),
                    "tts.openai_compatible.max_response_bytes",
                    integer=True,
                    maximum=REMOTE_RESPONSE_BYTES_MAX,
                )
            ),
            max_error_bytes=int(
                bounded_remote_number(
                    openai_raw.get("max_error_bytes", 16_384),
                    "tts.openai_compatible.max_error_bytes",
                    integer=True,
                    maximum=REMOTE_ERROR_BYTES_MAX,
                )
            ),
            verify_tls=bool(openai_raw.get("verify_tls", True)),
        ),
    )
    if seasonal_required and (
        not tts.seasonal_ttsd.client_credential_file or not tts.seasonal_ttsd.voice or not tts.seasonal_ttsd.profile
    ):
        raise ValueError("selected seasonal_ttsd requires credential file, voice, and profile")
    if seasonal_required and tts.seasonal_ttsd.refresh_margin_seconds < 0:
        raise ValueError("tts.seasonal_ttsd.refresh_margin_seconds must not be negative")
    if seasonal_required and tts.seasonal_ttsd.refresh_margin_seconds >= tts.seasonal_ttsd.token_ttl_seconds:
        raise ValueError("tts.seasonal_ttsd.refresh_margin_seconds must be less than token TTL")
    if openai_required and (
        not tts.openai_compatible.api_key_file or not tts.openai_compatible.model or not tts.openai_compatible.voice
    ):
        raise ValueError("selected openai_compatible requires API key file, model, and voice")
    if openai_required and (not math.isfinite(tts.openai_compatible.speed) or not 0 < tts.openai_compatible.speed <= 4):
        raise ValueError("tts.openai_compatible.speed must be finite and in (0, 4]")
    if openai_required and tts.openai_compatible.response_format not in {"wav", "mp3", "flac", "opus", "aac"}:
        raise ValueError("tts.openai_compatible.response_format is unsupported")

    # ------------------------------------------------------------------
    # audio
    # ------------------------------------------------------------------
    audio = AudioConfig(**raw["audio"])

    # ------------------------------------------------------------------
    # controller lifecycle
    # ------------------------------------------------------------------
    lifecycle_raw = raw.get("lifecycle", {}) or {}
    lifecycle = LifecycleTimeouts(
        total_seconds=float(lifecycle_raw.get("total_seconds", 30.0)),
        active_request_seconds=float(lifecycle_raw.get("active_request_seconds", 10.0)),
        publication_seconds=float(lifecycle_raw.get("publication_seconds", 8.0)),
        source_stop_seconds=float(lifecycle_raw.get("source_stop_seconds", 8.0)),
        tts_stop_seconds=float(lifecycle_raw.get("tts_stop_seconds", 8.0)),
        task_cancel_seconds=float(lifecycle_raw.get("task_cancel_seconds", 5.0)),
        resource_close_seconds=float(lifecycle_raw.get("resource_close_seconds", 5.0)),
    )
    lifecycle.validate()

    # ------------------------------------------------------------------
    # database
    # ------------------------------------------------------------------
    db_raw = raw.get("database", {})
    db_path = str(db_raw.get("path", "")).strip() or str(Path(paths.operational_state_dir) / "seasonalweather.sqlite3")
    db_hk_raw = db_raw.get("housekeeping", {})
    database = DatabaseConfig(
        enabled=bool(db_raw.get("enabled", True)),
        path=db_path,
        busy_timeout_ms=int(db_raw.get("busy_timeout_ms", 5000)),
        journal_mode=str(db_raw.get("journal_mode", "WAL") or "WAL").strip().upper(),
        housekeeping=DatabaseHousekeepingConfig(
            enabled=bool(db_hk_raw.get("enabled", True)),
            interval_seconds=int(db_hk_raw.get("interval_seconds", 900)),
            startup_delay_seconds=int(db_hk_raw.get("startup_delay_seconds", 45)),
            api_command_retention_days=int(db_hk_raw.get("api_command_retention_days", 14)),
            audio_asset_grace_seconds=int(db_hk_raw.get("audio_asset_grace_seconds", 900)),
            generated_audio_retention_seconds=int(db_hk_raw.get("generated_audio_retention_seconds", 10800)),
            generated_audio_max_bytes=int(db_hk_raw.get("generated_audio_max_bytes", 1073741824)),
            tmp_file_grace_seconds=int(db_hk_raw.get("tmp_file_grace_seconds", 900)),
            wal_checkpoint=bool(db_hk_raw.get("wal_checkpoint", True)),
        ),
    )
    jobs_raw = raw.get("jobs", {}) or {}
    jobs = JobRepositoryConfig(
        enabled=bool(jobs_raw.get("enabled", False)),
        required=bool(jobs_raw.get("required", False)),
        path=str(jobs_raw.get("path", "")).strip(),
        busy_timeout_ms=int(jobs_raw.get("busy_timeout_ms", 5000)),
        lease_seconds=int(jobs_raw.get("lease_seconds", 60)),
        assignment_ack_seconds=int(jobs_raw.get("assignment_ack_seconds", 10)),
        progress_retention=int(jobs_raw.get("progress_retention", 100)),
        event_retention=int(jobs_raw.get("event_retention", 500)),
        reconciliation_batch_size=int(jobs_raw.get("reconciliation_batch_size", 100)),
        payload_max_bytes=int(jobs_raw.get("payload_max_bytes", 65536)),
        result_max_bytes=int(jobs_raw.get("result_max_bytes", 65536)),
        shutdown_reconciliation_seconds=float(jobs_raw.get("shutdown_reconciliation_seconds", 5.0)),
    )
    jobs.validate(operational_database_path=database.path)

    # ------------------------------------------------------------------
    # service_area
    # ------------------------------------------------------------------
    transmitters = raw["service_area"]["transmitters"]
    same_all: List[str] = []
    for _tx, items in transmitters.items():
        for item in items:
            same_all.append(str(item["same_fips"]).zfill(6))
    same_all = sorted(set(same_all))
    service_area = ServiceAreaConfig(same_fips_all=same_all, transmitters=transmitters)

    # ------------------------------------------------------------------
    # secrets — read from environment, only place in the codebase that
    # touches os.environ for these keys
    # ------------------------------------------------------------------
    secrets = SecretsConfig(
        nwws_jid=nwws_jid,
        nwws_password=nwws_password,
        icecast_source_password=environment.required("ICECAST_SOURCE_PASSWORD"),
        icecast_admin_password=environment.optional("ICECAST_ADMIN_PASSWORD"),
        icecast_relay_password=environment.optional("ICECAST_RELAY_PASSWORD"),
        api_token=environment.raw_optional("SEASONAL_API_TOKEN"),
        api_tokens_json=environment.raw_optional("SEASONAL_API_TOKENS_JSON"),
        liquidsoap_host=environment.optional("LIQUIDSOAP_TELNET_HOST", network.liquidsoap.host),
        liquidsoap_port=environment.integer("LIQUIDSOAP_TELNET_PORT", network.liquidsoap.port),
    )

    # ------------------------------------------------------------------
    # logs — runtime policy from config.yaml; Discord webhook URLs from .env
    # ------------------------------------------------------------------
    _lr = _get(raw, "logs", "runtime") or {}
    _logs_runtime = LogsRuntimeConfig(
        level=str(_get(_lr, "level", default="INFO") or "INFO").strip().upper(),
        color=str(_get(_lr, "color", default="never") or "never").strip().lower(),
        httpx_level=str(_get(_lr, "httpx_level", default="WARNING") or "WARNING").strip().upper(),
        httpcore_level=str(_get(_lr, "httpcore_level", default="WARNING") or "WARNING").strip().upper(),
        uvicorn_access_level=str(_get(_lr, "uvicorn_access_level", default="WARNING") or "WARNING").strip().upper(),
        uvicorn_error_level=str(_get(_lr, "uvicorn_error_level", default="INFO") or "INFO").strip().upper(),
        asyncio_level=str(_get(_lr, "asyncio_level", default="WARNING") or "WARNING").strip().upper(),
        slixmpp_level=str(_get(_lr, "slixmpp_level", default="WARNING") or "WARNING").strip().upper(),
        slixmpp_xmlstream_level=str(_get(_lr, "slixmpp_xmlstream_level", default="WARNING") or "WARNING")
        .strip()
        .upper(),
        logger_levels={
            str(k).strip(): str(v).strip().upper()
            for k, v in (_get(_lr, "logger_levels", default={}) or {}).items()
            if str(k).strip() and str(v).strip()
        },
        cap_poll_summary=bool(_get(_lr, "cap_poll_summary", default=False)),
        ipaws_poll_summary=bool(_get(_lr, "ipaws_poll_summary", default=False)),
        conductor_cycle_push=bool(_get(_lr, "conductor_cycle_push", default=False)),
        conductor_alert_push=bool(_get(_lr, "conductor_alert_push", default=False)),
        conductor_live_time_push=bool(_get(_lr, "conductor_live_time_push", default=False)),
        segment_refresher_synth=bool(_get(_lr, "segment_refresher_synth", default=False)),
        segment_refresher_alert_lifecycle=bool(_get(_lr, "segment_refresher_alert_lifecycle", default=False)),
    )
    _ld = _get(raw, "logs", "discord") or {}
    _logs_discord = LogsDiscordConfig(
        enabled=bool(_get(_ld, "enabled", default=False)),
        alerts_enabled=bool(_get(_ld, "alerts_enabled", default=True)),
        ops_enabled=bool(_get(_ld, "ops_enabled", default=True)),
        api_enabled=bool(_get(_ld, "api_enabled", default=True)),
        errors_enabled=bool(_get(_ld, "errors_enabled", default=True)),
        # URLs come exclusively from env; not from config.yaml (no auth on webhooks)
        alerts_url=environment.optional("SEASONAL_DISCORD_ALERTS_WEBHOOK"),
        ops_url=environment.optional("SEASONAL_DISCORD_OPS_WEBHOOK"),
        api_url=environment.optional("SEASONAL_DISCORD_API_WEBHOOK"),
        errors_url=environment.optional("SEASONAL_DISCORD_ERRORS_WEBHOOK"),
        rate_limit_per_minute=int(_get(_ld, "rate_limit_per_minute", default=20)),
        post_tests=bool(_get(_ld, "post_tests", default=True)),
        post_voice_only=bool(_get(_ld, "post_voice_only", default=True)),
        cycle_rebuild_log=bool(_get(_ld, "cycle_rebuild_log", default=True)),
        alerttracker_lifecycle_log=bool(_get(_ld, "alerttracker_lifecycle_log", default=False)),
        ops_detail_log=bool(_get(_ld, "ops_detail_log", default=False)),
        source_health_log=bool(_get(_ld, "source_health_log", default=True)),
        icon_cdn_url=str(_get(_ld, "icon_cdn_url", default="") or "").strip(),
    )
    logs = LogsConfig(runtime=_logs_runtime, discord=_logs_discord)

    return AppConfig(
        station=station,
        stream=stream,
        network=network,
        cycle=cycle,
        observations=observations,
        nwws=nwws,
        nws=nws,
        pns=pns,
        now=now,
        health=health,
        policy=policy,
        tts=tts,
        audio=audio,
        paths=paths,
        lifecycle=lifecycle,
        database=database,
        jobs=jobs,
        service_area=service_area,
        same=same,
        cap=cap,
        ipaws=ipaws,
        ern=ern,
        samedec=samedec,
        tests=tests,
        zonecounty=zonecounty,
        mareas=mareas,
        station_feed=station_feed,
        api=api,
        dedupe=dedupe,
        secrets=secrets,
        logs=logs,
    )
