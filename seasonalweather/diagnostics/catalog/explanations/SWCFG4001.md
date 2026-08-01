# SWCFG4001 — Preflight dependency is degraded or optional

## Meaning
An optional dependency or one with an explicit safe fallback is degraded, unavailable, or unsupported.

## Trigger
An opted-in bounded probe reports non-healthy state without making the candidate unusable.

## Correction or recovery
Inspect the dependency and restore full service when practical.

## Operational effect
The candidate remains valid and preflight may remain ready under explicit policy.

## Rationale
Safe degradation must be visible without being mislabeled as invalid configuration.

## Alternatives or migration
Make the dependency required only when continuity policy truly cannot tolerate its loss.

## Related diagnostics
See `SWCFG3002` for required unavailability and `SWCFG7007` for a probe deadline.
