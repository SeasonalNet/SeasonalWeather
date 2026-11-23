from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from seasonalweather.config import OptionalServiceNetworkConfig, PostgresArchiveConfig
from seasonalweather.database.postgresql_archive import ArchiveOrigin, PostgresArchiveRepository
from seasonalweather.database.product_identity import ProductDelivery
from seasonalweather.database.product_reconciliation import ProductReconciliation


class ArchiveConnection:
    def __init__(self) -> None:
        self.rows: dict[str, dict[UUID, tuple[Any, ...]]] = {}
        self.result: list[tuple[Any, ...]] = []

    def connect(self, configuration: OptionalServiceNetworkConfig) -> "ArchiveConnection":
        assert configuration.enabled
        return self

    def execute(self, operation: str, parameters: Sequence[object] | None = None) -> "ArchiveConnection":
        if operation.startswith("INSERT"):
            assert parameters is not None and isinstance(parameters[0], UUID)
            table = operation.split('"')[3]
            _ = self.rows.setdefault(table, {}).setdefault(parameters[0], tuple(parameters))
        elif operation.startswith("SELECT"):
            assert parameters is not None
            self.result = self._select(operation, parameters)
        else:
            assert operation.startswith("LOCK TABLE")
        return self

    def _select(self, operation: str, parameters: Sequence[object]) -> list[tuple[Any, ...]]:
        links = self.rows.get("product_deliveries", {}).values()
        if operation.startswith("SELECT product_id"):
            return [(link[-2],) for link in links if link[-1] == parameters[0]]
        matches = set()
        aliases, digest = parameters
        assert isinstance(aliases, list)
        for link in links:
            payload = self.rows["source_deliveries"][link[-1]][3]
            assert isinstance(payload, Jsonb)
            if set(payload.obj["aliases"]).intersection(aliases) or payload.obj["content_hash"] == digest:
                matches.add((link[-2], payload.obj["content_hash"]))
        return sorted(matches)

    def fetchone(self) -> tuple[Any, ...] | None:
        return next(iter(self.result), None)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.result

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


def test_repeated_cross_source_and_conflicting_deliveries() -> None:
    connection = ArchiveConnection()
    repository = PostgresArchiveRepository(
        PostgresArchiveConfig(), OptionalServiceNetworkConfig(enabled=True), connector=connection
    )
    reconcile = ProductReconciliation(repository)
    when = datetime(2026, 11, 23, tzinfo=UTC)
    live = ProductDelivery(
        source="nwws",
        delivery_key="receipt-1",
        origin=ArchiveOrigin.LIVE,
        received_at=when,
        issued_at=when,
        wmo_heading="WFUS51 KBOX 230000",
        awips_id="TORBOX",
        office="KBOX",
        vtec_event_ids=("KBOX.TO.W.0001",),
        text="original text\nnext line",
    )
    recovered = replace(
        live,
        source="api",
        delivery_key="api-receipt",
        origin=ArchiveOrigin.UPSTREAM_GAP_RECOVERY,
        api_product_id="api-id",
        text="original  text\r\nnext line\r\n",
    )
    corrected = replace(recovered, origin=ArchiveOrigin.HISTORICAL_IMPORT, text="corrected text")
    original = reconcile.ingest(live)
    assert reconcile.ingest(live) == reconcile.ingest(recovered) == original
    conflict = reconcile.ingest(corrected)
    assert conflict != original
    assert reconcile.ingest(corrected) == conflict
    assert set(reconcile.versions_for(replace(recovered, text="different text"))) == {original, conflict}
    assert len(connection.rows["source_deliveries"]) == len(connection.rows["product_deliveries"]) == 3
    assert connection.rows["source_deliveries"][corrected.id][1] == "historical_import"
    assert connection.rows["source_deliveries"][corrected.id][6] is False
    assert len(connection.rows["reconciliation_anomalies"]) == 1
    probe = replace(live, text="probe")
    assert set(reconcile.versions_for(replace(probe, vtec_event_ids=()))) == {original, conflict}
    assert set(reconcile.versions_for(replace(probe, wmo_heading=""))) == {original, conflict}
    assert set(reconcile.versions_for(replace(probe, wmo_heading="", office="", api_product_id="api-id"))) == {
        original,
        conflict,
    }
    assert reconcile.versions_for(replace(probe, issued_at=None)) == ()
