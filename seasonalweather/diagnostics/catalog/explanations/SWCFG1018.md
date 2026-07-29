# SWCFG1018 — Configuration value is too long

## Meaning
A string or sequence exceeds its schema length limit.

## Trigger
A collection with more entries than the documented field permits triggers this condition.

## Correction or recovery
Remove excess items or shorten the value while preserving intended behavior.

## Operational effect
The candidate is rejected before oversized data reaches runtime services.

## Rationale
Schema bounds keep configuration processing and public output predictable.

## Alternatives or migration
Use a documented grouping or selection field when one is available.

## Related diagnostics
No related diagnostic is assigned in catalog version 1.
