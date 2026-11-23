"""SQLite side of controller archive replication."""

import json
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4


def append_alert_snapshot(conn: sqlite3.Connection, alerts: list[dict[str, object]]) -> None:
    """Append using the caller's operational write transaction."""
    _ = conn.execute(
        "INSERT INTO archive_outbox (id, recorded_at, payload_json) VALUES (?, ?, ?)",
        (str(uuid4()), datetime.now(UTC).isoformat(), json.dumps({"active_alerts": alerts})),
    )
