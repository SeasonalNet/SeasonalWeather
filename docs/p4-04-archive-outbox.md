# SQLite archive outbox (P4-04)

With SQLite enabled and `network.postgresql.enabled: true`, every active-alert
state replacement also queues a version-1 `alert_lifecycle_versions` snapshot
in the same SQLite transaction. This includes empty snapshots when alerts clear.
The payload contains `active_alerts` and excludes local audio paths. Replay
records have `local_outbox_replay` origin and are not broadcastable. This packet
does not archive source deliveries, forecast or observation data, or airtime.

Apply the existing PostgreSQL archive migration explicitly before enabling the
archive. The controller never runs PostgreSQL migrations automatically. An
optional supervised task reads up to 32 SQLite records in enqueue order, closes
the SQLite read connection, then commits the batch to PostgreSQL. It deletes
only the committed IDs in a new SQLite transaction. Stable UUIDs make retry
safe when a commit succeeds but the acknowledgment or local deletion is lost.
A failed batch stays queued and retries with 1–60 seconds of backoff.
The outbox retry diagnostic is `SWDB6001` for structured PostgreSQL access
denials (SQLSTATE class `28` or `42501`) and `SWDB3001` for other failures.

The optional `postgresql` health component reports `outbox_pending`,
`outbox_oldest_at`, and `outbox_retrying`. An outage does not make the controller
unready, but pending records are not expired or capped: monitor backlog age,
count, and local disk capacity. If PostgreSQL is disabled, the controller neither
queues new snapshots nor drains existing records; re-enabling resumes retained
work. Do not remove outbox rows as routine maintenance.
