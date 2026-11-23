"""Bounded, archive-only recovery of a finite set of upstream resources."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from .postgresql_archive import ArchiveOrigin, ArchiveRecord, ArchiveTable, PostgresArchiveRepository
from .product_identity import stable_id

log = logging.getLogger(__name__)
_ORIGIN = ArchiveOrigin.UPSTREAM_GAP_RECOVERY


@dataclass(frozen=True)
class RecoveryResource:
    key: str
    table: ArchiveTable
    fetch: Callable[[], Awaitable[dict[str, object]]]


def archive_snapshot(
    archive: PostgresArchiveRepository, resource: RecoveryResource, payload: dict[str, object]
) -> None:
    now = datetime.now(UTC)
    identity = stable_id("recovery", resource.table.value, resource.key, json.dumps(payload, sort_keys=True))
    checkpoint = ArchiveRecord(
        id=stable_id("recovery-checkpoint", str(identity)),
        table=ArchiveTable.BACKFILL_CHECKPOINTS,
        origin=_ORIGIN,
        recorded_at=now,
        payload={"resource": resource.key, "archive_id": str(identity)},
    )
    record = ArchiveRecord(id=identity, table=resource.table, origin=_ORIGIN, recorded_at=now, payload=payload)
    with archive.transaction() as connection:
        archive.insert_record(connection, record, replay=True)
        if resource.table is not ArchiveTable.BACKFILL_CHECKPOINTS:
            archive.insert_record(connection, checkpoint, replay=True)


async def recover_recent(archive: PostgresArchiveRepository, resources: Sequence[RecoveryResource]) -> None:
    """Attempt at most 32 resources, serially, with two attempts per resource."""
    for resource in resources[:32]:
        for attempt in range(2):
            await asyncio.sleep(0.5 * (attempt + 1))
            try:
                payload = await resource.fetch()
                await asyncio.to_thread(archive_snapshot, archive, resource, payload)
                break
            except Exception as exc:
                if attempt:
                    log.warning("Recent archive recovery failed (%s)", type(exc).__name__)
