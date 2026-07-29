# SWCFG1006 — Non-string mapping key

## Meaning
A YAML mapping key is not a string.

## Trigger
The mapping entry `1: value` uses an integer key.

## Correction or recovery
Use a named field such as `station_1: value` where the schema permits that key.

## Operational effect
The candidate is rejected and no ambiguous configuration path is created.

## Rationale
SeasonalWeather paths and schema fields use string keys with deterministic escaping.

## Alternatives or migration
Use a sequence for ordered unnamed values when the documented schema calls for one.

## Related diagnostics
No related diagnostic is assigned in catalog version 1.
