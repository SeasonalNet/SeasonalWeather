"""Select configured NWS resources for the startup archive pass."""

import asyncio
from datetime import UTC, datetime, timedelta
from functools import partial
from urllib.parse import quote
from uuid import UUID

from ..alerts.nws_api import NWSApi
from ..config import AppConfig
from .postgresql_archive import ArchiveOrigin, ArchiveRecord, ArchiveTable, PostgresArchiveRepository
from .product_identity import ProductDelivery, stable_id
from .product_reconciliation import ProductReconciliation
from .recent_backfill import RecoveryResource, recover_recent


async def recover_nws(archive: PostgresArchiveRepository, api: NWSApi, cfg: AppConfig, areas: list[str]) -> None:
    resources = (
        [
            RecoveryResource(
                "active:" + ",".join(areas),
                ArchiveTable.ALERT_LIFECYCLE_VERSIONS,
                partial(api.recovery_json, "alerts/active", {"area": ",".join(areas), "status": "actual"}),
            )
        ]
        if areas
        else []
    )
    resources.extend(
        RecoveryResource(
            f"zone:{zone}",
            ArchiveTable.FORECAST_SNAPSHOTS,
            partial(api.recovery_json, f"zones/{quote(kind, safe='')}/{quote(zone, safe='')}"),
        )
        for kind, zone in dict.fromkeys(cfg.cycle.fc.forecast_zones[:8])
    )
    resources.extend(
        RecoveryResource(
            f"observation:{station}",
            ArchiveTable.OBSERVATION_SAMPLES,
            partial(api.recovery_json, f"stations/{quote(station, safe='')}/observations/latest"),
        )
        for station in dict.fromkeys(cfg.observations.stations[:8])
    )
    await recover_recent(archive, resources)
    office = quote(cfg.cycle.primary_wfo, safe="")
    index = RecoveryResource(
        f"products:HWO:{office}",
        ArchiveTable.BACKFILL_CHECKPOINTS,
        partial(api.recovery_json, f"products/types/HWO/locations/{office}", {"limit": "5"}),
    )
    await recover_recent(
        archive, [RecoveryResource(index.key, index.table, partial(_recover_products, archive, api, index))]
    )


async def _recover_products(
    archive: PostgresArchiveRepository, api: NWSApi, index: RecoveryResource
) -> dict[str, object]:
    payload = await index.fetch()
    items = payload.get("@graph")
    if not isinstance(items, list):
        raise ValueError("Unexpected recovery product index")
    recovered: list[str] = []
    for row in items[:5]:
        if not isinstance(row, dict) or not isinstance(key := row.get("id"), str):
            continue
        issued = NWSApi._issuance_sort_key(row.get("issuanceTime"))
        if issued < datetime.now(UTC) - timedelta(days=2):
            continue
        product_id = key.rstrip("/").rsplit("/", 1)[-1]
        version = await _recover_product(archive, api, product_id, issued)
        recovered.append(version)
    return {"versions": recovered}


def _completed_version(archive: PostgresArchiveRepository, product_id: str) -> str | None:
    checkpoint = archive.get(ArchiveTable.BACKFILL_CHECKPOINTS, stable_id("nws-product-checkpoint", product_id))
    if (
        checkpoint is not None
        and isinstance(saved := checkpoint.payload.get("version"), str)
        and archive.get(ArchiveTable.PRODUCT_VERSIONS, UUID(saved)) is not None
    ):
        return saved
    version = ProductReconciliation(archive).version_for_api_id(product_id)
    if version is None:
        return None
    receipt = ArchiveRecord(
        id=stable_id("nws-product-checkpoint", product_id),
        table=ArchiveTable.BACKFILL_CHECKPOINTS,
        origin=ArchiveOrigin.UPSTREAM_GAP_RECOVERY,
        recorded_at=datetime.now(UTC),
        payload={"api_product_id": product_id, "version": str(version)},
    )
    with archive.transaction() as connection:
        archive.insert_record(connection, receipt, replay=True)
    return str(version)


async def _recover_product(archive: PostgresArchiveRepository, api: NWSApi, product_id: str, issued: datetime) -> str:
    await asyncio.sleep(0.5)
    detail = await api.recovery_json(f"products/{quote(product_id, safe='')}")
    raw = detail.get("productText")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Missing recovery product text")
    delivery = ProductDelivery(
        source="nws_api",
        delivery_key=product_id,
        origin=ArchiveOrigin.UPSTREAM_GAP_RECOVERY,
        received_at=datetime.now(UTC),
        text=raw,
        api_product_id=product_id,
        issued_at=issued,
    )
    version = await asyncio.to_thread(ProductReconciliation(archive).ingest, delivery)
    _ = await asyncio.to_thread(_completed_version, archive, product_id)
    return str(version)
