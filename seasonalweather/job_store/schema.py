from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 2

MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE jobs (
            job_id TEXT PRIMARY KEY,
            command_id TEXT,
            job_type TEXT NOT NULL,
            queue TEXT NOT NULL,
            executor TEXT NOT NULL,
            priority INTEGER NOT NULL,
            status TEXT NOT NULL,
            payload_schema_version INTEGER NOT NULL,
            result_schema_version INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            not_before TEXT NOT NULL,
            deadline_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
            max_attempts INTEGER NOT NULL CHECK (max_attempts >= 1),
            dedupe_key TEXT,
            config_generation INTEGER,
            replay_policy TEXT NOT NULL,
            cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
            lease_id TEXT,
            attempt_id TEXT,
            lease_owner TEXT,
            lease_controller_id TEXT,
            ack_deadline_at TEXT,
            lease_expires_at TEXT,
            result_json TEXT,
            error_json TEXT,
            result_hash TEXT,
            version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
            command_sync_pending INTEGER NOT NULL DEFAULT 0
                CHECK (command_sync_pending IN (0, 1))
        )
        """,
        """
        CREATE UNIQUE INDEX jobs_active_dedupe
            ON jobs(job_type, dedupe_key)
         WHERE dedupe_key IS NOT NULL
           AND status IN ('pending', 'leased', 'running')
        """,
        """
        CREATE INDEX jobs_eligibility
            ON jobs(status, queue, executor, priority, not_before, created_at)
        """,
        "CREATE INDEX jobs_command_sync ON jobs(command_sync_pending, command_id)",
        """
        CREATE TABLE job_attempts (
            job_id TEXT NOT NULL,
            attempt INTEGER NOT NULL CHECK (attempt >= 1),
            attempt_id TEXT NOT NULL UNIQUE,
            lease_id TEXT NOT NULL UNIQUE,
            lease_owner TEXT NOT NULL,
            acquired_at TEXT NOT NULL,
            ack_deadline_at TEXT NOT NULL,
            lease_expires_at TEXT NOT NULL,
            acknowledged_at TEXT,
            started_at TEXT,
            finished_at TEXT,
            outcome TEXT,
            error_json TEXT,
            PRIMARY KEY (job_id, attempt),
            FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE job_lease_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            attempt INTEGER NOT NULL,
            lease_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE job_progress (
            progress_id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            attempt INTEGER NOT NULL,
            lease_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            reason TEXT,
            numeric_json TEXT NOT NULL DEFAULT '{}',
            occurred_at TEXT NOT NULL,
            FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE job_relationships (
            relationship_id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            related_job_id TEXT NOT NULL,
            relation TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(job_id, related_job_id, relation),
            FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE RESTRICT,
            FOREIGN KEY (related_job_id) REFERENCES jobs(job_id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE job_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            data_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE job_result_commits (
            job_id TEXT PRIMARY KEY,
            attempt INTEGER NOT NULL,
            lease_id TEXT NOT NULL,
            result_schema_version INTEGER NOT NULL,
            result_json TEXT NOT NULL,
            result_hash TEXT NOT NULL,
            committed_at TEXT NOT NULL,
            FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE RESTRICT
        )
        """,
    ),
    2: (
        """
        CREATE TABLE artifact_publication_receipts (
            job_id TEXT NOT NULL,
            attempt_id TEXT NOT NULL,
            artifact_digest TEXT NOT NULL,
            artifact_size_bytes INTEGER NOT NULL,
            artifact_class TEXT NOT NULL,
            target_key TEXT NOT NULL,
            prior_digest TEXT,
            result_hash TEXT,
            disposition TEXT NOT NULL,
            prepared_at TEXT,
            promoted_at TEXT,
            committed_at TEXT,
            metadata_json TEXT NOT NULL,
            PRIMARY KEY(job_id, attempt_id),
            FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE RESTRICT
        )
        """,
        "CREATE INDEX artifact_publication_receipts_state ON artifact_publication_receipts(disposition, committed_at)",
    ),
}


def apply_migrations(conn: sqlite3.Connection) -> int:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS job_schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM job_schema_migrations").fetchone()
    current = int(row[0])
    if current > SCHEMA_VERSION:
        raise RuntimeError("job database schema is newer than this application")
    for version in range(current + 1, SCHEMA_VERSION + 1):
        for statement in MIGRATIONS[version]:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO job_schema_migrations(version) VALUES (?)",
            (version,),
        )
    return SCHEMA_VERSION
