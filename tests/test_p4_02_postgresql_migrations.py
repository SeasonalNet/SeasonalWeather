from __future__ import annotations

import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any

import pytest

from seasonalweather.config import OptionalServiceNetworkConfig, PostgresArchiveConfig
from seasonalweather.database.postgresql_migrations import (
    ManualMaintenanceRequired,
    MigrationHistoryError,
    MigrationOperation,
    MigrationOperationKind,
    MigrationResult,
    MigrationSafety,
    PostgresMigration,
    PostgresMigrationError,
    PostgresMigrationRunner,
    advisory_lock_key,
    validate_migrations,
)


class _Cursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


@dataclass
class _Stored:
    version: int
    name: str
    checksum: str
    safety: str


class _Database:
    def __init__(self) -> None:
        self.metadata: list[_Stored] = []
        self.tables: set[str] = set()
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.fail_sql: str | None = None


class _Connection:
    def __init__(self, database: _Database, *, lock_delay: float = 0.0) -> None:
        self.database = database
        self.lock_delay = lock_delay
        self.operations: list[str] = []
        self.transaction_metadata: list[_Stored] = []
        self.transaction_tables: set[str] = set()
        self.in_transaction = False
        self.closed = False
        self._held = False

    def execute(self, operation: str, parameters: Sequence[object] | None = None) -> _Cursor:
        self.operations.append(operation)
        if operation == "BEGIN":
            self.in_transaction = True
            self.transaction_metadata = list(self.database.metadata)
            self.transaction_tables = set(self.database.tables)
            return _Cursor([])
        if "pg_advisory_xact_lock" in operation:
            self.database.lock.acquire()
            self._held = True
            self.database.active += 1
            self.database.max_active = max(self.database.max_active, self.database.active)
            if self.lock_delay:
                time.sleep(self.lock_delay)
            return _Cursor([])
        if operation.lstrip().startswith("CREATE TABLE IF NOT EXISTS"):
            self.transaction_tables.add("metadata")
            return _Cursor([])
        if operation.lstrip().startswith("SELECT version, migration_name"):
            # PostgreSQL's default READ COMMITTED isolation observes the
            # committed history after the transaction-scoped lock is held.
            self.transaction_metadata = list(self.database.metadata)
            return _Cursor(
                [(item.version, item.name, item.checksum, item.safety) for item in self.transaction_metadata]
            )
        if operation.lstrip().startswith("INSERT INTO"):
            if parameters is None:
                raise AssertionError("metadata insert must be parameterized")
            version, name, checksum, safety = parameters
            if not isinstance(version, int):
                raise AssertionError("metadata version must be an integer")
            self.transaction_metadata.append(_Stored(version, str(name), str(checksum), str(safety)))
            return _Cursor([])
        if self.database.fail_sql and self.database.fail_sql in operation:
            raise RuntimeError("simulated migration interruption")
        return _Cursor([])

    def commit(self) -> None:
        self.database.metadata = list(self.transaction_metadata)
        self.database.tables = set(self.transaction_tables)
        self._release_lock()
        self.in_transaction = False

    def rollback(self) -> None:
        self._release_lock()
        self.in_transaction = False

    def close(self) -> None:
        self._release_lock()
        self.closed = True

    def _release_lock(self) -> None:
        if self._held:
            self.database.active -= 1
            self._held = False
            self.database.lock.release()


class _Connector:
    def __init__(self, database: _Database, *, lock_delay: float = 0.0) -> None:
        self.database = database
        self.lock_delay = lock_delay

    def connect(self, configuration: OptionalServiceNetworkConfig) -> _Connection:
        del configuration
        return _Connection(self.database, lock_delay=self.lock_delay)


def _config() -> tuple[PostgresArchiveConfig, OptionalServiceNetworkConfig]:
    return PostgresArchiveConfig(), OptionalServiceNetworkConfig(
        enabled=True,
        address="postgres",
        port=5432,
        database="seasonalweather",
    )


def _safe(version: int, name: str, sql: str) -> PostgresMigration:
    return PostgresMigration(
        version,
        name,
        (MigrationOperation(MigrationOperationKind.CREATE_TABLE, sql),),
    )


def _runner(
    database: _Database,
    migrations: Iterable[PostgresMigration],
    *,
    lock_delay: float = 0.0,
    configuration: PostgresArchiveConfig | None = None,
) -> PostgresMigrationRunner:
    archive, network = _config()
    return PostgresMigrationRunner(
        configuration=configuration or archive,
        network=network,
        migrations=migrations,
        connector=_Connector(database, lock_delay=lock_delay),
    )


