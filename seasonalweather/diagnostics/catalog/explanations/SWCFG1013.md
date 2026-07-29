# SWCFG1013 — Invalid configuration schema identifier

## Meaning
`config_schema` is not a positive integer.

## Trigger
Values such as `config_schema: "1"` or `config_schema: 0` trigger this condition.

## Correction or recovery
Use the unquoted integer `config_schema: 1`.

## Operational effect
Schema selection fails and the candidate is rejected.

## Rationale
Strict schema identity prevents implicit coercion and accidental version selection.

## Alternatives or migration
Omitting the field retains the documented legacy schema-1 behavior, but explicit versioning is preferred.

## Related diagnostics
No related diagnostic is assigned in catalog version 1.
