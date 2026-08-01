# SWCFG3002 — Required preflight dependency is unavailable

## Meaning
A required explicitly configured dependency cannot be reached or inspected and no safe fallback is available.

## Trigger
An opted-in bounded read-only probe reports an unavailable or unsupported required dependency.

## Correction or recovery
Restore the configured dependency, configure a supported fallback, and rerun preflight.

## Operational effect
Syntax and schema validity are unchanged, but environmental readiness is blocked.

## Rationale
Temporary dependency state is environmental evidence rather than a parse failure.

## Alternatives or migration
Mark a dependency optional only when degraded operation is explicitly safe.

## Related diagnostics
See `SWCFG4001` for safe degradation and `SWCFG7007` for a probe deadline.
