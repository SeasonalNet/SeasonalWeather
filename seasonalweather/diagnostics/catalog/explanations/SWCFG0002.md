# SWCFG0002 — Configuration setting is deprecated

## Meaning
A setting remains supported for compatibility but has a documented removal condition.

## Trigger
A versioned deprecation rule finds the exact field in a schema-valid candidate.

## Correction or recovery
Follow the supported replacement or remove an inactive setting using the fenced machine-readable fix when provided.

## Operational effect
Deprecation is nonblocking in P1-14.

## Rationale
This is a general configuration-authoring condition. The `8xxx` class is reserved for runtime lifecycle, startup, recovery, reconciliation, drain, and shutdown conditions.

## Alternatives or migration
Retain the setting temporarily while planning the documented migration.

## Related diagnostics
No related diagnostic is assigned in catalog version 1.
