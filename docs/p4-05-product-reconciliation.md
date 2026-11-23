# Archive product reconciliation (P4-05)

Controller archive writers can call `ProductReconciliation.ingest` with a
`ProductDelivery`. Supply a stable receipt key within each source; an API
product ID is identity evidence, not a receipt key. Supply a timezone-aware
issuance time, full WMO heading, AWIPS ID, issuing office, and VTEC event IDs
where available. Missing fields do not create wildcard aliases.

A delivery preserves raw content, origin, import reference, and identity
evidence. Replaying the same source receipt, origin, import reference, and
normalized content returns the existing version. Independent deliveries remain
separate even when their content converges on one version. Content hashing
normalizes line endings and whitespace within lines and preserves case and
internal line boundaries. Use product text without transport wrappers.

An API ID, a WMO/office/issue/AWIPS combination, or a VTEC/office/issue/AWIPS
combination can correlate distinct content. Each version stays accessible by
UUID; `versions_for` lists versions matching the supplied evidence. An anomaly
records each observed conflicting version pair and the delivery that first
exposed it. Writes are atomic and ingestion serializes on the source delivery
table. This is an archive-only entry point, not a live feed or backfill job.

No schema migration is required beyond P4-03. Archive ingestion sets no
broadcastable flags, and never calls the broadcast path. P4-04 alert snapshots
continue using their independent local outbox and replay path.
