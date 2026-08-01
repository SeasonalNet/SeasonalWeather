# SWCFG2004 — Validator report was rejected

## Meaning
An immutable validator report cannot be trusted as admission evidence.

## Trigger
The candidate hash, active generation, report/stamp version, compatibility identities, stage order, or summary is missing, stale, mismatched, malformed, future, or contradictory.

## Correction or recovery
Revalidate the exact candidate with a supported validator against the current active-generation context.

## Operational effect
The report is rejected and no configuration is applied.

## Rationale
Report verification is a fail-closed admission boundary, not configuration commit authority.

## Alternatives or migration
Preserve the candidate bytes and rerun deterministic validation; do not edit or patch a signed-off report.

## Related diagnostics
See `SWCFG2003` for the underlying compatibility classification.
