"""Read-only controller application services for API projections."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
from pathlib import Path
from typing import Any

from seasonalweather.application.errors import ControlError
from seasonalweather.configuration.inspection import (
    configuration_schema,
    effective_configuration,
    validate_configuration,
)
from seasonalweather.database.station_feed import StationFeedRepository


class RuntimeReadModelService:
    """Own bounded status, station-feed, and configuration projections."""

    def __init__(self, orchestrator: Any, *, config_path: str) -> None:
        self.orch = orchestrator
        self.config_path = str(config_path)
        self._station_feed_repo = getattr(orchestrator, "station_feed_repo", None)
        if self._station_feed_repo is None:
            database = getattr(orchestrator, "database", None)
            if database is not None:
                self._station_feed_repo = StationFeedRepository(database)

    @staticmethod
    def _serialize(value: dt.datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone(dt.UTC).replace(microsecond=0).isoformat()

    def _now_utc(self) -> dt.datetime:
        return dt.datetime.now(dt.UTC)

    def _config_file_hash(self) -> str | None:
        try:
            digest = hashlib.sha256()
            with Path(self.config_path).open("rb") as stream:
                for chunk in iter(lambda: stream.read(65536), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        except Exception:
            return None

    def _empty_station_feed(self) -> dict[str, Any]:
        return {
            "stationId": self.orch.cfg.station_feed.station_id,
            "generatedAt": self._serialize(self._now_utc()),
            "source": self.orch.cfg.station_feed.source,
            "alerts": [],
        }

    async def health(self) -> dict[str, Any]:
        try:
            reachable = bool(self.orch.telnet.ping())
        except Exception:
            reachable = False
        return {"ok": reachable, "liquidsoap_telnet": {"reachable": reachable}, "api": {"version": "1.1.0"}}

    async def status(self) -> dict[str, Any]:
        self.orch._update_mode()
        try:
            reachable = bool(self.orch.telnet.ping())
        except Exception:
            reachable = False
        source = getattr(self.orch, "nwws_source", None)
        source_health = None
        if source is not None:
            try:
                source_health = source.health().to_dict()
            except Exception:
                source_health = {"state": "unavailable"}
        return {
            "mode": getattr(self.orch, "mode", "unknown"),
            "heightened_until": self._serialize(getattr(self.orch, "heightened_until", None)),
            "last_heightened_at": self._serialize(getattr(self.orch, "last_heightened_at", None)),
            "last_product_desc": getattr(self.orch, "last_product_desc", None),
            "liquidsoap_telnet_reachable": reachable,
            "nwws_queue_size": int(getattr(self.orch, "nwws_queue", asyncio.Queue()).qsize()),
            "nwws_source": source_health,
            "cap_queue_size": int(getattr(self.orch, "cap_queue", asyncio.Queue()).qsize()),
            "ern_queue_size": int(getattr(self.orch, "ern_queue", asyncio.Queue()).qsize()),
            "config_sha256": self._config_file_hash(),
        }

    async def station_feed(self, *, missing_ok: bool = False) -> dict[str, Any]:
        repository = self._station_feed_repo
        if repository is not None:
            try:
                return {
                    "stationId": self.orch.cfg.station_feed.station_id,
                    "generatedAt": self._serialize(self._now_utc()),
                    "source": self.orch.cfg.station_feed.source,
                    "alerts": repository.load_alerts(
                        now=self._now_utc(),
                        max_items=max(1, int(self.orch.cfg.station_feed.max_items or 1)),
                    ),
                }
            except Exception as exc:
                if not missing_ok:
                    raise ControlError(
                        "station_feed_database_error",
                        "Station feed SQLite read model could not be loaded.",
                    ) from exc
        if missing_ok:
            return self._empty_station_feed()
        raise ControlError("station_feed_database_unavailable", "Station feed SQLite read model is unavailable.")

    async def public_handled_alerts(self) -> dict[str, Any]:
        return await self.station_feed(missing_ok=True)

    async def config_summary(self) -> dict[str, Any]:
        cfg = self.orch.cfg
        return {
            "config_path": {"configured": bool(self.config_path)},
            "config_sha256": self._config_file_hash(),
            "station": {
                "name": cfg.station.name,
                "service_area_name": cfg.station.service_area_name,
                "timezone": cfg.station.timezone,
            },
            "cycle": {
                "normal_interval_seconds": cfg.cycle.normal_interval_seconds,
                "heightened_interval_seconds": cfg.cycle.heightened_interval_seconds,
                "min_heightened_seconds": cfg.cycle.min_heightened_seconds,
                "reference_point_count": len(cfg.cycle.reference_points),
            },
            "observations": {"stations": list(cfg.observations.stations)},
            "nwws": {"server": cfg.nwws.server, "port": cfg.nwws.port, "allowed_wfos": list(cfg.nwws.allowed_wfos)},
            "policy": {
                "toneout_product_types": list(cfg.policy.toneout_product_types),
                "min_tone_gap_seconds": cfg.policy.min_tone_gap_seconds,
            },
            "api": {
                "auth": {
                    "mode": cfg.api.auth.mode.value,
                    "credential_count": len(cfg.api.auth.credentials),
                    "legacy_mode_normalized": cfg.api.auth.legacy_mode_normalized,
                    "legacy_scope_normalized": cfg.api.auth.legacy_scope_normalized,
                    "exchange_available": bool(
                        cfg.database.enabled and cfg.api.auth.mode.value in {"exchange", "hybrid"}
                    ),
                    "store": {"kind": "controller-sqlite"},
                    "ttl_policy": {
                        "minimum_seconds": cfg.api.auth.exchange.minimum_ttl_seconds,
                        "default_seconds": cfg.api.auth.exchange.default_ttl_seconds,
                        "maximum_read_seconds": cfg.api.auth.exchange.maximum_read_ttl_seconds,
                        "maximum_write_seconds": cfg.api.auth.exchange.maximum_write_ttl_seconds,
                    },
                },
                "allow_remote": cfg.api.allow_remote,
            },
            "tts": {
                "backend": cfg.tts.backend,
                "engine": cfg.tts.local.engine,
                "voice": cfg.tts.voice,
                "rate_wpm": cfg.tts.rate_wpm,
                "volume": cfg.tts.volume,
            },
            "audio": {
                "sample_rate": cfg.audio.sample_rate,
                "attention_tone_hz": cfg.audio.attention_tone_hz,
                "attention_tone_seconds": cfg.audio.attention_tone_seconds,
                "post_alert_silence_seconds": cfg.audio.post_alert_silence_seconds,
            },
            "service_area": {
                "same_fips_count": len(cfg.service_area.same_fips_all),
                "transmitter_count": len(cfg.service_area.transmitters),
            },
            "features": {"station_feed_enabled": cfg.station_feed.enabled},
        }

    async def config_schema(self) -> dict[str, object]:
        return configuration_schema()

    async def effective_config(self) -> dict[str, object]:
        return effective_configuration(self.orch.cfg)

    async def validate_config(self, *, preflight: bool = False, warnings_as_errors: bool = False) -> dict[str, object]:
        return await validate_configuration(
            self.config_path,
            preflight=preflight,
            warnings_as_errors=warnings_as_errors,
        )
