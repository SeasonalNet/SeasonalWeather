# SWCFG1003 — Malformed YAML syntax

## Meaning
The YAML parser cannot form a complete document from the source.

## Trigger
An unclosed sequence such as `stations: [KXYZ` is malformed.

## Correction or recovery
Close the sequence, for example `stations: [KXYZ]`, and run config lint again.

## Operational effect
Compilation stops before schema validation.

## Rationale
Guessing through malformed syntax could change alert or service-area behavior.

## Alternatives or migration
Use block-style YAML when nested flow syntax is difficult to review.

## Related diagnostics
No related diagnostic is assigned in catalog version 1.
