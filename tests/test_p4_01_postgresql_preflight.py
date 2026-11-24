from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import psycopg.errors as pg_errors
import pytest

from seasonalweather.config import load_config
from seasonalweather.database import SeasonalDatabase
from seasonalweather.database.postgresql import (
    PostgresConnector,
    PostgresPreflight,
    PostgresPreflightState,
)
from seasonalweather.diagnostics.bindings import FOUNDATION_CODES
from seasonalweather.runtime_diagnostics.models import CorrelationContext, DiagnosticRole
from seasonalweather.runtime_diagnostics.repository import OccurrenceRepository
from seasonalweather.runtime_diagnostics.service import RuntimeDiagnosticService
from seasonalweather.runtime_diagnostics.sink import RuntimeDiagnosticSink


class _Cursor:
    def __init__(self, row=()):
        self._row = tuple(row)

    def fetchone(self):
        return self._row or None


class _Connection:
    class _Info:
        server_version = 160000

    info = _Info()
    pgconn = SimpleNamespace(ssl_in_use=True)

    def __init__(
        self, *, postgis=True, spatial_reference=True, ownership=("seasonal", "seasonal"), privileges=(True, True, True)
    ):
        self.operations: list[str] = []
        self.rollbacks = 0
        self.closed = False
        self.postgis = postgis
        self.spatial_reference = spatial_reference
        self.ownership = ownership
        self.privileges = privileges

    def execute(self, operation, _parameters=None):
        self.operations.append(operation)
        if "current_setting('server_version_num')" in operation:
            return _Cursor(("160000",))
        if "SELECT current_user" in operation:
            return _Cursor(("seasonal",))
        if "current_database(), current_user, current_schema()" in operation:
            return _Cursor(("seasonalweather", "seasonal", "public"))
        if "pg_get_userbyid" in operation:
            return _Cursor(self.ownership)
        if "has_database_privilege" in operation:
            return _Cursor(self.privileges)
        if "to_regclass" in operation:
            return _Cursor((True,))
        if "pg_extension" in operation:
            return self._probe(self.postgis)
        if "EXTRACT(EPOCH FROM clock_timestamp())" in operation:
            return _Cursor((1_753_000_000.0,))
        if "spatial_ref_sys" in operation:
            return self._probe(self.spatial_reference)
        if "SELECT value FROM" in operation:
            return _Cursor((1,))
        return _Cursor()

    @staticmethod
    def _probe(result):
        if isinstance(result, Exception):
            raise result
        return _Cursor((result,))

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class _Connector:
    def __init__(self, connection=None, error=None):
        self.connection = connection or _Connection()
        self.error = error
        self.calls = 0

    def connect(self, _configuration):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.connection


class _Sink:
    def __init__(self):
        self.calls: list[dict[str, object]] = []

    def emit(self, code, **kwargs):
        self.calls.append({"code": code, **kwargs})


def _configuration(monkeypatch):
    monkeypatch.setenv("ICECAST_SOURCE_PASSWORD", "test-source")
    monkeypatch.setenv("NWWS_JID", "changeme@nwws-oi.weather.gov")
    monkeypatch.setenv("NWWS_PASSWORD", "CHANGEME")
    return load_config("config/config.yaml")


def test_disabled_postgresql_does_not_connect(monkeypatch):
    cfg = _configuration(monkeypatch)
    connector = _Connector()
    preflight = PostgresPreflight(
        configuration=cfg.database.postgres,
        network=cfg.network.postgresql,
        connector=cast(PostgresConnector, cast(object, connector)),
    )

    result = asyncio.run(preflight.run())

    assert result.state is PostgresPreflightState.DISABLED
    assert connector.calls == 0
    assert preflight.snapshot()["reason"] == "disabled_by_configuration"


def test_successful_preflight_checks_transaction_and_closes_connection(monkeypatch):
    cfg = _configuration(monkeypatch)
    cfg = replace(
        cfg,
        network=replace(
            cfg.network, postgresql=replace(cfg.network.postgresql, enabled=True, database="seasonalweather")
        ),
    )
    connection = _Connection()
    connector = _Connector(connection)

    def migrate() -> str:
        assert connection.rollbacks >= 2
        return "PostgreSQL archive schema is at version 1."

    preflight = PostgresPreflight(
        configuration=cfg.database.postgres,
        network=cfg.network.postgresql,
        connector=cast(PostgresConnector, cast(object, connector)),
        clock=lambda: dt.datetime.fromtimestamp(1_753_000_000.0, tz=dt.UTC),
        migrate=migrate,
    )

    result = asyncio.run(preflight.run())

    assert result.state is PostgresPreflightState.AVAILABLE
    assert not result.failures
    assert any(check.name == "migration_state" and "version 1" in check.summary for check in result.checks)
    assert {check.name for check in result.checks} == {
        "connectivity",
        "tls_policy",
        "authentication",
        "version",
        "database_identity",
        "schema_ownership",
        "privileges",
        "migration_state",
        "extension_state",
        "transactional_read_write",
        "clock_divergence",
        "spatial_reference",
    }
    assert "CREATE TEMP TABLE" in " ".join(connection.operations)
    assert connection.rollbacks >= 2
    assert connection.closed is True


