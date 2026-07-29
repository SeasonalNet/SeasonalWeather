# SWCFG1005 — Unsupported YAML tag

## Meaning
The source uses a YAML tag outside SeasonalWeather's safe core scalar and collection types.

## Trigger
A value such as `name: !custom station` triggers this condition.

## Correction or recovery
Replace the tagged value with an ordinary string, number, boolean, null, sequence, or mapping.

## Operational effect
The candidate is rejected before arbitrary tagged construction can occur.

## Rationale
Custom tags are neither portable configuration nor safe runtime constructors.

## Alternatives or migration
Express deployment-specific values through supported fields and documented environment bindings.

## Related diagnostics
No related diagnostic is assigned in catalog version 1.
