import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from seasonalweather.database.nws_backfill import _completed_version, _recover_product
from seasonalweather.database.postgresql_archive import ArchiveOrigin, ArchiveTable
from seasonalweather.database.recent_backfill import RecoveryResource, archive_snapshot, recover_recent


def test_recovery_records_and_checkpoint_use_stable_ids_in_one_transaction():
    archive = Mock()
    connection = object()

    @contextmanager
    def transaction():
        yield connection

    archive.transaction.side_effect = transaction
    resource = RecoveryResource("zone:X", ArchiveTable.FORECAST_SNAPSHOTS, AsyncMock())
    archive_snapshot(archive, resource, {"name": "X"})
    archive_snapshot(archive, resource, {"name": "X"})
    records = [call.args[1] for call in archive.insert_record.call_args_list]
    assert [record.id for record in records[:2]] == [record.id for record in records[2:]]
    assert records[1].table is ArchiveTable.BACKFILL_CHECKPOINTS
    assert records[1].payload["archive_id"] == str(records[0].id)
    assert all(record.origin is ArchiveOrigin.UPSTREAM_GAP_RECOVERY and not record.broadcastable for record in records)


def test_recovery_retries_then_continues(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    fetch = AsyncMock(side_effect=RuntimeError)
    next_fetch = AsyncMock(return_value={"name": "X"})
    save = Mock()
    monkeypatch.setattr("seasonalweather.database.recent_backfill.archive_snapshot", save)
    record_type = ArchiveTable.OBSERVATION_SAMPLES
    resources = [RecoveryResource("bad", record_type, fetch), RecoveryResource("good", record_type, next_fetch)]
    asyncio.run(recover_recent(Mock(), resources))
    assert fetch.await_count == 2
    next_fetch.assert_awaited_once()
    save.assert_called_once()


def test_product_checkpoint_is_repaired_from_archive_alias(monkeypatch):
    archive = Mock()
    archive.transaction.side_effect = lambda: transaction()
    version = uuid4()
    lookup = Mock(return_value=version)
    monkeypatch.setattr("seasonalweather.database.nws_backfill.ProductReconciliation.version_for_api_id", lookup)

    @contextmanager
    def transaction():
        yield object()

    archive.get.return_value = None
    assert _completed_version(archive, "id") == str(version)
    receipt = archive.insert_record.call_args.args[1]
    assert receipt.payload == {"api_product_id": "id", "version": str(version)}
    lookup.assert_called_once_with("id")
    lookup.reset_mock()

    archive.get.side_effect = [receipt, Mock()]
    assert _completed_version(archive, "id") == str(version)
    lookup.assert_not_called()


def test_revalidation_returns_new_version_even_with_existing_checkpoint(monkeypatch):
    old, new = uuid4(), uuid4()
    api = Mock(recovery_json=AsyncMock(return_value={"productText": "Corrected text"}))
    ingest = Mock(return_value=new)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    monkeypatch.setattr("seasonalweather.database.nws_backfill.ProductReconciliation.ingest", ingest)
    monkeypatch.setattr("seasonalweather.database.nws_backfill._completed_version", Mock(return_value=str(old)))

    assert asyncio.run(_recover_product(Mock(), api, "same-id", datetime.now(UTC))) == str(new)
    api.recovery_json.assert_awaited_once_with("products/same-id")
    delivery = ingest.call_args.args[0]
    assert delivery.api_product_id == "same-id"
    assert delivery.text == "Corrected text"
    assert delivery.origin is ArchiveOrigin.UPSTREAM_GAP_RECOVERY
