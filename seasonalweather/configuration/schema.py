"""Strict schema 1 for the complete supported YAML configuration."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from .issues import CompileIssue, IssuePhase
from .origins import OriginKind, ValueOrigin
from .paths import ROOT_PATH, ConfigPath
from .redaction import is_secret_path
from .source import (
    DEFAULT_LIMITS,
    CompilerLimits,
    NodeLocations,
    ParsedSource,
    SourceLocation,
)

CURRENT_CONFIG_SCHEMA = 1
SUPPORTED_CONFIG_SCHEMAS = frozenset({CURRENT_CONFIG_SCHEMA})
_MISSING = object()


class SchemaKind(StrEnum):
    OBJECT = "object"
    MAP = "map"
    LIST = "list"
    TUPLE = "tuple"
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"


@dataclass(frozen=True)
class SchemaNode:
    kind: SchemaKind
    fields: Mapping[str, SchemaNode] | None = None
    values: SchemaNode | None = None
    item: SchemaNode | None = None
    tuple_items: tuple[SchemaNode, ...] = ()
    required: bool = False
    default: object = _MISSING
    enum: frozenset[object] | None = None
    nullable: bool = False
    secret: bool = False
    min_length: int | None = None
    max_length: int | None = None


def public_schema_document() -> dict[str, object]:
    """Return the bounded, deterministic public configuration schema."""

    return {
        "config_schema": CURRENT_CONFIG_SCHEMA,
        "supported_config_schemas": sorted(SUPPORTED_CONFIG_SCHEMAS),
        "schema": _public_schema_node(SCHEMA_V1),
    }


def _public_schema_node(node: SchemaNode) -> dict[str, object]:
    result: dict[str, object] = {"type": node.kind.value, **_public_schema_metadata(node)}
    result.update(_public_schema_children(node))
    return result


def _public_schema_metadata(node: SchemaNode) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value, enabled in (
        ("nullable", True, node.nullable),
        ("required", True, node.required),
        ("secret", True, node.secret),
        ("min_length", node.min_length, node.min_length is not None),
        ("max_length", node.max_length, node.max_length is not None),
        ("default", node.default, node.default is not _MISSING),
    ):
        if enabled:
            result[key] = value
    if node.enum is not None:
        result["enum"] = sorted(node.enum, key=lambda item: str(item))
    return result


def _public_schema_children(node: SchemaNode) -> dict[str, object]:
    result: dict[str, object] = {}
    if node.fields is not None:
        result["properties"] = {key: _public_schema_node(child) for key, child in sorted(node.fields.items())}
    if node.values is not None:
        result["additional_properties"] = _public_schema_node(node.values)
    if node.item is not None:
        result["items"] = _public_schema_node(node.item)
    if node.tuple_items:
        result["prefix_items"] = [_public_schema_node(item) for item in node.tuple_items]
    return result


def _s(**kwargs: Any) -> SchemaNode:
    return SchemaNode(SchemaKind.STRING, **kwargs)


def _i(**kwargs: Any) -> SchemaNode:
    return SchemaNode(SchemaKind.INTEGER, **kwargs)


def _n(**kwargs: Any) -> SchemaNode:
    return SchemaNode(SchemaKind.NUMBER, **kwargs)


def _b(**kwargs: Any) -> SchemaNode:
    return SchemaNode(SchemaKind.BOOLEAN, **kwargs)


def _l(item: SchemaNode, **kwargs: Any) -> SchemaNode:
    return SchemaNode(SchemaKind.LIST, item=item, **kwargs)


def _t(*items: SchemaNode, **kwargs: Any) -> SchemaNode:
    return SchemaNode(SchemaKind.TUPLE, tuple_items=items, **kwargs)


def _m(values: SchemaNode, **kwargs: Any) -> SchemaNode:
    return SchemaNode(SchemaKind.MAP, values=values, **kwargs)


def _o(fields: Mapping[str, SchemaNode], **kwargs: Any) -> SchemaNode:
    return SchemaNode(SchemaKind.OBJECT, fields=fields, **kwargs)


def _strings(**kwargs: Any) -> SchemaNode:
    return _l(_s(), **kwargs)


def _gate() -> SchemaNode:
    return _o(
        {
            "block_heightened": _b(default=True),
            "block_recent_toneout": _b(default=True),
            "block_recent_severe_cap": _b(default=True),
            "block_recent_ern": _b(default=True),
        },
        default={},
    )


def _test_schedule(*, rmt: bool) -> SchemaNode:
    fields: dict[str, SchemaNode] = {
        "weekday": _i(default=2),
        "hour": _i(default=11),
        "minute": _i(default=0),
        "script_lines": _strings(default=[]),
        "postpone_policy": _s(default="delay_window"),
        "postpone_minutes": _i(default=15),
        "max_postpone_hours": _i(default=6),
        "max_postpone_days": _i(default=0 if rmt else 2),
        "gate": _gate(),
    }
    if rmt:
        fields["nth"] = _i(default=1)
    return _o(fields, default={})


_TEXT_OVERRIDE = _o(
    {
        "match": _s(required=True),
        "replace": _s(required=True),
        "regex": _b(default=False),
        "ignore_case": _b(default=False),
    }
)
_ALIAS_OVERRIDE = _o(
    {
        "match": _s(required=True),
        "alias": _s(required=True),
        "regex": _b(default=False),
        "ignore_case": _b(default=False),
    }
)
_PHONEME_OVERRIDE = _o(
    {
        "match": _s(required=True),
        "ph": _s(required=True),
        "regex": _b(default=False),
        "ignore_case": _b(default=False),
    }
)

_VOICE_TEXT_PAUL = _o(
    {
        "run_as": _s(default="voicetext"),
        "retries": _i(default=1),
        "retry_sleep_ms": _i(default=150),
        "reset_every": _i(default=0),
        "kill_before": _b(default=False),
        "vtml_lexicon": _b(default=True),
        "alias_overrides": _l(_ALIAS_OVERRIDE, default=[]),
        "phoneme_overrides_x_cmu": _l(_PHONEME_OVERRIDE, default=[]),
    },
    default={},
)
_PNS_SUBTYPE = _o(
    {
        "name": _s(required=True),
        "enabled": _b(required=True),
        "audio": _b(required=True),
        "event": _s(required=True),
        "code": _s(required=True),
        "key_prefix": _s(required=True),
        "intro": _s(required=True),
        "headline_contains": _strings(required=True),
        "body_contains_all": _strings(required=True),
        "body_contains_any": _strings(required=True),
        "reject_contains": _strings(required=True),
        "max_fresh_hours": _n(required=True),
        "require_same_day": _b(required=True),
        "max_chars": _i(required=True),
    }
)
_HEALTH_SOURCE = _o(
    {
        "enabled": _b(default=True),
        "role": _s(default="forecast"),
        "stale_after_seconds": _i(default=600),
        "failure_threshold": _i(default=3),
        "critical": _b(default=False),
    }
)
_TRANSMITTER_AREA = _o(
    {
        "name": _s(required=True),
        "same_fips": _s(required=True),
    }
)

SCHEMA_V1 = _o(
    {
        "config_schema": _i(),
        "station": _o(
            {
                "name": _s(required=True),
                "service_area_name": _s(required=True),
                "timezone": _s(required=True),
                "disclaimer": _s(required=True),
                "deployment_type": _s(
                    default="land",
                    enum=frozenset(
                        {
                            "land",
                            "coastal",
                            "land_coastal",
                            "land_marine",
                            "marine",
                        }
                    ),
                ),
            },
            required=True,
        ),
        "stream": _o(
            {
                "icecast_host": _s(required=True),
                "icecast_port": _i(required=True),
                "icecast_mount": _s(required=True),
            },
            required=True,
        ),
        "cycle": _o(
            {
                "normal_interval_seconds": _i(required=True),
                "heightened_interval_seconds": _i(required=True),
                "min_heightened_seconds": _i(required=True),
                "lead_time_seconds": _i(default=90),
                "alert_focus": _o(
                    {
                        "hold_event_codes": _strings(default=[]),
                        "hold_vtec_significance": _strings(default=[]),
                        "excluded_sources": _strings(default=[]),
                        "test_event_codes": _strings(default=[]),
                        "marine_event_codes": _strings(default=[]),
                        "marine_hold_event_codes": _strings(default=[]),
                    },
                    default={},
                ),
                "reference_points": _l(_t(_n(), _n(), _s()), required=True, min_length=1),
                "last_product_max_chars": _i(default=260),
                "spc": _o(
                    {
                        "enabled": _b(default=False),
                        "wfos": _strings(default=["LWX"]),
                        "days": _i(default=3),
                        "min_dn": _i(default=3),
                        "timeout_s": _n(default=6.0),
                    },
                    default={},
                ),
                "fc": _o(
                    {
                        "use_short": _b(default=True),
                        "periods_normal": _i(default=14),
                        "periods_per_group": _i(default=4),
                        "max_points_normal": _i(default=6),
                        "max_points_7day": _i(default=2),
                        "point_max_chars": _i(default=1600),
                        "line_max_chars": _i(default=1600),
                        "rotate_period_s": _i(default=300),
                        "rotate_step": _i(default=0),
                        "forecast_zones": _l(
                            _o(
                                {
                                    "id": _s(required=True),
                                    "label": _s(required=True),
                                }
                            ),
                            default=[],
                        ),
                    },
                    default={},
                ),
                "obs": _o(
                    {
                        "max_normal": _i(default=0),
                        "rotate_period_s": _i(default=300),
                        "rotate_step": _i(default=0),
                        "aliases": _m(_s(), default={}),
                    },
                    default={},
                ),
                "hwo": _o(
                    {
                        "max_chars_normal": _i(default=0),
                        "max_chars_heightened": _i(default=0),
                        "speak_unavailable": _b(default=True),
                    },
                    default={},
                ),
                "afd": _o(
                    {
                        "max_chars_normal": _i(default=0),
                        "max_chars_heightened": _i(default=0),
                    },
                    default={},
                ),
                "syn": _o(
                    {
                        "max_chars_normal": _i(default=1500),
                        "max_chars_heightened": _i(default=0),
                    },
                    default={},
                ),
                "cwf": _o(
                    {
                        "enabled": _b(default=False),
                        "offices": _strings(default=[]),
                        "max_chars_normal": _i(default=2000),
                        "max_chars_heightened": _i(default=1200),
                    },
                    default={},
                ),
                "offnt2": _o(
                    {
                        "enabled": _b(default=False),
                        "source_office": _s(default="KWBC"),
                        "product_type": _s(default="OFF"),
                        "zones": _l(
                            _o(
                                {
                                    "id": _s(required=True),
                                    "label": _s(required=True),
                                }
                            ),
                            default=[],
                        ),
                        "include_synopsis": _b(default=True),
                        "max_chars_normal": _i(default=2400),
                        "max_chars_heightened": _i(default=1200),
                        "max_airtime_seconds": _i(default=90),
                        "rotate_period_s": _i(default=1800),
                        "rotate_step": _i(default=1),
                        "defer_in_heightened": _b(default=True),
                    },
                    default={},
                ),
                "rwr": _o(
                    {
                        "enabled": _b(default=False),
                        "office": _s(default="LWX"),
                        "staleness_minutes": _i(default=75),
                        "anchor_stations": _strings(default=[]),
                        "fallback_stations": _strings(default=[]),
                        "pressure_trend_threshold_inhg": _n(default=0.02),
                        "pressure_cache_hours": _n(default=3.0),
                        "max_compact_per_section": _i(default=8),
                        "station_names": _m(_s(), default={}),
                    },
                    default={},
                ),
                "marine_obs": _o(
                    {
                        "enabled": _b(default=False),
                        "max_stations": _i(default=0),
                        "anchor_stations": _strings(default=[]),
                        "station_names": _m(_s(), default={}),
                    },
                    default={},
                ),
            },
            required=True,
        ),
        "observations": _o({"stations": _strings(required=True)}, required=True),
        "nwws": _o(
            {
                "enabled": _b(default=True),
                "server": _s(default="nwws-oi.weather.gov"),
                "port": _i(default=5222),
                "room": _s(default="NWWS@conference.nwws-oi.weather.gov"),
                "nick": _s(default="SeasonalWeather"),
                "allowed_wfos": _strings(default=[]),
                "resiliency": _o(
                    {
                        "stall_seconds": _i(default=60),
                        "muc_confirm_seconds": _i(default=30),
                        "start_wait_seconds": _i(default=25),
                        "join_wait_seconds": _i(default=35),
                        "backoff_max_seconds": _i(default=90),
                        "rx_log_first_n": _i(default=20),
                        "decision_log_first_n": _i(default=20),
                        "decision_log_every": _i(default=0),
                    },
                    default={},
                ),
            },
            required=True,
        ),
        "nws": _o({"user_agent": _s(default="")}, default={}),
        "pns": _o(
            {
                "enabled": _b(default=True),
                "default_expire_hours": _n(default=4.0),
                "hard_stop_delimiter": _s(default="&&"),
                "suppress_unknown_audio": _b(default=True),
                "reject_audio_keywords": _strings(default=[]),
                "subtypes": _l(_PNS_SUBTYPE, default=[]),
            },
            default={},
        ),
        "now": _o(
            {
                "enabled": _b(default=True),
                "intro": _s(default="A statement from the National Weather Service."),
                "default_expire_minutes": _i(default=60),
                "api_backfill": _o(
                    {
                        "enabled": _b(default=True),
                        "initial_delay_seconds": _i(default=15),
                        "interval_seconds": _i(default=120),
                        "lookback_minutes": _i(default=120),
                        "max_products_per_office": _i(default=25),
                    },
                    default={},
                ),
            },
            default={},
        ),
        "health": _o(
            {
                "enabled": _b(default=True),
                "check_interval_seconds": _i(default=30),
                "min_hold_seconds": _i(default=300),
                "detached_loop_only": _b(default=True),
                "source_impaired_message": _s(default=""),
                "degraded_message": _s(default=""),
                "critical_message": _s(default=""),
                "detached_message": _s(default=""),
                "sources": _m(_HEALTH_SOURCE, default={}),
            },
            default={},
        ),
        "policy": _o(
            {
                "toneout_product_types": _strings(required=True),
                "min_tone_gap_seconds": _n(default=2.0),
            },
            required=True,
        ),
        "same": _o(
            {
                "enabled": _b(default=True),
                "sender": _s(default="SEASNWXR"),
                "duration_minutes": _i(default=60),
                "amplitude": _n(default=0.35),
                "native_encoder": _o(
                    {
                        "enabled": _b(default=False),
                        "bin": _s(default="samegen"),
                        "timeout_seconds": _n(default=5.0),
                        "fallback_to_python": _b(default=True),
                    },
                    default={},
                ),
            },
            default={},
        ),
        "cap": _o(
            {
                "enabled": _b(default=True),
                "dryrun": _b(default=False),
                "poll_seconds": _i(default=60),
                "user_agent": _s(default="SeasonalWeather (CAP monitor)"),
                "url": _s(default=""),
                "ledger_path": _s(default=""),
                "ledger_max_age_days": _i(default=14),
                "full": _o(
                    {
                        "enabled": _b(default=True),
                        "severities": _strings(default=["Severe", "Extreme"]),
                        "events": _strings(default=[]),
                        "cooldown_seconds": _i(default=180),
                    },
                    default={},
                ),
                "voice": _o(
                    {
                        "enabled": _b(default=True),
                        "events": _strings(default=[]),
                        "cooldown_seconds": _i(default=600),
                    },
                    default={},
                ),
            },
            default={},
        ),
        "ipaws": _o(
            {
                "enabled": _b(default=False),
                "dryrun": _b(default=True),
                "poll_seconds": _i(default=60),
                "user_agent": _s(default="SeasonalWeather (IPAWS monitor)"),
                "url": _s(default=""),
                "ledger_path": _s(default=""),
                "ledger_max_age_days": _i(default=14),
                "full_events": _strings(default=[]),
                "voice_events": _strings(default=[]),
                "ern_dedup_ttl_seconds": _i(default=900),
            },
            default={},
        ),
        "ern": _o(
            {
                "enabled": _b(default=False),
                "dryrun": _b(default=True),
                "url": _s(default=""),
                "name": _s(default="ERN/JON"),
                "decoder_backend": _s(
                    default="auto",
                    enum=frozenset(
                        {
                            "auto",
                            "default",
                            "samedec",
                            "same_dec",
                            "rust",
                            "native",
                            "python",
                            "legacy",
                            "internal",
                        }
                    ),
                ),
                "sample_rate": _i(default=48000),
                "tail_seconds": _n(default=10.0),
                "trigger_ratio": _n(default=8.0),
                "dedupe_seconds": _n(default=20.0),
                "confidence_min": _n(default=0.25),
                "relay": _o(
                    {
                        "enabled": _b(default=False),
                        "events": _strings(default=["RWT", "RMT"]),
                        "min_confidence": _n(default=0.8),
                        "cooldown_seconds": _i(default=300),
                        "senders": _strings(default=[]),
                    },
                    default={},
                ),
            },
            default={},
        ),
        "samedec": _o(
            {
                "bin": _s(default="/usr/local/bin/samedec"),
                "confidence": _n(default=0.85),
                "start_delay_s": _n(default=1.4),
            },
            default={},
        ),
        "tests": _o(
            {
                "enabled": _b(default=False),
                "postpone_policy": _s(default="delay_window"),
                "postpone_minutes": _i(default=15),
                "max_postpone_hours": _i(default=6),
                "max_postpone_days": _i(default=2),
                "jitter_seconds": _i(default=60),
                "toneout_cooldown_seconds": _i(default=1800),
                "cap_block_seconds": _i(default=3600),
                "ern_block_seconds": _i(default=3600),
                "presentation": _o(
                    {
                        "headline_template": _s(default="{event} for the {service_area_name}"),
                        "area_text": _s(default=""),
                        "discord_area_text": _s(default=""),
                    },
                    default={},
                ),
                "rwt": _test_schedule(rmt=False),
                "rmt": _test_schedule(rmt=True),
            },
            default={},
        ),
        "zonecounty": _o(
            {
                "enabled": _b(default=True),
                "dbx_url": _s(default=""),
                "cache_days": _i(default=30),
                "index_url": _s(default="https://www.weather.gov/gis/ZoneCounty"),
                "base_url": _s(default=""),
            },
            default={},
        ),
        "mareas": _o(
            {
                "enabled": _b(default=True),
                "url": _s(default=""),
                "cache_days": _i(default=30),
            },
            default={},
        ),
        "station_feed": _o(
            {
                "enabled": _b(default=False),
                "station_id": _s(default="seasonalweather"),
                "source": _s(default="seasonalweather"),
                "max_items": _i(default=24),
                "ttl_seconds": _i(default=7200),
                "fetch_nws": _b(default=False),
                "debug": _b(default=False),
                "ern_area_names": _b(default=True),
                "housekeeping": _o(
                    {
                        "enabled": _b(default=True),
                        "interval_sec": _i(default=60),
                        "grace_sec": _i(default=5),
                        "housekeep_seconds": _i(default=30),
                    },
                    default={},
                ),
                "nwws": _o(
                    {
                        "vtec_event_labels": _m(_s(), default={}),
                        "tz_abbrev_overrides": _m(_s(), default={}),
                    },
                    default={},
                ),
            },
            default={},
        ),
        "api": _o(
            {
                "auth": _o(
                    {
                        "mode": _s(
                            required=True,
                            enum=frozenset({"static", "exchange", "hybrid"}),
                        ),
                        "subject": _s(default="local-admin"),
                        "scopes": _s(nullable=True),
                        "exchange": _o(
                            {
                                "minimum_ttl_seconds": _i(default=60),
                                "default_ttl_seconds": _i(default=900),
                                "maximum_read_ttl_seconds": _i(default=3600),
                                "maximum_write_ttl_seconds": _i(default=900),
                            },
                            default={},
                        ),
                    }
                ),
                "subject": _s(),
                "scopes": _s(nullable=True),
                "allow_remote": _b(default=False),
                "audio_max_bytes": _i(default=20971520),
                "audio_max_seconds": _i(default=180),
                "audio_ttl_seconds": _i(default=86400),
                "ffmpeg_bin": _s(default="ffmpeg"),
                "full_eas_heightened": _b(default=False),
                "manual_full_eas_heightens": _b(default=True),
            },
            default={},
        ),
        "dedupe": _o({"ttl_seconds": _i(default=900)}, default={}),
        "tts": _o(
            {
                "backend": _s(required=True),
                "fallback_backend": _s(nullable=True, default=None),
                "voice": _s(default="9"),
                "rate_wpm": _i(default=165),
                "volume": _n(default=1.0),
                "text_overrides": _l(_TEXT_OVERRIDE, default=[]),
                "local": _o(
                    {
                        "engine": _s(default="espeak-ng"),
                        "voice": _s(default="9"),
                        "rate_wpm": _i(default=165),
                        "voicetext_paul": _VOICE_TEXT_PAUL,
                    },
                    default={},
                ),
                "seasonal_ttsd": _o(
                    {
                        "base_url": _s(default=""),
                        "client_credential_file": _s(default=""),
                        "voice": _s(default="voicetext-paul"),
                        "profile": _s(default="wav-48k-stereo"),
                        "token_ttl_seconds": _i(default=900),
                        "refresh_margin_seconds": _i(default=120),
                        "connect_timeout_seconds": _n(default=5.0),
                        "token_timeout_seconds": _n(default=10.0),
                        "synthesis_timeout_seconds": _n(default=180.0),
                        "max_input_bytes": _i(default=65536),
                        "max_response_bytes": _i(default=67108864),
                        "max_error_bytes": _i(default=16384),
                        "verify_tls": _b(default=True),
                    },
                    default={},
                ),
                "openai_compatible": _o(
                    {
                        "base_url": _s(default=""),
                        "api_key_file": _s(default=""),
                        "model": _s(default=""),
                        "voice": _s(default=""),
                        "response_format": _s(default="wav"),
                        "speed": _n(default=1.0),
                        "connect_timeout_seconds": _n(default=5.0),
                        "synthesis_timeout_seconds": _n(default=180.0),
                        "max_input_bytes": _i(default=65536),
                        "max_response_bytes": _i(default=67108864),
                        "max_error_bytes": _i(default=16384),
                        "verify_tls": _b(default=True),
                    },
                    default={},
                ),
                # Legacy flat local configuration remains accepted and is
                # normalized deterministically by config.py.
                "voicetext_paul": _VOICE_TEXT_PAUL,
            },
            required=True,
        ),
        "audio": _o(
            {
                "sample_rate": _i(required=True),
                "attention_tone_hz": _i(required=True),
                "attention_tone_seconds": _n(required=True),
                "eom_beep_hz": _i(required=True),
                "eom_beep_seconds": _n(required=True),
                "inter_segment_silence_seconds": _n(required=True),
                "post_alert_silence_seconds": _n(required=True),
            },
            required=True,
        ),
        "paths": _o(
            {
                "work_dir": _s(required=True),
                "audio_dir": _s(required=True),
                "cache_dir": _s(required=True),
                "config_dir": _s(required=True),
                "log_dir": _s(required=True),
            },
            required=True,
        ),
        "lifecycle": _o(
            {
                "total_seconds": _n(default=30.0),
                "active_request_seconds": _n(default=10.0),
                "publication_seconds": _n(default=8.0),
                "source_stop_seconds": _n(default=8.0),
                "tts_stop_seconds": _n(default=8.0),
                "task_cancel_seconds": _n(default=5.0),
                "resource_close_seconds": _n(default=5.0),
            },
            default={},
        ),
        "database": _o(
            {
                "enabled": _b(default=True),
                "path": _s(default=""),
                "busy_timeout_ms": _i(default=5000),
                "journal_mode": _s(default="WAL"),
                "housekeeping": _o(
                    {
                        "enabled": _b(default=True),
                        "interval_seconds": _i(default=900),
                        "startup_delay_seconds": _i(default=45),
                        "api_command_retention_days": _i(default=14),
                        "audio_asset_grace_seconds": _i(default=900),
                        "generated_audio_retention_seconds": _i(default=10800),
                        "generated_audio_max_bytes": _i(default=1073741824),
                        "tmp_file_grace_seconds": _i(default=900),
                        "wal_checkpoint": _b(default=True),
                    },
                    default={},
                ),
            },
            default={},
        ),
        "jobs": _o(
            {
                "enabled": _b(default=False),
                "required": _b(default=False),
                "path": _s(default=""),
                "busy_timeout_ms": _i(default=5000),
                "lease_seconds": _i(default=60),
                "assignment_ack_seconds": _i(default=10),
                "progress_retention": _i(default=100),
                "event_retention": _i(default=500),
                "reconciliation_batch_size": _i(default=100),
                "payload_max_bytes": _i(default=65536),
                "result_max_bytes": _i(default=65536),
                "shutdown_reconciliation_seconds": _n(default=5.0),
            },
            default={},
        ),
        "service_area": _o(
            {"transmitters": _m(_l(_TRANSMITTER_AREA, min_length=1), required=True)},
            required=True,
        ),
        "logs": _o(
            {
                "runtime": _o(
                    {
                        "level": _s(default="INFO"),
                        "color": _s(
                            default="never",
                            enum=frozenset({"never", "auto", "always"}),
                        ),
                        "httpx_level": _s(default="WARNING"),
                        "httpcore_level": _s(default="WARNING"),
                        "uvicorn_access_level": _s(default="WARNING"),
                        "uvicorn_error_level": _s(default="INFO"),
                        "asyncio_level": _s(default="WARNING"),
                        "slixmpp_level": _s(default="WARNING"),
                        "slixmpp_xmlstream_level": _s(default="WARNING"),
                        "logger_levels": _m(_s(), default={}),
                        "cap_poll_summary": _b(default=False),
                        "ipaws_poll_summary": _b(default=False),
                        "conductor_cycle_push": _b(default=False),
                        "conductor_alert_push": _b(default=False),
                        "conductor_live_time_push": _b(default=False),
                        "segment_refresher_synth": _b(default=False),
                        "segment_refresher_alert_lifecycle": _b(default=False),
                    },
                    default={},
                ),
                "discord": _o(
                    {
                        "enabled": _b(default=False),
                        "alerts_enabled": _b(default=True),
                        "ops_enabled": _b(default=True),
                        "api_enabled": _b(default=True),
                        "errors_enabled": _b(default=True),
                        "rate_limit_per_minute": _i(default=20),
                        "post_tests": _b(default=True),
                        "post_voice_only": _b(default=True),
                        "cycle_rebuild_log": _b(default=True),
                        "alerttracker_lifecycle_log": _b(default=False),
                        "ops_detail_log": _b(default=False),
                        "source_health_log": _b(default=True),
                        "icon_cdn_url": _s(default=""),
                    },
                    default={},
                ),
            },
            default={},
        ),
    }
)


@dataclass(frozen=True)
class SchemaOutcome:
    issues: tuple[CompileIssue, ...]
    origins: tuple[ValueOrigin, ...]


def validate_schema(
    parsed: ParsedSource,
    *,
    limits: CompilerLimits = DEFAULT_LIMITS,
) -> SchemaOutcome:
    issues: list[CompileIssue] = []
    origins: list[ValueOrigin] = []
    _validate_node(
        parsed.value,
        SCHEMA_V1,
        path=ROOT_PATH,
        parsed=parsed,
        issues=issues,
        origins=origins,
    )
    ordered = sorted(issues, key=CompileIssue.sort_key)
    if len(ordered) > limits.max_issues:
        ordered = ordered[: max(0, limits.max_issues - 1)]
        ordered.append(
            CompileIssue(
                rule_id="compiler.issue_limit",
                phase=IssuePhase.SCHEMA,
                message="Additional configuration issues were omitted.",
                primary=parsed.document_location,
            )
        )
    return SchemaOutcome(
        issues=tuple(ordered),
        origins=tuple(
            sorted(
                origins,
                key=lambda origin: (
                    origin.path,
                    origin.kind.value,
                    origin.environment_variable or "",
                    origin.declaration_id or "",
                ),
            )
        ),
    )


def _validate_node(
    value: object,
    schema: SchemaNode,
    *,
    path: ConfigPath,
    parsed: ParsedSource,
    issues: list[CompileIssue],
    origins: list[ValueOrigin],
) -> None:
    if value is None and schema.nullable:
        _record_file_origin(path, parsed, origins)
        return
    if not _matches_kind(value, schema.kind):
        issues.append(
            _schema_issue(
                "schema.type",
                f"Expected {_kind_name(schema.kind)}.",
                path,
                parsed,
                span="value",
            )
        )
        return
    _record_file_origin(path, parsed, origins)
    if schema.enum is not None and value not in schema.enum:
        issues.append(
            _schema_issue(
                "schema.enum",
                "Value is not one of the supported choices.",
                path,
                parsed,
                span="value",
            )
        )
    if isinstance(value, str):
        _validate_length(value, schema, path, parsed, issues)
    _validate_container(
        value,
        schema,
        path=path,
        parsed=parsed,
        issues=issues,
        origins=origins,
    )


def _validate_container(
    value: object,
    schema: SchemaNode,
    *,
    path: ConfigPath,
    parsed: ParsedSource,
    issues: list[CompileIssue],
    origins: list[ValueOrigin],
) -> None:
    if schema.kind is SchemaKind.OBJECT:
        _validate_object(
            cast(dict[str, object], value),
            schema,
            path=path,
            parsed=parsed,
            issues=issues,
            origins=origins,
        )
    elif schema.kind is SchemaKind.MAP:
        _validate_map(
            cast(dict[str, object], value),
            schema,
            path=path,
            parsed=parsed,
            issues=issues,
            origins=origins,
        )
    elif schema.kind is SchemaKind.LIST:
        _validate_list(
            cast(list[object], value),
            schema,
            path=path,
            parsed=parsed,
            issues=issues,
            origins=origins,
        )
    elif schema.kind is SchemaKind.TUPLE:
        _validate_tuple(
            cast(list[object], value),
            schema,
            path=path,
            parsed=parsed,
            issues=issues,
            origins=origins,
        )


def _validate_object(
    value: dict[str, object],
    schema: SchemaNode,
    *,
    path: ConfigPath,
    parsed: ParsedSource,
    issues: list[CompileIssue],
    origins: list[ValueOrigin],
) -> None:
    fields = schema.fields or {}
    for key in sorted(value):
        if key not in fields:
            issues.append(
                _schema_issue(
                    "schema.unknown_field",
                    "Configuration field is not recognized.",
                    path.field(key),
                    parsed,
                    span="key",
                    help_text="Remove the field or use a supported schema field.",
                )
            )
    for name, child_schema in fields.items():
        child_path = path.field(name)
        if name in value:
            _validate_node(
                value[name],
                child_schema,
                path=child_path,
                parsed=parsed,
                issues=issues,
                origins=origins,
            )
        elif child_schema.required:
            issues.append(_missing_issue(child_path, path, parsed))
        elif child_schema.default is not _MISSING:
            _record_default_origins(child_path, child_schema, origins)


def _validate_map(
    value: dict[str, object],
    schema: SchemaNode,
    *,
    path: ConfigPath,
    parsed: ParsedSource,
    issues: list[CompileIssue],
    origins: list[ValueOrigin],
) -> None:
    for key in sorted(value):
        _validate_node(
            value[key],
            schema.values or _s(),
            path=path.field(key),
            parsed=parsed,
            issues=issues,
            origins=origins,
        )


def _validate_list(
    value: list[object],
    schema: SchemaNode,
    *,
    path: ConfigPath,
    parsed: ParsedSource,
    issues: list[CompileIssue],
    origins: list[ValueOrigin],
) -> None:
    _validate_length(value, schema, path, parsed, issues)
    for index, item in enumerate(value):
        _validate_node(
            item,
            schema.item or _s(),
            path=path.index(index),
            parsed=parsed,
            issues=issues,
            origins=origins,
        )


def _validate_tuple(
    value: list[object],
    schema: SchemaNode,
    *,
    path: ConfigPath,
    parsed: ParsedSource,
    issues: list[CompileIssue],
    origins: list[ValueOrigin],
) -> None:
    if len(value) != len(schema.tuple_items):
        issues.append(
            _schema_issue(
                "schema.tuple_length",
                "Sequence has the wrong number of items.",
                path,
                parsed,
                span="value",
            )
        )
        return
    for index, (item, item_schema) in enumerate(zip(value, schema.tuple_items, strict=True)):
        _validate_node(
            item,
            item_schema,
            path=path.index(index),
            parsed=parsed,
            issues=issues,
            origins=origins,
        )


def _matches_kind(value: object, kind: SchemaKind) -> bool:
    if kind is SchemaKind.INTEGER:
        return isinstance(value, int) and not isinstance(value, bool)
    if kind is SchemaKind.NUMBER:
        return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
    expected_types = {
        SchemaKind.OBJECT: dict,
        SchemaKind.MAP: dict,
        SchemaKind.LIST: list,
        SchemaKind.TUPLE: list,
        SchemaKind.STRING: str,
        SchemaKind.BOOLEAN: bool,
    }
    return isinstance(value, expected_types[kind])


def _kind_name(kind: SchemaKind) -> str:
    return {
        SchemaKind.OBJECT: "an object",
        SchemaKind.MAP: "an object",
        SchemaKind.LIST: "a sequence",
        SchemaKind.TUPLE: "a fixed-length sequence",
        SchemaKind.STRING: "a string",
        SchemaKind.INTEGER: "an integer",
        SchemaKind.NUMBER: "a finite number",
        SchemaKind.BOOLEAN: "a boolean",
    }[kind]


def _validate_length(
    value: str | list[object],
    schema: SchemaNode,
    path: ConfigPath,
    parsed: ParsedSource,
    issues: list[CompileIssue],
) -> None:
    if schema.min_length is not None and len(value) < schema.min_length:
        issues.append(
            _schema_issue(
                "schema.min_length",
                "Value contains fewer items or characters than permitted.",
                path,
                parsed,
                span="value",
            )
        )
    if schema.max_length is not None and len(value) > schema.max_length:
        issues.append(
            _schema_issue(
                "schema.max_length",
                "Value contains more items or characters than permitted.",
                path,
                parsed,
                span="value",
            )
        )


def _record_file_origin(
    path: ConfigPath,
    parsed: ParsedSource,
    origins: list[ValueOrigin],
) -> None:
    node = parsed.locations.get(path)
    if node is not None:
        origins.append(ValueOrigin(path=path, kind=OriginKind.FILE, location=node.value))


def _missing_issue(
    path: ConfigPath,
    parent: ConfigPath,
    parsed: ParsedSource,
) -> CompileIssue:
    parent_node = parsed.locations.get(parent)
    location = parent_node.value if parent_node else parsed.document_location
    insertion = SourceLocation(
        source_id=location.source_id,
        span=location.span,
        label="containing mapping",
    )
    return CompileIssue(
        rule_id="schema.required",
        phase=IssuePhase.SCHEMA,
        message="Required configuration field is missing.",
        path=path,
        primary=insertion,
        redacted=is_secret_path(path),
        help=f"Add the required {path.to_human()} field.",
    )


def _record_default_origins(
    path: ConfigPath,
    schema: SchemaNode,
    origins: list[ValueOrigin],
) -> None:
    origins.append(
        ValueOrigin(
            path=path,
            kind=OriginKind.DEFAULT,
            declaration_id=f"schema.v1:{path.to_pointer()}",
        )
    )
    if schema.kind is not SchemaKind.OBJECT:
        return
    for name, child in (schema.fields or {}).items():
        if child.default is not _MISSING:
            _record_default_origins(path.field(name), child, origins)


def _schema_issue(
    rule_id: str,
    message: str,
    path: ConfigPath,
    parsed: ParsedSource,
    *,
    span: str,
    help_text: str | None = None,
) -> CompileIssue:
    node: NodeLocations | None = parsed.locations.get(path)
    location = None
    if node:
        location = node.key if span == "key" and node.key else node.value
    if location is None:
        location = parsed.document_location
    return CompileIssue(
        rule_id=rule_id,
        phase=IssuePhase.SCHEMA,
        message=message,
        path=path,
        primary=location,
        redacted=is_secret_path(path),
        help=help_text,
    )
