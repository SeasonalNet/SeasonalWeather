# SWCFG0001 — Configuration advisory has a supported correction

## Meaning
A valid setting has one concrete, supported alternative that is clearer or more canonical.

## Trigger
The validator recognizes a bounded advisory rule, such as a supported backend spelling alias.

## Correction or recovery
Review the machine-readable fix and apply it only when its source and old-value fences still match.

## Operational effect
The advisory does not block validation or environmental readiness by default.

## Rationale
Suggestions are limited to deterministic corrections rather than subjective style advice.

## Alternatives or migration
Retain the supported value until the operator chooses to adopt the canonical alternative.

## Related diagnostics
No related diagnostic is assigned in catalog version 1.
