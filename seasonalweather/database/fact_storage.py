"""PostgreSQL persistence for pre-air candidates, not an airtime archive."""

from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

from psycopg.errors import ForeignKeyViolation
from psycopg.sql import SQL, Identifier
from psycopg.types.json import Jsonb

from .postgresql_archive import ArchiveRecord, ArchiveTable, PostgresArchiveRepository

_SOURCE_TABLES = frozenset(
    {ArchiveTable.ALERT_LIFECYCLE_VERSIONS, ArchiveTable.FORECAST_SNAPSHOTS, ArchiveTable.OBSERVATION_SAMPLES}
)


class CandidateProvenanceError(ValueError):
    """A candidate refers to an unavailable or inadmissible archive source."""


class CandidateConflictError(ValueError):
    """The stable candidate ID is already bound to different content."""


@dataclass(frozen=True)
class ArchiveRef:
    table: ArchiveTable
    id: UUID


@dataclass(frozen=True)
class CandidateRow:
    id: UUID
    expires_at: datetime
    payload: dict[str, object]
    current: ArchiveRef
    previous: ArchiveRef
    valid_from: datetime

    def __post_init__(self) -> None:
        if self.valid_from.utcoffset() is None or self.expires_at.utcoffset() is None:
            raise ValueError("Candidate validity times must include a timezone")
        if self.valid_from >= self.expires_at:
            raise ValueError("Candidate validity must start before expiration")
        if any(ref.table not in _SOURCE_TABLES for ref in (self.current, self.previous)):
            raise CandidateProvenanceError("Candidate references an inadmissible archive source table")


class PostgresFactCandidateRepository:
    """Store opaque candidate payloads with exact archive links and no playout authority."""

    def __init__(self, archive: PostgresArchiveRepository) -> None:
        self._archive = archive
        self._table = Identifier(archive.schema, "broadcast_fact_candidates")

    def lookup(self, ref: ArchiveRef) -> ArchiveRecord | None:
        return self._archive.get(ref.table, ref.id)

    def store(self, row: CandidateRow) -> None:
        """Persist the caller's stable ID, or verify an identical, already committed retry."""
        values = (
            row.id,
            row.expires_at,
            Jsonb(row.payload),
            row.current.table.value,
            row.current.id,
            row.previous.table.value,
            row.previous.id,
            row.valid_from,
        )
        insert = SQL(
            "INSERT INTO {} (id, expires_at, payload, current_table, current_id, previous_table, previous_id, valid_from) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING RETURNING id"
        ).format(self._table)
        verify = SQL(
            "SELECT 1 FROM {} WHERE id = %s AND expires_at = %s AND payload = %s "
            "AND current_table = %s AND current_id = %s AND previous_table = %s AND previous_id = %s AND valid_from = %s"
        ).format(self._table)
        try:
            with self._archive.transaction() as connection:
                inserted = connection.execute(insert.as_string(), values).fetchone()
                # A separate statement observes a concurrent winner after ON CONFLICT waits for it.
                if inserted is None and connection.execute(verify.as_string(), values).fetchone() is None:
                    raise CandidateConflictError("Candidate ID is already bound to different content")
        except ForeignKeyViolation as exc:
            raise CandidateProvenanceError("Candidate references a missing archive record") from exc

    def get(self, candidate_id: UUID, *, as_of: datetime) -> CandidateRow | None:
        """Read the candidate within its inclusive-start, exclusive-end validity window."""
        if as_of.utcoffset() is None:
            raise ValueError("Candidate lookup time must include a timezone")
        select = SQL(
            "SELECT expires_at, payload, current_table, current_id, previous_table, previous_id, valid_from "
            "FROM {} WHERE id = %s AND valid_from <= %s AND expires_at > %s"
        ).format(self._table)
        with self._archive.transaction() as connection:
            found = cast(
                "tuple[datetime, dict[str, object], str, UUID, str, UUID, datetime] | None",
                connection.execute(select.as_string(), (candidate_id, as_of, as_of)).fetchone(),
            )
        if found is None:
            return None
        expires_at, payload, current_table, current_id, previous_table, previous_id, valid_from = found
        return CandidateRow(
            candidate_id,
            expires_at,
            payload,
            ArchiveRef(ArchiveTable(current_table), current_id),
            ArchiveRef(ArchiveTable(previous_table), previous_id),
            valid_from=valid_from,
        )
