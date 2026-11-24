from unittest.mock import Mock

import psycopg.errors as pg_errors
import pytest

from seasonalweather.database.archive_replication import ArchiveReplication


@pytest.mark.parametrize(
    ("error_type", "code"),
    [
        (RuntimeError, "SWDB3001"),
        (pg_errors.ConnectionFailure, "SWDB3001"),
        (pg_errors.InvalidPassword, "SWDB6001"),
        (pg_errors.InsufficientPrivilege, "SWDB6001"),
    ],
)
def test_archive_retry_emits_bounded_foundation_diagnostic(error_type, code):
    sink = Mock()
    ArchiveReplication._diagnose_retry(sink, error_type("private connection information"))
    args, details = sink.emit.call_args
    assert args == (code,)
    assert details["component"] == "postgresql-outbox"
    assert "private connection information" not in str(details)
    assert "remain queued" in details["operational_effect"]
