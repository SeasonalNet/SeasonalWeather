# SWGEO2001 — Geographic dataset change is not permitted

## Meaning

A staged or retained version does not satisfy the requested geographic validation or lifecycle policy.

## Trigger

A feature-count check, predecessor check, or promotion or rollback prerequisite rejects the change.

## Correction or recovery

Review the expected counts, predecessor and validation state. Stage a corrected dataset or select a valid retained rollback version.

## Operational effect

The rejected change cannot replace the active dataset or alter code-based targeting.

## Rationale

Operators need to distinguish this condition from failures owned by adjacent subsystems.

## Alternatives or migration

Keep the existing version active while reviewing the candidate. Do not weaken policy merely to force promotion.

## Related diagnostics

`SWGEO1001` reports invalid geographic input rather than a geographic policy rejection.
