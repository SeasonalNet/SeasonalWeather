from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import shutil
import subprocess
import uuid
import wave
from pathlib import Path
from typing import Any

from .api.models import (
    CreateAudioInsertRequest,
    CreateTextInsertRequest,
    InterruptPolicy,
    OriginateAudioRequest,
    OriginateTextRequest,
    VoiceMode,
)
from .broadcast.segment_store import render_segment_wav_async
from .database.assets import AudioAssetRepository
from .database.inserts import CycleInsertRepository
from .database.station_feed import StationFeedRepository
from .same.locations import (
    normalize_same_location,
    same_location_matches_service_area,
)
from .tts.audio import wav_duration_seconds
from .validation.admission import validate_wav_upload


class ControlError(Exception):
    def __init__(
        self, code: str, message: str, *, status_code: int = 422, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class NotFoundError(ControlError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(code, message, status_code=404, details=details)


class ConflictError(ControlError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(code, message, status_code=409, details=details)


class DependencyUnavailableError(ControlError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(code, message, status_code=503, details=details)


class OrchestratorControl:
    def __init__(self, orch: Any, *, config_path: str) -> None:
        self.orch = orch
        self.config_path = str(config_path)
        self._assets_lock = asyncio.Lock()
        self._supported_interrupt_policies = {InterruptPolicy.INTERRUPT_THEN_REFILL.value}
        db = getattr(self.orch, "database", None)
        self._audio_asset_repo = AudioAssetRepository(db) if db is not None else None
        self._cycle_insert_repo = CycleInsertRepository(db) if db is not None else None
        self._station_feed_repo = getattr(self.orch, "station_feed_repo", None)
        if self._station_feed_repo is None and db is not None:
            self._station_feed_repo = StationFeedRepository(db)

    def _now_utc(self) -> dt.datetime:
        return dt.datetime.now(dt.UTC)

    def _now_local(self) -> dt.datetime:
        tz = getattr(self.orch, "_tz", dt.UTC)
        return dt.datetime.now(tz=tz)

    def _work_paths(self) -> tuple[Path, Path, Path, Path]:
        return self.orch._paths()

    def _api_work_dir(self) -> Path:
        work_dir, _audio_dir, _cache_dir, _log_dir = self._work_paths()
        path = work_dir / "api"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _audio_asset_dir(self) -> Path:
        path = self._api_work_dir() / "uploads"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _sha256_file(self, path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def _config_file_hash(self) -> str | None:
        try:
            return self._sha256_file(Path(self.config_path))
        except Exception:
            return None

    def _empty_station_feed_payload(self) -> dict[str, Any]:
        return {
            "stationId": self.orch.cfg.station_feed.station_id,
            "generatedAt": self._serialize_dt(self._now_utc()),
            "source": self.orch.cfg.station_feed.source,
            "alerts": [],
        }

    def _asset_expiry_seconds(self) -> int:
        return max(300, min(self.orch.cfg.api.audio_ttl_seconds, 7 * 86400))

    def _asset_max_size_bytes(self) -> int:
        return max(1024, self.orch.cfg.api.audio_max_bytes)

    def _asset_max_duration_seconds(self) -> float:
        return max(1.0, min(float(self.orch.cfg.api.audio_max_seconds), 3600.0))

    def _require_insert_repo(self) -> CycleInsertRepository:
        if self._cycle_insert_repo is None:
            raise DependencyUnavailableError(
                "database_required",
                "Scheduled broadcast inserts require the SQLite database to be enabled.",
            )
        return self._cycle_insert_repo

    def _to_utc_dt(self, value: dt.datetime) -> dt.datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ControlError("invalid_datetime", "Datetime values must include a timezone offset.")
        return value.astimezone(dt.UTC).replace(microsecond=0)

    def _insert_audio_path(self, insert_id: str) -> Path:
        _work_dir, audio_dir, _cache_dir, _logs_dir = self._work_paths()
        return audio_dir / f"insert_{insert_id}.wav"

    def _validate_insert_id(self, insert_id: str) -> str:
        v = str(insert_id or "").strip()
        if not v or len(v) > 64 or not all(ch.isalnum() or ch in {"_", "-"} for ch in v):
            raise ControlError("invalid_insert_id", "insert_id contains unsupported characters.")
        return v

    def _enum_value(self, value: Any) -> str:
        return str(getattr(value, "value", value))

    def _station_sample_rate(self) -> int:
        try:
            return int(getattr(self.orch.cfg.audio, "sample_rate", 16000) or 16000)
        except Exception:
            return 16000

    def _ffmpeg_bin(self) -> str:
        return self.orch.cfg.api.ffmpeg_bin or "ffmpeg"

    def _require_ffmpeg(self) -> str:
        ffmpeg = self._ffmpeg_bin()
        resolved = shutil.which(ffmpeg)
        if not resolved:
            raise DependencyUnavailableError(
                "ffmpeg_missing",
                "ffmpeg is required for uploaded-audio normalization but was not found in PATH.",
                details={"binary": ffmpeg},
            )
        return resolved

    def _probe_station_wav(self, path: Path) -> dict[str, Any]:
        try:
            with wave.open(str(path), "rb") as wf:
                channels = int(wf.getnchannels())
                sample_rate_hz = int(wf.getframerate())
                frames = int(wf.getnframes())
                sampwidth = int(wf.getsampwidth())
        except wave.Error as exc:
            raise ControlError(
                "invalid_wav", "Normalized WAV could not be parsed.", details={"path": str(path)}
            ) from exc

        duration_seconds = float(frames) / float(sample_rate_hz or 1)
        expected_rate = self._station_sample_rate()
        if channels != 2 or sampwidth != 2 or sample_rate_hz != expected_rate:
            raise ControlError(
                "normalized_wav_mismatch",
                "Normalized WAV does not match the station playout format.",
                details={
                    "path": str(path),
                    "channels": channels,
                    "sample_width_bytes": sampwidth,
                    "sample_rate_hz": sample_rate_hz,
                    "expected_channels": 2,
                    "expected_sample_width_bytes": 2,
                    "expected_sample_rate_hz": expected_rate,
                },
            )
        if duration_seconds <= 0.0:
            raise ControlError("invalid_wav_duration", "Uploaded WAV has zero duration.")
        if duration_seconds > self._asset_max_duration_seconds():
            raise ControlError(
                "wav_too_long",
                "Uploaded WAV exceeds the configured duration limit.",
                details={"max_seconds": self._asset_max_duration_seconds(), "duration_seconds": duration_seconds},
            )
        return {
            "duration_seconds": duration_seconds,
            "sample_rate_hz": sample_rate_hz,
            "channels": channels,
            "frames": frames,
            "sample_width_bytes": sampwidth,
        }

    def _normalize_uploaded_wav(self, *, src_path: Path, dest_path: Path) -> dict[str, Any]:
        ffmpeg = self._require_ffmpeg()
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src_path),
            "-map_metadata",
            "-1",
            "-vn",
            "-ac",
            "2",
            "-ar",
            str(self._station_sample_rate()),
            "-c:a",
            "pcm_s16le",
            str(dest_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not dest_path.exists():
            stderr = (proc.stderr or proc.stdout or "").strip()
            raise ControlError(
                "audio_normalization_failed",
                "Uploaded WAV could not be normalized to the station playout format.",
                details={"stderr": stderr[-1200:]},
            )
        return self._probe_station_wav(dest_path)

    def _serialize_dt(self, value: dt.datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone(dt.UTC).replace(microsecond=0).isoformat()

    def _ensure_backend_ready(self) -> None:
        try:
            ok = bool(self.orch.telnet.ping())
        except Exception as exc:
            raise DependencyUnavailableError(
                "liquidsoap_unreachable", "Liquidsoap telnet backend is unavailable."
            ) from exc
        if not ok:
            raise DependencyUnavailableError("liquidsoap_unreachable", "Liquidsoap telnet backend is unavailable.")

    def _same_codes_in_service_area(self, same_codes: list[str]) -> list[str]:
        allow = getattr(self.orch, "_same_fips_allow_set", set())
        normalized: list[str] = []
        disallowed: list[str] = []

        for raw in same_codes:
            code = normalize_same_location(raw)
            if not code:
                disallowed.append(str(raw))
                continue
            # Manual FULL EAS origination must not auto-wildcard a whole state.
            # State-wide codes are allowed when explicitly configured, but not
            # merely because one county in that state is configured.
            if not same_location_matches_service_area(code, allow, allow_statewide_input=False):
                disallowed.append(code)
                continue
            normalized.append(code)

        if disallowed:
            raise ControlError(
                "same_code_out_of_service_area",
                "One or more SAME codes are outside the configured service area.",
                details={"same_codes": disallowed},
            )
        return normalized

    def _validate_interrupt_policy(self, policy: str) -> None:
        if policy not in self._supported_interrupt_policies:
            raise ControlError(
                "interrupt_policy_unsupported",
                "This Liquidsoap integration currently supports only interrupt_then_refill.",
                details={"supported": sorted(self._supported_interrupt_policies), "requested": policy},
            )

    def _manual_full_eas_should_heighten(self) -> bool:
        return self.orch.cfg.api.full_eas_heightened

    async def get_health(self) -> dict[str, Any]:
        try:
            liquidsoap_ok = bool(self.orch.telnet.ping())
        except Exception:
            liquidsoap_ok = False
        return {
            "ok": liquidsoap_ok,
            "liquidsoap_telnet": {"reachable": liquidsoap_ok},
            "api": {"version": "1.1.0"},
        }

    async def get_status(self) -> dict[str, Any]:
        self.orch._update_mode()
        try:
            liquidsoap_ok = bool(self.orch.telnet.ping())
        except Exception:
            liquidsoap_ok = False
        nwws_source = getattr(self.orch, "nwws_source", None)
        nwws_health = None
        if nwws_source is not None:
            try:
                nwws_health = nwws_source.health().to_dict()
            except Exception:
                nwws_health = {"state": "unavailable"}
        return {
            "mode": getattr(self.orch, "mode", "unknown"),
            "heightened_until": self._serialize_dt(getattr(self.orch, "heightened_until", None)),
            "last_heightened_at": self._serialize_dt(getattr(self.orch, "last_heightened_at", None)),
            "last_product_desc": getattr(self.orch, "last_product_desc", None),
            "liquidsoap_telnet_reachable": liquidsoap_ok,
            "nwws_queue_size": int(getattr(self.orch, "nwws_queue", asyncio.Queue()).qsize()),
            "nwws_source": nwws_health,
            "cap_queue_size": int(getattr(self.orch, "cap_queue", asyncio.Queue()).qsize()),
            "ern_queue_size": int(getattr(self.orch, "ern_queue", asyncio.Queue()).qsize()),
            "config_sha256": self._config_file_hash(),
        }

    async def get_station_feed(self, *, missing_ok: bool = False) -> dict[str, Any]:
        repo = self._station_feed_repo
        if repo is not None:
            try:
                return {
                    "stationId": self.orch.cfg.station_feed.station_id,
                    "generatedAt": self._serialize_dt(self._now_utc()),
                    "source": self.orch.cfg.station_feed.source,
                    "alerts": repo.load_alerts(
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
            return self._empty_station_feed_payload()
        raise ControlError(
            "station_feed_database_unavailable",
            "Station feed SQLite read model is unavailable.",
        )

    async def get_public_handled_alerts(self) -> dict[str, Any]:
        return await self.get_station_feed(missing_ok=True)

    async def get_config_summary(self) -> dict[str, Any]:
        cfg = self.orch.cfg
        return {
            "config_path": self.config_path,
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
            "nwws": {
                "server": cfg.nwws.server,
                "port": cfg.nwws.port,
                "allowed_wfos": list(cfg.nwws.allowed_wfos),
            },
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
                    "store": {
                        "kind": "controller-sqlite",
                        "path": cfg.database.path,
                    },
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
            "features": {
                "station_feed_enabled": self.orch.cfg.station_feed.enabled,
            },
        }

    async def rebuild_cycle(self, *, reason: str | None, actor: str) -> dict[str, Any]:
        self._ensure_backend_ready()
        reason_text = (reason or "admin-request").strip() or "admin-request"
        self.orch._schedule_cycle_refill(reason=f"api-{reason_text[:48]}")
        # _API_REBUILD_DL_
        try:
            self.orch.discord.api_action(
                method="POST",
                endpoint="/v1/cycle/rebuild",
                actor=actor,
                status="succeeded",
                details={"reason": reason_text, "mode": getattr(self.orch, "mode", "unknown")},
            )
        except Exception:
            pass
        return {
            "ok": True,
            "reason": reason_text,
            "actor": actor,
            "mode": getattr(self.orch, "mode", "unknown"),
        }

    async def set_heightened_mode(self, *, minutes: int, reason: str, actor: str) -> dict[str, Any]:
        now = self._now_local()
        self.orch.last_heightened_at = now
        self.orch.heightened_until = now + dt.timedelta(minutes=minutes)
        self.orch._update_mode()
        self.orch.last_product_desc = f"Manual heightened mode: {reason}"[:200]
        self.orch._schedule_cycle_refill(reason="api-heightened")
        # _API_SET_HEIGHTENED_DL_
        try:
            self.orch.discord.api_action(
                method="POST",
                endpoint="/v1/mode/heightened",
                actor=actor,
                status="succeeded",
                details={"minutes": minutes, "reason": reason},
            )
        except Exception:
            pass
        return {
            "ok": True,
            "mode": getattr(self.orch, "mode", "unknown"),
            "heightened_until": self._serialize_dt(self.orch.heightened_until),
            "reason": reason,
            "actor": actor,
        }

    async def clear_heightened_mode(self, *, reason: str | None, actor: str) -> dict[str, Any]:
        self.orch.heightened_until = None
        self.orch._update_mode()
        self.orch.last_product_desc = (
            f"Manual heightened mode cleared: {reason}" if reason else "Manual heightened mode cleared"
        )[:200]
        self.orch._schedule_cycle_refill(reason="api-clear-heightened")
        # _API_CLEAR_HEIGHTENED_DL_
        try:
            self.orch.discord.api_action(
                method="POST",
                endpoint="/v1/mode/clear",
                actor=actor,
                status="succeeded",
                details={"reason": reason or ""},
            )
        except Exception:
            pass
        return {
            "ok": True,
            "mode": getattr(self.orch, "mode", "unknown"),
            "reason": reason,
            "actor": actor,
        }

    async def originate_test(self, *, event_code: str, actor: str) -> dict[str, Any]:
        self._ensure_backend_ready()
        allowed, why = self.orch.tests_runtime.gate()
        if not allowed:
            raise ConflictError(
                "test_gate_blocked", "Required test origination is currently blocked.", details={"reason": why}
            )
        await self.orch.tests_runtime.originate_required_test(event_code)
        # _API_ORIGINATE_TEST_DL_
        try:
            self.orch.discord.api_action(
                method="POST",
                endpoint="/v1/originate/test",
                actor=actor,
                status="succeeded",
                details={"event_code": event_code},
            )
        except Exception:
            pass
        return {"ok": True, "event_code": event_code, "actor": actor}

    async def stage_wav_upload(self, *, filename: str, content_type: str, data: bytes, actor: str) -> dict[str, Any]:
        rejection = validate_wav_upload(
            filename=filename,
            content_type=content_type,
            size_bytes=len(data),
            maximum_bytes=self._asset_max_size_bytes(),
        )
        if rejection is not None:
            details = {"max_bytes": rejection.maximum_bytes} if rejection.maximum_bytes is not None else None
            raise ControlError(
                rejection.reason_code,
                rejection.message,
                status_code=rejection.status_code,
                details=details,
            )

        filename_clean = Path(filename or "upload.wav").name or "upload.wav"

        asset_id = f"aud_{uuid.uuid4().hex[:20]}"
        asset_dir = self._audio_asset_dir()
        src_path = asset_dir / f"{asset_id}.upload.wav"
        wav_path = asset_dir / f"{asset_id}.wav"
        meta_path = asset_dir / f"{asset_id}.json"
        src_path.write_bytes(data)

        try:
            probe = self._normalize_uploaded_wav(src_path=src_path, dest_path=wav_path)
        finally:
            src_path.unlink(missing_ok=True)

        sha256 = self._sha256_file(wav_path)
        uploaded_at = self._now_utc().replace(microsecond=0)
        expires_at = uploaded_at + dt.timedelta(seconds=self._asset_expiry_seconds())
        meta = {
            "asset_id": asset_id,
            "filename": filename_clean,
            "content_type": content_type or "audio/wav",
            "duration_seconds": round(float(probe["duration_seconds"]), 3),
            "sample_rate_hz": int(probe["sample_rate_hz"]),
            "target_sample_rate_hz": self._station_sample_rate(),
            "channels": int(probe["channels"]),
            "sample_width_bytes": int(probe["sample_width_bytes"]),
            "frames": int(probe["frames"]),
            "normalized": True,
            "sha256": sha256,
            "uploaded_at": uploaded_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "path": str(wav_path),
            "actor": actor,
        }
        if self._audio_asset_repo is not None:
            try:
                self._audio_asset_repo.upsert_asset(meta)
            except Exception:
                meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
        else:
            meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
        return {
            "asset_id": asset_id,
            "filename": filename_clean,
            "content_type": content_type or "audio/wav",
            "duration_seconds": round(float(probe["duration_seconds"]), 3),
            "sample_rate_hz": int(probe["sample_rate_hz"]),
            "target_sample_rate_hz": self._station_sample_rate(),
            "channels": int(probe["channels"]),
            "sample_width_bytes": int(probe["sample_width_bytes"]),
            "frames": int(probe["frames"]),
            "normalized": True,
            "sha256": sha256,
            "uploaded_at": uploaded_at.isoformat(),
            "expires_at": expires_at.isoformat(),
        }

    def _load_audio_asset(self, asset_id: str) -> dict[str, Any]:
        meta_path = self._audio_asset_dir() / f"{asset_id}.json"
        meta: dict[str, Any] | None = None
        if self._audio_asset_repo is not None:
            try:
                meta = self._audio_asset_repo.get_asset(asset_id)
            except Exception:
                meta = None

        if meta is None:
            if not meta_path.exists():
                raise NotFoundError(
                    "audio_asset_not_found", "Audio asset was not found.", details={"audio_asset_id": asset_id}
                )
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if self._audio_asset_repo is not None:
                try:
                    self._audio_asset_repo.upsert_asset(meta)
                except Exception:
                    pass

        expires_at = dt.datetime.fromisoformat(meta["expires_at"])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=dt.UTC)
        if self._now_utc() > expires_at.astimezone(dt.UTC):
            raise NotFoundError("audio_asset_expired", "Audio asset has expired.", details={"audio_asset_id": asset_id})
        wav_path = Path(meta["path"])
        if not wav_path.exists():
            raise NotFoundError("audio_asset_missing_file", "Audio asset metadata exists but the WAV file is missing.")
        return meta

    async def _push_manual_audio(
        self,
        *,
        wav_path: Path,
        headline: str,
        event_code: str,
        voice_mode: str,
        sender: str | None,
        interrupt_policy: str,
        actor: str,
    ) -> None:
        self._validate_interrupt_policy(interrupt_policy)
        self._ensure_backend_ready()

        meta = self.orch._np_meta(
            title=headline,
            kind="alert",
            extra={
                "sw_alert_source": "api",
                "sw_alert_mode": voice_mode,
                "sw_event_code": event_code,
                "sw_sender": sender or "",
                "sw_actor": actor,
            },
        )
        if interrupt_policy == InterruptPolicy.INTERRUPT_THEN_REFILL.value:
            full = voice_mode == VoiceMode.FULL_EAS.value

            async def _use_existing_audio():
                return wav_path

            await self.orch._render_and_push_interrupt_audio(
                source="api-full" if full else "api-voice",
                full=full,
                render=_use_existing_audio,
                meta=meta,
            )
        else:
            async with self.orch._cycle_lock:
                if voice_mode == VoiceMode.FULL_EAS.value and hasattr(self.orch.telnet, "push_full_alert"):
                    self.orch.telnet.push_full_alert(str(wav_path), meta=meta)
                elif hasattr(self.orch.telnet, "push_voice_alert"):
                    self.orch.telnet.push_voice_alert(str(wav_path), meta=meta)
                else:
                    self.orch.telnet.push_alert(str(wav_path), meta=meta)

        now = self._now_local()
        self.orch.last_product_desc = headline[:200]
        if voice_mode == VoiceMode.FULL_EAS.value:
            self.orch.last_toneout_at = now
            if self._manual_full_eas_should_heighten():
                self.orch.last_heightened_at = now
                self.orch.heightened_until = now + dt.timedelta(seconds=self.orch.cfg.cycle.min_heightened_seconds)
                self.orch._update_mode()
        self.orch._schedule_cycle_refill("post-api-origination")

    async def originate_text(self, req: OriginateTextRequest, *, actor: str) -> dict[str, Any]:
        same_codes = list(req.same_codes)
        if req.voice_mode == VoiceMode.FULL_EAS.value:
            same_codes = self._same_codes_in_service_area(same_codes)

        try:
            _ot_result = await self.orch.manual_runtime.originate_text(
                event_code=req.event_code,
                headline=req.headline,
                script_text=req.text,
                voice_mode=req.voice_mode,
                same_locations=same_codes,
                sender=req.sender,
                actor=actor,
                interrupt_policy=req.interrupt_policy,
                expires_in_minutes=req.expires_in_minutes,
                heightened_override=req.heightened,
            )
        except NotImplementedError as exc:
            raise ControlError("manual_origination_not_supported", str(exc)) from exc
        except FileNotFoundError as exc:
            raise NotFoundError(
                "manual_audio_missing", "Manual origination audio source is missing.", details={"path": str(exc)}
            ) from exc
        except ValueError as exc:
            raise ControlError("invalid_manual_origination", str(exc)) from exc
        # _API_ORIGINATE_TEXT_DL_
        try:
            self.orch.discord.api_action(
                method="POST",
                endpoint="/v1/originate/text",
                actor=actor,
                status="succeeded",
                headline=req.headline,
                details={
                    "event_code": req.event_code,
                    "voice_mode": req.voice_mode,
                },
            )
        except Exception:
            pass
        # _API_TEXT_EAS_DL_
        if req.voice_mode == "full_eas":
            try:
                self.orch.discord.alert_aired(
                    code=req.event_code,
                    event=req.headline,
                    source="SeasonalWeather (local API)",
                    mode="full",
                    area=", ".join(req.same_codes) if req.same_codes else "",
                )
            except Exception:
                pass
        return _ot_result

    async def originate_audio(self, req: OriginateAudioRequest, *, actor: str) -> dict[str, Any]:
        same_codes = list(req.same_codes)
        if req.voice_mode == VoiceMode.FULL_EAS.value:
            same_codes = self._same_codes_in_service_area(same_codes)
        meta = self._load_audio_asset(req.audio_asset_id)
        source_wav = Path(meta["path"])
        _work_dir, audio_dir, _cache_dir, _logs_dir = self._work_paths()
        ts = self._now_local().strftime("%Y%m%d-%H%M%S")
        out_path = audio_dir / f"api_audio_{ts}_{req.audio_asset_id}.wav"
        shutil.copy2(source_wav, out_path)

        try:
            result = await self.orch.manual_runtime.originate_audio(
                event_code=req.event_code,
                headline=req.headline,
                wav_path=out_path,
                voice_mode=req.voice_mode,
                same_locations=same_codes,
                sender=req.sender,
                actor=actor,
                interrupt_policy=req.interrupt_policy,
                expires_in_minutes=req.expires_in_minutes,
                heightened_override=req.heightened,
            )
        except FileNotFoundError as exc:
            out_path.unlink(missing_ok=True)
            raise NotFoundError(
                "manual_audio_missing", "Manual origination audio source is missing.", details={"path": str(exc)}
            ) from exc
        except ValueError as exc:
            out_path.unlink(missing_ok=True)
            raise ControlError("invalid_manual_origination", str(exc)) from exc

        result["audio_asset_id"] = req.audio_asset_id
        result["audio_path"] = str(out_path)
        # _API_ORIGINATE_AUDIO_DL_
        try:
            self.orch.discord.api_action(
                method="POST",
                endpoint="/v1/originate/audio",
                actor=actor,
                status="succeeded",
                headline=req.headline,
                details={
                    "event_code": req.event_code,
                    "voice_mode": req.voice_mode,
                    "asset_id": req.audio_asset_id,
                },
            )
        except Exception:
            pass
        # _API_AUDIO_EAS_DL_
        if req.voice_mode == "full_eas":
            try:
                self.orch.discord.alert_aired(
                    code=req.event_code,
                    event=req.headline,
                    source="SeasonalWeather (local API)",
                    mode="full",
                    area=", ".join(req.same_codes) if req.same_codes else "",
                )
            except Exception:
                pass
        return result

    def _format_insert_snapshot(self, item: dict[str, Any]) -> dict[str, Any]:
        repeat = {
            "mode": item.get("repeat_mode") or "once",
            "every_n_rotations": int(item.get("repeat_every_rotations") or 1),
            "max_airings": int(item.get("max_airings") or 1),
        }
        estimate = self._estimate_insert_airtime(item)
        snapshot = {
            "insert_id": item["insert_id"],
            "kind": item["kind"],
            "title": item["title"],
            "placement": item["placement"],
            "start_after": item.get("start_after"),
            "expires_at": item["expires_at"],
            "repeat": repeat,
            "defer_during_active_alerts": bool(item.get("defer_during_active_alerts", True)),
            "status": item.get("status") or "active",
            "actor": item.get("actor") or "",
            "created_at": item.get("created_at") or "",
            "updated_at": item.get("updated_at") or "",
            "last_aired_at": item.get("last_aired_at"),
            "airing_count": int(item.get("airing_count") or 0),
            "max_airings": int(item.get("max_airings") or 1),
            "duration_seconds": round(float(item.get("duration_seconds") or 0.0), 3),
            "estimated_next_air_at": estimate.get("estimated_next_air_at"),
            "estimate_confidence": estimate.get("estimate_confidence"),
            "estimate_window_seconds": estimate.get("estimate_window_seconds"),
            "audio_asset_id": item.get("audio_asset_id"),
        }
        return snapshot

    def _estimate_insert_airtime(self, item: dict[str, Any]) -> dict[str, Any]:
        if item.get("status") != "active":
            return {"estimated_next_air_at": None, "estimate_confidence": None, "estimate_window_seconds": None}

        now = self._now_utc().replace(microsecond=0)
        try:
            start_raw = item.get("start_after")
            if start_raw:
                start = dt.datetime.fromisoformat(str(start_raw))
                if start.tzinfo is None:
                    start = start.replace(tzinfo=dt.UTC)
                start = start.astimezone(dt.UTC).replace(microsecond=0)
            else:
                start = now
        except Exception:
            start = now

        try:
            expires = dt.datetime.fromisoformat(str(item.get("expires_at")))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=dt.UTC)
            expires = expires.astimezone(dt.UTC).replace(microsecond=0)
        except Exception:
            expires = now
        if expires <= now:
            return {"estimated_next_air_at": None, "estimate_confidence": None, "estimate_window_seconds": None}

        placement_offsets = {
            "after_time": 45,
            "after_status": 120,
            "end_of_rotation": 300,
        }
        placement = str(item.get("placement") or "after_time")
        buffered = 0.0
        try:
            conductor = getattr(self.orch, "conductor", None)
            if conductor is not None:
                buffered = float(getattr(conductor, "estimated_remaining_s", 0.0) or 0.0)
        except Exception:
            buffered = 0.0
        estimate = now + dt.timedelta(seconds=buffered + placement_offsets.get(placement, 120))
        if estimate < start:
            estimate = start
        if estimate >= expires:
            return {"estimated_next_air_at": None, "estimate_confidence": "best_effort", "estimate_window_seconds": 180}
        return {
            "estimated_next_air_at": self._serialize_dt(estimate),
            "estimate_confidence": "best_effort",
            "estimate_window_seconds": 180,
        }

    async def _render_text_insert_audio(self, *, insert_id: str, text: str) -> tuple[Path, float]:
        out_path = self._insert_audio_path(insert_id)
        duration = await render_segment_wav_async(
            self.orch.tts,
            text,
            out_path,
            sample_rate=self._station_sample_rate(),
        )
        return out_path, float(duration)

    def _copy_insert_audio_asset(self, *, insert_id: str, audio_asset_id: str) -> tuple[Path, float]:
        meta = self._load_audio_asset(audio_asset_id)
        source_wav = Path(meta["path"])
        out_path = self._insert_audio_path(insert_id)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_wav, out_path)
        try:
            duration = float(meta.get("duration_seconds") or 0.0)
        except Exception:
            duration = 0.0
        if duration <= 0.0:
            duration = float(wav_duration_seconds(out_path))
        return out_path, duration

    def _base_insert_record(
        self,
        *,
        insert_id: str,
        kind: str,
        title: str,
        placement: str,
        start_after: dt.datetime | None,
        expires_at: dt.datetime,
        repeat_mode: str,
        repeat_every_rotations: int,
        max_airings: int,
        defer_during_active_alerts: bool,
        actor: str,
        audio_path: Path,
        duration_seconds: float,
        text: str | None = None,
        audio_asset_id: str | None = None,
    ) -> dict[str, Any]:
        now_iso = self._serialize_dt(self._now_utc())
        start_iso = self._serialize_dt(start_after) if start_after is not None else None
        expires_iso = self._serialize_dt(expires_at)
        assert now_iso is not None and expires_iso is not None
        return {
            "insert_id": insert_id,
            "kind": kind,
            "title": title,
            "text": text,
            "audio_path": str(audio_path),
            "audio_asset_id": audio_asset_id,
            "placement": placement,
            "start_after": start_iso,
            "expires_at": expires_iso,
            "repeat_mode": repeat_mode,
            "repeat_every_rotations": repeat_every_rotations,
            "max_airings": max_airings,
            "defer_during_active_alerts": defer_during_active_alerts,
            "status": "active",
            "actor": actor,
            "created_at": now_iso,
            "updated_at": now_iso,
            "last_aired_at": None,
            "airing_count": 0,
            "last_aired_rotation": None,
            "duration_seconds": duration_seconds,
            "meta": {"source": "api"},
        }

    def _notify_inserts_changed(self) -> None:
        try:
            conductor = getattr(self.orch, "conductor", None)
            if conductor is not None and hasattr(conductor, "notify_inserts_changed"):
                conductor.notify_inserts_changed()
        except Exception:
            pass

    async def create_text_insert(self, req: CreateTextInsertRequest, *, actor: str) -> dict[str, Any]:
        repo = self._require_insert_repo()
        now = self._now_utc().replace(microsecond=0)
        expires_at = self._to_utc_dt(req.expires_at)
        if expires_at <= now:
            raise ControlError("insert_expired", "expires_at must be in the future.")
        start_after = self._to_utc_dt(req.start_after) if req.start_after is not None else None
        insert_id = f"ins_{uuid.uuid4().hex[:20]}"
        audio_path, duration = await self._render_text_insert_audio(insert_id=insert_id, text=req.text)
        repeat = req.repeat
        record = self._base_insert_record(
            insert_id=insert_id,
            kind="text",
            title=req.title,
            text=req.text,
            audio_path=audio_path,
            duration_seconds=duration,
            placement=self._enum_value(req.placement),
            start_after=start_after,
            expires_at=expires_at,
            repeat_mode=self._enum_value(repeat.mode),
            repeat_every_rotations=int(repeat.every_n_rotations),
            max_airings=int(repeat.max_airings),
            defer_during_active_alerts=bool(req.defer_during_active_alerts),
            actor=actor,
        )
        repo.upsert_insert(record)
        self._notify_inserts_changed()
        try:
            self.orch.discord.api_action(
                method="POST",
                endpoint="/v1/inserts/text",
                actor=actor,
                status="succeeded",
                headline=req.title,
                details={"insert_id": insert_id, "placement": self._enum_value(req.placement)},
            )
        except Exception:
            pass
        snapshot = self._format_insert_snapshot(record)
        return {"ok": True, "insert": snapshot, "insert_id": insert_id}

    async def create_audio_insert(self, req: CreateAudioInsertRequest, *, actor: str) -> dict[str, Any]:
        repo = self._require_insert_repo()
        now = self._now_utc().replace(microsecond=0)
        expires_at = self._to_utc_dt(req.expires_at)
        if expires_at <= now:
            raise ControlError("insert_expired", "expires_at must be in the future.")
        start_after = self._to_utc_dt(req.start_after) if req.start_after is not None else None
        insert_id = f"ins_{uuid.uuid4().hex[:20]}"
        audio_path, duration = self._copy_insert_audio_asset(insert_id=insert_id, audio_asset_id=req.audio_asset_id)
        repeat = req.repeat
        record = self._base_insert_record(
            insert_id=insert_id,
            kind="audio",
            title=req.title,
            audio_asset_id=req.audio_asset_id,
            audio_path=audio_path,
            duration_seconds=duration,
            placement=self._enum_value(req.placement),
            start_after=start_after,
            expires_at=expires_at,
            repeat_mode=self._enum_value(repeat.mode),
            repeat_every_rotations=int(repeat.every_n_rotations),
            max_airings=int(repeat.max_airings),
            defer_during_active_alerts=bool(req.defer_during_active_alerts),
            actor=actor,
        )
        repo.upsert_insert(record)
        self._notify_inserts_changed()
        try:
            self.orch.discord.api_action(
                method="POST",
                endpoint="/v1/inserts/audio",
                actor=actor,
                status="succeeded",
                headline=req.title,
                details={
                    "insert_id": insert_id,
                    "placement": self._enum_value(req.placement),
                    "asset_id": req.audio_asset_id,
                },
            )
        except Exception:
            pass
        snapshot = self._format_insert_snapshot(record)
        return {"ok": True, "insert": snapshot, "insert_id": insert_id}

    async def list_inserts(self, *, include_inactive: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        repo = self._require_insert_repo()
        now_iso = self._serialize_dt(self._now_utc())
        if now_iso is not None:
            repo.expire_due(now_iso)
        return [
            self._format_insert_snapshot(item)
            for item in repo.list_inserts(include_inactive=include_inactive, limit=limit)
        ]

    async def get_insert(self, insert_id: str) -> dict[str, Any]:
        repo = self._require_insert_repo()
        insert_id = self._validate_insert_id(insert_id)
        now_iso = self._serialize_dt(self._now_utc())
        if now_iso is not None:
            repo.expire_due(now_iso)
        item = repo.get_insert(insert_id)
        if item is None:
            raise NotFoundError("insert_not_found", "Scheduled insert was not found.", details={"insert_id": insert_id})
        return self._format_insert_snapshot(item)

    async def cancel_insert(self, insert_id: str, *, actor: str) -> dict[str, Any]:
        repo = self._require_insert_repo()
        insert_id = self._validate_insert_id(insert_id)
        updated_at = self._serialize_dt(self._now_utc())
        assert updated_at is not None
        item = repo.cancel_insert(insert_id=insert_id, updated_at=updated_at)
        if item is None:
            raise NotFoundError("insert_not_found", "Scheduled insert was not found.", details={"insert_id": insert_id})
        self._notify_inserts_changed()
        try:
            self.orch.discord.api_action(
                method="DELETE",
                endpoint=f"/v1/inserts/{insert_id}",
                actor=actor,
                status="succeeded",
                details={"insert_id": insert_id},
            )
        except Exception:
            pass
        return {"ok": True, "insert": self._format_insert_snapshot(item), "insert_id": insert_id}


# _CONTROL_DL_APPLIED_

# _CONTROL_EAS_ALERT_DL_APPLIED_
