# SWCFG1012 — Duplicate configuration key

## Meaning
The same key appears more than once in one YAML mapping.

## Trigger
Defining `name:` twice under `station:` triggers this condition.

## Correction or recovery
Keep one reviewed value for the key and remove the duplicate.

## Operational effect
The entire candidate is rejected; neither value silently wins.

## Rationale
Last-value-wins parsing can conceal dangerous service-area or alert-policy mistakes.

## Alternatives or migration
Split distinct concepts into the separate schema fields documented for that section.

## Related diagnostics
No related diagnostic is assigned in catalog version 1.
