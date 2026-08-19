from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .core import SeasonalDatabase


class CycleInsertRepository:
    """SQLite repository for bounded operator-scheduled cycle inserts."""

    def __init__(self, db: SeasonalDatabase) -> None:
        self.db = db

    def upsert_insert(self, record: Mapping[str, Any]) -> None:
        meta = dict(record.get("meta") or {})
        meta_json = json.dumps(meta, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO cycle_inserts (
                    insert_id, kind, title, text, audio_path, audio_asset_id,
                    placement, start_after, expires_at, repeat_mode,
                    repeat_every_rotations, max_airings,
                    defer_during_active_alerts, status, actor,
                    created_at, updated_at, last_aired_at,
                    airing_count, last_aired_rotation, duration_seconds, meta_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(insert_id) DO UPDATE SET
                    kind = excluded.kind,
                    title = excluded.title,
                    text = excluded.text,
                    audio_path = excluded.audio_path,
                    audio_asset_id = excluded.audio_asset_id,
                    placement = excluded.placement,
                    start_after = excluded.start_after,
                    expires_at = excluded.expires_at,
                    repeat_mode = excluded.repeat_mode,
                    repeat_every_rotations = excluded.repeat_every_rotations,
                    max_airings = excluded.max_airings,
                    defer_during_active_alerts = excluded.defer_during_active_alerts,
                    status = excluded.status,
                    actor = excluded.actor,
                    updated_at = excluded.updated_at,
                    last_aired_at = excluded.last_aired_at,
                    airing_count = excluded.airing_count,
                    last_aired_rotation = excluded.last_aired_rotation,
                    duration_seconds = excluded.duration_seconds,
                    meta_json = excluded.meta_json
                """,
                (
                    str(record["insert_id"]),
                    str(record["kind"]),
                    str(record.get("title") or ""),
                    str(record.get("text") or "") or None,
                    str(record.get("audio_path") or "") or None,
                    str(record.get("audio_asset_id") or "") or None,
                    str(record.get("placement") or "after_time"),
                    str(record.get("start_after") or "") or None,
                    str(record["expires_at"]),
                    str(record.get("repeat_mode") or "once"),
                    int(record.get("repeat_every_rotations") or 1),
                    int(record.get("max_airings") or 1),
                    1 if bool(record.get("defer_during_active_alerts", True)) else 0,
                    str(record.get("status") or "active"),
                    str(record.get("actor") or ""),
                    str(record.get("created_at") or ""),
                    str(record.get("updated_at") or ""),
                    str(record.get("last_aired_at") or "") or None,
                    int(record.get("airing_count") or 0),
                    record.get("last_aired_rotation"),
                    float(record.get("duration_seconds") or 0.0),
                    meta_json,
                ),
            )

    def get_insert(self, insert_id: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM cycle_inserts WHERE insert_id = ?", (insert_id,)).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def list_inserts(self, *, include_inactive: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self.db.connect() as conn:
            if include_inactive:
                rows = conn.execute(
                    """
                    SELECT * FROM cycle_inserts
                    ORDER BY created_at DESC, insert_id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM cycle_inserts
                    WHERE status = 'active'
                    ORDER BY created_at DESC, insert_id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def list_due(
        self,
        *,
        placement: str,
        rotation_count: int,
        now_iso: str,
        active_alert_focus: bool,
        limit: int = 3,
        expire: bool = True,
    ) -> list[dict[str, Any]]:
        if expire:
            self.expire_due(now_iso)
        limit = max(1, min(int(limit), 10))
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM cycle_inserts
                WHERE status = 'active'
                  AND placement = ?
                  AND (start_after IS NULL OR start_after <= ?)
                  AND expires_at > ?
                  AND airing_count < max_airings
                  AND audio_path IS NOT NULL
                  AND (? = 0 OR defer_during_active_alerts = 0)
                ORDER BY created_at, insert_id
                LIMIT 50
                """,
                (placement, now_iso, now_iso, 1 if active_alert_focus else 0),
            ).fetchall()

        due: list[dict[str, Any]] = []
        for row in rows:
            item = self._row_to_dict(row)
            if not self._rotation_due(item, rotation_count):
                continue
            due.append(item)
            if len(due) >= limit:
                break
        return due

    def mark_aired(self, *, insert_id: str, aired_at: str, rotation_count: int) -> dict[str, Any] | None:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM cycle_inserts WHERE insert_id = ?", (insert_id,)).fetchone()
            if row is None:
                return None
            current_count = int(row["airing_count"] or 0)
            max_airings = max(1, int(row["max_airings"] or 1))
            next_count = current_count + 1
            status = "completed" if next_count >= max_airings else str(row["status"] or "active")
            conn.execute(
                """
                UPDATE cycle_inserts
                SET airing_count = ?, last_aired_at = ?, last_aired_rotation = ?,
                    status = ?, updated_at = ?
                WHERE insert_id = ?
                """,
                (next_count, aired_at, int(rotation_count), status, aired_at, insert_id),
            )
        return self.get_insert(insert_id)

    def cancel_insert(self, *, insert_id: str, updated_at: str) -> dict[str, Any] | None:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM cycle_inserts WHERE insert_id = ?", (insert_id,)).fetchone()
            if row is None:
                return None
            conn.execute(
                """
                UPDATE cycle_inserts
                SET status = 'cancelled', updated_at = ?
                WHERE insert_id = ? AND status = 'active'
                """,
                (updated_at, insert_id),
            )
        return self.get_insert(insert_id)

    def expire_due(self, now_iso: str) -> int:
        with self.db.transaction() as conn:
            cur = conn.execute(
                """
                UPDATE cycle_inserts
                SET status = 'expired', updated_at = ?
                WHERE status = 'active' AND expires_at <= ?
                """,
                (now_iso, now_iso),
            )
        return int(cur.rowcount or 0)

    @staticmethod
    def _rotation_due(item: Mapping[str, Any], rotation_count: int) -> bool:
        airing_count = int(item.get("airing_count") or 0)
        max_airings = max(1, int(item.get("max_airings") or 1))
        if airing_count >= max_airings:
            return False

        repeat_mode = str(item.get("repeat_mode") or "once")
        if repeat_mode == "once":
            return airing_count == 0

        every = max(1, int(item.get("repeat_every_rotations") or 1))
        last_raw = item.get("last_aired_rotation")
        if last_raw is None:
            return True
        try:
            last = int(last_raw)
        except Exception:
            return True
        return int(rotation_count) - last >= every

    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, Any]:
        meta: dict[str, Any]
        try:
            loaded = json.loads(row["meta_json"] or "{}")
            meta = loaded if isinstance(loaded, dict) else {}
        except Exception:
            meta = {}
        return {
            "insert_id": str(row["insert_id"]),
            "kind": str(row["kind"]),
            "title": str(row["title"]),
            "text": row["text"],
            "audio_path": row["audio_path"],
            "audio_asset_id": row["audio_asset_id"],
            "placement": str(row["placement"]),
            "start_after": row["start_after"],
            "expires_at": str(row["expires_at"]),
            "repeat_mode": str(row["repeat_mode"]),
            "repeat_every_rotations": int(row["repeat_every_rotations"] or 1),
            "max_airings": int(row["max_airings"] or 1),
            "defer_during_active_alerts": bool(row["defer_during_active_alerts"]),
            "status": str(row["status"]),
            "actor": str(row["actor"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "last_aired_at": row["last_aired_at"],
            "airing_count": int(row["airing_count"] or 0),
            "last_aired_rotation": row["last_aired_rotation"],
            "duration_seconds": float(row["duration_seconds"] or 0.0),
            "meta": meta,
        }
