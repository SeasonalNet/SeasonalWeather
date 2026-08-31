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
produces bounded `SWDB3001` diagnostic occurrences. Neither state participates
in required readiness. Migration execution, archive schema creation, and
replication are intentionally deferred to later Phase 4 packets.
