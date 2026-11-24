# Optional PostgreSQL archive preflight

P4-01 introduces PostgreSQL as an optional archive dependency. The controller
continues to use its local SQLite state for routine broadcast continuity; a
PostgreSQL outage must not prevent the station from starting or broadcasting.

The repository configuration declares the archive role under `database.postgres`:

```yaml
database:
  postgres:
    mode: optional
    role: archive
```

The connection endpoint remains separately controlled by
`network.postgresql.enabled`, `address`, `port`, and `database`. It is disabled
by default. PostgreSQL authentication follows the deployment's libpq
credential mechanism; passwords and DSNs are not stored in YAML or emitted in
diagnostics.

When the endpoint is enabled, startup runs one bounded preflight. It verifies
connectivity, configured TLS state, authentication, server version, database
and schema identity, schema ownership, required privileges, migration metadata,
PostGIS extension and SRID 4326 availability, transactional read/write
behavior, and client/server clock divergence. The write test uses a temporary
table and rolls the transaction back before the connection closes.

The result is exposed as the optional `postgresql` health component. A
successful preflight is `healthy`; a failed preflight is `unavailable` and
produces bounded diagnostics. A confirmed missing PostGIS extension or missing
required SRID 4326 produces `SWDB2001`. A PostgreSQL SQLSTATE in class `28`
(authentication/authorization) or `42501` (insufficient privilege), or a
preflight probe that confirms a required schema ownership or privilege check
failed, produces `SWDB6001`. During connection startup, psycopg/libpq can
report an authentication failure as an `OperationalError` without SQLSTATE.
Such failures remain `SWDB3001`, even when the password was rejected; driver
error text is not used for classification. These diagnostics do not affect
required readiness. Before activating the archive, provision the database,
install PostGIS, and give the controller role ownership of the configured
schema and the privileges required by preflight. Startup then runs the
[safe migration path](p4-02-postgresql-migrations.md).

If PostgreSQL becomes unavailable, keep the controller running on local
SQLite. Inspect the PostgreSQL health component and its diagnostics; monitor the
[outbox backlog](p4-04-archive-outbox.md) and local disk capacity. Restore
connectivity and confirm pending rows drain. If startup qualification failed,
restart the controller in an approved window to rerun preflight and safe
migrations. Do not delete queued rows to clear a backlog.
