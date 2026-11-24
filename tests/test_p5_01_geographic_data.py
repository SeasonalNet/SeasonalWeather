from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from seasonalweather.config import OptionalServiceNetworkConfig, PostgresArchiveConfig
from seasonalweather.database.geographic import (
    GeographicDatasetRepository,
    GeographicDatasetType,
    GeographicDatasetVersion,
    GeographicFeature,
    GeographicFeatureQuery,
    GeographicImportPipeline,
    GeographicImportRequest,
    GeographicLifecycleStatus,
    GeographicValidationStatus,
)


class _Connection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...] | None]] = []
        self.events: list[str] = []
        self.fail_features = False
        self.feature_rows: tuple[tuple[object, ...], ...] = ()

    def execute(self, operation: str, parameters: Sequence[object] | None = None) -> SimpleNamespace:
        self.calls.append((operation, None if parameters is None else tuple(parameters)))
        if self.fail_features and operation.startswith("INSERT INTO geographic_features"):
            raise RuntimeError("feature insert failed")
        if operation.startswith("SELECT id,"):
            return SimpleNamespace(fetchall=lambda: self.feature_rows)
        if operation.startswith("SELECT count(*)"):
            return SimpleNamespace(fetchone=lambda: (2,))
        return SimpleNamespace(fetchone=lambda: None)

    def commit(self) -> None:
        self.events.append("commit")

    def rollback(self) -> None:
        self.events.append("rollback")

    def close(self) -> None:
        self.events.append("close")


class _Connector:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def connect(self, configuration: OptionalServiceNetworkConfig) -> _Connection:
        assert configuration.enabled
        return self.connection


_NET = OptionalServiceNetworkConfig(enabled=True, address="postgres", port=5432, database="weather")
_CONFIG = PostgresArchiveConfig(schema="archive_test")
_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _request(*, features: tuple[GeographicFeature, ...] = ()) -> GeographicImportRequest:
    return GeographicImportRequest(
        dataset_type=GeographicDatasetType.COUNTIES,
        source_identity="nws:counties",
        source_version="2026-01-01",
        import_id=uuid4(),
        imported_at=_NOW,
        coordinate_srid=4326,
        features=features,
        metadata={"source": "test"},
    )


def test_geographic_dataset_types_cover_revision_10_section_34() -> None:
    assert {dataset_type.value for dataset_type in GeographicDatasetType} == {
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
    }


def test_geographic_import_stages_version_and_features_atomically() -> None:
    connection = _Connection()
    repository = GeographicDatasetRepository(_CONFIG, _NET, connector=_Connector(connection))
    features = (
        GeographicFeature(source_feature_id="001", geometry_wkt="POINT (-75 40)", properties={"name": "A"}),
        GeographicFeature(source_feature_id="002", properties={"name": "B"}),
    )

    version = GeographicImportPipeline(repository).stage(_request(features=features))

    assert version.lifecycle_status is GeographicLifecycleStatus.STAGED
    assert version.validation_status is GeographicValidationStatus.PENDING
    assert version.source_identity == "nws:counties"
    assert version.source_version == "2026-01-01"
    assert version.import_id is not None
    assert version.imported_at == _NOW
    assert version.coordinate_srid == 4326
    assert version.metadata == {"source": "test"}
    assert connection.calls[0][0] == "SELECT pg_catalog.set_config('search_path', %s, true)"
    assert any("INSERT INTO geographic_dataset_versions" in operation for operation, _ in connection.calls)
    feature_calls = [call for call in connection.calls if call[0].startswith("INSERT INTO geographic_features")]
    assert len(feature_calls) == 2
    assert any(isinstance(value, Jsonb) for value in feature_calls[0][1] or ())
    assert "ST_GeomFromText(%s, %s)" in feature_calls[0][0]
    assert connection.events == ["commit", "close"]


def test_geographic_import_rolls_back_all_rows_on_feature_failure() -> None:
    connection = _Connection()
    connection.fail_features = True
    repository = GeographicDatasetRepository(_CONFIG, _NET, connector=_Connector(connection))

    with pytest.raises(RuntimeError, match="feature insert failed"):
        _ = GeographicImportPipeline(repository).stage(
            _request(features=(GeographicFeature(source_feature_id="001", properties={}),))
        )

    assert connection.events == ["rollback", "close"]


