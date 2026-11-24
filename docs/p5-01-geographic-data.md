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

Staging and query admission report invalid geographic input as `SWGEO1001`
and rejection by geographic dataset policy as `SWGEO2001`. These are
boundary diagnostics, not durable runtime occurrences. Source adapters still
own ingest and normalization failures before geographic admission. Database
connectivity, transactions, migrations, and storage remain `SWDB` concerns;
confirmed PostGIS incompatibility is `SWDB2001`. See
[P5-02](p5-02-spatial-validation.md) for validation and lifecycle behavior.
