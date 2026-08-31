"""Controller-owned PostgreSQL migration execution and schema governance.

The migration runner is deliberately separate from PostgreSQL startup
preflight.  Preflight qualifies an optional dependency; this module performs
an explicitly requested maintenance operation against that dependency.  All
metadata and migration statements run in one transaction protected by a
transaction-scoped advisory lock, so a failed or interrupted run leaves the
last committed schema version authoritative.
"""

from __future__ import annotations

import hashlib
import logging
import re
import struct
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, cast

from ..config import OptionalServiceNetworkConfig, PostgresArchiveConfig
from .postgresql import default_postgres_connector

log = logging.getLogger("seasonalweather.database.postgresql_migrations")

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_MIGRATION_NAME = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_MAX_GUIDANCE = 512
_METADATA_COLUMNS = "version, migration_name, checksum, safety"


class MigrationOperationKind(StrEnum):
    """Governed operation categories used to classify migration safety."""

    CREATE_TABLE = "create_table"
    ADD_NULLABLE_COLUMN = "add_nullable_column"
    CREATE_VIEW = "create_view"
    CREATE_SAFE_INDEX = "create_safe_index"
    UPDATE_SCHEMA_METADATA = "update_schema_metadata"
    DESTRUCTIVE_COLUMN_REMOVAL = "destructive_column_removal"
    LARGE_TABLE_REWRITE = "large_table_rewrite"
    GEOMETRY_SRID_CHANGE = "geometry_srid_change"
    AMBIGUOUS_DEDUPLICATION = "ambiguous_deduplication"
    ARCHIVE_REBUILD = "archive_rebuild"
    LARGE_HISTORICAL_IMPORT = "large_historical_import"

    @property
    def automatic_safe(self) -> bool:
        return self in {
            self.CREATE_TABLE,
            self.ADD_NULLABLE_COLUMN,
            self.CREATE_VIEW,
            self.CREATE_SAFE_INDEX,
            self.UPDATE_SCHEMA_METADATA,
        }


class MigrationSafety(StrEnum):
    AUTOMATIC_SAFE = "automatic_safe"
    MANUAL_MAINTENANCE = "manual_maintenance"


_MANUAL_GUIDANCE: dict[MigrationOperationKind, str] = {
    MigrationOperationKind.DESTRUCTIVE_COLUMN_REMOVAL: (
        "review dependent readers and take a verified backup before removing data"
    ),
    MigrationOperationKind.LARGE_TABLE_REWRITE: (
        "schedule a maintenance window and confirm rewrite duration and free space"
    ),
    MigrationOperationKind.GEOMETRY_SRID_CHANGE: (
        "verify spatial transforms, dependent indexes, and a reversible backup"
    ),
    MigrationOperationKind.AMBIGUOUS_DEDUPLICATION: (
        "resolve conflicting identities explicitly; do not discard competing records"
    ),
    MigrationOperationKind.ARCHIVE_REBUILD: ("review archive rebuild scope, retention impact, and rollback evidence"),
    MigrationOperationKind.LARGE_HISTORICAL_IMPORT: (
        "bound the import, verify provenance, and run it as planned maintenance"
    ),
}


class PostgresMigrationError(RuntimeError):
    """Base error for bounded, operator-facing migration failures."""


class MigrationHistoryError(PostgresMigrationError):
    """The durable migration history is not compatible with this code."""


class MigrationExecutionError(PostgresMigrationError):
    """A migration failed and its transaction was rolled back."""

    def __init__(self, migration: PostgresMigration, cause: BaseException) -> None:
        self.version = migration.version
        self.migration_name = migration.name
        self.cause_type = type(cause).__name__
        super().__init__(
            f"PostgreSQL migration {migration.version} ({migration.name}) failed "
            f"with {self.cause_type}; the migration transaction was rolled back."
        )


