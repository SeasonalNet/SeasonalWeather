from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .core import SeasonalDatabase
from .outbox import append_alert_snapshot


class AlertStateRepository:
    def __init__(self, db: SeasonalDatabase) -> None:
        self.db = db

    def replace_active_alerts(self, alerts: Iterable[Mapping[str, Any]]) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM active_alert_vtec")
            conn.execute("DELETE FROM active_alert_same")
            conn.execute("DELETE FROM active_alerts")
            snapshot: list[dict[str, Any]] = []
            for alert in alerts:
                if self.db.archive_outbox_enabled:
                    snapshot.append({key: value for key, value in alert.items() if key != "audio_path"})
                conn.execute(
                    """
                    INSERT INTO active_alerts (
                        alert_id, source, event, code, headline, script_text, audio_path,
                        expires_at, issued_at, cycle_only, watch_number,
                        first_aired_at, last_aired_at, airing_count, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(alert["id"]),
                        str(alert.get("source") or ""),
                        str(alert.get("event") or ""),
                        str(alert.get("code") or ""),
                        str(alert.get("headline") or ""),
                        str(alert.get("script_text") or ""),
                        str(alert.get("audio_path") or "") or None,
                        str(alert.get("expires") or ""),
                        str(alert.get("issued") or ""),
                        1 if bool(alert.get("cycle_only", False)) else 0,
                        int(alert["watch_number"]) if alert.get("watch_number") is not None else None,
                        str(alert.get("first_aired") or "") or None,
                        str(alert.get("last_aired") or "") or None,
                        int(alert.get("airing_count", 0) or 0),
                        str(alert.get("created_at") or alert.get("issued") or ""),
                        str(alert.get("updated_at") or alert.get("last_aired") or alert.get("issued") or ""),
                    ),
                )
                for vtec in alert.get("vtec") or []:
                    conn.execute(
                        "INSERT INTO active_alert_vtec(alert_id, vtec) VALUES (?, ?)",
                        (str(alert["id"]), str(vtec)),
                    )
                for same_code in alert.get("same_locs") or []:
                    conn.execute(
                        "INSERT INTO active_alert_same(alert_id, same_code) VALUES (?, ?)",
                        (str(alert["id"]), str(same_code)),
                    )

            if self.db.archive_outbox_enabled:
                append_alert_snapshot(conn, snapshot)

    def load_active_alerts(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM active_alerts ORDER BY COALESCE(first_aired_at, issued_at), issued_at, alert_id"
            ).fetchall()
            if not rows:
                return []
            vtec_rows = conn.execute("SELECT alert_id, vtec FROM active_alert_vtec ORDER BY alert_id, vtec").fetchall()
            same_rows = conn.execute(
                "SELECT alert_id, same_code FROM active_alert_same ORDER BY alert_id, same_code"
            ).fetchall()
        vtec_by_alert: dict[str, list[str]] = {}
        same_by_alert: dict[str, list[str]] = {}
        for row in vtec_rows:
            vtec_by_alert.setdefault(str(row["alert_id"]), []).append(str(row["vtec"]))
        for row in same_rows:
            same_by_alert.setdefault(str(row["alert_id"]), []).append(str(row["same_code"]))
        items: list[dict[str, Any]] = []
        for row in rows:
            alert_id = str(row["alert_id"])
            items.append(
                {
                    "id": alert_id,
                    "source": str(row["source"]),
                    "event": str(row["event"]),
                    "code": str(row["code"]),
                    "vtec": vtec_by_alert.get(alert_id, []),
                    "headline": str(row["headline"]),
                    "script_text": str(row["script_text"]),
                    "audio_path": row["audio_path"],
                    "expires": str(row["expires_at"]),
                    "issued": str(row["issued_at"]),
                    "same_locs": same_by_alert.get(alert_id, []),
                    "cycle_only": bool(row["cycle_only"]),
                    "watch_number": row["watch_number"],
                    "first_aired": row["first_aired_at"],
                    "last_aired": row["last_aired_at"],
                    "airing_count": int(row["airing_count"] or 0),
                }
            )
        return items


class CapLedgerRepository:
    def __init__(self, db: SeasonalDatabase) -> None:
        self.db = db

    def replace_entries(self, entries: Mapping[str, str]) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM cap_seen_ledger")
            for key, seen_at in entries.items():
                conn.execute(
                    "INSERT INTO cap_seen_ledger(dedupe_key, seen_at) VALUES (?, ?)",
                    (str(key), str(seen_at)),
                )

    def prune_before(self, cutoff_iso: str) -> int:
        with self.db.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM cap_seen_ledger WHERE seen_at < ?",
                (cutoff_iso,),
            )
        return int(cur.rowcount or 0)

    def load_entries(self) -> dict[str, str]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT dedupe_key, seen_at FROM cap_seen_ledger").fetchall()
        return {str(row["dedupe_key"]): str(row["seen_at"]) for row in rows}
