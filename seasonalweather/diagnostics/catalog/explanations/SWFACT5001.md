# SWFACT5001 — Derived broadcast fact invariant violated

## Meaning

The fact operation detected a contradiction in provenance or deterministic identity.

## Trigger

The operation detects impossible source linkage or different canonical content for the same deterministic fact identity. An unavailable source or failed database or geography operation is not itself a fact invariant violation.

## Correction or recovery

Investigate the sanitized evidence, source linkage, and derivation version. Correct the underlying inconsistency before retrying the operation.

## Operational effect

The affected fact operation fails closed rather than accepting or publishing a contradictory candidate.

## Rationale

A fact cannot be trusted if its claimed sources or deterministic identity cannot be verified.

## Alternatives or migration

Continue without the affected enrichment while correcting the invariant. Do not bypass provenance validation or replace conflicting content.

## Related diagnostics

`SWFACT4001` reports insufficient comparable history without an invariant violation.
