from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

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
        ssl_in_use = True

        server_version = 160000

    info = _Info()

    def __init__(self):
        self.operations: list[str] = []
        self.rollbacks = 0
        self.closed = False

    def execute(self, operation, parameters=None):
        self.operations.append(operation)
        if "current_setting('server_version_num')" in operation:
            return _Cursor(("160000",))
        if "SELECT current_user" in operation:
            return _Cursor(("seasonal",))
        if "current_database(), current_user, current_schema()" in operation:
            return _Cursor(("seasonalweather", "seasonal", "public"))
        if "pg_get_userbyid" in operation:
            return _Cursor(("seasonal", "seasonal"))
        if "has_database_privilege" in operation:
            return _Cursor((True, True, True))
        if "to_regclass" in operation:
            return _Cursor((True,))
        if "pg_extension" in operation:
            return _Cursor((True,))
        if "EXTRACT(EPOCH FROM clock_timestamp())" in operation:
            return _Cursor((1_753_000_000.0,))
        if "spatial_ref_sys" in operation:
            return _Cursor((True,))
        if "SELECT value FROM" in operation:
            return _Cursor((1,))
        return _Cursor()

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
    preflight = PostgresPreflight(
        configuration=cfg.database.postgres,
        network=cfg.network.postgresql,
        connector=cast(PostgresConnector, cast(object, connector)),
        clock=lambda: dt.datetime.fromtimestamp(1_753_000_000.0, tz=dt.UTC),
    )

    result = asyncio.run(preflight.run())

    assert result.state is PostgresPreflightState.AVAILABLE
    assert not result.failures
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


def test_connection_failure_is_bounded_and_emits_redacted_diagnostic(monkeypatch):
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
            cast(object, _Connector(error=RuntimeError("password=SUPERSECRET host=postgres.example"))),
        ),
        diagnostic_sink=sink,
    )

    result = asyncio.run(preflight.run())

    assert result.state is PostgresPreflightState.UNAVAILABLE
    assert [failure.name for failure in result.failures] == ["connectivity"]
    assert len(sink.calls) == 1
    assert sink.calls[0]["code"] == "SWDB3001"
    assert "SUPERSECRET" not in repr(sink.calls)
    assert "postgres.example" not in repr(sink.calls)


def test_connection_failure_promotes_catalogued_database_diagnostic(monkeypatch, tmp_path):
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
        codes={"database.operation_failed": FOUNDATION_CODES["database.operation_failed"]},
    )
    preflight = PostgresPreflight(
        configuration=cfg.database.postgres,
        network=cfg.network.postgresql,
        connector=cast(
            PostgresConnector,
            cast(object, _Connector(error=RuntimeError("database unavailable"))),
        ),
        diagnostic_sink=sink,
    )

    result = asyncio.run(preflight.run())

    assert result.state is PostgresPreflightState.UNAVAILABLE
    occurrences = repository.active()
    assert len(occurrences) == 1
    assert occurrences[0].code == FOUNDATION_CODES["database.operation_failed"]
    assert str(occurrences[0].latest_instance["message"]) == "PostgreSQL preflight check connectivity failed."
    assert "database unavailable" not in str(occurrences[0].latest_instance)


def test_configured_postgresql_requires_database_name(monkeypatch, tmp_path):
    _configuration(monkeypatch)
    source = tmp_path / "config.yaml"
    text = Path("config/config.yaml").read_text(encoding="utf-8")
    text = text.replace(
        '    enabled: false\n    address: ""\n    port: 5432\n    database: ""',
        '    enabled: true\n    address: "postgres"\n    port: 5432\n    database: ""',
        1,
    )
    source.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match="network.postgresql.database"):
        load_config(str(source))


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
