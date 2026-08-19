from __future__ import annotations

import json
from typing import Any

from .core import SeasonalDatabase


class CommandRepository:
    def __init__(self, db: SeasonalDatabase) -> None:
        self.db = db

    def get_by_command_id(self, command_id: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM api_commands WHERE command_id = ?", (command_id,)).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def get_by_idempotency_key(self, idempotency_key: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM api_commands WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def list_by_type_and_status(
        self,
        command_type: str,
        statuses: tuple[str, ...],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        if len(statuses) != 2:
            return []
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM api_commands
                 WHERE command_type = ?
                   AND status IN (?, ?)
                 ORDER BY accepted_at, command_id
                 LIMIT ?
                """,
                (command_type, *statuses, limit),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def nonterminal_cursor(
        self,
        command_type: str,
        statuses: tuple[str, ...],
    ) -> tuple[str, str] | None:
        if len(statuses) != 2:
            return None
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT accepted_at, command_id FROM api_commands
                 WHERE command_type = ?
                   AND status IN (?, ?)
                 ORDER BY accepted_at DESC, command_id DESC
                 LIMIT 1
                """,
                (command_type, *statuses),
            ).fetchone()
        return (str(row["accepted_at"]), str(row["command_id"])) if row is not None else None

    def list_by_type_and_status_page(
        self,
        command_type: str,
        statuses: tuple[str, ...],
        *,
        after: tuple[str, str] | None,
        through: tuple[str, str] | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], tuple[str, str] | None]:
        if len(statuses) != 2 or through is None:
            return [], None
        after_at = after[0] if after is not None else None
        after_id = after[1] if after is not None else None
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM api_commands
                 WHERE command_type = ?
                   AND status IN (?, ?)
                   AND (
                       accepted_at < ?
                       OR (accepted_at = ? AND command_id <= ?)
                   )
                   AND (
                       ? IS NULL
                       OR accepted_at > ?
                       OR (accepted_at = ? AND command_id > ?)
                   )
                 ORDER BY accepted_at, command_id
                 LIMIT ?
                """,
                (
                    command_type,
                    *statuses,
                    through[0],
                    through[0],
                    through[1],
                    after_at,
                    after_at,
                    after_at,
                    after_id,
                    limit,
                ),
            ).fetchall()
        records = [self._row_to_dict(row) for row in rows]
        cursor = (str(rows[-1]["accepted_at"]), str(rows[-1]["command_id"])) if rows else None
        return records, cursor

    def insert(self, record: dict[str, Any]) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO api_commands (
                    command_id, command_type, status, accepted_at, idempotency_key,
                    actor, payload_hash, payload_json, request_id, started_at,
                    finished_at, idempotent_replay_count, result_json, error_json,
                    created_at, reason, correlation_id, cancel_requested_at,
                    audit_context_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["command_id"],
                    record["command_type"],
                    record["status"],
                    record["accepted_at"],
                    record["idempotency_key"],
                    record["actor"],
                    record["payload_hash"],
                    "{}",
                    record["request_id"],
                    record.get("started_at"),
                    record.get("finished_at"),
                    int(record.get("idempotent_replay_count", 0) or 0),
                    json.dumps(record.get("result") or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                    if record.get("result") is not None
                    else None,
                    json.dumps(record.get("error") or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                    if record.get("error") is not None
                    else None,
                    record["created_at"],
                    record.get("reason"),
                    record.get("correlation_id"),
                    record.get("cancel_requested_at"),
                    json.dumps(
                        record.get("audit_context") or {},
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ),
                ),
            )

    def update(self, record: dict[str, Any]) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE api_commands
                   SET status = ?,
                       started_at = ?,
                       finished_at = ?,
                       idempotent_replay_count = ?,
                       result_json = ?,
                       error_json = ?,
                       reason = ?,
                       correlation_id = ?,
                       cancel_requested_at = ?,
                       audit_context_json = ?
                 WHERE command_id = ?
                """,
                (
                    record["status"],
                    record.get("started_at"),
                    record.get("finished_at"),
                    int(record.get("idempotent_replay_count", 0) or 0),
                    json.dumps(record.get("result") or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                    if record.get("result") is not None
                    else None,
                    json.dumps(record.get("error") or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                    if record.get("error") is not None
                    else None,
                    record.get("reason"),
                    record.get("correlation_id"),
                    record.get("cancel_requested_at"),
                    json.dumps(
                        record.get("audit_context") or {},
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ),
                    record["command_id"],
                ),
            )

    def prune_terminal_before(self, cutoff_iso: str) -> int:
        with self.db.transaction() as conn:
            cur = conn.execute(
                """
                DELETE FROM api_commands
                 WHERE status IN ('succeeded', 'failed', 'cancelled', 'expired', 'superseded')
                   AND COALESCE(finished_at, accepted_at) < ?
                """,
                (cutoff_iso,),
            )
        return int(cur.rowcount or 0)

    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, Any]:
        return {
            "command_id": str(row["command_id"]),
            "command_type": str(row["command_type"]),
            "status": str(row["status"]),
            "accepted_at": str(row["accepted_at"]),
            "created_at": str(row["created_at"] or row["accepted_at"]),
            "idempotency_key": str(row["idempotency_key"]),
            "actor": str(row["actor"]),
            "payload_hash": str(row["payload_hash"]),
            "request_id": str(row["request_id"]),
            "reason": row["reason"],
            "correlation_id": row["correlation_id"],
            "cancel_requested_at": row["cancel_requested_at"],
            "audit_context": json.loads(row["audit_context_json"] or "{}"),
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "idempotent_replay_count": int(row["idempotent_replay_count"] or 0),
            "result": json.loads(row["result_json"] or "null"),
            "error": json.loads(row["error_json"] or "null"),
        }
