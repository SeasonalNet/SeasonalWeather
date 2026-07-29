# SWCFG7004 — Configuration collection contains too many items

## Meaning
One YAML sequence or mapping exceeds its bounded item count.

## Trigger
A generated list containing more than ten thousand entries can trigger this condition.

## Correction or recovery
Reduce the collection to supported deployment values.

## Operational effect
The candidate is rejected without iterating an unbounded collection.

## Rationale
Per-collection limits complement the total node bound.

## Alternatives or migration
Use the owning database or source adapter for dynamic large collections.

## Related diagnostics
No related diagnostic is assigned in catalog version 1.
