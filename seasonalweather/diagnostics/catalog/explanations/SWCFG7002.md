# SWCFG7002 — Configuration nesting is too deep

## Meaning
The YAML document exceeds the compiler's nesting-depth limit.

## Trigger
Generated mappings nested through dozens of levels can trigger this condition.

## Correction or recovery
Flatten the candidate to the documented schema structure.

## Operational effect
Parsing stops before deeply nested input consumes excessive resources.

## Rationale
The supported schema is shallow and does not need unbounded recursion.

## Alternatives or migration
Compare the reported structure with `config/config.yaml`.

## Related diagnostics
No related diagnostic is assigned in catalog version 1.
