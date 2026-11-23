"""Optional controller task; no network I/O under a SQLite transaction."""

import asyncio
import json
import logging
import sqlite3
from datetime import datetime
from uuid import UUID

from ..config import AppConfig
from .core import SeasonalDatabase
from .postgresql_archive import ArchiveOrigin, ArchiveRecord, ArchiveTable, PostgresArchiveRepository

log = logging.getLogger(__name__)
_BATCH_SIZE = 32


def _decode_payload(raw: str) -> dict[str, object]:
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ValueError("Invalid archive outbox JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Archive outbox payload must be an object")
    return payload


class ArchiveReplication:
    def __init__(self, db: SeasonalDatabase, archive: PostgresArchiveRepository) -> None:
        self.db = db
        self.archive = archive
        self.retrying = False
        self.has_committed = False

    @classmethod
    def from_config(cls, db: SeasonalDatabase | None, cfg: AppConfig) -> "ArchiveReplication | None":
        if db is None or not db.archive_outbox_enabled:
            return None
        return cls(db, PostgresArchiveRepository(cfg.database.postgres, cfg.network.postgresql))

    def backlog(self) -> dict[str, object]:
        with self.db.connect() as conn:
            count, oldest = conn.execute("SELECT COUNT(*), MIN(recorded_at) FROM archive_outbox").fetchone()
        return {"outbox_pending": count, "outbox_oldest_at": oldest}

    def drain_batch(self) -> int:
        with self.db.connect() as conn:
            rows: list[sqlite3.Row] = conn.execute(
                "SELECT id, recorded_at, payload_json FROM archive_outbox ORDER BY sequence LIMIT ?",
                (_BATCH_SIZE,),
            ).fetchall()
        if not rows:
            return 0
        records = tuple(
            ArchiveRecord(
                table=ArchiveTable.ALERT_LIFECYCLE_VERSIONS,
                id=UUID(row["id"]),
                origin=ArchiveOrigin.LOCAL_OUTBOX_REPLAY,
                recorded_at=datetime.fromisoformat(row["recorded_at"]),
                payload=_decode_payload(row["payload_json"]),
            )
            for row in rows
        )
        self.archive.replay_many(records)
        with self.db.transaction() as conn:
            _ = conn.executemany("DELETE FROM archive_outbox WHERE id = ?", [(row["id"],) for row in rows])
        return len(rows)

    async def run(self) -> None:
        delay = 1.0
        while True:
            try:
                drained = await asyncio.to_thread(self.drain_batch)
            except Exception as exc:
                if not self.retrying:
                    log.warning("Archive replication retrying (%s)", type(exc).__name__)
                self.retrying = True
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
            else:
                if self.retrying:
                    log.info("Archive replication resumed")
                self.retrying = False
                self.has_committed |= drained > 0
                delay = 1.0
                await asyncio.sleep(0.05 if drained == _BATCH_SIZE else delay)
