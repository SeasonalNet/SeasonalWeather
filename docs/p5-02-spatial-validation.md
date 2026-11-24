# P5-02 spatial validation and safe dataset lifecycle

P5-02 extends the optional geographic import boundary with validation and
lifecycle controls. Staging remains separate from activation: an import is
validated while `staged` and `pending`, and only a validated version can be
promoted.

## Validation

`GeographicDatasetRepository.validate_staged()` runs the PostGIS checks in one
transaction and records bounded evidence in the dataset version metadata:

- present geometry must be non-empty and valid;
- present geometry must use the version coordinate SRID;
- an optional expected SRID and expected feature count must match;
- the feature count must not change beyond the configured comparison ratio from
the current active predecessor (or a retained declared predecessor when no
active version exists);
- a missing, stale, or rejected predecessor is recorded as validation evidence
rather than being treated as a safe comparison.

Null geometry is permitted. Geometry is an enrichment input, not the targeting
authority. PostGIS or database failures raise
`GeographicValidationUnavailable`, roll back the transaction, and leave the
candidate pending so it cannot be promoted accidentally.

## Promotion and rollback

`GeographicImportPipeline.promote()` / `GeographicDatasetRepository.promote()`
lock the candidate and all active versions for its dataset type. The candidate
must be both `valid` and `staged`; existing active rows become `superseded`,
and the candidate becomes `active` while retaining the prior active version
relationship. Migration 3 enforces at most one active version per dataset type
for concurrent first promotions.

`rollback_to()` reactivates a retained, valid `active` or `superseded` version
in the same transaction. The current active version is superseded rather than
deleted. This preserves a rollback target and prevents a failed or malformed
replacement from destroying the last known-good dataset.

## Targeting fallback

SAME and UGC remain authoritative. `SameTargetResolver` accepts an optional
geographic lookup callback only as a final enhancement after UGC, ZoneCounty,
marine crosswalk, and NWS code resolution produce no SAME result. Returned
locations still pass through the existing service-area filter. Missing PostGIS,
missing active geometry, lookup errors, and empty GIS results return no GIS
locations, allowing the existing code-based path to continue unchanged.

PostgreSQL/PostGIS remains optional; the SQLite/local operational path and
basic broadcast targeting do not depend on this feature.

## Diagnostics and coverage boundary

Validation rejection remains durable, bounded evidence in the version metadata.
It does not automatically create a runtime occurrence. `SWGEO1001` identifies
invalid admitted geographic input (including geometry and CRS/SRID semantics).
`SWGEO2001` identifies a geographic policy rejection, such as a feature-count
limit or an ineligible promotion or rollback target. Neither can replace the
active dataset. Malformed upstream products retain their source diagnostic
when rejected by ingest or normalization.

Preflight reports confirmed PostGIS extension incompatibility as `SWDB2001`;
connection and database operation failures retain `SWDB3001`. A geographic
consequence may be reported separately only when configured capability is
materially degraded and the originating failure is preserved. Disabled GIS,
missing geometry, and empty results do not generate per-lookup occurrences.
The optional callback does not itself qualify or monitor a PostGIS capability.

P5-02 tests cover the acceptance paths and failure boundaries for validation,
predecessor selection, promotion, rollback, active-version reads, and GIS
fallback. Real PostGIS execution and concurrent database integration remain
infrastructure-level checks rather than portable unit-test requirements.
