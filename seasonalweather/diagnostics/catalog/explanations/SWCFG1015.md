# SWCFG1015 — Configuration value has the wrong type

## Meaning
A field's YAML type differs from the strict schema type.

## Trigger
`icecast_port: "8000"` supplies a string where an integer is required.

## Correction or recovery
Use `icecast_port: 8000` without quotes.

## Operational effect
The candidate is rejected without coercing the value.

## Rationale
Strict types make operator intent and effective behavior predictable.

## Alternatives or migration
Follow the scalar style in `config/config.yaml` for the reported field.

## Related diagnostics
No related diagnostic is assigned in catalog version 1.
