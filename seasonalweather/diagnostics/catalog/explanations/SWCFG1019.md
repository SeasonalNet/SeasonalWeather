# SWCFG1019 — Fixed-length sequence has the wrong size

## Meaning
A tuple-shaped field has too few or too many items.

## Trigger
A reference point containing only latitude instead of latitude and longitude triggers this condition.

## Correction or recovery
Supply every item in the documented order and remove extras.

## Operational effect
The candidate is rejected before positional values can be misinterpreted.

## Rationale
Fixed-shape sequences require exact arity to preserve field meaning.

## Alternatives or migration
Use the tuple example in the repository configuration as the template.

## Related diagnostics
No related diagnostic is assigned in catalog version 1.
