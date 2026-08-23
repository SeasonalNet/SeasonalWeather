# SWSEG3001 — Segment refresh dependency failed

## Meaning
A segment could not be refreshed because its builder, synthesis operation, or segment-store dependency failed.

## Trigger
The required segment refresher catches an execution failure while producing or storing a bounded segment candidate.

## Correction or recovery
Inspect the bounded exception evidence and the affected segment's freshness record. The refresher will retry according to its registry policy.

## Operational effect
The affected segment may remain stale while the existing cycle continues under its freshness and fallback policy.

## Rationale
Segment refresh failures need an operational identity at the segment authority boundary so stale audio and bounded retry are distinguishable from an uncaught controller failure.

## Alternatives or migration
Do not bypass the segment registry or publish an unvalidated candidate. Correct the underlying builder, synthesis, or store dependency and let the bounded refresh policy retry.

## Related diagnostics
See `SWSEG4001` when fallback is used and `SWSEG8001` when publication evidence is ambiguous.
