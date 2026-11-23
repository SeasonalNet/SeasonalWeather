# PostgreSQL core archive (P4-03)

`PostgresMigrationRunner` includes version 1, `core_archive_schema`. It
creates archive tables in the configured PostgreSQL schema during safe startup
migration. This does not change SQLite's role in broadcasting. See [migration governance](p4-02-postgresql-migrations.md) for
operator and compatibility rules.

`PostgresArchiveRepository` owns SQL and transaction handling for all 12 archive
tables: import origins, source deliveries, canonical product versions,
product-delivery links, forecast snapshots, observation samples, alert lifecycle
versions, broadcast and airtime history, derived broadcast facts, backfill
checkpoints, and reconciliation anomalies. Authentication is not centralized by
this packet; it creates no authentication tables and does not copy tokens.

Records have application-supplied UUIDs, mandatory origin, a timezone-aware
recording timestamp, optional retention deadline, and an object payload with a
positive format version. Payload format versions are independent of database
migration versions. The repository preserves versions without interpreting the
payload; consumers must check versions before interpreting them. Product-delivery
links enforce both foreign keys and unique pairs. Airtime entries and derived
facts reference broadcast history. Records may link to their import origin.

The origin categories are `live`, `local_outbox_replay`, `upstream_gap_recovery`,
`historical_import`, and `legacy_import`. Records default to non-broadcastable;
Python and PostgreSQL both prohibit marking historical or legacy records
broadcastable. The flag is archive metadata, not an authorization to air.

Insert related records in parent-first order. A failed batch rolls back in full.
No implicit overwrite, canonical deduplication, retention purge, replication, or
backfill execution is introduced here.
