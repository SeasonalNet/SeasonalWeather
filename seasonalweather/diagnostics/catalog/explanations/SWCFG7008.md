# SWCFG7008 — Configuration reload safe-point timed out

## Meaning
Controller activity did not quiesce before the reload deadline.

## Trigger
Alert, synthesis, publication, result, conductor, refresh, or lifecycle blockers remain active.

## Correction or recovery
Retry after the blocker clears or choose a suitable bounded timeout.

## Operational effect
The attempt is deferred and the old generation remains active.

## Rationale
Reload must not force a partial commit through safety-critical work.

## Alternatives or migration
Use dry-run when only validation and classification are needed.

## Related diagnostics
See `SWCFG8001` only when recovery evidence becomes ambiguous.
