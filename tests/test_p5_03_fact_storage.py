"""Candidate storage contracts without a live archive service."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, call
from uuid import uuid4

import pytest
from psycopg.errors import ForeignKeyViolation
from psycopg.types.json import Jsonb

from seasonalweather.config import OptionalServiceNetworkConfig, PostgresArchiveConfig
from seasonalweather.database.fact_storage import (
    ArchiveRef,
    CandidateConflictError,
    CandidateProvenanceError,
    CandidateRow,
    PostgresFactCandidateRepository,
)
from seasonalweather.database.postgresql_archive import ArchiveOrigin, ArchiveTable, PostgresArchiveRepository
from seasonalweather.database.postgresql_migrations import (
    POSTGRES_MIGRATIONS,
    MigrationOperationKind,
    MigrationSafety,
    PostgresMigrationRunner,
)

_VALID_FROM = datetime(2026, 11, 24, 11, tzinfo=UTC)
_EXPIRY = datetime(2026, 11, 24, 12, tzinfo=UTC)


@pytest.fixture
def row() -> CandidateRow:
    return CandidateRow(
        uuid4(),
        _EXPIRY,
        {"version": 1},
        ArchiveRef(ArchiveTable.FORECAST_SNAPSHOTS, uuid4()),
        ArchiveRef(ArchiveTable.OBSERVATION_SAMPLES, uuid4()),
        valid_from=_VALID_FROM,
    )


@pytest.fixture
def connection() -> Mock:
    return Mock()


@pytest.fixture
def repository(connection: Mock) -> PostgresFactCandidateRepository:
    archive = PostgresArchiveRepository(
        PostgresArchiveConfig(schema="test_archive"),
        OptionalServiceNetworkConfig(enabled=True),
        connector=Mock(connect=Mock(return_value=connection)),
    )
    return PostgresFactCandidateRepository(archive)


def test_lookup_uses_exact_archive_ref(
    repository: PostgresFactCandidateRepository,
    connection: Mock,
    row: CandidateRow,
) -> None:
    connection.execute.return_value.fetchone.side_effect = [
        (row.current.id, ArchiveOrigin.LIVE.value, _EXPIRY, {"raw": "evidence"}, None, 1, False, None),
        None,
    ]
    found = repository.lookup(row.current)
    assert found is not None and found.table is row.current.table and found.id == row.current.id
    assert found.payload == {"raw": "evidence"}
    assert repository.lookup(row.previous) is None
    current, previous = connection.execute.call_args_list
    assert 'FROM "test_archive"."forecast_snapshots"' in current.args[0]
    assert current.args[1] == (row.current.id,)
    assert 'FROM "test_archive"."observation_samples"' in previous.args[0]
    assert previous.args[1] == (row.previous.id,)


def test_exact_retry_checks_all_fields_without_overwrite(
    repository: PostgresFactCandidateRepository,
    connection: Mock,
    row: CandidateRow,
) -> None:
    connection.execute.return_value.fetchone.side_effect = [(row.id,), None, (1,)]
    repository.store(row)
    repository.store(row)
    first, retry, verify = connection.execute.call_args_list
    assert first.args[0].startswith('INSERT INTO "test_archive"."broadcast_fact_candidates"')
    assert "ON CONFLICT (id) DO NOTHING RETURNING id" in first.args[0]
    for args in (first.args, retry.args, verify.args):
        candidate_id, expires_at, payload, current_table, current_id, previous_table, previous_id, valid_from = args[1]
        assert candidate_id == row.id and expires_at == row.expires_at and valid_from == row.valid_from
        assert isinstance(payload, Jsonb) and payload.obj == row.payload
        assert (current_table, current_id) == (row.current.table.value, row.current.id)
        assert (previous_table, previous_id) == (row.previous.table.value, row.previous.id)
    assert "expires_at = %s AND payload = %s" in verify.args[0]
    assert "current_table = %s AND current_id = %s AND previous_table = %s AND previous_id = %s" in verify.args[0]
    assert "AND valid_from = %s" in verify.args[0]
    assert connection.commit.call_count == connection.close.call_count == 2
    connection.rollback.assert_not_called()


def test_conflicting_id_rolls_back(
    repository: PostgresFactCandidateRepository,
    connection: Mock,
    row: CandidateRow,
) -> None:
    connection.execute.return_value.fetchone.side_effect = [None, None]
    with pytest.raises(CandidateConflictError, match="different content"):
        repository.store(row)
    assert connection.mock_calls[-2:] == [call.rollback(), call.close()]
    connection.commit.assert_not_called()


def test_missing_source_fails_closed_and_rolls_back(
    repository: PostgresFactCandidateRepository,
    connection: Mock,
    row: CandidateRow,
) -> None:
    cause = ForeignKeyViolation()
    connection.execute.side_effect = cause
    with pytest.raises(CandidateProvenanceError, match="missing archive record") as error:
        repository.store(row)
    assert error.value.__cause__ is cause
    assert connection.mock_calls[-2:] == [call.rollback(), call.close()]
    connection.commit.assert_not_called()


def test_get_uses_inclusive_start_and_exclusive_expiry(
    repository: PostgresFactCandidateRepository,
    connection: Mock,
    row: CandidateRow,
) -> None:
    stored = (
        row.expires_at,
        row.payload,
        row.current.table.value,
        row.current.id,
        row.previous.table.value,
        row.previous.id,
        row.valid_from,
    )
    connection.execute.return_value.fetchone.side_effect = [None, stored, stored, None, None]
    tick = timedelta(microseconds=1)
    instants = (_VALID_FROM - tick, _VALID_FROM, _EXPIRY - tick, _EXPIRY, _EXPIRY + tick)
    assert [repository.get(row.id, as_of=instant) for instant in instants] == [None, row, row, None, None]
    for actual, instant in zip(connection.execute.call_args_list, instants, strict=True):
        assert "WHERE id = %s AND valid_from <= %s AND expires_at > %s" in actual.args[0]
        assert actual.args[1] == (row.id, instant, instant)


def test_inadmissible_table_and_naive_times_are_rejected(
    repository: PostgresFactCandidateRepository,
    connection: Mock,
    row: CandidateRow,
) -> None:
    with pytest.raises(CandidateProvenanceError, match="inadmissible archive source"):
        _ = replace(row, current=ArchiveRef(ArchiveTable.DERIVED_BROADCAST_FACTS, uuid4()))
    naive = _EXPIRY.replace(tzinfo=None)
    with pytest.raises(ValueError, match="include a timezone"):
        _ = replace(row, expires_at=naive)
    with pytest.raises(ValueError, match="include a timezone"):
        _ = replace(row, valid_from=naive)
    with pytest.raises(ValueError, match="start before expiration"):
        _ = replace(row, valid_from=row.expires_at)
    with pytest.raises(ValueError, match="include a timezone"):
        _ = repository.get(row.id, as_of=naive)
    connection.execute.assert_not_called()


def test_v4_upgrades_existing_archive_without_changing_prior_migrations(connection: Mock) -> None:
    prior_checksums = (
        "00d0069361ad7915bdc2cc19df851d10bbf1925788f008d9993fda462079b504",
        "4ad39d429adee972e4d253a31f5d61bfd69a1c7295e5ed17bbb86b4d9659d1de",
        "896392b35e6858afbd33255010c1b3bc861b50cb1bc479d1cc35c1ce79e91167",
    )
    prior = POSTGRES_MIGRATIONS[:3]
    history = [
        (m.version, m.name, checksum, m.safety.value) for m, checksum in zip(prior, prior_checksums, strict=True)
    ]
    migration = POSTGRES_MIGRATIONS[3]
    upgraded_history = [*history, (migration.version, migration.name, migration.checksum, migration.safety.value)]
    connection.execute.return_value.fetchall.side_effect = [history, [("public",)], upgraded_history]
    runner = PostgresMigrationRunner(
        configuration=PostgresArchiveConfig(schema="test_archive"),
        network=OptionalServiceNetworkConfig(enabled=True),
        connector=Mock(connect=Mock(return_value=connection)),
    )
    assert runner.apply_pending().applied_versions == (4,)
    assert runner.apply_pending().state == "up_to_date"
    assert migration.safety is MigrationSafety.AUTOMATIC_SAFE
    (operation,) = migration.operations
    assert operation.kind is MigrationOperationKind.CREATE_TABLE
    ddl = operation.sql
    assert ddl.startswith("CREATE TABLE broadcast_fact_candidates")
    assert "valid_from timestamptz NOT NULL" in ddl
    assert "CHECK (valid_from < expires_at)" in ddl
    for table in (
        ArchiveTable.ALERT_LIFECYCLE_VERSIONS,
        ArchiveTable.FORECAST_SNAPSHOTS,
        ArchiveTable.OBSERVATION_SAMPLES,
    ):
        assert ddl.count(f"REFERENCES {table.value}(id)") == 2
    assert "broadcast_id" not in ddl and "broadcastable" not in ddl
    assert connection.execute.call_args_list.count(call(ddl)) == 1
