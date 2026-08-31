"""Optional PostgreSQL archive startup preflight.

This module owns only the connection and startup qualification boundary.  It
does not create archive schema, run migrations, or write operational state.
The PostgreSQL client is imported lazily so the disabled default remains
entirely local and does not require a database driver at import time.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, cast

from ..config import OptionalServiceNetworkConfig, PostgresArchiveConfig

log = logging.getLogger("seasonalweather.database.postgresql")

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_MAX_CHECKS = 16
_MAX_SUMMARY = 256
_MAX_PRECHECK_SECONDS = 30.0


class PostgresPreflightState(StrEnum):
    DISABLED = "disabled"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class PostgresCheck:
    name: str
    passed: bool
    summary: str

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.name) or len(self.summary) > _MAX_SUMMARY:
            raise ValueError("PostgreSQL preflight check is malformed")


@dataclass(frozen=True)
class PostgresPreflightResult:
    state: PostgresPreflightState
    checks: tuple[PostgresCheck, ...] = ()
    elapsed_milliseconds: int = 0
    server_version: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "checks", tuple(self.checks[:_MAX_CHECKS]))
        if self.elapsed_milliseconds < 0:
            raise ValueError("PostgreSQL preflight duration cannot be negative")
        if self.server_version is not None and self.server_version <= 0:
            raise ValueError("PostgreSQL server version must be positive")

    @property
    def failures(self) -> tuple[PostgresCheck, ...]:
        return tuple(check for check in self.checks if not check.passed)

    def snapshot(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "reason": (
                "disabled_by_configuration"
                if self.state is PostgresPreflightState.DISABLED
                else "preflight_passed"
                if self.state is PostgresPreflightState.AVAILABLE
                else "preflight_not_run"
                if not self.checks
                else "preflight_failed"
            ),
            "checks": len(self.checks),
            "failed_checks": len(self.failures),
            "elapsed_milliseconds": self.elapsed_milliseconds,
            "server_version": self.server_version or 0,
        }


class PostgresConnection(Protocol):
    def execute(self, operation: str, parameters: Sequence[object] | None = None) -> Any: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


class PostgresConnector(Protocol):
    def connect(self, configuration: OptionalServiceNetworkConfig) -> PostgresConnection: ...


class PostgresDependencyUnavailable(RuntimeError):
    """The optional PostgreSQL client is not installed in this image."""


class _PsycopgConnector:
    def connect(self, configuration: OptionalServiceNetworkConfig) -> PostgresConnection:
        try:
            import psycopg
        except ImportError as exc:
            raise PostgresDependencyUnavailable("PostgreSQL client is unavailable") from exc

        connect_timeout = max(1, min(30, math.ceil(configuration.connect_timeout_seconds)))
        kwargs: dict[str, Any] = {
            "host": configuration.address,
            "port": configuration.port,
            "dbname": configuration.database,
            "connect_timeout": connect_timeout,
            "sslmode": "require" if configuration.tls else "disable",
        }
        # Authentication is deliberately delegated to libpq's deployment
        # credentials (.pgpass, service configuration, or PG* environment).
        # No password or DSN is admitted into SeasonalWeather configuration.
        return cast(PostgresConnection, psycopg.connect(**kwargs))


def _row(connection: PostgresConnection, operation: str, parameters: Sequence[object] | None = None) -> tuple[Any, ...]:
    cursor = connection.execute(operation, parameters)
    fetched = cursor.fetchone()
    if fetched is None:
        return ()
    return tuple(fetched)


def _safe_summary(exception: BaseException) -> str:
    return f"PostgreSQL check failed ({type(exception).__name__})."[:_MAX_SUMMARY]


def _check(name: str, callback: Callable[[], str]) -> PostgresCheck:
    try:
        return PostgresCheck(name, True, callback())
    except Exception as exc:
        return PostgresCheck(name, False, _safe_summary(exc))


def _identifier(value: str, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if _IDENTIFIER.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be a lowercase PostgreSQL identifier")
    return normalized


def _server_version(connection: PostgresConnection) -> tuple[int, str]:
    row = _row(connection, "SELECT current_setting('server_version_num')")
    if not row:
        raise RuntimeError("server version was not returned")
    value = int(str(row[0]))
    if value <= 0:
        raise RuntimeError("server version was invalid")
    return value, "PostgreSQL server version is supported."


def _authentication_check(connection: PostgresConnection) -> str:
    row = _row(connection, "SELECT current_user")
    if not row or not str(row[0]).strip():
        raise RuntimeError("authenticated PostgreSQL user was not returned")
    return "PostgreSQL authentication succeeded."


def _tls_check(connection: PostgresConnection, required: bool) -> str:
    info = getattr(connection, "info", None)
    observed = getattr(info, "ssl_in_use", None)
    if not isinstance(observed, bool):
        observed = getattr(connection, "ssl_in_use", None)
    if not isinstance(observed, bool):
        raise RuntimeError("TLS state was not exposed by the client")
    if observed is not required:
        raise RuntimeError("TLS state does not match configured policy")
    return "TLS state matches configured policy."


def _identity_check(
    connection: PostgresConnection,
    configuration: OptionalServiceNetworkConfig,
    expected_schema: str,
) -> str:
    row = _row(connection, "SELECT current_database(), current_user, current_schema()")
    if len(row) < 3:
        raise RuntimeError("database identity was incomplete")
    if str(row[0]) != configuration.database:
        raise RuntimeError("connected database identity does not match configuration")
    if str(row[2]) != expected_schema:
        raise RuntimeError("connected schema identity does not match configuration")
    return "Database and schema identity match configuration."


def _ownership_check(connection: PostgresConnection, schema: str) -> str:
    row = _row(
        connection,
        """SELECT pg_get_userbyid(n.nspowner), current_user
           FROM pg_namespace AS n WHERE n.nspname = %s""",
        (schema,),
    )
    if len(row) < 2 or str(row[0]) != str(row[1]):
        raise RuntimeError("configured schema is not owned by the authenticated user")
    return "Configured schema ownership is valid."


def _privileges_check(connection: PostgresConnection, schema: str) -> str:
    row = _row(
        connection,
        """SELECT has_database_privilege(current_user, current_database(), 'CONNECT'),
                          has_schema_privilege(current_user, %s, 'USAGE'),
                          has_schema_privilege(current_user, %s, 'CREATE')""",
        (schema, schema),
    )
    if len(row) < 3 or not all(bool(value) for value in row[:3]):
        raise RuntimeError("required database or schema privileges are unavailable")
    return "Required database and schema privileges are available."


def _migration_check(connection: PostgresConnection, schema: str, migration_table: str) -> str:
    row = _row(
        connection,
        "SELECT to_regclass(%s) IS NOT NULL",
        (f"{schema}.{migration_table}",),
    )
    if not row or not bool(row[0]):
        raise RuntimeError("migration metadata is unavailable")
    return "Migration metadata is present."


def _extension_check(connection: PostgresConnection) -> str:
    row = _row(
        connection,
        "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'postgis')",
    )
    if not row or not bool(row[0]):
        raise RuntimeError("PostGIS extension is unavailable")
    return "PostGIS extension is installed."


def _transaction_check(connection: PostgresConnection) -> str:
    connection.rollback()
    try:
        connection.execute("CREATE TEMP TABLE seasonalweather_preflight_probe (value integer)")
        connection.execute("INSERT INTO seasonalweather_preflight_probe (value) VALUES (1)")
        row = _row(connection, "SELECT value FROM seasonalweather_preflight_probe")
        if not row or int(row[0]) != 1:
            raise RuntimeError("transactional read did not return the written value")
        return "Transactional read/write behavior passed and will be rolled back."
    finally:
        connection.rollback()


def _clock_check(
    connection: PostgresConnection,
    *,
    clock: Callable[[], dt.datetime],
    maximum_skew_seconds: float,
) -> str:
    row = _row(connection, "SELECT EXTRACT(EPOCH FROM clock_timestamp())")
    if not row:
        raise RuntimeError("server clock was not returned")
    server_epoch = float(row[0])
    local_epoch = clock().timestamp()
    if abs(server_epoch - local_epoch) > maximum_skew_seconds:
        raise RuntimeError("server clock divergence exceeds configured bound")
    return "Server clock divergence is within the configured bound."


def _spatial_reference_check(connection: PostgresConnection) -> str:
    row = _row(
        connection,
        "SELECT EXISTS (SELECT 1 FROM public.spatial_ref_sys WHERE srid = 4326)",
    )
    if not row or not bool(row[0]):
        raise RuntimeError("required SRID 4326 is unavailable")
    return "Required spatial reference data is available."


def _run_sync(
    configuration: OptionalServiceNetworkConfig,
    archive: PostgresArchiveConfig,
    connector: PostgresConnector,
    clock: Callable[[], dt.datetime],
) -> PostgresPreflightResult:
    started = time.monotonic()
    schema = _identifier(archive.schema, "database.postgres.schema")
    migration_table = _identifier(archive.migration_table, "database.postgres.migration_table")
    connection: PostgresConnection | None = None
    try:
        connection = connector.connect(configuration)
    except Exception as exc:
        return PostgresPreflightResult(
            PostgresPreflightState.UNAVAILABLE,
            (PostgresCheck("connectivity", False, _safe_summary(exc)),),
            elapsed_milliseconds=max(0, int((time.monotonic() - started) * 1000)),
        )

    checks: list[PostgresCheck] = []
    version: int | None = None
    try:
        checks.append(PostgresCheck("connectivity", True, "PostgreSQL connection succeeded."))
        checks.append(_check("tls_policy", lambda: _tls_check(connection, configuration.tls)))
        checks.append(_check("authentication", lambda: _authentication_check(connection)))

        def version_check() -> str:
            nonlocal version
            version, summary = _server_version(connection)
            return summary

        checks.append(_check("version", version_check))
        checks.append(_check("database_identity", lambda: _identity_check(connection, configuration, schema)))
        checks.append(_check("schema_ownership", lambda: _ownership_check(connection, schema)))
        checks.append(_check("privileges", lambda: _privileges_check(connection, schema)))
        checks.append(_check("migration_state", lambda: _migration_check(connection, schema, migration_table)))
        checks.append(_check("extension_state", lambda: _extension_check(connection)))
        checks.append(_check("transactional_read_write", lambda: _transaction_check(connection)))
        checks.append(
            _check(
                "clock_divergence",
                lambda: _clock_check(
                    connection,
                    clock=clock,
                    maximum_skew_seconds=archive.clock_skew_seconds,
                ),
            )
        )
        checks.append(_check("spatial_reference", lambda: _spatial_reference_check(connection)))
    finally:
        try:
            connection.close()
        except Exception:
            log.debug("PostgreSQL preflight connection close failed", exc_info=True)
    state = (
        PostgresPreflightState.AVAILABLE
        if all(check.passed for check in checks)
        else PostgresPreflightState.UNAVAILABLE
    )
    return PostgresPreflightResult(
        state,
        tuple(checks),
        elapsed_milliseconds=max(0, int((time.monotonic() - started) * 1000)),
        server_version=version,
    )


class PostgresPreflight:
    """Run one bounded startup qualification for the optional archive."""

    def __init__(
        self,
        *,
        configuration: PostgresArchiveConfig,
        network: OptionalServiceNetworkConfig,
        connector: PostgresConnector | None = None,
        clock: Callable[[], dt.datetime] | None = None,
        diagnostic_sink: Any = None,
    ) -> None:
        self.configuration = configuration
        self.network = network
        self.connector = connector or _PsycopgConnector()
        self.clock = clock or (lambda: dt.datetime.now(dt.UTC))
        self._diagnostic_sink = diagnostic_sink
        self.result = PostgresPreflightResult(
            PostgresPreflightState.DISABLED if not network.enabled else PostgresPreflightState.UNAVAILABLE
        )

    def set_diagnostic_sink(self, sink: Any) -> None:
        self._diagnostic_sink = sink

    def snapshot(self) -> dict[str, object]:
        return self.result.snapshot()

    async def run(self) -> PostgresPreflightResult:
        if not self.network.enabled:
            self.result = PostgresPreflightResult(PostgresPreflightState.DISABLED)
            return self.result
        timeout = max(0.05, min(_MAX_PRECHECK_SECONDS, self.network.connect_timeout_seconds + 1.0))
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    _run_sync,
                    self.network,
                    self.configuration,
                    self.connector,
                    self.clock,
                ),
                timeout=timeout,
            )
        except TimeoutError:
            result = PostgresPreflightResult(
                PostgresPreflightState.UNAVAILABLE,
                (PostgresCheck("startup_deadline", False, "PostgreSQL preflight timed out."),),
            )
        except Exception as exc:
            result = PostgresPreflightResult(
                PostgresPreflightState.UNAVAILABLE,
                (PostgresCheck("startup_preflight", False, _safe_summary(exc)),),
            )
        self.result = result
        self._emit_failures(result)
        return result

    def _emit_failures(self, result: PostgresPreflightResult) -> None:
        emit = getattr(self._diagnostic_sink, "emit", None)
        if not callable(emit):
            return
        try:
            from ..diagnostics.bindings import FOUNDATION_CODES

            for check in result.failures:
                emit(
                    FOUNDATION_CODES["database.operation_failed"],
                    component="postgresql-preflight",
                    message=f"PostgreSQL preflight check {check.name} failed.",
                    operational_effect="The optional PostgreSQL archive is unavailable; local broadcasting remains authoritative.",
                    recovery_action="Inspect the bounded PostgreSQL preflight state and correct the configured dependency.",
                    source_id="postgresql",
                )
        except Exception:
            log.debug("PostgreSQL preflight diagnostic promotion failed", exc_info=True)


__all__ = [
    "PostgresCheck",
    "PostgresConnection",
    "PostgresConnector",
    "PostgresPreflight",
    "PostgresPreflightResult",
    "PostgresPreflightState",
]