def test_geographic_feature_query_is_bounded_and_read_only() -> None:
    connection = _Connection()
    feature_id = uuid4()
    connection.feature_rows = ((feature_id, "001", "POINT (-75 40)", {"name": "A"}),)
    repository = GeographicDatasetRepository(_CONFIG, _NET, connector=_Connector(connection))

    result = repository.query_features(
        GeographicFeatureQuery(dataset_version_id=uuid4(), source_feature_ids=("001",), limit=5)
    )

    assert result == (
        GeographicFeature(
            id=feature_id, source_feature_id="001", geometry_wkt="POINT (-75 40)", properties={"name": "A"}
        ),
    )
    assert "source_feature_id = ANY(%s)" in connection.calls[1][0]
    assert connection.events == ["close"]


def test_geographic_feature_count_uses_configured_schema_search_path() -> None:
    connection = _Connection()
    repository = GeographicDatasetRepository(_CONFIG, _NET, connector=_Connector(connection))

    assert repository.feature_count(uuid4()) == 2
    assert connection.calls[0][0] == "SELECT pg_catalog.set_config('search_path', %s, true)"
    assert connection.calls[1][0].startswith("SELECT count(*) FROM geographic_features")
    assert connection.events == ["close"]


@pytest.mark.parametrize(
    ("lifecycle_status", "validation_status", "message"),
    (
        (GeographicLifecycleStatus.ACTIVE, GeographicValidationStatus.PENDING, "Only staged"),
        (GeographicLifecycleStatus.STAGED, GeographicValidationStatus.VALID, "pending validation"),
    ),
)
def test_geographic_repository_never_mutates_active_or_validated_versions(
    lifecycle_status: GeographicLifecycleStatus,
    validation_status: GeographicValidationStatus,
    message: str,
) -> None:
    connection = _Connection()
    repository = GeographicDatasetRepository(_CONFIG, _NET, connector=_Connector(connection))
    version = replace(_request().version(), lifecycle_status=lifecycle_status, validation_status=validation_status)

    with pytest.raises(ValueError, match=message):
        _ = repository.stage_import(version, ())

    assert connection.events == []


def test_geographic_models_reject_unsafe_or_unbounded_input() -> None:
    with pytest.raises(ValueError, match="timestamps"):
        _ = GeographicImportRequest(
            dataset_type=GeographicDatasetType.COUNTIES,
            source_identity="nws:counties",
            source_version="1",
            import_id=uuid4(),
            imported_at=datetime(2026, 1, 1),
            coordinate_srid=4326,
            features=(),
        ).version()

    with pytest.raises(ValueError, match="feature bound"):
        _ = GeographicImportRequest(
            dataset_type=GeographicDatasetType.COUNTIES,
            source_identity="nws:counties",
            source_version="1",
            import_id=uuid4(),
            imported_at=_NOW,
            coordinate_srid=4326,
            features=(GeographicFeature(source_feature_id="001"),),
            max_features=0,
        )

    with pytest.raises(ValueError, match="validity"):
        _ = GeographicDatasetVersion(
            dataset_type=GeographicDatasetType.COUNTIES,
            source_identity="nws:counties",
            source_version="1",
            import_id=uuid4(),
            imported_at=_NOW,
            coordinate_srid=4326,
            valid_from=_NOW,
            valid_until=_NOW.replace(year=2025),
        )

    version_id = uuid4()
    with pytest.raises(ValueError, match="cannot roll back"):
        _ = GeographicDatasetVersion(
            dataset_type=GeographicDatasetType.COUNTIES,
            source_identity="nws:counties",
            source_version="1",
            import_id=uuid4(),
            imported_at=_NOW,
            coordinate_srid=4326,
            id=version_id,
            previous_version_id=version_id,
        )


def test_geographic_feature_query_limit_is_bounded() -> None:
    with pytest.raises(ValueError, match="query limit"):
        _ = GeographicFeatureQuery(dataset_version_id=uuid4(), limit=0)


def test_import_request_bounds_are_enforced() -> None:
    feature = GeographicFeature(source_feature_id="001", properties={"value": "x"})
    with pytest.raises(ValueError, match="feature bound"):
        _ = GeographicImportRequest(
            dataset_type=GeographicDatasetType.COUNTIES,
            source_identity="nws:counties",
            source_version="1",
            import_id=uuid4(),
            imported_at=_NOW,
            coordinate_srid=4326,
            features=(feature,),
            max_features=0,
        )

    with pytest.raises(ValueError, match="byte bound"):
        _ = GeographicImportRequest(
            dataset_type=GeographicDatasetType.COUNTIES,
            source_identity="nws:counties",
            source_version="1",
            import_id=uuid4(),
            imported_at=_NOW,
            coordinate_srid=4326,
            features=(feature,),
            max_bytes=1,
        )
