"""Version-one PostGIS geographic dataset schema.

The schema deliberately stores only staged dataset versions.  Promotion to an
active dataset is a separate P5-02 concern.
"""

_GEOGRAPHIC_DATASET_TYPES = (
    "counties",
    "forecast_zones",
    "fire_weather_zones",
    "marine_zones",
    "offshore_zones",
    "alert_polygons",
    "service_areas",
    "transmitters",
    "coverage_models",
    "observation_sites",
    "river_gauges",
    "reference_points",
)


def _quoted_values(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


GEOGRAPHIC_V1_SQL = (
    f"""
CREATE TABLE geographic_dataset_versions (
    id uuid PRIMARY KEY,
    dataset_type text NOT NULL CHECK (dataset_type IN ({_quoted_values(_GEOGRAPHIC_DATASET_TYPES)})),
    source_identity text NOT NULL CHECK (length(btrim(source_identity)) BETWEEN 1 AND 512),
    source_version text NOT NULL CHECK (length(btrim(source_version)) BETWEEN 1 AND 256),
    import_id uuid NOT NULL REFERENCES import_origins(id),
    imported_at timestamptz NOT NULL,
    valid_from timestamptz,
    valid_until timestamptz CHECK (valid_until IS NULL OR valid_from IS NULL OR valid_until >= valid_from),
    coordinate_srid integer NOT NULL CHECK (coordinate_srid BETWEEN 0 AND 998999),
    validation_status text NOT NULL CHECK (validation_status IN ('pending', 'valid', 'invalid')),
    lifecycle_status text NOT NULL CHECK (lifecycle_status IN ('staged', 'active', 'superseded', 'rejected')),
    previous_version_id uuid REFERENCES geographic_dataset_versions(id),
    metadata jsonb NOT NULL CHECK (jsonb_typeof(metadata) = 'object'),
    CHECK (previous_version_id IS NULL OR previous_version_id <> id)
)
""",
    """
CREATE TABLE geographic_features (
    id uuid PRIMARY KEY,
    dataset_version_id uuid NOT NULL REFERENCES geographic_dataset_versions(id) ON DELETE CASCADE,
    source_feature_id text NOT NULL CHECK (length(btrim(source_feature_id)) BETWEEN 1 AND 512),
    geometry geometry(Geometry),
    properties jsonb NOT NULL CHECK (jsonb_typeof(properties) = 'object'),
    UNIQUE (dataset_version_id, source_feature_id)
)
""",
    """
CREATE INDEX geographic_features_dataset_version_idx
    ON geographic_features (dataset_version_id)
""",
)

# P5-02 lifecycle invariant: repository promotion assumes one active version
# per dataset type, including when the first promotion races with another one.
GEOGRAPHIC_V2_SQL = (
    """
CREATE UNIQUE INDEX geographic_dataset_versions_one_active_idx
    ON geographic_dataset_versions (dataset_type)
    WHERE lifecycle_status = 'active'
""",
)

__all__ = ["GEOGRAPHIC_V1_SQL", "GEOGRAPHIC_V2_SQL"]
