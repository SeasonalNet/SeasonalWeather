# P5-01 versioned geographic data

P5-01 adds an optional PostgreSQL/PostGIS staging boundary for the geographic
sources needed by later enrichment work:

- counties and forecast/fire-weather zones;
- marine and offshore zones;
- alert polygons and station service areas;
- transmitters and optional coverage models;
- observation sites, river gauges, and reference points.

Migration 2 creates `geographic_dataset_versions` and
`geographic_features` in the configured PostgreSQL schema. Every version keeps
its source identity and version, import-origin UUID, import and validity times,
coordinate SRID, validation status, lifecycle status, metadata, and a previous
version relationship. Features retain a source feature ID, PostGIS geometry,
and JSON properties.

`GeographicImportPipeline.stage()` is intentionally bounded and atomic. It
writes only `staged` versions with `pending` validation and never changes an
active dataset. Read-only feature queries require an explicit dataset version
and use bounded, parameterized filters. Feature-count and byte limits,
timezone-aware timestamps,
source identifiers, SRID bounds, and JSON object sizes are checked before the
write. Geometry validation, implausible-count checks, promotion, active-version
selection, and rollback are P5-02 responsibilities.

PostgreSQL remains optional. SQLite remains the local operational store, and
missing PostgreSQL/PostGIS does not change the basic broadcast path.

## Diagnostics boundary

P5-01 defines persistence and staging models; it does not own runtime
occurrence emission. Existing PostgreSQL startup, migration, and persistence
failures use the active `SWDB3001` catalog diagnostic where they cross an
operational boundary. P5-01 does not add a GIS-specific namespace or code:
validation, promotion, rollback, and geometry fallback belong to P5-02 and
later integration work, where any new actionable conditions can be assigned
through the established `SWDB` catalog governance.
