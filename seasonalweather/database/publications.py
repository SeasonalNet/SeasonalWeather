from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .core import SeasonalDatabase


class PublicationLeaseRepository:
    """Durable protection for audio submitted to an external player."""

    def __init__(self, db: SeasonalDatabase | None) -> None:
        self.db = db

    def create(
        self,
        *,
        publication_id: str,
        artifact_path: str,
        plane: str,
        request_id: int | None,
        created_at: str,
        metadata: Mapping[str, str] | None = None,
    ) -> None:
        if self.db is None:
            return
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO publication_leases
                    (publication_id, artifact_path, plane, request_id, state, created_at, updated_at, released_at, metadata_json)
                VALUES (?, ?, ?, ?, 'submitted', ?, ?, NULL, ?)
                ON CONFLICT(publication_id) DO UPDATE SET
                    artifact_path = excluded.artifact_path,
                    plane = excluded.plane,
                    request_id = excluded.request_id,
                    updated_at = excluded.updated_at,
                    released_at = NULL,
                    metadata_json = excluded.metadata_json
                """,
                (
                    publication_id,
                    artifact_path,
                    plane,
                    request_id,
                    created_at,
                    created_at,
                    json.dumps(dict(metadata or {}), sort_keys=True, separators=(",", ":")),
                ),
            )

    def set_state(self, publication_id: str, state: str, updated_at: str, *, released_at: str | None = None) -> None:
        if self.db is None:
            return
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE publication_leases SET state = ?, updated_at = ?, released_at = ? WHERE publication_id = ?",
                (state, updated_at, released_at, publication_id),
            )

    def active_request_ids(self, *, plane: str | None = None) -> set[int]:
        if self.db is None:
            return set()
        with self.db.connect() as conn:
            if plane is None:
                rows = conn.execute(
                    """
                    SELECT request_id FROM publication_leases
                    WHERE request_id IS NOT NULL
                      AND state NOT IN ('finished', 'cancelled', 'failed')
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT request_id FROM publication_leases
                    WHERE request_id IS NOT NULL AND plane = ?
                      AND state NOT IN ('finished', 'cancelled', 'failed')
                    """,
                    (plane,),
                ).fetchall()
        return {int(row[0]) for row in rows}

    def cancel_plane(self, plane: str, updated_at: str) -> set[int]:
        if self.db is None:
            return set()
        with self.db.transaction() as conn:
            rows = conn.execute(
                """
                SELECT request_id FROM publication_leases
                WHERE plane = ? AND state NOT IN ('finished', 'cancelled', 'failed')
                """,
                (plane,),
            ).fetchall()
            conn.execute(
                """
                UPDATE publication_leases
                SET state = 'cancelled', updated_at = ?, released_at = NULL
                WHERE plane = ? AND state NOT IN ('finished', 'cancelled', 'failed')
                """,
                (updated_at, plane),
            )
        return {int(row[0]) for row in rows if row[0] is not None}

    def runtime_epoch(self) -> str | None:
        if self.db is None:
            return None
        with self.db.connect() as conn:
            row = conn.execute("SELECT player_epoch FROM publication_runtime WHERE runtime_id = 1").fetchone()
        return str(row[0]) if row is not None else None

    def reconcile_output(self, publication_id: str, updated_at: str) -> list[dict[str, object]]:
        if self.db is None:
            return []
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT plane, created_at, state FROM publication_leases WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()
            if row is None:
                return []
            if str(row[2]) in {"on_air", "finished", "cancelled", "failed"}:
                return []
            conn.execute(
                "UPDATE publication_leases SET state = 'on_air', updated_at = ? WHERE publication_id = ?",
                (updated_at, publication_id),
            )
            rows = conn.execute(
                "SELECT publication_id, artifact_path, metadata_json FROM publication_leases WHERE publication_id = ?",
                (publication_id,),
            ).fetchall()
        return [
            {
                "publication_id": str(item[0]),
                "artifact_path": str(item[1]),
                "metadata": json.loads(str(item[2] or "{}")),
            }
            for item in rows
        ]

    def reconcile_inventory(self, snapshot: Mapping[str, Any], updated_at: str) -> int:
        if self.db is None:
            return 0
        epoch, all_ids, on_air_ids, statuses = _inventory_values(snapshot)
        if not epoch:
            return 0

        with self.db.transaction() as conn:
            prior = conn.execute("SELECT player_epoch FROM publication_runtime WHERE runtime_id = 1").fetchone()
            epoch_changed = prior is not None and str(prior[0]) != epoch
            conn.execute(
                """
                INSERT INTO publication_runtime(runtime_id, player_epoch, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(runtime_id) DO UPDATE SET player_epoch = excluded.player_epoch, updated_at = excluded.updated_at
                """,
                (epoch, updated_at),
            )
            rows = conn.execute(
                """
                SELECT publication_id, request_id, state
                FROM publication_leases
                WHERE state NOT IN ('finished', 'failed')
                  AND (state != 'cancelled' OR released_at IS NULL)
                """
            ).fetchall()
            changed = 0
            for row in rows:
                transition = _inventory_transition(row, all_ids, on_air_ids, statuses, epoch_changed, updated_at)
                if transition is None:
                    continue
                conn.execute(
                    """
                    UPDATE publication_leases
                    SET state = ?, updated_at = ?, released_at = ?
                    WHERE publication_id = ?
                    """,
                    (*transition, row[0]),
                )
                changed += 1
        return changed

    def protected_paths(self, *, now: str, grace_cutoff: str) -> set[str]:
        if self.db is None:
            return set()
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT artifact_path FROM publication_leases
                WHERE state NOT IN ('finished', 'cancelled', 'failed')
                   OR released_at IS NULL
                   OR released_at >= ?
                """,
                (grace_cutoff,),
            ).fetchall()
        return {str(row[0]) for row in rows if str(row[0] or "").strip()}


