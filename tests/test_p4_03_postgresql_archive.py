"""Archive repository and migration contracts without a live service."""

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Self
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from seasonalweather.config import OptionalServiceNetworkConfig, PostgresArchiveConfig
from seasonalweather.database.postgresql_archive import (
    ArchiveOrigin,
    ArchiveRecord,
    ArchiveTable,
    PostgresArchiveRepository,
)
from seasonalweather.database.postgresql_migrations import POSTGRES_MIGRATIONS, PostgresMigrationRunner


class _Connection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...] | None]] = []
        self.events: list[str] = []
        self.row: tuple[object, ...] | None = None
        self.fail_insert = False

    def execute(self, operation: str, parameters: Sequence[object] | None = None) -> SimpleNamespace:
        self.calls.append((operation, None if parameters is None else tuple(parameters)))
        if self.fail_insert and operation.startswith('INSERT INTO "archive_test"."source_deliveries"'):
            raise RuntimeError("insert failed")
        if operation == "SELECT pg_catalog.current_setting('search_path')":
            return SimpleNamespace(fetchall=lambda: [("public",)])
        return SimpleNamespace(fetchone=lambda: self.row, fetchall=list)

    def commit(self) -> None:
        self.events.append("commit")

    def rollback(self) -> None:
        self.events.append("rollback")

    def close(self) -> None:
        self.events.append("close")

    def connect(self, configuration: OptionalServiceNetworkConfig) -> Self:
        assert configuration.enabled
        return self


def _record() -> ArchiveRecord:
    return ArchiveRecord(
        table=ArchiveTable.SOURCE_DELIVERIES,
        id=uuid4(),
        origin=ArchiveOrigin.LIVE,
        recorded_at=datetime.now(UTC),
        payload={"source": "NWWS"},
    )


_NET = OptionalServiceNetworkConfig(enabled=True, address="postgres", port=5432, database="weather")
_CONFIG = PostgresArchiveConfig(schema="archive_test")


def test_migration_registers_every_archive_table_in_configured_schema() -> None:
    connection = _Connection()
    result = PostgresMigrationRunner(configuration=_CONFIG, network=_NET, connector=connection).apply_pending()
    assert result.applied_versions == (1, 2)
    ddl = {sql.split()[2]: sql for sql, _ in connection.calls if sql.startswith("CREATE TABLE ")}
    assert set(table.value for table in ArchiveTable) <= ddl.keys()
    for table in ArchiveTable:
        assert "origin text NOT NULL" in ddl[table]
        assert "broadcastable boolean NOT NULL DEFAULT false" in ddl[table]
        assert all(origin.value in ddl[table] for origin in ArchiveOrigin)
    assert "REFERENCES product_versions(id)" in ddl[ArchiveTable.PRODUCT_DELIVERIES]
    assert "REFERENCES source_deliveries(id)" in ddl[ArchiveTable.PRODUCT_DELIVERIES]
    assert ("SELECT pg_catalog.set_config('search_path', %s, true)", ('"archive_test", pg_temp',)) in connection.calls
    assert "CREATE TABLE geographic_dataset_versions" in ddl["geographic_dataset_versions"]
    assert "geometry(Geometry)" in ddl["geographic_features"]
    assert len(POSTGRES_MIGRATIONS[0].checksum) == 64
    assert len(POSTGRES_MIGRATIONS[1].checksum) == 64
    assert connection.events == ["commit", "close"]


@pytest.mark.parametrize("origin", [ArchiveOrigin.HISTORICAL_IMPORT, ArchiveOrigin.LEGACY_IMPORT])
def test_imports_cannot_be_broadcastable(origin: ArchiveOrigin) -> None:
    record = replace(_record(), origin=origin)
    assert not record.broadcastable
    with pytest.raises(ValueError, match="cannot be broadcastable"):
        _ = replace(record, broadcastable=True)


def test_retention_version_and_relationship_validation() -> None:
    record = _record()
    with pytest.raises(ValueError, match="retention"):
        _ = replace(record, retain_until=record.recorded_at - timedelta(seconds=1))
    with pytest.raises(ValueError, match="version"):
        _ = replace(record, payload_version=0)
    with pytest.raises(ValueError, match="relationship"):
        _ = replace(record, table=ArchiveTable.PRODUCT_DELIVERIES)
    assert replace(record, table=ArchiveTable.PRODUCT_DELIVERIES, product_id=uuid4(), delivery_id=uuid4())


def test_repository_round_trip_preserves_identity_and_compatibility_metadata() -> None:
    connection = _Connection()
    repository = PostgresArchiveRepository(_CONFIG, _NET, connector=connection)
    record = replace(_record(), payload_version=2, retain_until=datetime.now(UTC) + timedelta(days=1))
    repository.insert_many((record,))
    statement, values = connection.calls[0]
    assert 'INSERT INTO "archive_test"."source_deliveries"' in statement
    assert "NWWS" not in statement
    assert values is not None and isinstance(values[3], Jsonb)
    connection.row = (*values[:3], record.payload, *values[4:])
    assert repository.get(record.table, record.id) == record
    assert connection.calls[-1][1] == (record.id,)
    assert connection.events == ["commit", "close", "close"]


def test_repository_rolls_back_entire_batch_on_failure() -> None:
    connection = _Connection()
    connection.fail_insert = True
    repository = PostgresArchiveRepository(_CONFIG, _NET, connector=connection)
    record = _record()
    with pytest.raises(RuntimeError, match="insert failed"):
        repository.insert_many((replace(record, table=ArchiveTable.IMPORT_ORIGINS), record))
    assert len(connection.calls) == 2
    assert connection.events == ["rollback", "close"]
