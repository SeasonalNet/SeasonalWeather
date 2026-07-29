# SWCFG1007 — YAML anchor is unsupported

## Meaning
The configuration declares a YAML anchor.

## Trigger
`defaults: &shared` declares an unsupported anchor.

## Correction or recovery
Write each supported field explicitly at its authoritative path.

## Operational effect
Parsing stops before schema validation.

## Rationale
Explicit values retain direct source spans, origins, and reviewable ownership.

## Alternatives or migration
Generate a complete candidate outside the runtime, then lint the expanded file.

## Related diagnostics
No related diagnostic is assigned in catalog version 1.
