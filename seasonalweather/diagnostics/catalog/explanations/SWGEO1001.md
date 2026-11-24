# SWGEO1001 — Geographic input is invalid

## Meaning

A geographic dataset, feature, or query does not meet the geographic input contract.

## Trigger

The geographic boundary rejects dataset metadata, geometry, CRS/SRID, or query semantics.

## Correction or recovery

Correct the dataset, coordinate reference, or query and submit it again. Inspect bounded validation evidence for the affected version.

## Operational effect

Invalid input cannot activate a dataset. The existing active version and code-based targeting remain unchanged.

## Rationale

Operators need to distinguish this condition from failures owned by adjacent subsystems.

## Alternatives or migration

Keep serving the known-good version or use SAME/UGC without geographic enrichment.

## Related diagnostics

`SWGEO2001` reports a geographic policy rejection rather than malformed input.
