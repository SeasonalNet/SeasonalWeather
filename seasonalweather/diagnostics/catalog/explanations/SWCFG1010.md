# SWCFG1010 — Root value is not a mapping

## Meaning
The top-level YAML value is not an object of named configuration sections.

## Trigger
A document containing only `- station` has a sequence root.

## Correction or recovery
Use a mapping root beginning with fields such as `config_schema:` and `station:`.

## Operational effect
The candidate cannot enter schema validation.

## Rationale
The configuration schema is organized by named top-level sections.

## Alternatives or migration
Copy the repository example and edit only supported values.

## Related diagnostics
No related diagnostic is assigned in catalog version 1.
