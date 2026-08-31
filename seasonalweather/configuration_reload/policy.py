"""Versioned exhaustive reload policy over the authoritative typed schema."""

from __future__ import annotations

from dataclasses import dataclass

from seasonalweather.configuration.origins import ENVIRONMENT_BINDINGS
from seasonalweather.configuration.paths import ConfigPath
from seasonalweather.configuration.schema import SCHEMA_V1, SchemaKind, SchemaNode
from seasonalweather.diagnostics.bindings import RELOAD_CODES

from .models import RELOAD_POLICY_VERSION, ReloadDisposition

_WILDCARD = "*"


@dataclass(frozen=True, order=True)
class ReloadPolicyRule:
    pattern: tuple[str, ...]
    disposition: ReloadDisposition
    identity: str


class UnclassifiedPathError(ValueError):
    diagnostic_code = RELOAD_CODES["unclassified_change"]


def _leaf_patterns(node: SchemaNode, prefix: tuple[str, ...] = ()) -> tuple[tuple[str, ...], ...]:
    return _LEAF_HANDLERS.get(node.kind, _scalar_patterns)(node, prefix)


def _object_patterns(node: SchemaNode, prefix: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    if node.fields is None:
        raise RuntimeError("object schema node has no fields")
    return tuple(
        pattern for name, child in sorted(node.fields.items()) for pattern in _leaf_patterns(child, (*prefix, name))
    )


def _map_patterns(node: SchemaNode, prefix: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    if node.values is None:
        raise RuntimeError("map schema node has no value schema")
    return _leaf_patterns(node.values, (*prefix, _WILDCARD))


def _list_patterns(node: SchemaNode, prefix: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    if node.item is None:
        raise RuntimeError("list schema node has no item schema")
    return _leaf_patterns(node.item, (*prefix, _WILDCARD))


def _tuple_patterns(node: SchemaNode, prefix: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    return tuple(
        pattern
        for index, child in enumerate(node.tuple_items)
        for pattern in _leaf_patterns(child, (*prefix, str(index)))
    )


def _scalar_patterns(_node: SchemaNode, prefix: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    return (prefix,)


_LEAF_HANDLERS = {
    SchemaKind.OBJECT: _object_patterns,
    SchemaKind.MAP: _map_patterns,
    SchemaKind.LIST: _list_patterns,
    SchemaKind.TUPLE: _tuple_patterns,
}


SCHEMA_LEAF_PATTERNS = _leaf_patterns(SCHEMA_V1)
ENVIRONMENT_PATTERNS = tuple(
    sorted({tuple(str(item) for item in path) for path, _variable, _default in ENVIRONMENT_BINDINGS})
)
ALL_POLICY_PATTERNS = tuple(sorted(set(SCHEMA_LEAF_PATTERNS) | set(ENVIRONMENT_PATTERNS)))


_DECLARED_POLICY: dict[tuple[str, ...], ReloadDisposition] = {
    ("api", "allow_remote"): ReloadDisposition.RESTART_REQUIRED,
    ("api", "audio_max_bytes"): ReloadDisposition.RESTART_REQUIRED,
    ("api", "audio_max_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("api", "audio_ttl_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("api", "auth", "exchange", "default_ttl_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("api", "auth", "exchange", "maximum_read_ttl_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("api", "auth", "exchange", "maximum_write_ttl_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("api", "auth", "exchange", "minimum_ttl_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("api", "auth", "mode"): ReloadDisposition.RESTART_REQUIRED,
    ("api", "auth", "scopes"): ReloadDisposition.RESTART_REQUIRED,
    ("api", "auth", "subject"): ReloadDisposition.RESTART_REQUIRED,
    ("api", "ffmpeg_bin"): ReloadDisposition.RESTART_REQUIRED,
    ("api", "full_eas_heightened"): ReloadDisposition.RESTART_REQUIRED,
    ("api", "manual_full_eas_heightens"): ReloadDisposition.RESTART_REQUIRED,
    ("api", "scopes"): ReloadDisposition.RESTART_REQUIRED,
    ("api", "subject"): ReloadDisposition.RESTART_REQUIRED,
    ("audio", "attention_tone_hz"): ReloadDisposition.QUIESCENT,
    ("audio", "attention_tone_seconds"): ReloadDisposition.QUIESCENT,
    ("audio", "eom_beep_hz"): ReloadDisposition.QUIESCENT,
    ("audio", "eom_beep_seconds"): ReloadDisposition.QUIESCENT,
    ("audio", "inter_segment_silence_seconds"): ReloadDisposition.QUIESCENT,
    ("audio", "post_alert_silence_seconds"): ReloadDisposition.QUIESCENT,
    ("audio", "sample_rate"): ReloadDisposition.QUIESCENT,
    ("cap", "dryrun"): ReloadDisposition.QUIESCENT,
    ("cap", "enabled"): ReloadDisposition.QUIESCENT,
    ("cap", "full", "cooldown_seconds"): ReloadDisposition.QUIESCENT,
    ("cap", "full", "enabled"): ReloadDisposition.QUIESCENT,
    ("cap", "full", "events", "*"): ReloadDisposition.QUIESCENT,
    ("cap", "full", "severities", "*"): ReloadDisposition.QUIESCENT,
    ("cap", "ledger_max_age_days"): ReloadDisposition.QUIESCENT,
    ("cap", "ledger_path"): ReloadDisposition.QUIESCENT,
    ("cap", "poll_seconds"): ReloadDisposition.QUIESCENT,
    ("cap", "url"): ReloadDisposition.QUIESCENT,
    ("cap", "user_agent"): ReloadDisposition.QUIESCENT,
    ("cap", "voice", "cooldown_seconds"): ReloadDisposition.QUIESCENT,
    ("cap", "voice", "enabled"): ReloadDisposition.QUIESCENT,
    ("cap", "voice", "events", "*"): ReloadDisposition.QUIESCENT,
    ("config_schema",): ReloadDisposition.RESTART_REQUIRED,
    ("cycle", "afd", "max_chars_heightened"): ReloadDisposition.QUIESCENT,
    ("cycle", "afd", "max_chars_normal"): ReloadDisposition.QUIESCENT,
    ("cycle", "alert_focus", "excluded_sources", "*"): ReloadDisposition.QUIESCENT,
    ("cycle", "alert_focus", "hold_event_codes", "*"): ReloadDisposition.QUIESCENT,
    ("cycle", "alert_focus", "hold_vtec_significance", "*"): ReloadDisposition.QUIESCENT,
    ("cycle", "alert_focus", "marine_event_codes", "*"): ReloadDisposition.QUIESCENT,
    ("cycle", "alert_focus", "marine_hold_event_codes", "*"): ReloadDisposition.QUIESCENT,
    ("cycle", "alert_focus", "test_event_codes", "*"): ReloadDisposition.QUIESCENT,
    ("cycle", "cwf", "enabled"): ReloadDisposition.QUIESCENT,
    ("cycle", "cwf", "max_chars_heightened"): ReloadDisposition.QUIESCENT,
    ("cycle", "cwf", "max_chars_normal"): ReloadDisposition.QUIESCENT,
    ("cycle", "cwf", "offices", "*"): ReloadDisposition.QUIESCENT,
    ("cycle", "offnt2", "defer_in_heightened"): ReloadDisposition.QUIESCENT,
    ("cycle", "offnt2", "enabled"): ReloadDisposition.QUIESCENT,
    ("cycle", "offnt2", "include_synopsis"): ReloadDisposition.QUIESCENT,
    ("cycle", "offnt2", "max_airtime_seconds"): ReloadDisposition.QUIESCENT,
    ("cycle", "offnt2", "max_chars_heightened"): ReloadDisposition.QUIESCENT,
    ("cycle", "offnt2", "max_chars_normal"): ReloadDisposition.QUIESCENT,
    ("cycle", "offnt2", "product_type"): ReloadDisposition.QUIESCENT,
    ("cycle", "offnt2", "rotate_period_s"): ReloadDisposition.QUIESCENT,
    ("cycle", "offnt2", "rotate_step"): ReloadDisposition.QUIESCENT,
    ("cycle", "offnt2", "source_office"): ReloadDisposition.QUIESCENT,
    ("cycle", "offnt2", "zones", "*", "id"): ReloadDisposition.QUIESCENT,
    ("cycle", "offnt2", "zones", "*", "label"): ReloadDisposition.QUIESCENT,
    ("cycle", "fc", "forecast_zones", "*", "id"): ReloadDisposition.QUIESCENT,
    ("cycle", "fc", "forecast_zones", "*", "label"): ReloadDisposition.QUIESCENT,
    ("cycle", "fc", "line_max_chars"): ReloadDisposition.QUIESCENT,
    ("cycle", "fc", "max_points_7day"): ReloadDisposition.QUIESCENT,
    ("cycle", "fc", "max_points_normal"): ReloadDisposition.QUIESCENT,
    ("cycle", "fc", "periods_normal"): ReloadDisposition.QUIESCENT,
    ("cycle", "fc", "periods_per_group"): ReloadDisposition.QUIESCENT,
    ("cycle", "fc", "point_max_chars"): ReloadDisposition.QUIESCENT,
    ("cycle", "fc", "rotate_period_s"): ReloadDisposition.QUIESCENT,
    ("cycle", "fc", "rotate_step"): ReloadDisposition.QUIESCENT,
    ("cycle", "fc", "use_short"): ReloadDisposition.QUIESCENT,
    ("cycle", "heightened_interval_seconds"): ReloadDisposition.QUIESCENT,
    ("cycle", "hwo", "max_chars_heightened"): ReloadDisposition.QUIESCENT,
    ("cycle", "hwo", "max_chars_normal"): ReloadDisposition.QUIESCENT,
    ("cycle", "hwo", "speak_unavailable"): ReloadDisposition.QUIESCENT,
    ("cycle", "last_product_max_chars"): ReloadDisposition.QUIESCENT,
    ("cycle", "lead_time_seconds"): ReloadDisposition.QUIESCENT,
    ("cycle", "marine_obs", "anchor_stations", "*"): ReloadDisposition.QUIESCENT,
    ("cycle", "marine_obs", "enabled"): ReloadDisposition.QUIESCENT,
    ("cycle", "marine_obs", "max_stations"): ReloadDisposition.QUIESCENT,
    ("cycle", "marine_obs", "station_names", "*"): ReloadDisposition.QUIESCENT,
    ("cycle", "min_heightened_seconds"): ReloadDisposition.QUIESCENT,
    ("cycle", "normal_interval_seconds"): ReloadDisposition.QUIESCENT,
    ("cycle", "primary_wfo"): ReloadDisposition.QUIESCENT,
    ("cycle", "obs", "aliases", "*"): ReloadDisposition.QUIESCENT,
    ("cycle", "obs", "max_normal"): ReloadDisposition.QUIESCENT,
    ("cycle", "obs", "rotate_period_s"): ReloadDisposition.QUIESCENT,
    ("cycle", "obs", "rotate_step"): ReloadDisposition.QUIESCENT,
    ("cycle", "reference_points", "*", "0"): ReloadDisposition.QUIESCENT,
    ("cycle", "reference_points", "*", "1"): ReloadDisposition.QUIESCENT,
    ("cycle", "reference_points", "*", "2"): ReloadDisposition.QUIESCENT,
    ("cycle", "rwr", "anchor_stations", "*"): ReloadDisposition.QUIESCENT,
    ("cycle", "rwr", "enabled"): ReloadDisposition.QUIESCENT,
    ("cycle", "rwr", "fallback_stations", "*"): ReloadDisposition.QUIESCENT,
    ("cycle", "rwr", "max_compact_per_section"): ReloadDisposition.QUIESCENT,
    ("cycle", "rwr", "office"): ReloadDisposition.QUIESCENT,
    ("cycle", "rwr", "pressure_cache_hours"): ReloadDisposition.QUIESCENT,
    ("cycle", "rwr", "pressure_trend_threshold_inhg"): ReloadDisposition.QUIESCENT,
    ("cycle", "rwr", "staleness_minutes"): ReloadDisposition.QUIESCENT,
    ("cycle", "rwr", "station_names", "*"): ReloadDisposition.QUIESCENT,
    ("cycle", "spc", "days"): ReloadDisposition.QUIESCENT,
    ("cycle", "spc", "enabled"): ReloadDisposition.QUIESCENT,
    ("cycle", "spc", "min_dn"): ReloadDisposition.QUIESCENT,
    ("cycle", "spc", "timeout_s"): ReloadDisposition.QUIESCENT,
    ("cycle", "spc", "wfos", "*"): ReloadDisposition.QUIESCENT,
    ("cycle", "syn", "max_chars_heightened"): ReloadDisposition.QUIESCENT,
    ("cycle", "syn", "max_chars_normal"): ReloadDisposition.QUIESCENT,
    ("database", "busy_timeout_ms"): ReloadDisposition.RESTART_REQUIRED,
    ("database", "enabled"): ReloadDisposition.RESTART_REQUIRED,
    ("database", "housekeeping", "api_command_retention_days"): ReloadDisposition.RESTART_REQUIRED,
    ("database", "housekeeping", "audio_asset_grace_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("database", "housekeeping", "enabled"): ReloadDisposition.RESTART_REQUIRED,
    ("database", "housekeeping", "generated_audio_max_bytes"): ReloadDisposition.RESTART_REQUIRED,
    ("database", "housekeeping", "generated_audio_retention_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("database", "housekeeping", "interval_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("database", "housekeeping", "startup_delay_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("database", "housekeeping", "tmp_file_grace_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("database", "housekeeping", "wal_checkpoint"): ReloadDisposition.RESTART_REQUIRED,
    ("database", "journal_mode"): ReloadDisposition.RESTART_REQUIRED,
    ("database", "path"): ReloadDisposition.RESTART_REQUIRED,
    ("database", "postgres", "clock_skew_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("database", "postgres", "migration_table"): ReloadDisposition.RESTART_REQUIRED,
    ("database", "postgres", "mode"): ReloadDisposition.RESTART_REQUIRED,
    ("database", "postgres", "role"): ReloadDisposition.RESTART_REQUIRED,
    ("database", "postgres", "schema"): ReloadDisposition.RESTART_REQUIRED,
    ("dedupe", "ttl_seconds"): ReloadDisposition.LIVE,
    ("ern", "confidence_min"): ReloadDisposition.RESTART_REQUIRED,
    ("ern", "decoder_backend"): ReloadDisposition.RESTART_REQUIRED,
    ("ern", "dedupe_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("ern", "dryrun"): ReloadDisposition.RESTART_REQUIRED,
    ("ern", "enabled"): ReloadDisposition.RESTART_REQUIRED,
    ("ern", "name"): ReloadDisposition.RESTART_REQUIRED,
    ("ern", "relay", "cooldown_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("ern", "relay", "enabled"): ReloadDisposition.RESTART_REQUIRED,
    ("ern", "relay", "events", "*"): ReloadDisposition.RESTART_REQUIRED,
    ("ern", "relay", "min_confidence"): ReloadDisposition.RESTART_REQUIRED,
    ("ern", "relay", "senders", "*"): ReloadDisposition.RESTART_REQUIRED,
    ("ern", "sample_rate"): ReloadDisposition.RESTART_REQUIRED,
    ("ern", "tail_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("ern", "trigger_ratio"): ReloadDisposition.RESTART_REQUIRED,
    ("ern", "url"): ReloadDisposition.RESTART_REQUIRED,
    ("health", "check_interval_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("health", "critical_message"): ReloadDisposition.RESTART_REQUIRED,
    ("health", "degraded_message"): ReloadDisposition.RESTART_REQUIRED,
    ("health", "detached_loop_only"): ReloadDisposition.RESTART_REQUIRED,
    ("health", "detached_message"): ReloadDisposition.RESTART_REQUIRED,
    ("health", "enabled"): ReloadDisposition.RESTART_REQUIRED,
    ("health", "min_hold_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("health", "source_impaired_message"): ReloadDisposition.RESTART_REQUIRED,
    ("health", "sources", "*", "critical"): ReloadDisposition.RESTART_REQUIRED,
    ("health", "sources", "*", "enabled"): ReloadDisposition.RESTART_REQUIRED,
    ("health", "sources", "*", "failure_threshold"): ReloadDisposition.RESTART_REQUIRED,
    ("health", "sources", "*", "role"): ReloadDisposition.RESTART_REQUIRED,
    ("health", "sources", "*", "stale_after_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("ipaws", "dryrun"): ReloadDisposition.QUIESCENT,
    ("ipaws", "enabled"): ReloadDisposition.QUIESCENT,
    ("ipaws", "ern_dedup_ttl_seconds"): ReloadDisposition.QUIESCENT,
    ("ipaws", "full_events", "*"): ReloadDisposition.QUIESCENT,
    ("ipaws", "ledger_max_age_days"): ReloadDisposition.QUIESCENT,
    ("ipaws", "ledger_path"): ReloadDisposition.QUIESCENT,
    ("ipaws", "poll_seconds"): ReloadDisposition.QUIESCENT,
    ("ipaws", "url"): ReloadDisposition.QUIESCENT,
    ("ipaws", "user_agent"): ReloadDisposition.QUIESCENT,
    ("ipaws", "voice_events", "*"): ReloadDisposition.QUIESCENT,
    ("jobs", "assignment_ack_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("jobs", "busy_timeout_ms"): ReloadDisposition.RESTART_REQUIRED,
    ("jobs", "enabled"): ReloadDisposition.RESTART_REQUIRED,
    ("jobs", "event_retention"): ReloadDisposition.RESTART_REQUIRED,
    ("jobs", "lease_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("jobs", "path"): ReloadDisposition.RESTART_REQUIRED,
    ("jobs", "payload_max_bytes"): ReloadDisposition.RESTART_REQUIRED,
    ("jobs", "progress_retention"): ReloadDisposition.RESTART_REQUIRED,
    ("jobs", "reconciliation_batch_size"): ReloadDisposition.RESTART_REQUIRED,
    ("jobs", "required"): ReloadDisposition.RESTART_REQUIRED,
    ("jobs", "result_max_bytes"): ReloadDisposition.RESTART_REQUIRED,
    ("jobs", "shutdown_reconciliation_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("network", "api", "bind_host"): ReloadDisposition.RESTART_REQUIRED,
    ("network", "api", "port"): ReloadDisposition.RESTART_REQUIRED,
    ("network", "liquidsoap", "host"): ReloadDisposition.RESTART_REQUIRED,
    ("network", "liquidsoap", "port"): ReloadDisposition.RESTART_REQUIRED,
    ("network", "liquidsoap", "timeout_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("network", "postgresql", "address"): ReloadDisposition.RESTART_REQUIRED,
    ("network", "postgresql", "connect_timeout_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("network", "postgresql", "database"): ReloadDisposition.RESTART_REQUIRED,
    ("network", "postgresql", "enabled"): ReloadDisposition.RESTART_REQUIRED,
    ("network", "postgresql", "port"): ReloadDisposition.RESTART_REQUIRED,
    ("network", "postgresql", "tls"): ReloadDisposition.RESTART_REQUIRED,
    ("network", "redis", "address"): ReloadDisposition.RESTART_REQUIRED,
    ("network", "redis", "connect_timeout_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("network", "redis", "database"): ReloadDisposition.RESTART_REQUIRED,
    ("network", "redis", "enabled"): ReloadDisposition.RESTART_REQUIRED,
    ("network", "redis", "port"): ReloadDisposition.RESTART_REQUIRED,
    ("network", "redis", "tls"): ReloadDisposition.RESTART_REQUIRED,
    ("network", "swwp", "assignment_ack_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("network", "swwp", "bind_host"): ReloadDisposition.RESTART_REQUIRED,
    ("network", "swwp", "capability_validity_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("network", "swwp", "controller_path"): ReloadDisposition.RESTART_REQUIRED,
    ("network", "swwp", "heartbeat_interval_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("network", "swwp", "heartbeat_timeout_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("network", "swwp", "lease_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("network", "swwp", "max_message_bytes"): ReloadDisposition.RESTART_REQUIRED,
    ("network", "swwp", "verify_tls"): ReloadDisposition.RESTART_REQUIRED,
    ("network", "swwp", "worker_controller_url"): ReloadDisposition.RESTART_REQUIRED,
    ("lifecycle", "active_request_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("lifecycle", "publication_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("lifecycle", "resource_close_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("lifecycle", "source_stop_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("lifecycle", "task_cancel_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("lifecycle", "total_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("lifecycle", "tts_stop_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("lifecycle", "optional_tasks", "cooldown_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("lifecycle", "optional_tasks", "policy"): ReloadDisposition.RESTART_REQUIRED,
    ("lifecycle", "optional_tasks", "restart_initial_delay_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("lifecycle", "optional_tasks", "restart_max_delay_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("lifecycle", "optional_tasks", "stable_after_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("lifecycle", "optional_tasks", "thrash_limit"): ReloadDisposition.RESTART_REQUIRED,
    ("lifecycle", "optional_tasks", "thrash_window_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "discord", "alerts_enabled"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "discord", "alerts_url"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "discord", "alerttracker_lifecycle_log"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "discord", "api_enabled"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "discord", "api_url"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "discord", "cycle_rebuild_log"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "discord", "enabled"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "discord", "errors_enabled"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "discord", "errors_url"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "discord", "icon_cdn_url"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "discord", "ops_detail_log"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "discord", "ops_enabled"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "discord", "ops_url"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "discord", "post_tests"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "discord", "post_voice_only"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "discord", "rate_limit_per_minute"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "discord", "source_health_log"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "runtime", "asyncio_level"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "runtime", "cap_poll_summary"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "runtime", "color"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "runtime", "conductor_alert_push"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "runtime", "conductor_cycle_push"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "runtime", "conductor_live_time_push"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "runtime", "httpcore2_level"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "runtime", "httpx2_level"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "runtime", "httpcore_level"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "runtime", "httpx_level"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "runtime", "ipaws_poll_summary"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "runtime", "level"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "runtime", "logger_levels", "*"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "runtime", "segment_refresher_alert_lifecycle"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "runtime", "segment_refresher_synth"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "runtime", "slixmpp_level"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "runtime", "slixmpp_xmlstream_level"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "runtime", "uvicorn_access_level"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "runtime", "uvicorn_error_level"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "outputs", "alertmanager", "enabled"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "outputs", "alertmanager", "endpoint"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "outputs", "alertmanager", "queue_size"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "outputs", "alertmanager", "timeout_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "outputs", "collectors", "container_runtime_target"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "outputs", "collectors", "node_exporter_target"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "outputs", "collectors", "scrape_interval_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "outputs", "otlp", "enabled"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "outputs", "otlp", "endpoint"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "outputs", "otlp", "queue_size"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "outputs", "otlp", "timeout_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "outputs", "snmpv3", "auth_protocol"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "outputs", "snmpv3", "auth_secret_env"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "outputs", "snmpv3", "enabled"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "outputs", "snmpv3", "host"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "outputs", "snmpv3", "port"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "outputs", "snmpv3", "privacy_protocol"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "outputs", "snmpv3", "privacy_secret_env"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "outputs", "snmpv3", "queue_size"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "outputs", "snmpv3", "timeout_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "outputs", "snmpv3", "username"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "outputs", "syslog_tls", "ca_file"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "outputs", "syslog_tls", "enabled"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "outputs", "syslog_tls", "host"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "outputs", "syslog_tls", "port"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "outputs", "syslog_tls", "queue_size"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "outputs", "syslog_tls", "server_name"): ReloadDisposition.RESTART_REQUIRED,
    ("logs", "outputs", "syslog_tls", "timeout_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("mareas", "cache_days"): ReloadDisposition.RESTART_REQUIRED,
    ("mareas", "enabled"): ReloadDisposition.RESTART_REQUIRED,
    ("mareas", "url"): ReloadDisposition.RESTART_REQUIRED,
    ("now", "api_backfill", "enabled"): ReloadDisposition.RESTART_REQUIRED,
    ("now", "api_backfill", "initial_delay_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("now", "api_backfill", "interval_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("now", "api_backfill", "lookback_minutes"): ReloadDisposition.RESTART_REQUIRED,
    ("now", "api_backfill", "max_products_per_office"): ReloadDisposition.RESTART_REQUIRED,
    ("now", "default_expire_minutes"): ReloadDisposition.RESTART_REQUIRED,
    ("now", "enabled"): ReloadDisposition.RESTART_REQUIRED,
    ("now", "intro"): ReloadDisposition.RESTART_REQUIRED,
    ("nws", "user_agent"): ReloadDisposition.RESTART_REQUIRED,
    ("nwws", "allowed_wfos", "*"): ReloadDisposition.QUIESCENT,
    ("nwws", "enabled"): ReloadDisposition.RESTART_REQUIRED,
    ("nwws", "nick"): ReloadDisposition.RESTART_REQUIRED,
    ("nwws", "port"): ReloadDisposition.RESTART_REQUIRED,
    ("nwws", "resiliency", "backoff_max_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("nwws", "resiliency", "decision_log_every"): ReloadDisposition.RESTART_REQUIRED,
    ("nwws", "resiliency", "decision_log_first_n"): ReloadDisposition.RESTART_REQUIRED,
    ("nwws", "resiliency", "join_wait_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("nwws", "resiliency", "muc_confirm_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("nwws", "resiliency", "rx_log_first_n"): ReloadDisposition.RESTART_REQUIRED,
    ("nwws", "resiliency", "stall_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("nwws", "resiliency", "start_wait_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("nwws", "room"): ReloadDisposition.RESTART_REQUIRED,
    ("nwws", "server"): ReloadDisposition.RESTART_REQUIRED,
    ("observations", "stations", "*"): ReloadDisposition.QUIESCENT,
    ("paths", "audio_dir"): ReloadDisposition.RESTART_REQUIRED,
    ("paths", "cache_dir"): ReloadDisposition.RESTART_REQUIRED,
    ("paths", "config_dir"): ReloadDisposition.RESTART_REQUIRED,
    ("paths", "artifact_dir"): ReloadDisposition.RESTART_REQUIRED,
    ("paths", "diagnostic_export_dir"): ReloadDisposition.RESTART_REQUIRED,
    ("paths", "job_state_dir"): ReloadDisposition.RESTART_REQUIRED,
    ("paths", "log_dir"): ReloadDisposition.RESTART_REQUIRED,
    ("paths", "operational_state_dir"): ReloadDisposition.RESTART_REQUIRED,
    ("paths", "runtime_dir"): ReloadDisposition.RESTART_REQUIRED,
    ("paths", "secret_dir"): ReloadDisposition.RESTART_REQUIRED,
    ("paths", "temporary_dir"): ReloadDisposition.RESTART_REQUIRED,
    ("paths", "work_dir"): ReloadDisposition.RESTART_REQUIRED,
    ("pns", "default_expire_hours"): ReloadDisposition.RESTART_REQUIRED,
    ("pns", "enabled"): ReloadDisposition.RESTART_REQUIRED,
    ("pns", "hard_stop_delimiter"): ReloadDisposition.RESTART_REQUIRED,
    ("pns", "reject_audio_keywords", "*"): ReloadDisposition.RESTART_REQUIRED,
    ("pns", "subtypes", "*", "audio"): ReloadDisposition.RESTART_REQUIRED,
    ("pns", "subtypes", "*", "body_contains_all", "*"): ReloadDisposition.RESTART_REQUIRED,
    ("pns", "subtypes", "*", "body_contains_any", "*"): ReloadDisposition.RESTART_REQUIRED,
    ("pns", "subtypes", "*", "code"): ReloadDisposition.RESTART_REQUIRED,
    ("pns", "subtypes", "*", "enabled"): ReloadDisposition.RESTART_REQUIRED,
    ("pns", "subtypes", "*", "event"): ReloadDisposition.RESTART_REQUIRED,
    ("pns", "subtypes", "*", "headline_contains", "*"): ReloadDisposition.RESTART_REQUIRED,
    ("pns", "subtypes", "*", "intro"): ReloadDisposition.RESTART_REQUIRED,
    ("pns", "subtypes", "*", "key_prefix"): ReloadDisposition.RESTART_REQUIRED,
    ("pns", "subtypes", "*", "max_chars"): ReloadDisposition.RESTART_REQUIRED,
    ("pns", "subtypes", "*", "max_fresh_hours"): ReloadDisposition.RESTART_REQUIRED,
    ("pns", "subtypes", "*", "name"): ReloadDisposition.RESTART_REQUIRED,
    ("pns", "subtypes", "*", "reject_contains", "*"): ReloadDisposition.RESTART_REQUIRED,
    ("pns", "subtypes", "*", "require_same_day"): ReloadDisposition.RESTART_REQUIRED,
    ("pns", "suppress_unknown_audio"): ReloadDisposition.RESTART_REQUIRED,
    ("policy", "min_tone_gap_seconds"): ReloadDisposition.QUIESCENT,
    ("policy", "toneout_product_types", "*"): ReloadDisposition.QUIESCENT,
    ("same", "amplitude"): ReloadDisposition.QUIESCENT,
    ("same", "duration_minutes"): ReloadDisposition.QUIESCENT,
    ("same", "enabled"): ReloadDisposition.QUIESCENT,
    ("same", "native_encoder", "bin"): ReloadDisposition.QUIESCENT,
    ("same", "native_encoder", "enabled"): ReloadDisposition.QUIESCENT,
    ("same", "native_encoder", "fallback_to_python"): ReloadDisposition.QUIESCENT,
    ("same", "native_encoder", "timeout_seconds"): ReloadDisposition.QUIESCENT,
    ("same", "sender"): ReloadDisposition.QUIESCENT,
    ("samedec", "bin"): ReloadDisposition.RESTART_REQUIRED,
    ("samedec", "confidence"): ReloadDisposition.RESTART_REQUIRED,
    ("samedec", "start_delay_s"): ReloadDisposition.RESTART_REQUIRED,
    ("secrets", "api_token"): ReloadDisposition.RESTART_REQUIRED,
    ("secrets", "api_tokens_json"): ReloadDisposition.RESTART_REQUIRED,
    ("secrets", "icecast_admin_password"): ReloadDisposition.RESTART_REQUIRED,
    ("secrets", "icecast_relay_password"): ReloadDisposition.RESTART_REQUIRED,
    ("secrets", "icecast_source_password"): ReloadDisposition.RESTART_REQUIRED,
    ("secrets", "liquidsoap_host"): ReloadDisposition.RESTART_REQUIRED,
    ("secrets", "liquidsoap_port"): ReloadDisposition.RESTART_REQUIRED,
    ("secrets", "nwws_jid"): ReloadDisposition.RESTART_REQUIRED,
    ("secrets", "nwws_password"): ReloadDisposition.RESTART_REQUIRED,
    ("service_area", "transmitters", "*", "*", "name"): ReloadDisposition.QUIESCENT,
    ("service_area", "transmitters", "*", "*", "same_fips"): ReloadDisposition.QUIESCENT,
    ("station", "deployment_type"): ReloadDisposition.QUIESCENT,
    ("station", "disclaimer"): ReloadDisposition.QUIESCENT,
    ("station", "organization_name"): ReloadDisposition.QUIESCENT,
    ("station", "name"): ReloadDisposition.QUIESCENT,
    ("station", "now_playing_album"): ReloadDisposition.RESTART_REQUIRED,
    ("station", "now_playing_artist"): ReloadDisposition.RESTART_REQUIRED,
    ("station", "service_area_name"): ReloadDisposition.QUIESCENT,
    ("station", "service_name"): ReloadDisposition.QUIESCENT,
    ("station", "timezone"): ReloadDisposition.QUIESCENT,
    ("station_feed", "debug"): ReloadDisposition.RESTART_REQUIRED,
    ("station_feed", "enabled"): ReloadDisposition.RESTART_REQUIRED,
    ("station_feed", "ern_area_names"): ReloadDisposition.RESTART_REQUIRED,
    ("station_feed", "fetch_nws"): ReloadDisposition.RESTART_REQUIRED,
    ("station_feed", "housekeeping", "enabled"): ReloadDisposition.RESTART_REQUIRED,
    ("station_feed", "housekeeping", "grace_sec"): ReloadDisposition.RESTART_REQUIRED,
    ("station_feed", "housekeeping", "housekeep_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("station_feed", "housekeeping", "interval_sec"): ReloadDisposition.RESTART_REQUIRED,
    ("station_feed", "max_items"): ReloadDisposition.RESTART_REQUIRED,
    ("station_feed", "nwws", "tz_abbrev_overrides", "*"): ReloadDisposition.RESTART_REQUIRED,
    ("station_feed", "nwws", "vtec_event_labels", "*"): ReloadDisposition.RESTART_REQUIRED,
    ("station_feed", "source"): ReloadDisposition.RESTART_REQUIRED,
    ("station_feed", "station_id"): ReloadDisposition.RESTART_REQUIRED,
    ("station_feed", "ttl_seconds"): ReloadDisposition.RESTART_REQUIRED,
    ("stream", "icecast_host"): ReloadDisposition.RESTART_REQUIRED,
    ("stream", "icecast_mount"): ReloadDisposition.RESTART_REQUIRED,
    ("stream", "icecast_port"): ReloadDisposition.RESTART_REQUIRED,
    ("tests", "cap_block_seconds"): ReloadDisposition.QUIESCENT,
    ("tests", "enabled"): ReloadDisposition.QUIESCENT,
    ("tests", "ern_block_seconds"): ReloadDisposition.QUIESCENT,
    ("tests", "jitter_seconds"): ReloadDisposition.QUIESCENT,
    ("tests", "max_postpone_days"): ReloadDisposition.QUIESCENT,
    ("tests", "max_postpone_hours"): ReloadDisposition.QUIESCENT,
    ("tests", "postpone_minutes"): ReloadDisposition.QUIESCENT,
    ("tests", "postpone_policy"): ReloadDisposition.QUIESCENT,
    ("tests", "presentation", "area_text"): ReloadDisposition.QUIESCENT,
    ("tests", "presentation", "discord_area_text"): ReloadDisposition.QUIESCENT,
    ("tests", "presentation", "headline_template"): ReloadDisposition.QUIESCENT,
    ("tests", "rmt", "gate", "block_heightened"): ReloadDisposition.QUIESCENT,
    ("tests", "rmt", "gate", "block_recent_ern"): ReloadDisposition.QUIESCENT,
    ("tests", "rmt", "gate", "block_recent_severe_cap"): ReloadDisposition.QUIESCENT,
    ("tests", "rmt", "gate", "block_recent_toneout"): ReloadDisposition.QUIESCENT,
    ("tests", "rmt", "hour"): ReloadDisposition.QUIESCENT,
    ("tests", "rmt", "max_postpone_days"): ReloadDisposition.QUIESCENT,
    ("tests", "rmt", "max_postpone_hours"): ReloadDisposition.QUIESCENT,
    ("tests", "rmt", "minute"): ReloadDisposition.QUIESCENT,
    ("tests", "rmt", "nth"): ReloadDisposition.QUIESCENT,
    ("tests", "rmt", "postpone_minutes"): ReloadDisposition.QUIESCENT,
    ("tests", "rmt", "postpone_policy"): ReloadDisposition.QUIESCENT,
    ("tests", "rmt", "script_lines", "*"): ReloadDisposition.QUIESCENT,
    ("tests", "rmt", "weekday"): ReloadDisposition.QUIESCENT,
    ("tests", "rwt", "gate", "block_heightened"): ReloadDisposition.QUIESCENT,
    ("tests", "rwt", "gate", "block_recent_ern"): ReloadDisposition.QUIESCENT,
    ("tests", "rwt", "gate", "block_recent_severe_cap"): ReloadDisposition.QUIESCENT,
    ("tests", "rwt", "gate", "block_recent_toneout"): ReloadDisposition.QUIESCENT,
    ("tests", "rwt", "hour"): ReloadDisposition.QUIESCENT,
    ("tests", "rwt", "max_postpone_days"): ReloadDisposition.QUIESCENT,
    ("tests", "rwt", "max_postpone_hours"): ReloadDisposition.QUIESCENT,
    ("tests", "rwt", "minute"): ReloadDisposition.QUIESCENT,
    ("tests", "rwt", "postpone_minutes"): ReloadDisposition.QUIESCENT,
    ("tests", "rwt", "postpone_policy"): ReloadDisposition.QUIESCENT,
    ("tests", "rwt", "script_lines", "*"): ReloadDisposition.QUIESCENT,
    ("tests", "rwt", "weekday"): ReloadDisposition.QUIESCENT,
    ("tests", "toneout_cooldown_seconds"): ReloadDisposition.QUIESCENT,
    ("tts", "backend"): ReloadDisposition.QUIESCENT,
    ("tts", "fallback_backend"): ReloadDisposition.QUIESCENT,
    ("tts", "openai_compatible", "api_key_file"): ReloadDisposition.QUIESCENT,
    ("tts", "openai_compatible", "base_url"): ReloadDisposition.QUIESCENT,
    ("tts", "openai_compatible", "connect_timeout_seconds"): ReloadDisposition.QUIESCENT,
    ("tts", "openai_compatible", "max_error_bytes"): ReloadDisposition.QUIESCENT,
    ("tts", "openai_compatible", "max_input_bytes"): ReloadDisposition.QUIESCENT,
    ("tts", "openai_compatible", "max_response_bytes"): ReloadDisposition.QUIESCENT,
    ("tts", "openai_compatible", "model"): ReloadDisposition.QUIESCENT,
    ("tts", "openai_compatible", "response_format"): ReloadDisposition.QUIESCENT,
    ("tts", "openai_compatible", "speed"): ReloadDisposition.QUIESCENT,
    ("tts", "openai_compatible", "synthesis_timeout_seconds"): ReloadDisposition.QUIESCENT,
    ("tts", "openai_compatible", "verify_tls"): ReloadDisposition.QUIESCENT,
    ("tts", "openai_compatible", "voice"): ReloadDisposition.QUIESCENT,
    ("tts", "local", "engine"): ReloadDisposition.QUIESCENT,
    ("tts", "local", "rate_wpm"): ReloadDisposition.QUIESCENT,
    ("tts", "local", "voice"): ReloadDisposition.QUIESCENT,
    ("tts", "local", "voicetext_paul", "alias_overrides", "*", "alias"): ReloadDisposition.QUIESCENT,
    ("tts", "local", "voicetext_paul", "alias_overrides", "*", "ignore_case"): ReloadDisposition.QUIESCENT,
    ("tts", "local", "voicetext_paul", "alias_overrides", "*", "match"): ReloadDisposition.QUIESCENT,
    ("tts", "local", "voicetext_paul", "alias_overrides", "*", "regex"): ReloadDisposition.QUIESCENT,
    ("tts", "local", "voicetext_paul", "kill_before"): ReloadDisposition.QUIESCENT,
    ("tts", "local", "voicetext_paul", "phoneme_overrides_x_cmu", "*", "ignore_case"): ReloadDisposition.QUIESCENT,
    ("tts", "local", "voicetext_paul", "phoneme_overrides_x_cmu", "*", "match"): ReloadDisposition.QUIESCENT,
    ("tts", "local", "voicetext_paul", "phoneme_overrides_x_cmu", "*", "ph"): ReloadDisposition.QUIESCENT,
    ("tts", "local", "voicetext_paul", "phoneme_overrides_x_cmu", "*", "regex"): ReloadDisposition.QUIESCENT,
    ("tts", "local", "voicetext_paul", "reset_every"): ReloadDisposition.QUIESCENT,
    ("tts", "local", "voicetext_paul", "retries"): ReloadDisposition.QUIESCENT,
    ("tts", "local", "voicetext_paul", "retry_sleep_ms"): ReloadDisposition.QUIESCENT,
    ("tts", "local", "voicetext_paul", "run_as"): ReloadDisposition.QUIESCENT,
    ("tts", "local", "voicetext_paul", "vtml_lexicon"): ReloadDisposition.QUIESCENT,
    ("tts", "seasonal_ttsd", "base_url"): ReloadDisposition.QUIESCENT,
    ("tts", "seasonal_ttsd", "client_credential_file"): ReloadDisposition.QUIESCENT,
    ("tts", "seasonal_ttsd", "connect_timeout_seconds"): ReloadDisposition.QUIESCENT,
    ("tts", "seasonal_ttsd", "max_error_bytes"): ReloadDisposition.QUIESCENT,
    ("tts", "seasonal_ttsd", "max_input_bytes"): ReloadDisposition.QUIESCENT,
    ("tts", "seasonal_ttsd", "max_response_bytes"): ReloadDisposition.QUIESCENT,
    ("tts", "seasonal_ttsd", "profile"): ReloadDisposition.QUIESCENT,
    ("tts", "seasonal_ttsd", "refresh_margin_seconds"): ReloadDisposition.QUIESCENT,
    ("tts", "seasonal_ttsd", "synthesis_timeout_seconds"): ReloadDisposition.QUIESCENT,
    ("tts", "seasonal_ttsd", "token_timeout_seconds"): ReloadDisposition.QUIESCENT,
    ("tts", "seasonal_ttsd", "token_ttl_seconds"): ReloadDisposition.QUIESCENT,
    ("tts", "seasonal_ttsd", "verify_tls"): ReloadDisposition.QUIESCENT,
    ("tts", "seasonal_ttsd", "voice"): ReloadDisposition.QUIESCENT,
    ("tts", "rate_wpm"): ReloadDisposition.QUIESCENT,
    ("tts", "text_overrides", "*", "ignore_case"): ReloadDisposition.QUIESCENT,
    ("tts", "text_overrides", "*", "match"): ReloadDisposition.QUIESCENT,
    ("tts", "text_overrides", "*", "regex"): ReloadDisposition.QUIESCENT,
    ("tts", "text_overrides", "*", "replace"): ReloadDisposition.QUIESCENT,
    ("tts", "voice"): ReloadDisposition.QUIESCENT,
    ("tts", "voicetext_paul", "alias_overrides", "*", "alias"): ReloadDisposition.QUIESCENT,
    ("tts", "voicetext_paul", "alias_overrides", "*", "ignore_case"): ReloadDisposition.QUIESCENT,
    ("tts", "voicetext_paul", "alias_overrides", "*", "match"): ReloadDisposition.QUIESCENT,
    ("tts", "voicetext_paul", "alias_overrides", "*", "regex"): ReloadDisposition.QUIESCENT,
    ("tts", "voicetext_paul", "kill_before"): ReloadDisposition.QUIESCENT,
    ("tts", "voicetext_paul", "phoneme_overrides_x_cmu", "*", "ignore_case"): ReloadDisposition.QUIESCENT,
    ("tts", "voicetext_paul", "phoneme_overrides_x_cmu", "*", "match"): ReloadDisposition.QUIESCENT,
    ("tts", "voicetext_paul", "phoneme_overrides_x_cmu", "*", "ph"): ReloadDisposition.QUIESCENT,
    ("tts", "voicetext_paul", "phoneme_overrides_x_cmu", "*", "regex"): ReloadDisposition.QUIESCENT,
    ("tts", "voicetext_paul", "reset_every"): ReloadDisposition.QUIESCENT,
    ("tts", "voicetext_paul", "retries"): ReloadDisposition.QUIESCENT,
    ("tts", "voicetext_paul", "retry_sleep_ms"): ReloadDisposition.QUIESCENT,
    ("tts", "voicetext_paul", "run_as"): ReloadDisposition.QUIESCENT,
    ("tts", "voicetext_paul", "vtml_lexicon"): ReloadDisposition.QUIESCENT,
    ("tts", "volume"): ReloadDisposition.QUIESCENT,
    ("zonecounty", "base_url"): ReloadDisposition.RESTART_REQUIRED,
    ("zonecounty", "cache_days"): ReloadDisposition.RESTART_REQUIRED,
    ("zonecounty", "dbx_url"): ReloadDisposition.RESTART_REQUIRED,
    ("zonecounty", "enabled"): ReloadDisposition.RESTART_REQUIRED,
    ("zonecounty", "index_url"): ReloadDisposition.RESTART_REQUIRED,
}

DECLARED_POLICY_PATTERNS = frozenset(_DECLARED_POLICY)
AUTHORITATIVE_POLICY_PATTERNS = frozenset(ALL_POLICY_PATTERNS)

if DECLARED_POLICY_PATTERNS != AUTHORITATIVE_POLICY_PATTERNS:
    missing = tuple(sorted(AUTHORITATIVE_POLICY_PATTERNS - DECLARED_POLICY_PATTERNS))
    obsolete = tuple(sorted(DECLARED_POLICY_PATTERNS - AUTHORITATIVE_POLICY_PATTERNS))
    raise RuntimeError(
        f"reload policy declaration is out of sync with authoritative leaves; "
        f"missing={missing!r}, obsolete={obsolete!r}"
    )


def _rule_identity(pattern: tuple[str, ...], disposition: ReloadDisposition) -> str:
    safe = ".".join(pattern).replace("*", "item")
    return f"reload.v{RELOAD_POLICY_VERSION}.{disposition.value}.{safe}"


RULES = tuple(
    ReloadPolicyRule(pattern, disposition, _rule_identity(pattern, disposition))
    for pattern in ALL_POLICY_PATTERNS
    for disposition in (_DECLARED_POLICY[pattern],)
)
_RULES_BY_PATTERN = {rule.pattern: rule for rule in RULES}

if len(_RULES_BY_PATTERN) != len(ALL_POLICY_PATTERNS):
    raise RuntimeError("reload policy does not classify every schema and environment path exactly once")


def normalize_path(path: ConfigPath) -> tuple[str, ...]:
    return tuple(str(item) for item in path)


def classify_path(path: ConfigPath) -> ReloadPolicyRule:
    normalized = normalize_path(path)
    rule = _RULES_BY_PATTERN.get(normalized)
    if rule is not None:
        return rule
    for candidate in RULES:
        if len(candidate.pattern) != len(normalized):
            continue
        if all(expected in (_WILDCARD, actual) for expected, actual in zip(candidate.pattern, normalized, strict=True)):
            return candidate
    raise UnclassifiedPathError(f"configuration path is not classified: {path.to_pointer()}")


def policy_manifest() -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "pattern": "/" + "/".join(rule.pattern),
            "disposition": rule.disposition.value,
            "identity": rule.identity,
        }
        for rule in RULES
    )
