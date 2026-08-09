# SWCFG3003 — Configuration reload candidate or preparation failed

## Meaning
Candidate evidence or replacement resources could not be handled safely.

## Trigger
Persistence, verification, report transport, or preparation fails before commit.

## Correction or recovery
Inspect service-owned storage and the bounded reload audit, correct the dependency, and retry.

## Operational effect
The old generation remains active.

## Rationale
Partial preparation is not configuration success.

## Alternatives or migration
Use dry-run validation to separate candidate errors from runtime preparation failures.

## Related diagnostics
See `SWCFG8001` when durable evidence requires reconciliation.
