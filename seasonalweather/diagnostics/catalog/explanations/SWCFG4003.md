# SWCFG4003 — Configuration reload retirement is pending

## Meaning
An old optional resource remains after the new generation committed.

## Trigger
Postcommit retirement does not finish safely.

## Correction or recovery
Retry or reconcile the bounded cleanup while retaining the committed generation.

## Operational effect
The new generation remains active with degraded cleanup.

## Rationale
Postcommit cleanup failure cannot truthfully roll back durable success.

## Alternatives or migration
Use the normal controlled restart workflow if the old resource cannot be retired in process.

## Related diagnostics
See `SWCFG8001` for ambiguous lifecycle recovery.
