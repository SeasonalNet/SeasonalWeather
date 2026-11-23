"""Exercise the local/outbox commit boundary with a real SQLite page ceiling."""

import json
import sqlite3
from pathlib import Path

import pytest

from seasonalweather.database.alerts import AlertStateRepository
from seasonalweather.database.core import SeasonalDatabase


def test_sqlite_full_keeps_prior_alert_and_outbox_checkpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = SeasonalDatabase(path=str(tmp_path / "state.sqlite"))
    db.archive_outbox_enabled = True
    alerts = AlertStateRepository(db)
    alerts.replace_active_alerts([{"id": "prior"}])
    connect = db._connect

    def limited_connection() -> sqlite3.Connection:
        conn = connect()
        pages = int(conn.execute("PRAGMA page_count").fetchone()[0])
        _ = conn.execute(f"PRAGMA max_page_count = {pages}")
        return conn

    with monkeypatch.context() as patch:
        patch.setattr(db, "_connect", limited_connection)
        with pytest.raises(sqlite3.OperationalError) as failure:
            alerts.replace_active_alerts([{"id": "overflow", "headline": "x" * 1_000_000}])
        assert failure.value.sqlite_errorcode == sqlite3.SQLITE_FULL

    assert alerts.load_active_alerts()[0]["id"] == "prior"
    alerts.replace_active_alerts([{"id": "recovered"}])
    assert alerts.load_active_alerts()[0]["id"] == "recovered"
    with db.connect() as conn:
        rows = conn.execute("SELECT payload_json FROM archive_outbox ORDER BY sequence").fetchall()
    assert [json.loads(row[0]) for row in rows] == [
        {"active_alerts": [{"id": "prior"}]},
        {"active_alerts": [{"id": "recovered"}]},
    ]
