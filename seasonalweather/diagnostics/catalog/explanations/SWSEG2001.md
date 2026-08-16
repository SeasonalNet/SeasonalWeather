# SWSEG2001 — Segment registry policy is contradictory

## Meaning
An authoritative segment definition contains incompatible policy metadata.

## Trigger
Examples include ambiguous normal or focus ordering, missing ordering for an airable segment, or refresh cadence beyond maximum age.

## Correction or recovery
Correct the affected segment policy and construct the registry again.

## Operational effect
The registry is rejected before runtime consumers can observe contradictory ordering or freshness policy.

## Rationale
Segment ordering and freshness must be deterministic, and a maximum age cannot be shorter than the refresh cadence.

## Alternatives or migration
Use explicit unique positions and distinct valid cadence/max-age values. Do not select a first-wins or last-wins interpretation.

## Related diagnostics
See `SWSEG1001` for duplicate or otherwise invalid authoritative segment input.
