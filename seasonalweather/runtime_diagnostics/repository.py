"""Sole controller-owned SQLite authority for mutable diagnostic occurrences."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from seasonalweather.database import SeasonalDatabase

from .fingerprint import Fingerprint
from .models import (
    MAX_COUNT,
    OccurrenceRecord,
    OccurrenceState,
    OccurrenceTransition,
    ResolutionEvidence,
    RuntimeDiagnostic,
    timestamp,
)
from .redaction import redact_text

OCCURRENCE_REPOSITORY_SCHEMA_VERSION = 1
MAX_TRANSITIONS_PER_OCCURRENCE = 64
MAX_ACTIVE_OCCURRENCES = 10_000


class RecordDisposition(StrEnum):
    CREATED = "created"
    REPEATED = "repeated"
    MATERIAL_UPDATE = "material_update"


@dataclass(frozen=True)
class RecordResult:
    disposition: RecordDisposition
    occurrence: OccurrenceRecord


class OccurrenceRepository:
    def __init__(self, database: SeasonalDatabase) -> None:
        self.database = database

    def initialize(self) -> None:
        with self.database.transaction() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS diagnostic_schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            current = int(
                conn.execute("SELECT COALESCE(MAX(version), 0) FROM diagnostic_schema_migrations").fetchone()[0]
            )
            if current > OCCURRENCE_REPOSITORY_SCHEMA_VERSION:
                raise RuntimeError("diagnostic occurrence schema is newer than this application")
            if current < 1:
                self._migration_one(conn)
                conn.execute("INSERT INTO diagnostic_schema_migrations(version) VALUES (1)")

    @staticmethod
    def _migration_one(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE diagnostic_occurrences (
                occurrence_id TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                component TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('active', 'resolved')),
                diagnostic_schema_version INTEGER NOT NULL,
                catalog_version INTEGER NOT NULL,
                occurrence_schema_version INTEGER NOT NULL,
                fingerprint_version INTEGER NOT NULL,
                fingerprint TEXT NOT NULL,
                fingerprint_key TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                occurrence_count INTEGER NOT NULL CHECK (occurrence_count BETWEEN 1 AND 2147483647),
                initial_instance_json TEXT NOT NULL,
                latest_instance_json TEXT NOT NULL,
                material_hash TEXT NOT NULL,
                resolved_at TEXT,
                resolution_reason TEXT,
                resolution_evidence_json TEXT,
                prior_occurrence_id TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX diagnostic_occurrences_active_fingerprint
                ON diagnostic_occurrences(fingerprint_version, fingerprint)
             WHERE state = 'active'
            """
        )
        conn.execute(
            "CREATE INDEX diagnostic_occurrences_active_code_component "
            "ON diagnostic_occurrences(state, code, component, last_seen)"
        )
        conn.execute("CREATE INDEX diagnostic_occurrences_recent ON diagnostic_occurrences(last_seen DESC)")
        conn.execute(
            """
            CREATE TABLE diagnostic_transitions (
                transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurrence_id TEXT NOT NULL,
                transition_type TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                FOREIGN KEY (occurrence_id) REFERENCES diagnostic_occurrences(occurrence_id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX diagnostic_transitions_occurrence "
            "ON diagnostic_transitions(occurrence_id, transition_id DESC)"
        )

    def record(self, instance: RuntimeDiagnostic, fingerprint: Fingerprint) -> RecordResult:
        payload = instance.to_dict()
        payload_json = _canonical(payload)
        material_hash = _material_hash(payload)
        observed = timestamp(instance.observed_at)
        with self.database.transaction() as conn:
            row = conn.execute(
                """
                SELECT * FROM diagnostic_occurrences
                 WHERE fingerprint_version = ? AND fingerprint = ? AND state = 'active'
                """,
                (fingerprint.version, fingerprint.digest),
            ).fetchone()
            if row is not None:
                if row["fingerprint_key"] != fingerprint.canonical_key:
                    raise RuntimeError("diagnostic fingerprint collision")
                count = min(MAX_COUNT, int(row["occurrence_count"]) + 1)
                material_changed = row["material_hash"] != material_hash
                conn.execute(
                    """
                    UPDATE diagnostic_occurrences
                       SET last_seen = ?, occurrence_count = ?,
                           latest_instance_json = CASE WHEN ? THEN ? ELSE latest_instance_json END,
                           material_hash = CASE WHEN ? THEN ? ELSE material_hash END
                     WHERE occurrence_id = ? AND state = 'active'
                    """,
                    (
                        observed,
                        count,
                        int(material_changed),
                        payload_json,
                        int(material_changed),
                        material_hash,
                        row["occurrence_id"],
                    ),
                )
                transition = "material_update" if material_changed else "repeat"
                evidence = payload if material_changed else {"count": count}
                self._transition(conn, row["occurrence_id"], transition, observed, evidence)
                current = self._row_by_id(conn, row["occurrence_id"])
                return RecordResult(
                    RecordDisposition.MATERIAL_UPDATE if material_changed else RecordDisposition.REPEATED,
                    _record(current),
                )
            prior = conn.execute(
                """
                SELECT occurrence_id, fingerprint_key FROM diagnostic_occurrences
                 WHERE fingerprint_version = ? AND fingerprint = ? AND state = 'resolved'
                 ORDER BY resolved_at DESC LIMIT 1
                """,
                (fingerprint.version, fingerprint.digest),
            ).fetchone()
            if prior is not None and prior["fingerprint_key"] != fingerprint.canonical_key:
                raise RuntimeError("diagnostic fingerprint collision")
            active_count = int(
                conn.execute("SELECT COUNT(*) FROM diagnostic_occurrences WHERE state = 'active'").fetchone()[0]
            )
            if active_count >= MAX_ACTIVE_OCCURRENCES:
                raise RuntimeError("active diagnostic occurrence limit reached")
            occurrence_id = f"occ_{uuid.uuid4().hex}"
            conn.execute(
                """
                INSERT INTO diagnostic_occurrences (
                    occurrence_id, code, component, state,
                    diagnostic_schema_version, catalog_version, occurrence_schema_version,
                    fingerprint_version, fingerprint, fingerprint_key,
                    first_seen, last_seen, occurrence_count,
                    initial_instance_json, latest_instance_json, material_hash,
                    prior_occurrence_id
                ) VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    occurrence_id,
                    instance.code,
                    instance.context.component,
                    instance.diagnostic_schema_version,
                    instance.catalog_version,
                    instance.occurrence_schema_version,
                    fingerprint.version,
                    fingerprint.digest,
                    fingerprint.canonical_key,
                    observed,
                    observed,
                    payload_json,
                    payload_json,
                    material_hash,
                    prior["occurrence_id"] if prior is not None else None,
                ),
            )
            self._transition(conn, occurrence_id, "activated", observed, payload)
            return RecordResult(RecordDisposition.CREATED, _record(self._row_by_id(conn, occurrence_id)))

    def resolve(
        self,
        occurrence_id: str,
        *,
        observed_at: dt.datetime,
        reason: str,
        evidence: ResolutionEvidence,
    ) -> OccurrenceRecord | None:
        observed = timestamp(observed_at)
        bounded_reason = redact_text(reason, limit=512)
        bounded_evidence = evidence.to_dict()
        evidence_json = _canonical(bounded_evidence)
        with self.database.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM diagnostic_occurrences WHERE occurrence_id = ?",
                (occurrence_id,),
            ).fetchone()
            if row is None:
                return None
            if row["state"] == OccurrenceState.RESOLVED.value:
                return _record(row)
            conn.execute(
                """
                UPDATE diagnostic_occurrences
                   SET state = 'resolved', resolved_at = ?, resolution_reason = ?,
                       resolution_evidence_json = ?
                 WHERE occurrence_id = ? AND state = 'active'
                """,
                (observed, bounded_reason, evidence_json, occurrence_id),
            )
            self._transition(conn, occurrence_id, "resolved", observed, bounded_evidence | {"reason": bounded_reason})
            return _record(self._row_by_id(conn, occurrence_id))

    def transitions(
        self,
        occurrence_id: str,
        *,
        limit: int = MAX_TRANSITIONS_PER_OCCURRENCE,
    ) -> tuple[OccurrenceTransition, ...]:
        bounded = max(1, min(limit, MAX_TRANSITIONS_PER_OCCURRENCE))
        with self.database.connect() as conn:
            rows = conn.execute(
                """
                SELECT transition_type, observed_at, evidence_json
                  FROM diagnostic_transitions
                 WHERE occurrence_id = ?
                 ORDER BY transition_id
                 LIMIT ?
                """,
                (occurrence_id, bounded),
            ).fetchall()
        return tuple(
            OccurrenceTransition(
                transition_type=row["transition_type"],
                observed_at=_parse_time(row["observed_at"]) or dt.datetime.min.replace(tzinfo=dt.UTC),
                evidence=json.loads(row["evidence_json"]),
            )
            for row in rows
        )

    def get(self, occurrence_id: str) -> OccurrenceRecord | None:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM diagnostic_occurrences WHERE occurrence_id = ?",
                (occurrence_id,),
            ).fetchone()
            return _record(row) if row is not None else None

    def active(self, *, limit: int = 100) -> tuple[OccurrenceRecord, ...]:
        bounded = max(1, min(limit, 500))
        with self.database.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM diagnostic_occurrences
                 WHERE state = 'active'
                 ORDER BY last_seen DESC, occurrence_id
                 LIMIT ?
                """,
                (bounded,),
            ).fetchall()
            return tuple(_record(row) for row in rows)

    def recent(self, *, limit: int = 100) -> tuple[OccurrenceRecord, ...]:
        bounded = max(1, min(limit, 500))
        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM diagnostic_occurrences ORDER BY last_seen DESC, occurrence_id LIMIT ?",
                (bounded,),
            ).fetchall()
            return tuple(_record(row) for row in rows)

    def prune(self, *, resolved_before: dt.datetime, retain_resolved: int = 1_000) -> int:
        cutoff = timestamp(resolved_before)
        retain = max(0, min(retain_resolved, 100_000))
        with self.database.transaction() as conn:
            retained = conn.execute(
                """
                SELECT occurrence_id FROM diagnostic_occurrences
                 WHERE state = 'resolved'
                 ORDER BY resolved_at DESC, occurrence_id DESC
                 LIMIT ?
                """,
                (retain,),
            ).fetchall()
            keep = {row["occurrence_id"] for row in retained}
            candidates = conn.execute(
                """
                SELECT occurrence_id FROM diagnostic_occurrences
                 WHERE state = 'resolved' AND resolved_at < ?
                 ORDER BY resolved_at, occurrence_id
                 LIMIT 500
                """,
                (cutoff,),
            ).fetchall()
            remove = [row["occurrence_id"] for row in candidates if row["occurrence_id"] not in keep]
            if remove:
                conn.executemany(
                    "DELETE FROM diagnostic_occurrences WHERE occurrence_id = ? AND state = 'resolved'",
                    ((item,) for item in remove),
                )
            return len(remove)

    @staticmethod
    def _transition(
        conn: sqlite3.Connection,
        occurrence_id: str,
        transition_type: str,
        observed_at: str,
        evidence: dict[str, Any],
    ) -> None:
        _trim_transitions(conn, occurrence_id)
        conn.execute(
            """
            INSERT INTO diagnostic_transitions(
                occurrence_id, transition_type, observed_at, evidence_json
            ) VALUES (?, ?, ?, ?)
            """,
            (occurrence_id, transition_type, observed_at, _canonical(evidence)),
        )

    @staticmethod
    def _row_by_id(conn: sqlite3.Connection, occurrence_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM diagnostic_occurrences WHERE occurrence_id = ?",
            (occurrence_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("diagnostic occurrence disappeared")
        return cast(sqlite3.Row, row)


def _trim_transitions(conn: sqlite3.Connection, occurrence_id: str) -> None:
    count = int(
        conn.execute(
            "SELECT COUNT(*) FROM diagnostic_transitions WHERE occurrence_id = ?",
            (occurrence_id,),
        ).fetchone()[0]
    )
    remove = max(0, count - MAX_TRANSITIONS_PER_OCCURRENCE + 1)
    if remove:
        conn.execute(
            """
            DELETE FROM diagnostic_transitions
             WHERE transition_id IN (
                SELECT transition_id FROM diagnostic_transitions
                 WHERE occurrence_id = ?
                 ORDER BY CASE transition_type WHEN 'repeat' THEN 0 ELSE 1 END,
                          transition_id
                 LIMIT ?
             )
            """,
            (occurrence_id, remove),
        )


def _canonical(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _material_hash(payload: dict[str, Any]) -> str:
    import hashlib

    material = {
        key: payload[key]
        for key in ("severity", "blocking", "fatal", "retryable", "context", "operational_effect", "recovery_action")
    }
    return hashlib.sha256(_canonical(material).encode()).hexdigest()


def _parse_time(value: str | None) -> dt.datetime | None:
    if value is None:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.UTC)


def _record(row: sqlite3.Row) -> OccurrenceRecord:
    return OccurrenceRecord(
        occurrence_id=row["occurrence_id"],
        code=row["code"],
        state=OccurrenceState(row["state"]),
        fingerprint=row["fingerprint"],
        fingerprint_key=row["fingerprint_key"],
        fingerprint_version=int(row["fingerprint_version"]),
        diagnostic_schema_version=int(row["diagnostic_schema_version"]),
        catalog_version=int(row["catalog_version"]),
        occurrence_schema_version=int(row["occurrence_schema_version"]),
        first_seen=_parse_time(row["first_seen"]) or dt.datetime.min.replace(tzinfo=dt.UTC),
        last_seen=_parse_time(row["last_seen"]) or dt.datetime.min.replace(tzinfo=dt.UTC),
        count=int(row["occurrence_count"]),
        initial_instance=json.loads(row["initial_instance_json"]),
        latest_instance=json.loads(row["latest_instance_json"]),
        resolved_at=_parse_time(row["resolved_at"]),
        resolution_reason=row["resolution_reason"],
        resolution_evidence=(
            json.loads(row["resolution_evidence_json"]) if row["resolution_evidence_json"] is not None else None
        ),
        prior_occurrence_id=row["prior_occurrence_id"],
    )
