from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .schema import SCHEMA_VERSION, apply_migrations


class JobDatabase:
    """Controller-only SQLite owner for durable jobs."""

    def __init__(self, *, path: str, busy_timeout_ms: int) -> None:
        if not str(path).strip():
            raise ValueError("job database path is required")
        if str(path).startswith("file:"):
            raise ValueError("job database URI paths are not supported")
        if not 100 <= int(busy_timeout_ms) <= 30_000:
            raise ValueError("job database busy timeout is out of bounds")
        self.path = str(path)
        self.busy_timeout_ms = int(busy_timeout_ms)
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        conn.execute("PRAGMA synchronous = FULL")
        mode = str(conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
        if mode != "wal":
            conn.close()
            raise RuntimeError("job database requires WAL journal mode")
        return conn

    def initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as conn:
            apply_migrations(conn)
        self._initialized = True

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        if not self._initialized:
            self.initialize()
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def checkpoint(self) -> None:
        if not self._initialized:
            return
        with self.connection() as conn:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def close(self) -> None:
        self.checkpoint()

    def settings(self) -> dict[str, int | str | bool]:
        with self.connection() as conn:
            schema = int(conn.execute("SELECT COALESCE(MAX(version), 0) FROM job_schema_migrations").fetchone()[0])
            return {
                "initialized": self._initialized,
                "schema_version": schema,
                "expected_schema_version": SCHEMA_VERSION,
                "journal_mode": str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
                "synchronous": int(conn.execute("PRAGMA synchronous").fetchone()[0]),
                "foreign_keys": bool(conn.execute("PRAGMA foreign_keys").fetchone()[0]),
                "busy_timeout_ms": int(conn.execute("PRAGMA busy_timeout").fetchone()[0]),
            }
