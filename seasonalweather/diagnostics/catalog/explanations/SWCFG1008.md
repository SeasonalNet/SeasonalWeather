# SWCFG1008 — YAML alias is unsupported

## Meaning
The configuration references a YAML anchor through an alias.

## Trigger
`station: *shared` is an unsupported alias reference.

## Correction or recovery
Replace the alias with the complete supported mapping.

## Operational effect
Parsing stops and no expanded value is accepted.

## Rationale
Alias expansion would obscure which source location owns the effective value.

## Alternatives or migration
Use an external generation workflow that emits plain YAML and lint its output.

## Related diagnostics
No related diagnostic is assigned in catalog version 1.
