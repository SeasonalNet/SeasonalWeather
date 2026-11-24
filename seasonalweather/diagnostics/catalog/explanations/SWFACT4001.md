# SWFACT4001 — Insufficient history for a requested broadcast fact

## Meaning

There is not enough usable and comparable history to compute a requested derived broadcast fact.

## Trigger

An enabled and requested fact cannot be evaluated because compatible source records are unavailable. A valid comparison with no material change, disabled enrichment, and an empty lookup unrelated to a requested fact do not trigger this diagnostic.

## Correction or recovery

Check whether the fact kind has a supported history source and whether its records meet the comparison and freshness requirements. Retry when suitable evidence is available.

## Operational effect

The requested fact is omitted. Basic broadcasting and authoritative code-based alert targeting are unaffected.

## Rationale

The system does not guess at a change when comparative evidence is insufficient. Source, database, and geographic failures retain their owning diagnostics.

## Alternatives or migration

Continue without this enrichment or use a supported source of compatible history.

## Related diagnostics

`SWFACT5001` reports a violated fact invariant, not missing history.
