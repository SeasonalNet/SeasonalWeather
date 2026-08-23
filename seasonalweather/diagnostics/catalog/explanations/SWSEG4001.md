# SWSEG4001 — Segment refresh fallback was used

## Meaning
A failed segment refresh retained last-known-good audio or marked a bounded placeholder.

## Trigger
The segment refresher applies the registry-declared failure policy after a refresh dependency fails.

## Correction or recovery
Inspect the related `SWSEG3001`, source, or synthesis diagnostic and allow the next bounded refresh to replace the fallback.

## Operational effect
Broadcast cycle processing continues, but the affected segment is degraded or unavailable until refresh succeeds.

## Rationale
The explicit fallback preserves broadcast continuity while making freshness degradation actionable instead of leaving it visible only in logs.

## Alternatives or migration
Do not disable freshness fencing or replace the fallback with an unvalidated artifact. Correct the underlying refresh failure and allow the registry policy to recover the segment.

## Related diagnostics
See `SWSEG3001` for the underlying refresh failure and `SWSEG8001` for publication reconciliation.
