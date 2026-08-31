# PostgreSQL migration framework and schema governance

P4-02 establishes the controller-owned migration boundary for the optional
PostgreSQL archive. P4-01 startup preflight qualifies the configured endpoint;
the migration runner is an explicitly invoked maintenance operation and is not
run as part of routine startup. SQLite remains the authority for routine local
broadcast continuity.

## Versioned history

Each PostgreSQL migration is a checked-in, monotonically increasing version
with a stable name and one or more typed SQL operations. The runner creates the
configured migration metadata relation if needed and records the version, name,
content checksum, safety classification, and application timestamp. A changed
definition for an already-applied version, an unknown version, or a version gap
fails closed.

The current packet intentionally registers no archive tables. P4-03 owns the
archive schema and will add its versioned definitions through this boundary.

## Locking and recovery

The runner begins one PostgreSQL transaction, acquires a deterministic
`pg_advisory_xact_lock`, reads and validates metadata, applies all permitted
pending migrations, records metadata, and commits once. A concurrent attempt
waits on the same transaction-scoped lock and then observes the committed
history. Any statement failure, cancellation, or interruption rolls back the
whole run; a later attempt can retry the unapplied version.

## Safety classification

Migration operations must declare their category. The following categories may
run automatically:

- creating tables;
- adding nullable columns;
- creating views;
- creating safe indexes; and
- updating schema metadata.

Destructive column removal, large table rewrites, geometry SRID changes,
ambiguous deduplication, archive rebuilds, and large historical imports are
manual-maintenance operations. Automatic execution refuses a pending migration
containing any such operation and returns bounded guidance for an operator.
Explicit maintenance approval is required to execute it. Uncertain future
operations should use a manual category; safety is never inferred from an
arbitrary SQL string.

This packet does not execute a live migration, create archive tables, implement
repositories, replicate SQLite state, perform backfill, or change deployment
or startup behavior.
