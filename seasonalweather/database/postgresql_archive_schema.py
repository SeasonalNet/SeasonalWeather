"""Immutable version-one PostgreSQL archive migration statements."""

# Frozen v1 definitions, independent of the runtime record model.
_V1_TABLES = (
    ("import_origins", ""),
    ("source_deliveries", ""),
    ("product_versions", ""),
    (
        "product_deliveries",
        "product_id uuid NOT NULL REFERENCES product_versions(id), "
        "delivery_id uuid NOT NULL REFERENCES source_deliveries(id), "
        "UNIQUE (product_id, delivery_id),",
    ),
    ("forecast_snapshots", ""),
    ("observation_samples", ""),
    ("alert_lifecycle_versions", ""),
    ("broadcast_history", ""),
    ("airtime_history", "broadcast_id uuid NOT NULL REFERENCES broadcast_history(id),"),
    ("derived_broadcast_facts", "broadcast_id uuid NOT NULL REFERENCES broadcast_history(id),"),
    ("backfill_checkpoints", ""),
    ("reconciliation_anomalies", ""),
)


def _create_table(table: str, relationships: str) -> str:
    import_ref = "" if table == "import_origins" else "import_id uuid REFERENCES import_origins(id),"
    return f"""
CREATE TABLE {table} (
    id uuid PRIMARY KEY,
    {relationships}
    {import_ref}
    origin text NOT NULL CHECK (origin IN (
        'live', 'local_outbox_replay', 'upstream_gap_recovery', 'historical_import', 'legacy_import'
    )),
    recorded_at timestamptz NOT NULL,
    retain_until timestamptz CHECK (retain_until >= recorded_at),
    payload_version integer NOT NULL DEFAULT 1 CHECK (payload_version > 0),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    broadcastable boolean NOT NULL DEFAULT false CHECK (
        NOT broadcastable OR origin NOT IN ('historical_import', 'legacy_import')
    )
)
"""


ARCHIVE_V1_SQL = tuple(_create_table(table, relationships) for table, relationships in _V1_TABLES)
