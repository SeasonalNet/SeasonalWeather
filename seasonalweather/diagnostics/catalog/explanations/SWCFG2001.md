# SWCFG2001 — Unsupported configuration schema version

## Meaning
`config_schema` is a positive integer, but this release does not implement that version.

## Trigger
`config_schema: 2` triggers this condition in a release supporting only schema 1.

## Correction or recovery
Use `config_schema: 1` with fields valid for schema 1.

## Operational effect
Compilation stops before another schema can be guessed.

## Rationale
Treating an unknown version as the latest supported schema is unsafe and nondeterministic.

## Alternatives or migration
Upgrade SeasonalWeather only when the target release documents support for the newer schema.

## Related diagnostics
No related diagnostic is assigned in catalog version 1.
