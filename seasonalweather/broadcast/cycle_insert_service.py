"""Application service for operator-scheduled broadcast-cycle inserts."""

from __future__ import annotations

import contextlib
import datetime as dt
import uuid
from pathlib import Path
from typing import Any

from seasonalweather.api.models import CreateAudioInsertRequest, CreateTextInsertRequest
from seasonalweather.application.errors import ControlError, DependencyUnavailableError, NotFoundError
from seasonalweather.artifacts.audio_assets import AudioAssetService
from seasonalweather.broadcast.segment_store import render_segment_wav_async
from seasonalweather.database.inserts import CycleInsertRepository
from seasonalweather.tts.audio import wav_duration_seconds


class CycleInsertService:
    """Own insert persistence, audio preparation, and bounded read models."""

    def __init__(self, orchestrator: Any, assets: AudioAssetService) -> None:
        self.orch = orchestrator
        self.assets = assets
        db = getattr(orchestrator, "database", None)
        self._repository = CycleInsertRepository(db) if db is not None else None

    def _now_utc(self) -> dt.datetime:
        return dt.datetime.now(dt.UTC)

    def _serialize(self, value: dt.datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone(dt.UTC).replace(microsecond=0).isoformat()

    def _require_repository(self) -> CycleInsertRepository:
        if self._repository is None:
            raise DependencyUnavailableError(
                "database_required",
                "Scheduled broadcast inserts require the SQLite database to be enabled.",
            )
        return self._repository

    def _sample_rate(self) -> int:
        try:
            return int(getattr(self.orch.cfg.audio, "sample_rate", 16000) or 16000)
        except Exception:
            return 16000

    def _insert_path(self, insert_id: str) -> Path:
        _work_dir, audio_dir, _cache_dir, _logs_dir = self.orch._paths()
        return Path(audio_dir) / f"insert_{insert_id}.wav"

    @staticmethod
    def _enum(value: Any) -> str:
        return str(getattr(value, "value", value))

    @staticmethod
    def _validate_id(insert_id: str) -> str:
        value = str(insert_id or "").strip()
        if not value or len(value) > 64 or not all(char.isalnum() or char in {"_", "-"} for char in value):
            raise ControlError("invalid_insert_id", "insert_id contains unsupported characters.")
        return value

    async def _render_text(self, *, insert_id: str, text: str) -> tuple[Path, float]:
        path = self._insert_path(insert_id)
        duration = await render_segment_wav_async(
            self.orch.synthesizer,
            text,
            path,
            sample_rate=self._sample_rate(),
        )
        return path, float(duration)

    def _copy_audio(self, *, insert_id: str, asset_id: str) -> tuple[Path, float]:
        path = self._insert_path(insert_id)
        metadata = self.assets.copy_to(asset_id=asset_id, destination=path)
        try:
            duration = float(metadata.get("duration_seconds") or 0.0)
        except Exception:
            duration = 0.0
        if duration <= 0.0:
            duration = float(wav_duration_seconds(path))
        return path, duration

    def _base_record(
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
        created_at = self._serialize(self._now_utc())
        start_iso = self._serialize(start_after)
        expires_iso = self._serialize(expires_at)
        assert created_at is not None and expires_iso is not None
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
            "created_at": created_at,
            "updated_at": created_at,
            "last_aired_at": None,
            "airing_count": 0,
            "last_aired_rotation": None,
            "duration_seconds": duration_seconds,
            "meta": {"source": "api"},
        }

    def _notify_changed(self) -> None:
        try:
            conductor = getattr(self.orch, "conductor", None)
            if conductor is not None and hasattr(conductor, "notify_inserts_changed"):
                conductor.notify_inserts_changed()
        except Exception:
            pass

    def _snapshot(self, item: dict[str, Any]) -> dict[str, Any]:
        estimate = self._estimate(item)
        return {
            "insert_id": item["insert_id"],
            "kind": item["kind"],
            "title": item["title"],
            "placement": item["placement"],
            "start_after": item.get("start_after"),
            "expires_at": item["expires_at"],
            "repeat": {
                "mode": item.get("repeat_mode") or "once",
                "every_n_rotations": int(item.get("repeat_every_rotations") or 1),
                "max_airings": int(item.get("max_airings") or 1),
            },
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

    def _estimate(self, item: dict[str, Any]) -> dict[str, Any]:
        if item.get("status") != "active":
            return {"estimated_next_air_at": None, "estimate_confidence": None, "estimate_window_seconds": None}
        now = self._now_utc().replace(microsecond=0)
        try:
            start = dt.datetime.fromisoformat(str(item.get("start_after"))) if item.get("start_after") else now
            if start.tzinfo is None:
                start = start.replace(tzinfo=dt.UTC)
            start = start.astimezone(dt.UTC).replace(microsecond=0)
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
        offsets = {"after_time": 45, "after_status": 120, "end_of_rotation": 300}
        placement = str(item.get("placement") or "after_time")
        try:
            conductor = getattr(self.orch, "conductor", None)
            buffered = float(getattr(conductor, "estimated_remaining_s", 0.0) or 0.0) if conductor is not None else 0.0
        except Exception:
            buffered = 0.0
        estimate = now + dt.timedelta(seconds=buffered + offsets.get(placement, 120))
        if estimate < start:
            estimate = start
        if estimate >= expires:
            return {"estimated_next_air_at": None, "estimate_confidence": "best_effort", "estimate_window_seconds": 180}
        return {
            "estimated_next_air_at": self._serialize(estimate),
            "estimate_confidence": "best_effort",
            "estimate_window_seconds": 180,
        }

    async def create_text(self, req: CreateTextInsertRequest, *, actor: str) -> dict[str, Any]:
        repository = self._require_repository()
        now = self._now_utc().replace(microsecond=0)
        expires_at = self._to_utc(req.expires_at)
        if expires_at <= now:
            raise ControlError("insert_expired", "expires_at must be in the future.")
        start_after = self._to_utc(req.start_after) if req.start_after is not None else None
        insert_id = f"ins_{uuid.uuid4().hex[:20]}"
        audio_path, duration = await self._render_text(insert_id=insert_id, text=req.text)
        repeat = req.repeat
        record = self._base_record(
            insert_id=insert_id,
            kind="text",
            title=req.title,
            text=req.text,
            audio_path=audio_path,
            duration_seconds=duration,
            placement=self._enum(req.placement),
            start_after=start_after,
            expires_at=expires_at,
            repeat_mode=self._enum(repeat.mode),
            repeat_every_rotations=int(repeat.every_n_rotations),
            max_airings=int(repeat.max_airings),
            defer_during_active_alerts=bool(req.defer_during_active_alerts),
            actor=actor,
        )
        repository.upsert_insert(record)
        self._notify_changed()
        with contextlib.suppress(Exception):
            self.orch.discord.api_action(
                method="POST",
                endpoint="/v1/inserts/text",
                actor=actor,
                status="succeeded",
                headline=req.title,
                details={"insert_id": insert_id, "placement": self._enum(req.placement)},
            )
        return {"ok": True, "insert": self._snapshot(record), "insert_id": insert_id}

    async def create_audio(self, req: CreateAudioInsertRequest, *, actor: str) -> dict[str, Any]:
        repository = self._require_repository()
        now = self._now_utc().replace(microsecond=0)
        expires_at = self._to_utc(req.expires_at)
        if expires_at <= now:
            raise ControlError("insert_expired", "expires_at must be in the future.")
        start_after = self._to_utc(req.start_after) if req.start_after is not None else None
        insert_id = f"ins_{uuid.uuid4().hex[:20]}"
        audio_path, duration = self._copy_audio(insert_id=insert_id, asset_id=req.audio_asset_id)
        repeat = req.repeat
        record = self._base_record(
            insert_id=insert_id,
            kind="audio",
            title=req.title,
            audio_asset_id=req.audio_asset_id,
            audio_path=audio_path,
            duration_seconds=duration,
            placement=self._enum(req.placement),
            start_after=start_after,
            expires_at=expires_at,
            repeat_mode=self._enum(repeat.mode),
            repeat_every_rotations=int(repeat.every_n_rotations),
            max_airings=int(repeat.max_airings),
            defer_during_active_alerts=bool(req.defer_during_active_alerts),
            actor=actor,
        )
        repository.upsert_insert(record)
        self._notify_changed()
        with contextlib.suppress(Exception):
            self.orch.discord.api_action(
                method="POST",
                endpoint="/v1/inserts/audio",
                actor=actor,
                status="succeeded",
                headline=req.title,
                details={
                    "insert_id": insert_id,
                    "placement": self._enum(req.placement),
                    "asset_id": req.audio_asset_id,
                },
            )
        return {"ok": True, "insert": self._snapshot(record), "insert_id": insert_id}

    @staticmethod
    def _to_utc(value: dt.datetime) -> dt.datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ControlError("invalid_datetime", "Datetime values must include a timezone offset.")
        return value.astimezone(dt.UTC).replace(microsecond=0)

    def list(self, *, include_inactive: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        repository = self._require_repository()
        now = self._serialize(self._now_utc())
        if now is not None:
            repository.expire_due(now)
        return [
            self._snapshot(item) for item in repository.list_inserts(include_inactive=include_inactive, limit=limit)
        ]

    def get(self, insert_id: str) -> dict[str, Any]:
        repository = self._require_repository()
        value = self._validate_id(insert_id)
        now = self._serialize(self._now_utc())
        if now is not None:
            repository.expire_due(now)
        item = repository.get_insert(value)
        if item is None:
            raise NotFoundError(
                "insert_not_found", "Scheduled broadcast insert was not found.", details={"insert_id": value}
            )
        return self._snapshot(item)

    def cancel(self, insert_id: str, *, actor: str) -> dict[str, Any]:
        repository = self._require_repository()
        value = self._validate_id(insert_id)
        updated_at = self._serialize(self._now_utc())
        assert updated_at is not None
        item = repository.cancel_insert(insert_id=value, updated_at=updated_at)
        if item is None:
            raise NotFoundError(
                "insert_not_found", "Scheduled broadcast insert was not found.", details={"insert_id": value}
            )
        self._notify_changed()
        with contextlib.suppress(Exception):
            self.orch.discord.api_action(
                method="DELETE",
                endpoint=f"/v1/inserts/{value}",
                actor=actor,
                status="succeeded",
                details={"insert_id": value},
            )
        return {"ok": True, "insert": self._snapshot(item), "insert_id": value}
