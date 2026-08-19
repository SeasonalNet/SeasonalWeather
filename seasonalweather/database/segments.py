from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .core import SeasonalDatabase


class SegmentRepository:
    def __init__(self, db: SeasonalDatabase) -> None:
        self.db = db

    def replace_entries(self, entries: Iterable[Mapping[str, Any]]) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM cycle_segments")
            for entry in entries:
                self._upsert_unlocked(conn, entry)

    def upsert_entry(self, entry: Mapping[str, Any]) -> None:
        with self.db.transaction() as conn:
            self._upsert_unlocked(conn, entry)

    def load_entries(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM cycle_segments ORDER BY segment_key").fetchall()
        return [
            {
                "key": str(row["segment_key"]),
                "title": str(row["title"]),
                "text": str(row["text"]),
                "audio_path": str(row["audio_path"]),
                "duration_s": float(row["duration_s"] or 0.0),
                "last_updated_ts": float(row["last_updated_ts"] or 0.0),
                "refresh_interval_s": int(row["refresh_interval_s"] or 0),
                "max_age_s": int(row["max_age_s"] or 0),
                "is_placeholder": bool(row["is_placeholder"]),
                "source_name": row["source_name"],
                "product_identifier": row["product_identifier"],
                "product_type": row["product_type"],
                "issuing_office": row["issuing_office"],
                "issuance_time": row["issuance_time"],
                "fetch_time": row["fetch_time"],
                "last_successful_synthesis": row["last_successful_synthesis"],
                "content_hash": row["content_hash"],
                "source_reference": row["source_reference"],
                "last_error": row["last_error"],
                "consecutive_failures": int(row["consecutive_failures"] or 0),
                "last_aired": row["last_aired"],
                "next_eligible_airtime": row["next_eligible_airtime"],
            }
            for row in rows
        ]

    @staticmethod
    def _upsert_unlocked(conn: Any, entry: Mapping[str, Any]) -> None:
        conn.execute(
            """
            INSERT INTO cycle_segments (
                segment_key, title, text, audio_path, duration_s,
                last_updated_ts, refresh_interval_s, is_placeholder, max_age_s,
                source_name, product_identifier, product_type, issuing_office,
                issuance_time, fetch_time, last_successful_synthesis, content_hash,
                source_reference, last_error, consecutive_failures, last_aired,
                next_eligible_airtime
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(segment_key) DO UPDATE SET
                title = excluded.title,
                text = excluded.text,
                audio_path = excluded.audio_path,
                duration_s = excluded.duration_s,
                last_updated_ts = excluded.last_updated_ts,
                refresh_interval_s = excluded.refresh_interval_s,
                is_placeholder = excluded.is_placeholder,
                max_age_s = excluded.max_age_s,
                source_name = excluded.source_name,
                product_identifier = excluded.product_identifier,
                product_type = excluded.product_type,
                issuing_office = excluded.issuing_office,
                issuance_time = excluded.issuance_time,
                fetch_time = excluded.fetch_time,
                last_successful_synthesis = excluded.last_successful_synthesis,
                content_hash = excluded.content_hash,
                source_reference = excluded.source_reference,
                last_error = excluded.last_error,
                consecutive_failures = excluded.consecutive_failures,
                last_aired = excluded.last_aired,
                next_eligible_airtime = excluded.next_eligible_airtime
            """,
            (
                str(entry["key"]),
                str(entry.get("title") or ""),
                str(entry.get("text") or ""),
                str(entry.get("audio_path") or ""),
                float(entry.get("duration_s") or 0.0),
                float(entry.get("last_updated_ts") or 0.0),
                int(entry.get("refresh_interval_s") or 0),
                1 if bool(entry.get("is_placeholder", False)) else 0,
                int(entry.get("max_age_s") or 0),
                entry.get("source_name"),
                entry.get("product_identifier"),
                entry.get("product_type"),
                entry.get("issuing_office"),
                entry.get("issuance_time"),
                entry.get("fetch_time"),
                entry.get("last_successful_synthesis"),
                entry.get("content_hash"),
                entry.get("source_reference"),
                entry.get("last_error"),
                min(max(int(entry.get("consecutive_failures") or 0), 0), 1_000_000),
                entry.get("last_aired"),
                entry.get("next_eligible_airtime"),
            ),
        )
