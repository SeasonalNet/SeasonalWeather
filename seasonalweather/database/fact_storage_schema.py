"""Immutable version-four PostgreSQL candidate migration statement."""

FACT_CANDIDATES_V1_SQL = """
CREATE TABLE broadcast_fact_candidates (
    id uuid PRIMARY KEY,
    valid_from timestamptz NOT NULL,
    expires_at timestamptz NOT NULL CHECK (valid_from < expires_at),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    current_table text NOT NULL CHECK (
        current_table IN ('alert_lifecycle_versions', 'forecast_snapshots', 'observation_samples')
    ),
    current_id uuid NOT NULL,
    previous_table text NOT NULL CHECK (
        previous_table IN ('alert_lifecycle_versions', 'forecast_snapshots', 'observation_samples')
    ),
    previous_id uuid NOT NULL,
    current_alert_id uuid GENERATED ALWAYS AS (
        CASE WHEN current_table = 'alert_lifecycle_versions' THEN current_id END
    ) STORED REFERENCES alert_lifecycle_versions(id),
    current_forecast_id uuid GENERATED ALWAYS AS (
        CASE WHEN current_table = 'forecast_snapshots' THEN current_id END
    ) STORED REFERENCES forecast_snapshots(id),
    current_observation_id uuid GENERATED ALWAYS AS (
        CASE WHEN current_table = 'observation_samples' THEN current_id END
    ) STORED REFERENCES observation_samples(id),
    previous_alert_id uuid GENERATED ALWAYS AS (
        CASE WHEN previous_table = 'alert_lifecycle_versions' THEN previous_id END
    ) STORED REFERENCES alert_lifecycle_versions(id),
    previous_forecast_id uuid GENERATED ALWAYS AS (
        CASE WHEN previous_table = 'forecast_snapshots' THEN previous_id END
    ) STORED REFERENCES forecast_snapshots(id),
    previous_observation_id uuid GENERATED ALWAYS AS (
        CASE WHEN previous_table = 'observation_samples' THEN previous_id END
    ) STORED REFERENCES observation_samples(id)
)
"""