class ManualMaintenanceRequired(PostgresMigrationError):
    """Pending work contains an operation that cannot run automatically."""

    def __init__(self, migrations: Sequence[PostgresMigration]) -> None:
        self.migrations = tuple(migrations)
        guidance = "; ".join(
            f"v{migration.version} {migration.name}: {migration.manual_guidance}" for migration in self.migrations
        )
        self.guidance = guidance[:_MAX_GUIDANCE]
        super().__init__(f"PostgreSQL migration requires explicit manual maintenance approval: {self.guidance}")


class PostgresMigrationConnection(Protocol):
    def execute(self, operation: str, parameters: Sequence[object] | None = None) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


class PostgresMigrationConnector(Protocol):
    def connect(self, configuration: OptionalServiceNetworkConfig) -> PostgresMigrationConnection: ...


@dataclass(frozen=True)
class MigrationOperation:
    """One checked-in SQL operation with an explicit safety classification."""

    kind: MigrationOperationKind
    sql: str
    guidance: str = ""

    def __post_init__(self) -> None:
        sql = self.sql.strip()
        if not sql:
            raise ValueError("PostgreSQL migration SQL cannot be empty")
        if len(sql) > 1_000_000:
            raise ValueError("PostgreSQL migration SQL is too large")
        guidance = self.guidance.strip()
        if self.kind.automatic_safe and guidance:
            raise ValueError("automatic-safe PostgreSQL operations cannot require manual guidance")
        if not self.kind.automatic_safe and not guidance:
            guidance = _MANUAL_GUIDANCE[self.kind]
            object.__setattr__(self, "guidance", guidance)
        if len(guidance) > _MAX_GUIDANCE:
            raise ValueError("PostgreSQL migration guidance is too long")
        object.__setattr__(self, "sql", sql)

    @property
    def safety(self) -> MigrationSafety:
        return MigrationSafety.AUTOMATIC_SAFE if self.kind.automatic_safe else MigrationSafety.MANUAL_MAINTENANCE


@dataclass(frozen=True)
class PostgresMigration:
    """A monotonically versioned PostgreSQL migration definition."""

    version: int
    name: str
    operations: tuple[MigrationOperation, ...]

    def __post_init__(self) -> None:
        if self.version <= 0:
            raise ValueError("PostgreSQL migration version must be positive")
        if _MIGRATION_NAME.fullmatch(self.name) is None:
            raise ValueError("PostgreSQL migration name is malformed")
        operations = tuple(self.operations)
        if not operations:
            raise ValueError("PostgreSQL migration must contain at least one operation")
        object.__setattr__(self, "operations", operations)

    @property
    def safety(self) -> MigrationSafety:
        if any(operation.safety is MigrationSafety.MANUAL_MAINTENANCE for operation in self.operations):
            return MigrationSafety.MANUAL_MAINTENANCE
        return MigrationSafety.AUTOMATIC_SAFE

    @property
    def manual_guidance(self) -> str:
        guidance = "; ".join(operation.guidance for operation in self.operations if operation.guidance)
        return guidance[:_MAX_GUIDANCE]

    @property
    def checksum(self) -> str:
        digest = hashlib.sha256(usedforsecurity=False)
        digest.update(f"{self.version}\0{self.name}\0{self.safety.value}\0".encode())
        for operation in self.operations:
            digest.update(operation.kind.value.encode("utf-8"))
            digest.update(b"\0")
            digest.update(operation.sql.encode("utf-8"))
            digest.update(b"\0")
        return digest.hexdigest()


@dataclass(frozen=True)
class MigrationResult:
    state: str
    current_version: int
    applied_versions: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.current_version < 0:
            raise ValueError("PostgreSQL migration result is malformed")

    @property
    def changed(self) -> bool:
        return bool(self.applied_versions)

    def snapshot(self) -> dict[str, object]:
        return {
            "state": self.state,
            "current_version": self.current_version,
            "applied_versions": list(self.applied_versions),
        }


POSTGRES_MIGRATIONS: tuple[PostgresMigration, ...] = ()


