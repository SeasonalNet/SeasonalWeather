# SWCFG2003 — Configuration compatibility requirement is unsupported

## Meaning
A supplied software, schema, protocol, deployment, or capability identity is incompatible with the supported range.

## Trigger
The pure compatibility analyzer classifies an identity as older, newer, malformed, missing, contradictory, or unavailable without a viable fallback.

## Correction or recovery
Use supported explicit versions or provide an authorized, qualified capability and compatible fallback.

## Operational effect
Unsupported required compatibility blocks candidate validity; explicit advisories and optional capability loss do not block by default.

## Rationale
Compatibility is separate from syntax, schema, and temporary environmental availability.

## Alternatives or migration
Upgrade or downgrade only according to documented version support.

## Related diagnostics
See `SWCFG2004` when the incompatible evidence is an externally supplied validator report.
