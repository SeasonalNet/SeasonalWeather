"""Repositories for durable state that is not itself a primary domain record."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .core import SeasonalDatabase


class ObservationPressureRepository:
    def __init__(self, db: SeasonalDatabase) -> None:
        self.db = db

    def recent(self, station_id: str, cutoff_iso: str) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT observed_at, pressure_inhg
                FROM observation_pressure_history
                WHERE station_id = ? AND observed_at >= ?
                ORDER BY observed_at
                """,
                (station_id, cutoff_iso),
            ).fetchall()
        return [{"ts": str(row["observed_at"]), "p": float(row["pressure_inhg"])} for row in rows]

    def append_and_prune(self, station_id: str, observed_at: str, pressure_inhg: float, cutoff_iso: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO observation_pressure_history
                    (station_id, observed_at, pressure_inhg)
                VALUES (?, ?, ?)
                """,
                (station_id, observed_at, float(pressure_inhg)),
            )
            conn.execute(
                "DELETE FROM observation_pressure_history WHERE station_id = ? AND observed_at < ?",
                (station_id, cutoff_iso),
            )


class ProcessMarkerRepository:
    def __init__(self, db: SeasonalDatabase) -> None:
        self.db = db

    def get(self, marker_name: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT marker_json FROM runtime_process_markers WHERE marker_name = ?", (marker_name,)
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["marker_json"]))
        return payload if isinstance(payload, dict) else None

    def put(self, marker_name: str, payload: Mapping[str, Any], updated_at: str) -> None:
        encoded = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO runtime_process_markers(marker_name, marker_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(marker_name) DO UPDATE SET marker_json = excluded.marker_json, updated_at = excluded.updated_at
                """,
                (marker_name, encoded, updated_at),
            )

    def delete(self, marker_name: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM runtime_process_markers WHERE marker_name = ?", (marker_name,))


class ConfigurationCandidateRepository:
    def __init__(self, db: SeasonalDatabase) -> None:
        self.db = db

    def get(self, reference: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT metadata_json FROM configuration_candidates WHERE candidate_reference = ?",
                (reference,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["metadata_json"]))
        return payload if isinstance(payload, dict) else None

    def put(self, reference: str, metadata: Mapping[str, Any], captured_at: str) -> None:
        encoded = json.dumps(dict(metadata), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO configuration_candidates(candidate_reference, metadata_json, captured_at)
                VALUES (?, ?, ?)
                ON CONFLICT(candidate_reference) DO UPDATE SET metadata_json = excluded.metadata_json, captured_at = excluded.captured_at
                """,
                (reference, encoded, captured_at),
            )

    def put_report(
        self, candidate_reference: str, reference: str, digest: str, report: Mapping[str, Any], created_at: str
    ) -> None:
        encoded = json.dumps(dict(report), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO configuration_candidate_reports
                    (candidate_reference, report_reference, report_sha256, report_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(candidate_reference, report_reference) DO UPDATE SET
                    report_sha256 = excluded.report_sha256,
                    report_json = excluded.report_json,
                    created_at = excluded.created_at
                """,
                (candidate_reference, reference, digest, encoded, created_at),
            )

    def get_report(self, candidate_reference: str, reference: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT report_json FROM configuration_candidate_reports WHERE candidate_reference = ? AND report_reference = ?",
                (candidate_reference, reference),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["report_json"]))
        return payload if isinstance(payload, dict) else None

    def delete(self, reference: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM configuration_candidates WHERE candidate_reference = ?", (reference,))


class SegmentCommitRepository:
    def __init__(self, db: SeasonalDatabase) -> None:
        self.db = db

    def put_journal(self, record: Mapping[str, Any]) -> None:
        previous_entry = record.get("previous_entry")
        encoded = (
            json.dumps(previous_entry, sort_keys=True, separators=(",", ":")) if previous_entry is not None else None
        )
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO segment_commit_journals
                    (operation_id, segment_key, target_path, previous_path, command_id, committed, publication_won, previous_entry_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(operation_id) DO UPDATE SET
                    committed = excluded.committed, publication_won = excluded.publication_won
                """,
                (
                    record["operation_id"],
                    record["key"],
                    record["target"],
                    record.get("previous"),
                    record.get("command_id"),
                    1 if record.get("committed") else 0,
                    1 if record.get("publication_won") else 0,
                    encoded,
                ),
            )

    def update_flags(
        self, operation_id: str, *, committed: bool | None = None, publication_won: bool | None = None
    ) -> None:
        with self.db.transaction() as conn:
            if committed is not None:
                conn.execute(
                    "UPDATE segment_commit_journals SET committed = ? WHERE operation_id = ?",
                    (1 if committed else 0, operation_id),
                )
            if publication_won is not None:
                conn.execute(
                    "UPDATE segment_commit_journals SET publication_won = ? WHERE operation_id = ?",
                    (1 if publication_won else 0, operation_id),
                )

    def journals(self, key: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM segment_commit_journals"
        args: tuple[Any, ...] = ()
        if key is not None:
            query += " WHERE segment_key = ?"
            args = (key,)
        query += " ORDER BY operation_id"
        with self.db.connect() as conn:
            rows = conn.execute(query, args).fetchall()
        return [
            {
                "operation_id": row["operation_id"],
                "key": row["segment_key"],
                "target": row["target_path"],
                "previous": row["previous_path"],
                "command_id": row["command_id"],
                "committed": bool(row["committed"]),
                "publication_won": bool(row["publication_won"]),
                "previous_entry": json.loads(row["previous_entry_json"]) if row["previous_entry_json"] else None,
            }
            for row in rows
        ]

    def delete_journal(self, operation_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM segment_commit_journals WHERE operation_id = ?", (operation_id,))

    def put_receipt(self, key: str, command_id: str, target: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO segment_commit_receipts(segment_key, command_id, target_path) VALUES (?, ?, ?)",
                (key, command_id, target),
            )

    def receipts(self, key: str | None = None) -> list[dict[str, str]]:
        query = "SELECT segment_key, command_id, target_path FROM segment_commit_receipts"
        args: tuple[Any, ...] = ()
        if key is not None:
            query += " WHERE segment_key = ?"
            args = (key,)
        with self.db.connect() as conn:
            rows = conn.execute(query, args).fetchall()
        return [
            {"key": str(row["segment_key"]), "command_id": str(row["command_id"]), "target": str(row["target_path"])}
            for row in rows
        ]

    def delete_receipt(self, key: str, command_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM segment_commit_receipts WHERE segment_key = ? AND command_id = ?", (key, command_id)
            )
