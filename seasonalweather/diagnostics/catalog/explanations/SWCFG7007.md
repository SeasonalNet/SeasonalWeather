# SWCFG7007 — Environmental preflight probe timed out

## Meaning
A read-only environmental probe did not complete within its declared bounded deadline.

## Trigger
The preflight runner terminates and reaps an isolated probe process at its
timeout while allowing other probes to report.

## Correction or recovery
Inspect the explicitly configured dependency and retry after it responds within the supported deadline.

## Operational effect
Required probes without fallback block readiness; optional or safely degraded probes warn.

## Rationale
One hung dependency must not suppress the rest of a bounded preflight report.

## Alternatives or migration
Use an injected deterministic probe for offline tests; do not widen timeouts without operational evidence.

## Related diagnostics
See `SWCFG3002` and `SWCFG4001` for required and degraded dependency outcomes.
