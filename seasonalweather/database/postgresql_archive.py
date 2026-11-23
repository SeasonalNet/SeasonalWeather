"""Controller-owned PostgreSQL archive persistence, independent of SQLite runtime state."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from psycopg.sql import SQL, Identifier, Placeholder
from psycopg.types.json import Jsonb

from ..config import OptionalServiceNetworkConfig, PostgresArchiveConfig
from .postgresql import default_postgres_connector
from .postgresql_migrations import PostgresMigrationConnection, PostgresMigrationConnector, _identifier


class ArchiveOrigin(StrEnum):
    LIVE = "live"
    LOCAL_OUTBOX_REPLAY = "local_outbox_replay"
    UPSTREAM_GAP_RECOVERY = "upstream_gap_recovery"
    HISTORICAL_IMPORT = "historical_import"
    LEGACY_IMPORT = "legacy_import"


class ArchiveTable(StrEnum):
    IMPORT_ORIGINS = "import_origins"
    SOURCE_DELIVERIES = "source_deliveries"
    PRODUCT_VERSIONS = "product_versions"
    PRODUCT_DELIVERIES = "product_deliveries"
    FORECAST_SNAPSHOTS = "forecast_snapshots"
    OBSERVATION_SAMPLES = "observation_samples"
    ALERT_LIFECYCLE_VERSIONS = "alert_lifecycle_versions"
    BROADCAST_HISTORY = "broadcast_history"
    AIRTIME_HISTORY = "airtime_history"
    DERIVED_BROADCAST_FACTS = "derived_broadcast_facts"
    BACKFILL_CHECKPOINTS = "backfill_checkpoints"
    RECONCILIATION_ANOMALIES = "reconciliation_anomalies"


@dataclass(frozen=True, kw_only=True)
class ArchiveRecord:
    table: ArchiveTable
    id: UUID
    origin: ArchiveOrigin
    recorded_at: datetime
    payload: dict[str, object]
    retain_until: datetime | None = None
    payload_version: int = 1
    broadcastable: bool = False
    import_id: UUID | None = None
    product_id: UUID | None = None
    delivery_id: UUID | None = None
    broadcast_id: UUID | None = None

    def __post_init__(self) -> None:
        self._validate_timestamps()
        self._validate_relationships()
        if self.payload_version < 1:
            raise ValueError("Archive payload version must be positive")
        if self.broadcastable and self.origin in {ArchiveOrigin.HISTORICAL_IMPORT, ArchiveOrigin.LEGACY_IMPORT}:
            raise ValueError("Historical and legacy archive records cannot be broadcastable")

    def _validate_timestamps(self) -> None:
        if self.recorded_at.utcoffset() is None:
            raise ValueError("Archive timestamps must include a timezone")
        if self.retain_until is not None and (
            self.retain_until.utcoffset() is None or self.retain_until < self.recorded_at
        ):
            raise ValueError("Archive retention must be timezone-aware and not precede recording")

    def _validate_relationships(self) -> None:
        links = {key for key in ("product_id", "delivery_id", "broadcast_id") if getattr(self, key) is not None}
        if links != set(_LINK_COLUMNS.get(self.table, ())):
            raise ValueError("Archive record relationship fields do not match its table")
        if self.table is ArchiveTable.IMPORT_ORIGINS and self.import_id is not None:
            raise ValueError("An import origin cannot reference another import origin")


_LINK_COLUMNS = {
    ArchiveTable.PRODUCT_DELIVERIES: ("product_id", "delivery_id"),
    ArchiveTable.AIRTIME_HISTORY: ("broadcast_id",),
    ArchiveTable.DERIVED_BROADCAST_FACTS: ("broadcast_id",),
}
_BASE_COLUMNS = ("id", "origin", "recorded_at", "payload", "retain_until", "payload_version", "broadcastable")


def _columns(table: ArchiveTable) -> tuple[str, ...]:
    provenance = () if table is ArchiveTable.IMPORT_ORIGINS else ("import_id",)
    return _BASE_COLUMNS + provenance + _LINK_COLUMNS.get(table, ())


def _insert_values(record: ArchiveRecord, columns: tuple[str, ...]) -> tuple[object, ...]:
    special = {"payload": Jsonb(record.payload), "origin": record.origin.value}
    return tuple(special[key] if key in special else getattr(record, key) for key in columns)


class PostgresArchiveRepository:
    """Store related records atomically and read records by stable identifier."""

    def __init__(
        self,
        configuration: PostgresArchiveConfig,
        network: OptionalServiceNetworkConfig,
        *,
        connector: PostgresMigrationConnector | None = None,
    ) -> None:
        if not network.enabled:
            raise ValueError("PostgreSQL archive access is disabled by configuration")
        self._schema = _identifier(configuration.schema, "database.postgres.schema")
        self._network = network
        self._connector = connector or default_postgres_connector()

    def insert_many(self, records: tuple[ArchiveRecord, ...]) -> None:
        """Insert a parent-first batch, with no implicit overwrite or deduplication."""
        self._insert(records, replay=False)

    def replay_many(self, records: tuple[ArchiveRecord, ...]) -> None:
        """Commit stable outbox IDs, including retries after a lost acknowledgment."""
        if any(record.origin is not ArchiveOrigin.LOCAL_OUTBOX_REPLAY for record in records):
            raise ValueError("Only local outbox records can be replayed")
        self._insert(records, replay=True)

    def _insert(self, records: tuple[ArchiveRecord, ...], *, replay: bool) -> None:
        if not records:
            return
        with self.transaction() as connection:
            for record in records:
                self.insert_record(connection, record, replay=replay)

    def table_name(self, table: ArchiveTable) -> Identifier:
        return Identifier(self._schema, table.value)

    def insert_record(
        self, connection: PostgresMigrationConnection, record: ArchiveRecord, *, replay: bool = False
    ) -> None:
        columns = _columns(record.table)
        statement = SQL("INSERT INTO {} ({}) VALUES ({})").format(
            self.table_name(record.table),
            SQL(", ").join(map(Identifier, columns)),
            SQL(", ").join(Placeholder() for _ in columns),
        )
        if replay:
            statement += SQL(" ON CONFLICT (id) DO NOTHING")
        connection.execute(statement.as_string(), _insert_values(record, columns))

    @contextmanager
    def transaction(self) -> Iterator[PostgresMigrationConnection]:
        connection = self._connector.connect(self._network)
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, table: ArchiveTable, record_id: UUID) -> ArchiveRecord | None:
        columns = _columns(table)
        statement = SQL("SELECT {} FROM {} WHERE id = %s").format(
            SQL(", ").join(map(Identifier, columns)), Identifier(self._schema, table.value)
        )
        connection = self._connector.connect(self._network)
        try:
            row = connection.execute(statement.as_string(), (record_id,)).fetchone()
            if row is None:
                return None
            values = dict(zip(columns, row, strict=True))
            values["origin"] = ArchiveOrigin(values["origin"])
            return ArchiveRecord(table=table, **values)
        finally:
            connection.close()
