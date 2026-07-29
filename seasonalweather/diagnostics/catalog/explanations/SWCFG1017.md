# SWCFG1017 — Configuration value is too short

## Meaning
A string or sequence has fewer characters or items than its schema permits.

## Trigger
An empty list for a field requiring at least one configured item triggers this condition.

## Correction or recovery
Supply the minimum documented content at the reported path.

## Operational effect
The candidate is rejected as structurally incomplete.

## Rationale
Minimum lengths prevent empty values from masquerading as complete configuration.

## Alternatives or migration
Disable the owning feature through its documented switch when empty content is not meaningful.

## Related diagnostics
No related diagnostic is assigned in catalog version 1.
