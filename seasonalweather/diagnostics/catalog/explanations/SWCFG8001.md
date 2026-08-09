# SWCFG8001 — Configuration reload requires reconciliation

## Meaning
Durable reload evidence is incomplete or ambiguous at a lifecycle boundary.

## Trigger
Rollback, commit completion, startup recovery, or retirement cannot be proven from all required fences.

## Correction or recovery
Reconcile the journal and durable active generation idempotently before another commit.

## Operational effect
Success is not inferred and affected work remains bounded for recovery.

## Rationale
No single path or in-memory reference is sufficient commit evidence.

## Alternatives or migration
Use a controlled restart after preserving the reload journal when automatic reconciliation cannot converge.

## Related diagnostics
See `SWCFG3003`, `SWCFG4003`, and `SWCFG7008` for the originating boundary.
