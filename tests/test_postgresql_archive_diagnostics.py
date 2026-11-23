from unittest.mock import Mock

from seasonalweather.database.archive_replication import ArchiveReplication


def test_archive_retry_emits_bounded_foundation_diagnostic():
    sink = Mock()
    ArchiveReplication._diagnose_retry(sink, RuntimeError("private connection information"))
    args, details = sink.emit.call_args
    assert args == ("SWDB3001",)
    assert details["component"] == "postgresql-outbox"
    assert "private connection information" not in str(details)
    assert "remain queued" in details["operational_effect"]