def test_safe_migrations_are_transactional_and_repeatable() -> None:
    database = _Database()
    migrations = (_safe(1, "create_archive_probe", "CREATE TABLE archive_probe (id integer)"),)

    first = _runner(database, migrations).apply_pending()
    second = _runner(database, migrations).apply_pending()

    assert first.snapshot() == {"state": "applied", "current_version": 1, "applied_versions": [1]}
    assert second.snapshot() == {"state": "up_to_date", "current_version": 1, "applied_versions": []}
    assert [item.version for item in database.metadata] == [1]


def test_advisory_lock_serializes_concurrent_attempts() -> None:
    database = _Database()
    migration = _safe(1, "create_archive_probe", "CREATE TABLE archive_probe (id integer)")
    runner = _runner(database, (migration,), lock_delay=0.02)
    results: list[MigrationResult] = []

    def apply() -> None:
        results.append(runner.apply_pending())

    threads = [threading.Thread(target=apply) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(result.state for result in results) == ["applied", "up_to_date"]
    assert database.max_active == 1


def test_interrupted_migration_rolls_back_and_can_be_retried() -> None:
    database = _Database()
    migration = _safe(1, "create_archive_probe", "CREATE TABLE archive_probe (id integer)")
    database.fail_sql = "archive_probe"

    with pytest.raises(PostgresMigrationError, match="rolled back"):
        _runner(database, (migration,)).apply_pending()
    assert database.metadata == []

    database.fail_sql = None
    result = _runner(database, (migration,)).apply_pending()
    assert result.current_version == 1
    assert [item.version for item in database.metadata] == [1]


def test_manual_operations_refuse_automatic_execution_with_guidance() -> None:
    database = _Database()
    migration = PostgresMigration(
        1,
        "remove_legacy_column",
        (
            MigrationOperation(
                MigrationOperationKind.DESTRUCTIVE_COLUMN_REMOVAL,
                "ALTER TABLE archive DROP COLUMN legacy_value",
            ),
        ),
    )

    assert migration.safety is MigrationSafety.MANUAL_MAINTENANCE
    with pytest.raises(ManualMaintenanceRequired, match="verified backup") as error:
        _runner(database, (migration,)).apply_pending()
    assert error.value.guidance
    assert database.metadata == []

    result = _runner(database, (migration,)).apply_pending(allow_manual=True)
    assert result.applied_versions == (1,)


def test_history_checksum_drift_fails_closed() -> None:
    database = _Database()
    migration = _safe(1, "create_archive_probe", "CREATE TABLE archive_probe (id integer)")
    database.metadata = [_Stored(1, migration.name, "0" * 64, migration.safety.value)]

    with pytest.raises(MigrationHistoryError, match="checksum"):
        _runner(database, (migration,)).apply_pending()


def test_registry_requires_increasing_unique_versions() -> None:
    archive, network = _config()
    with pytest.raises(ValueError, match="unique increasing"):
        PostgresMigrationRunner(
            configuration=archive,
            network=network,
            migrations=(_safe(2, "second", "SELECT 2"), _safe(1, "first", "SELECT 1")),
            connector=_Connector(_Database()),
        )


def test_migration_definitions_have_no_arbitrary_count_ceiling() -> None:
    operations = tuple(
        MigrationOperation(MigrationOperationKind.CREATE_VIEW, f"CREATE VIEW archive_view_{index} AS SELECT 1")
        for index in range(257)
    )
    migration = PostgresMigration(1, "many_operations", operations)
    migrations = tuple(_safe(index, f"migration_{index}", "SELECT 1") for index in range(1, 258))

    assert len(migration.operations) == 257
    assert len(validate_migrations(migrations)) == 257


def test_advisory_key_is_stable_signed_bigint() -> None:
    key = advisory_lock_key("public", "seasonalweather_schema_metadata")
    assert -(2**63) <= key < 2**63
    assert key == advisory_lock_key("public", "seasonalweather_schema_metadata")


def test_disabled_archive_refuses_migration_without_connecting() -> None:
    database = _Database()
    archive, network = _config()
    network = replace(network, enabled=False)

    runner = PostgresMigrationRunner(
        configuration=archive,
        network=network,
        migrations=(_safe(1, "create_archive_probe", "CREATE TABLE archive_probe (id integer)"),),
        connector=_Connector(database),
    )

    with pytest.raises(PostgresMigrationError, match="disabled"):
        runner.apply_pending()
    assert database.metadata == []