@pytest.mark.parametrize(
    ("postgis", "spatial_reference", "check_name", "code"),
    [
        (False, True, "extension_state", "SWDB2001"),
        (True, False, "spatial_reference", "SWDB2001"),
        (RuntimeError("private query detail"), True, "extension_state", "SWDB3001"),
        (True, RuntimeError("private query detail"), "spatial_reference", "SWDB3001"),
        (True, pg_errors.InsufficientPrivilege("private query detail"), "spatial_reference", "SWDB6001"),
        (None, True, "extension_state", "SWDB3001"),
    ],
)
def test_postgis_preflight_diagnostic(monkeypatch, postgis, spatial_reference, check_name, code):
    cfg = _configuration(monkeypatch)
    connection = _Connection(postgis=postgis, spatial_reference=spatial_reference)
    sink = _Sink()

    preflight = PostgresPreflight(
        configuration=cfg.database.postgres,
        network=replace(cfg.network.postgresql, enabled=True, database="seasonalweather"),
        connector=cast(PostgresConnector, cast(object, _Connector(connection))),
        clock=lambda: dt.datetime.fromtimestamp(1_753_000_000.0, tz=dt.UTC),
        diagnostic_sink=sink,
        migrate=lambda: pytest.fail("PostGIS failure must prevent automatic migration"),
    )

    result = asyncio.run(preflight.run())

    assert result.state is PostgresPreflightState.UNAVAILABLE
    assert [failure.name for failure in result.failures] == [check_name]
    assert [call["code"] for call in sink.calls] == [code]
    assert "private query detail" not in repr((result, sink.calls))
    assert any("spatial_ref_sys" in sql for sql in connection.operations) is (postgis is True)
    assert connection.closed


@pytest.mark.parametrize(
    ("ownership", "privileges", "check_name", "code"),
    [
        (("other-role", "current-role"), (True, True, True), "schema_ownership", "SWDB6001"),
        (("current-role", "current-role"), (True, False, True), "privileges", "SWDB6001"),
        ((), (True, True, True), "schema_ownership", "SWDB3001"),
        (("current-role", "current-role"), (True, None, True), "privileges", "SWDB3001"),
    ],
)
def test_preflight_authorization_probes(monkeypatch, ownership, privileges, check_name, code):
    cfg = _configuration(monkeypatch)
    connection = _Connection(ownership=ownership, privileges=privileges)
    sink = _Sink()
    preflight = PostgresPreflight(
        configuration=cfg.database.postgres,
        network=replace(cfg.network.postgresql, enabled=True, database="seasonalweather"),
        connector=cast(PostgresConnector, cast(object, _Connector(connection))),
        clock=lambda: dt.datetime.fromtimestamp(1_753_000_000.0, tz=dt.UTC),
        diagnostic_sink=sink,
        migrate=lambda: pytest.fail("Failed access prerequisite must prevent automatic migration"),
    )

    result = asyncio.run(preflight.run())

    assert result.state is PostgresPreflightState.UNAVAILABLE
    assert [failure.name for failure in result.failures] == [check_name]
    assert [call["code"] for call in sink.calls] == [code]
    assert "other-role" not in repr((result, sink.calls))
    assert "current-role" not in repr((result, sink.calls))


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (RuntimeError("private PostgreSQL error details"), "SWDB3001"),
        (pg_errors.InsufficientPrivilege("private PostgreSQL error details"), "SWDB6001"),
    ],
)
def test_failed_migration_degrades_optional_archive(monkeypatch, error, code):
    cfg = _configuration(monkeypatch)
    connection = _Connection()
    sink = _Sink()

    def migrate() -> str:
        raise RuntimeError("PostgreSQL migration failed") from error

    preflight = PostgresPreflight(
        configuration=cfg.database.postgres,
        network=replace(cfg.network.postgresql, enabled=True, database="seasonalweather"),
        connector=cast(PostgresConnector, cast(object, _Connector(connection))),
        clock=lambda: dt.datetime.fromtimestamp(1_753_000_000.0, tz=dt.UTC),
        diagnostic_sink=sink,
        migrate=migrate,
    )

    result = asyncio.run(preflight.run())
    assert result.state is PostgresPreflightState.UNAVAILABLE
    assert [failure.name for failure in result.failures] == ["migration_state"]
    assert connection.closed
    assert sink.calls[0]["code"] == code
    assert "private PostgreSQL error details" not in repr((result, sink.calls))


