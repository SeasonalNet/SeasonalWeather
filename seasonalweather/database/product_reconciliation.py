"""Transactional archive ingestion without broadcast side effects."""

from itertools import combinations
from typing import cast
from uuid import UUID

from psycopg.sql import SQL

from .postgresql_archive import ArchiveRecord, ArchiveTable, PostgresArchiveRepository
from .postgresql_migrations import PostgresMigrationConnection
from .product_identity import ProductDelivery, stable_id


class ProductReconciliation:
    def __init__(self, archive: PostgresArchiveRepository) -> None:
        self.archive = archive

    def ingest(self, delivery: ProductDelivery) -> UUID:
        """Persist one occurrence and return its canonical content version ID."""
        with self.archive.transaction() as connection:
            # Serialize lookup + insert across controller processes, including the first sighting.
            table = self.archive.table_name(ArchiveTable.SOURCE_DELIVERIES)
            connection.execute(SQL("LOCK TABLE {} IN SHARE ROW EXCLUSIVE MODE").format(table).as_string())
            existing = self._linked_version(connection, delivery.id)
            if existing is not None:
                return existing
            return self._reconcile(connection, delivery)

    def versions_for(self, delivery: ProductDelivery) -> tuple[UUID, ...]:
        """Look up known versions by the same evidence used at ingestion."""
        with self.archive.transaction() as connection:
            return tuple(key for key, _ in self._matching_versions(connection, delivery))

    def _linked_version(self, connection: PostgresMigrationConnection, delivery_id: UUID) -> UUID | None:
        statement = SQL("SELECT product_id FROM {} WHERE delivery_id = %s ORDER BY product_id LIMIT 1").format(
            self.archive.table_name(ArchiveTable.PRODUCT_DELIVERIES)
        )
        row = connection.execute(statement.as_string(), (delivery_id,)).fetchone()
        return None if row is None else cast(UUID, row[0])

    def _matching_versions(
        self, connection: PostgresMigrationConnection, delivery: ProductDelivery
    ) -> list[tuple[UUID, str]]:
        statement = SQL(
            "SELECT DISTINCT l.product_id, d.payload->>'content_hash' FROM {} d "
            "JOIN {} l ON d.id = l.delivery_id "
            "WHERE d.payload->'aliases' ?| %s OR d.payload->>'content_hash' = %s "
            "ORDER BY l.product_id"
        ).format(
            self.archive.table_name(ArchiveTable.SOURCE_DELIVERIES),
            self.archive.table_name(ArchiveTable.PRODUCT_DELIVERIES),
        )
        cursor = connection.execute(statement.as_string(), (delivery.aliases(), delivery.content_hash))
        return cast(list[tuple[UUID, str]], cursor.fetchall())

    def _reconcile(self, connection: PostgresMigrationConnection, delivery: ProductDelivery) -> UUID:
        matches = self._matching_versions(connection, delivery)
        content_hash = delivery.content_hash
        version_id = next((key for key, digest in matches if digest == content_hash), None)
        if version_id is None:
            version_id = stable_id("product-version", content_hash)
            self._insert(
                connection,
                delivery,
                ArchiveTable.PRODUCT_VERSIONS,
                version_id,
                {"content_hash": content_hash, "text": delivery.text},
            )
        self._insert(
            connection,
            delivery,
            ArchiveTable.SOURCE_DELIVERIES,
            delivery.id,
            {
                "source": delivery.source,
                "delivery_key": delivery.delivery_key,
                "text": delivery.text,
                "content_hash": content_hash,
                "aliases": delivery.aliases(),
                "api_product_id": delivery.api_product_id,
                "wmo_heading": delivery.wmo_heading,
                "awips_id": delivery.awips_id,
                "office": delivery.office,
                "issued_at": delivery.issued_at.isoformat() if delivery.issued_at else None,
                "vtec_event_ids": list(delivery.vtec_event_ids),
            },
        )
        self.archive.insert_record(
            connection,
            ArchiveRecord(
                table=ArchiveTable.PRODUCT_DELIVERIES,
                id=stable_id("link", str(delivery.id)),
                origin=delivery.origin,
                recorded_at=delivery.received_at,
                import_id=delivery.import_id,
                product_id=version_id,
                delivery_id=delivery.id,
                payload={},
            ),
        )
        self._record_conflicts(connection, delivery, {version_id, *(key for key, _ in matches)})
        return version_id

    def _record_conflicts(
        self, connection: PostgresMigrationConnection, delivery: ProductDelivery, versions: set[UUID]
    ) -> None:
        for left, right in combinations(sorted(map(str, versions)), 2):
            self._insert(
                connection,
                delivery,
                ArchiveTable.RECONCILIATION_ANOMALIES,
                stable_id("product-conflict", left, right),
                {"kind": "product_identity_conflict", "versions": [left, right], "delivery_id": str(delivery.id)},
                replay=True,
            )

    def _insert(
        self,
        connection: PostgresMigrationConnection,
        delivery: ProductDelivery,
        table: ArchiveTable,
        key: UUID,
        payload: dict[str, object],
        *,
        replay: bool = False,
    ) -> None:
        record = ArchiveRecord(
            table=table,
            id=key,
            origin=delivery.origin,
            recorded_at=delivery.received_at,
            import_id=delivery.import_id,
            payload=payload,
        )
        self.archive.insert_record(connection, record, replay=replay)
