# SWCFG1020 — Unknown configuration field

## Meaning
Configuration schema 1 does not define the reported field.

## Trigger
Misspelled, obsolete, or local-only keys trigger this condition.

## Correction or recovery
Remove the field or replace it with the supported field documented for the same purpose.

## Operational effect
The candidate is rejected instead of silently ignoring the key.

## Rationale
Silent ignores can make operators believe alert or retention policy changed when it did not.

## Alternatives or migration
For historical fields, consult the intentional incompatibility table in `docs/configuration-compiler.md`.

## Related diagnostics
No related diagnostic is assigned in catalog version 1.