@pytest.mark.parametrize(
    ("error_type", "code"),
    [
        (pg_errors.InvalidPassword, "SWDB6001"),
        (pg_errors.InvalidAuthorizationSpecification, "SWDB6001"),
        (pg_errors.ConnectionFailure, "SWDB3001"),
        (pg_errors.OperationalError, "SWDB3001"),
        (RuntimeError, "SWDB3001"),
    ],
)
def test_connection_failure_is_bounded_and_emits_redacted_diagnostic(monkeypatch, error_type, code):
    cfg = _configuration(monkeypatch)
    cfg = replace(
        cfg,
        network=replace(
            cfg.network, postgresql=replace(cfg.network.postgresql, enabled=True, database="seasonalweather")
        ),
    )
    sink = _Sink()
    preflight = PostgresPreflight(
        configuration=cfg.database.postgres,
        network=cfg.network.postgresql,
        connector=cast(
            PostgresConnector,
            cast(
                object,
                _Connector(
                    error=error_type("password authentication failed: password=SUPERSECRET host=postgres.example")
                ),
            ),
        ),
        diagnostic_sink=sink,
    )

    result = asyncio.run(preflight.run())

    assert result.state is PostgresPreflightState.UNAVAILABLE
    assert [failure.name for failure in result.failures] == ["connectivity"]
    assert len(sink.calls) == 1
    assert sink.calls[0]["code"] == code
    assert "SUPERSECRET" not in repr((result, sink.calls))
    assert "postgres.example" not in repr((result, sink.calls))


@pytest.mark.parametrize(
    ("connect_error", "check_name", "rule"),
    [
        (RuntimeError("database unavailable"), "connectivity", "database.operation_failed"),
        (pg_errors.InvalidPassword("private connection detail"), "connectivity", "database.access_denied"),
        (None, "extension_state", "database.postgis_incompatible"),
    ],
)
def test_preflight_failure_promotes_catalogued_database_diagnostic(
    monkeypatch, tmp_path, connect_error, check_name, rule
):
    cfg = _configuration(monkeypatch)
    cfg = replace(
        cfg,
        network=replace(
            cfg.network, postgresql=replace(cfg.network.postgresql, enabled=True, database="seasonalweather")
        ),
    )
    database = SeasonalDatabase(path=str(tmp_path / "diagnostics.sqlite3"))
    database.bootstrap()
    repository = OccurrenceRepository(database)
    service = RuntimeDiagnosticService(repository)
    service.initialize()
    sink = RuntimeDiagnosticSink(
        service,
        CorrelationContext(
            role=DiagnosticRole.CONTROLLER,
            instance_id="postgresql-preflight-test",
            component="test",
            build_identity="test-build",
        ),
        codes={key: code for key, code in FOUNDATION_CODES.items() if key.startswith("database.")},
    )
    connector = _Connector(
        _Connection(postgis=False),
        error=connect_error,
    )
    preflight = PostgresPreflight(
        configuration=cfg.database.postgres,
        network=cfg.network.postgresql,
        connector=cast(PostgresConnector, cast(object, connector)),
        clock=lambda: dt.datetime.fromtimestamp(1_753_000_000.0, tz=dt.UTC),
        diagnostic_sink=sink,
    )

    result = asyncio.run(preflight.run())

    assert result.state is PostgresPreflightState.UNAVAILABLE
    occurrences = repository.active()
    assert len(occurrences) == 1
    assert occurrences[0].code == FOUNDATION_CODES[rule]
    assert str(occurrences[0].latest_instance["message"]) == f"PostgreSQL preflight check {check_name} failed."
    assert "database unavailable" not in str(occurrences[0].latest_instance)


def test_configured_postgresql_requires_database_name(monkeypatch, tmp_path):
    _ = _configuration(monkeypatch)
    source = tmp_path / "config.yaml"
    text = Path("config/config.yaml").read_text(encoding="utf-8")
    text = text.replace(
        '    enabled: false\n    address: ""\n    port: 5432\n    database: ""',
        '    enabled: true\n    address: "postgres"\n    port: 5432\n    database: ""',
        1,
    )
    source.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match="network.postgresql.database"):
        _ = load_config(str(source))


def test_health_snapshot_is_optional_and_bounded():
    from seasonalweather.health_service import _postgresql_health_probe

    runtime = SimpleNamespace(
        postgresql_preflight=SimpleNamespace(
            snapshot=lambda: {
                "state": "unavailable",
                "reason": "preflight_failed",
                "checks": 10,
                "failed_checks": 2,
                "elapsed_milliseconds": 31_000,
                "server_version": 0,
            }
        )
    )

    component = asyncio.run(_postgresql_health_probe(runtime))

    assert component.name == "postgresql"
    assert component.required is False
    assert component.state.value == "unavailable"
    assert component.details == {
        "checks": 10,
        "failed_checks": 2,
        "elapsed_milliseconds": 31_000,
        "server_version": 0,
    }
