# SWCFG2002 — Configuration semantic invariant is contradictory

## Meaning
Fields that are individually well typed form an impossible or unsafe combination.

## Trigger
Examples include unordered token lifetimes, a required disabled job store, overlapping database paths, or impossible deadline relationships.

## Correction or recovery
Use the primary and related source locations to make the values mutually consistent.

## Operational effect
The candidate is invalid and cannot be admitted.

## Rationale
Cross-field policy belongs to semantic validation rather than YAML parsing or structural schema validation.

## Alternatives or migration
When several corrections are valid, choose explicitly; no speculative fix is emitted.

## Related diagnostics
No related diagnostic is assigned in catalog version 1.
