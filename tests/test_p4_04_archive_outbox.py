import asyncio
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

import pytest
from psycopg.types.json import Jsonb

from seasonalweather.config import OptionalServiceNetworkConfig, PostgresArchiveConfig
from seasonalweather.database.alerts import AlertStateRepository
from seasonalweather.database.archive_replication import ArchiveReplication
from seasonalweather.database.core import SeasonalDatabase
from seasonalweather.database.postgresql_archive import PostgresArchiveRepository


class ArchiveServer:
    def __init__(self) -> None:
        self.committed: dict[UUID, object] = {}
        self.pending: dict[UUID, object] = {}
        self.offline = False
        self.fail_second = False
        self.lose_ack = False

    def connect(self, configuration: OptionalServiceNetworkConfig) -> "ArchiveServer":
        assert configuration.enabled
        if self.offline:
            raise OSError("offline")
        return self

    def execute(self, operation: str, parameters: Sequence[object] | None = None) -> None:
        assert '"alert_lifecycle_versions"' in operation
        assert "ON CONFLICT (id) DO NOTHING" in operation
        assert parameters is not None
        key, payload = parameters[0], parameters[3]
        assert isinstance(key, UUID) and isinstance(payload, Jsonb)
        if self.fail_second and self.pending:
            raise OSError("insert failed")
        if key not in self.committed:
            self.pending[key] = payload.obj

    def commit(self) -> None:
        self.committed.update(self.pending)
        self.pending.clear()
        if self.lose_ack:
            raise OSError("commit acknowledgment lost")

    def rollback(self) -> None:
        self.pending.clear()

    def close(self) -> None:
        assert not self.pending


def _replication(db: SeasonalDatabase, server: ArchiveServer) -> ArchiveReplication:
    archive = PostgresArchiveRepository(
        PostgresArchiveConfig(), OptionalServiceNetworkConfig(enabled=True), connector=server
    )
    return ArchiveReplication(db, archive)


def test_supervised_loop_backs_off_and_recovers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = SeasonalDatabase(path=str(tmp_path / "state.sqlite"))
    db.archive_outbox_enabled = True
    server = ArchiveServer()
    replication = _replication(db, server)
    alerts = AlertStateRepository(db)
    alerts.replace_active_alerts([{"id": "before-outage"}])
    server.offline = True
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 7:
            assert replication.retrying
            alerts.replace_active_alerts([{"id": "during-outage"}])
            server.offline = False
        if len(delays) == 8:
            assert not replication.retrying
            raise asyncio.CancelledError

    monkeypatch.setattr("seasonalweather.database.archive_replication.asyncio.sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(replication.run())
    assert delays == [1, 2, 4, 8, 16, 32, 60, 1]
    assert replication.backlog()["outbox_pending"] == 0
    assert len(server.committed) == 2


def test_snapshot_and_local_state_commit_together(tmp_path: Path) -> None:
    db = SeasonalDatabase(path=str(tmp_path / "state.sqlite"))
    alerts = AlertStateRepository(db)
    server = ArchiveServer()
    replication = _replication(db, server)
    alerts.replace_active_alerts([{"id": "prior"}])
    assert replication.backlog()["outbox_pending"] == 0

    db.archive_outbox_enabled = True
    with pytest.raises(TypeError):
        alerts.replace_active_alerts([{"id": "new", "headline": object()}])
    assert alerts.load_active_alerts()[0]["id"] == "prior"
    assert replication.backlog()["outbox_pending"] == 0

    alerts.replace_active_alerts([{"id": "new", "audio_path": "/local/alert.wav"}])
    assert alerts.load_active_alerts()[0]["audio_path"] == "/local/alert.wav"
    assert replication.backlog()["outbox_pending"] == 1
    assert replication.drain_batch() == 1
    assert list(server.committed.values()) == [{"active_alerts": [{"id": "new"}]}]


def test_outage_restart_partial_failure_and_lost_ack_catch_up(tmp_path: Path) -> None:
    path = str(tmp_path / "state.sqlite")
    db = SeasonalDatabase(path=path)
    db.archive_outbox_enabled = True
    alerts = AlertStateRepository(db)
    server = ArchiveServer()
    server.offline = True
    replication = _replication(db, server)
    for index in range(97):
        alerts.replace_active_alerts([{"id": str(index)}])
        if index % 32 == 0:
            with pytest.raises(OSError, match="offline"):
                _ = replication.drain_batch()
    assert alerts.load_active_alerts()[0]["id"] == "96"

    replication = _replication(SeasonalDatabase(path=path), server)
    backlog = replication.backlog()
    assert backlog["outbox_pending"] == 97
    assert isinstance(backlog["outbox_oldest_at"], str)
    server.offline = False
    server.fail_second = True
    with pytest.raises(OSError, match="insert failed"):
        _ = replication.drain_batch()
    assert not server.committed
    assert replication.backlog() == backlog

    server.fail_second = False
    server.lose_ack = True
    with pytest.raises(OSError, match="acknowledgment lost"):
        _ = replication.drain_batch()
    assert len(server.committed) == 32
    assert replication.backlog() == backlog

    server.lose_ack = False
    replication = _replication(SeasonalDatabase(path=path), server)
    assert [replication.drain_batch() for _ in range(5)] == [32, 32, 32, 1, 0]
    assert len(server.committed) == 97
    assert replication.backlog() == {"outbox_pending": 0, "outbox_oldest_at": None}