def _identifier(value: str, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if _IDENTIFIER.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be a lowercase PostgreSQL identifier")
    return normalized


def _qualified(schema: str, relation: str) -> str:
    return f'"{schema}"."{relation}"'


def advisory_lock_key(schema: str, migration_table: str) -> int:
    """Return the stable signed bigint used by the transaction lock."""

    material = f"seasonalweather:postgres-migrations:{schema}:{migration_table}".encode()
    return cast(int, struct.unpack(">q", hashlib.sha256(material, usedforsecurity=False).digest()[:8])[0])


def validate_migrations(migrations: Iterable[PostgresMigration]) -> tuple[PostgresMigration, ...]:
    """Validate and normalize the checked-in registry in version order."""

    normalized = tuple(migrations)
    versions = [migration.version for migration in normalized]
    if versions != sorted(set(versions)):
        raise ValueError("PostgreSQL migrations must have unique increasing versions")
    return normalized


def _fetchall(cursor: Any) -> list[tuple[Any, ...]]:
    rows = cursor.fetchall()
    return [tuple(row) for row in rows]


def _metadata_select(metadata_table: str) -> str:
    return " ".join(("SELECT", _METADATA_COLUMNS, "FROM", metadata_table, "ORDER BY version"))


def _metadata_insert(metadata_table: str) -> str:
    return " ".join(
        (
            "INSERT INTO",
            metadata_table,
            "(" + _METADATA_COLUMNS + ")",
            "VALUES (%s, %s, %s, %s)",
        )
    )


def _metadata_ddl(metadata_table: str) -> str:
    return f"""
        CREATE TABLE IF NOT EXISTS {metadata_table} (
            version INTEGER PRIMARY KEY,
            migration_name TEXT NOT NULL,
            checksum CHAR(64) NOT NULL,
            safety TEXT NOT NULL CHECK (safety IN ('automatic_safe', 'manual_maintenance')),
            applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """


class PostgresMigrationRunner:
    """Apply explicitly supplied PostgreSQL migrations under one safe boundary."""

    def __init__(
        self,
        *,
        configuration: PostgresArchiveConfig,
        network: OptionalServiceNetworkConfig,
        migrations: Iterable[PostgresMigration] = POSTGRES_MIGRATIONS,
        connector: PostgresMigrationConnector | None = None,
    ) -> None:
        self.configuration = configuration
        self.network = network
        self.migrations = validate_migrations(migrations)
        self.connector = connector or default_postgres_connector()

    def apply_pending(self, *, allow_manual: bool = False) -> MigrationResult:
        """Apply pending migrations, refusing manual work without approval."""

        if not self.network.enabled:
            raise PostgresMigrationError("PostgreSQL archive migrations are disabled by configuration")
        schema = _identifier(self.configuration.schema, "database.postgres.schema")
        migration_table = _identifier(self.configuration.migration_table, "database.postgres.migration_table")
        metadata_table = _qualified(schema, migration_table)
        try:
            connection = self.connector.connect(self.network)
        except Exception as exc:
            raise PostgresMigrationError(f"PostgreSQL migration connection failed with {type(exc).__name__}.") from exc
        try:
            return self._run_transaction(
                connection,
                schema=schema,
                migration_table=migration_table,
                metadata_table=metadata_table,
                allow_manual=allow_manual,
            )
        except ManualMaintenanceRequired:
            self._rollback_safely(connection)
            raise
        except MigrationHistoryError:
            self._rollback_safely(connection)
            raise
        except BaseException as exc:
            self._rollback_safely(connection)
            if isinstance(exc, MigrationExecutionError):
                raise
            raise PostgresMigrationError(
                f"PostgreSQL migration run failed with {type(exc).__name__}; all changes were rolled back."
            ) from exc
        finally:
            try:
                connection.close()
            except Exception:
                log.debug("PostgreSQL migration connection close failed", exc_info=True)

    def _run_transaction(
        self,
        connection: PostgresMigrationConnection,
        *,
        schema: str,
        migration_table: str,
        metadata_table: str,
        allow_manual: bool,
    ) -> MigrationResult:
        connection.execute("BEGIN")
        connection.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (advisory_lock_key(schema, migration_table),),
        )
        connection.execute(_metadata_ddl(metadata_table))
        rows = _fetchall(connection.execute(_metadata_select(metadata_table)))
        applied = self._validate_history(rows)
        pending = tuple(migration for migration in self.migrations if migration.version not in applied)
        if not pending:
            connection.commit()
            return MigrationResult("up_to_date", max(applied, default=0))
        self._require_manual_approval(pending, allow_manual)
        for migration in pending:
            self._apply_one(connection, metadata_table, migration)
        connection.commit()
        return MigrationResult("applied", pending[-1].version, tuple(migration.version for migration in pending))

    @staticmethod
    def _require_manual_approval(
        pending: Sequence[PostgresMigration],
        allow_manual: bool,
    ) -> None:
        manual = tuple(migration for migration in pending if migration.safety is MigrationSafety.MANUAL_MAINTENANCE)
        if manual and not allow_manual:
            raise ManualMaintenanceRequired(manual)

    def _validate_history(self, rows: Sequence[tuple[Any, ...]]) -> dict[int, PostgresMigration]:
        known = {migration.version: migration for migration in self.migrations}
        applied: dict[int, PostgresMigration] = {}
        for row in rows:
            version, migration = self._validate_history_row(row, known)
            applied[version] = migration
        self._validate_history_versions(applied)
        return applied

    @staticmethod
    def _validate_history_row(
        row: tuple[Any, ...],
        known: dict[int, PostgresMigration],
    ) -> tuple[int, PostgresMigration]:
        if len(row) < 4:
            raise MigrationHistoryError("PostgreSQL migration metadata row is incomplete")
        try:
            version = int(row[0])
        except (TypeError, ValueError) as exc:
            raise MigrationHistoryError("PostgreSQL migration metadata version is invalid") from exc
        migration = known.get(version)
        if migration is None:
            raise MigrationHistoryError(f"PostgreSQL migration version {version} is unknown to this build")
        if str(row[1]) != migration.name or str(row[2]) != migration.checksum or str(row[3]) != migration.safety.value:
            raise MigrationHistoryError(f"PostgreSQL migration version {version} does not match its recorded checksum")
        return version, migration

    @staticmethod
    def _validate_history_versions(applied: dict[int, PostgresMigration]) -> None:
        expected = list(range(1, max(applied, default=0) + 1))
        if sorted(applied) != expected:
            raise MigrationHistoryError("PostgreSQL migration metadata contains a version gap")

    @staticmethod
    def _apply_one(
        connection: PostgresMigrationConnection,
        metadata_table: str,
        migration: PostgresMigration,
    ) -> None:
        try:
            for operation in migration.operations:
                connection.execute(operation.sql)
            connection.execute(
                _metadata_insert(metadata_table),
                (migration.version, migration.name, migration.checksum, migration.safety.value),
            )
        except BaseException as exc:
            raise MigrationExecutionError(migration, exc) from exc

    @staticmethod
    def _rollback_safely(connection: PostgresMigrationConnection) -> None:
        try:
            connection.rollback()
        except Exception:
            log.debug("PostgreSQL migration rollback failed", exc_info=True)


__all__ = [
    "MigrationExecutionError",
    "MigrationHistoryError",
    "MigrationOperation",
    "MigrationOperationKind",
    "MigrationSafety",
    "ManualMaintenanceRequired",
    "MigrationResult",
    "POSTGRES_MIGRATIONS",
    "PostgresMigration",
    "PostgresMigrationConnection",
    "PostgresMigrationConnector",
    "PostgresMigrationError",
    "PostgresMigrationRunner",
    "advisory_lock_key",
    "validate_migrations",
]