__all__ = ["PublicationLeaseRepository"]


def _as_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _int_set(value: object) -> set[int]:
    if isinstance(value, (str, bytes)) or value is None:
        return set()
    if not isinstance(value, (list, tuple, set, frozenset)):
        return set()
    return {item for raw in value if (item := _as_int(raw)) is not None}


def _inventory_values(snapshot: Mapping[str, Any]) -> tuple[str, set[int], set[int], dict[int, str]]:
    statuses = {
        request_id: str(value).strip().lower()
        for raw_key, value in dict(snapshot.get("statuses") or {}).items()
        if (request_id := _as_int(raw_key)) is not None
    }
    return (
        str(snapshot.get("epoch") or "").strip(),
        _int_set(snapshot.get("all")),
        _int_set(snapshot.get("on_air")),
        statuses,
    )


def _inventory_transition(
    row: Any,
    all_ids: set[int],
    on_air_ids: set[int],
    statuses: Mapping[int, str],
    epoch_changed: bool,
    updated_at: str,
) -> tuple[str, str, str | None] | None:
    if str(row[2]) == "cancelled":
        request_id = _as_int(row[1])
        if request_id is None or request_id not in all_ids:
            return "cancelled", updated_at, updated_at
        return None
    state = _inventory_state(row[2], row[1], all_ids, on_air_ids, statuses, epoch_changed)
    if state is None or state == str(row[2]):
        return None
    return state, updated_at, updated_at if state in {"finished", "failed"} else None


def _inventory_state(
    previous: object,
    request_id: object,
    all_ids: set[int],
    on_air_ids: set[int],
    statuses: Mapping[int, str],
    epoch_changed: bool,
) -> str | None:
    rid = _as_int(request_id)
    if rid is None:
        return "unknown"
    if rid in on_air_ids:
        return "on_air"
    if rid in all_ids:
        return {
            "playing": "on_air",
            "ready": "ready",
            "resolving": "queued",
            "idle": "queued",
        }.get(statuses.get(rid, ""), "unknown")
    if epoch_changed:
        return "unknown"
    if str(previous) == "submitted":
        return "unknown"
    if str(previous) == "on_air":
        return "finished"
    return "failed"
