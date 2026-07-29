# SWCFG1009 — YAML merge key is unsupported

## Meaning
The configuration attempts to merge one mapping into another.

## Trigger
An entry such as `<<: *shared` triggers this condition.

## Correction or recovery
Expand the intended fields explicitly and remove the merge key.

## Operational effect
The candidate is rejected without applying merge precedence.

## Rationale
Merge precedence can hide duplicates and destroys direct field provenance.

## Alternatives or migration
Generate a flattened candidate in a separate tool and lint the result.

## Related diagnostics
No related diagnostic is assigned in catalog version 1.
