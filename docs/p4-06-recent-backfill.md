# Recent archive backfill

When archive replication is enabled, the controller starts an optional recovery
pass after reaching service readiness. Recovery records are archive-only and
carry `upstream_gap_recovery` provenance. The pass does not update local active
alerts, enqueue broadcast audio, or import deep history.

The pass requests active alerts by configured area, zone metadata for up to
eight forecast zones, latest observations for up to eight stations, and up to
five HWO references from the primary office within the previous two days.
Requests are serial and paced; a failed resource receives one retry. Snapshot
records and their checkpoint commit together. Restarting repeats upstream reads;
stable IDs prevent duplicate archive rows and product deliveries.

Each startup revalidates product text within the same five-reference, two-day
window, even for known IDs: NWS does not document that text is immutable for
an ID. Reconciliation deduplicates unchanged text and preserves changed text
as a distinct archive version. A checkpoint is a receipt of a committed
version, not a reason to skip this bounded recheck.
