"""Versioned geographic dataset models and bounded staging pipeline.

This module owns dataset-version and feature persistence for P5-01.  It does
not validate geometry, select an active version, or roll back a dataset; those
operations belong to P5-02.
"""

from __future__ import annotations

import json
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import cast
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from ..config import OptionalServiceNetworkConfig, PostgresArchiveConfig
from .postgresql import PostgresConnection, PostgresConnector, default_postgres_connector

_MAX_SOURCE_IDENTITY = 512
_MAX_SOURCE_VERSION = 256
_MAX_FEATURE_ID = 512
_MAX_WKT_BYTES = 1_000_000
_MAX_PROPERTY_BYTES = 1_000_000
_MAX_IMPORT_FEATURES = 100_000
_MAX_IMPORT_BYTES = 100_000_000
_MAX_QUERY_LIMIT = 10_000


class GeographicDatasetType(StrEnum):
    COUNTIES = "counties"
    FORECAST_ZONES = "forecast_zones"
    FIRE_WEATHER_ZONES = "fire_weather_zones"
    MARINE_ZONES = "marine_zones"
    OFFSHORE_ZONES = "offshore_zones"
    ALERT_POLYGONS = "alert_polygons"
    SERVICE_AREAS = "service_areas"
    TRANSMITTERS = "transmitters"
    COVERAGE_MODELS = "coverage_models"
    OBSERVATION_SITES = "observation_sites"
    RIVER_GAUGES = "river_gauges"
    REFERENCE_POINTS = "reference_points"


class GeographicValidationStatus(StrEnum):
    PENDING = "pending"
    VALID = "valid"
    INVALID = "invalid"


class GeographicLifecycleStatus(StrEnum):
    STAGED = "staged"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


@dataclass(frozen=True, kw_only=True)
class GeographicFeature:
    """One source-addressable feature in a staged dataset version."""

    source_feature_id: str
    properties: dict[str, object] = field(default_factory=dict)
    geometry_wkt: str | None = None
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        _bounded_text(self.source_feature_id, _MAX_FEATURE_ID, "source feature ID")
        _json_object_bytes(self.properties, _MAX_PROPERTY_BYTES, "feature properties")
        if self.geometry_wkt is not None:
            if not self.geometry_wkt.strip():
                raise ValueError("Geographic feature geometry cannot be empty")
            if len(self.geometry_wkt.encode("utf-8")) > _MAX_WKT_BYTES:
                raise ValueError("Geographic feature geometry is too large")


@dataclass(frozen=True, kw_only=True)
class GeographicDatasetVersion:
    """Immutable metadata for one imported geographic dataset version."""

    dataset_type: GeographicDatasetType
    source_identity: str
    source_version: str
    import_id: UUID
    imported_at: datetime
    coordinate_srid: int
    id: UUID = field(default_factory=uuid4)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    validation_status: GeographicValidationStatus = GeographicValidationStatus.PENDING
    lifecycle_status: GeographicLifecycleStatus = GeographicLifecycleStatus.STAGED
    previous_version_id: UUID | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_version_source(self.source_identity, self.source_version)
        _validate_version_timestamps(self.imported_at, self.valid_from, self.valid_until)
        _validate_version_srid(self.coordinate_srid)
        if self.previous_version_id == self.id:
            raise ValueError("A geographic version cannot roll back to itself")
        _json_object_bytes(self.metadata, _MAX_PROPERTY_BYTES, "geographic dataset metadata")


@dataclass(frozen=True, kw_only=True)
class GeographicFeatureQuery:
    """Bounded, read-only query for features in one explicit dataset version."""

    dataset_version_id: UUID
    source_feature_ids: tuple[str, ...] = ()
    limit: int = 1_000

    def __post_init__(self) -> None:
        source_feature_ids = tuple(self.source_feature_ids)
        object.__setattr__(self, "source_feature_ids", source_feature_ids)
        if not 1 <= self.limit <= _MAX_QUERY_LIMIT:
            raise ValueError("Geographic feature query limit is invalid")
        for source_feature_id in source_feature_ids:
            _bounded_text(source_feature_id, _MAX_FEATURE_ID, "source feature ID")


@dataclass(frozen=True, kw_only=True)
class GeographicImportRequest:
    """Bounded maintenance input; staging never promotes a dataset."""

    dataset_type: GeographicDatasetType
    source_identity: str
    source_version: str
    import_id: UUID
    imported_at: datetime
    coordinate_srid: int
    features: tuple[GeographicFeature, ...]
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    previous_version_id: UUID | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    max_features: int = _MAX_IMPORT_FEATURES
    max_bytes: int = _MAX_IMPORT_BYTES

    def __post_init__(self) -> None:
        features = tuple(self.features)
        object.__setattr__(self, "features", features)
        if self.max_features < 1 or self.max_features > _MAX_IMPORT_FEATURES:
            raise ValueError("Geographic import feature bound is invalid")
        if self.max_bytes < 1 or self.max_bytes > _MAX_IMPORT_BYTES:
            raise ValueError("Geographic import byte bound is invalid")
        if len(features) > self.max_features:
            raise ValueError("Geographic import exceeds its feature bound")
        size = sum(_feature_size(feature) for feature in features)
        if size > self.max_bytes:
            raise ValueError("Geographic import exceeds its byte bound")

    def version(self) -> GeographicDatasetVersion:
        return GeographicDatasetVersion(
            dataset_type=self.dataset_type,
            source_identity=self.source_identity,
            source_version=self.source_version,
            import_id=self.import_id,
            imported_at=self.imported_at,
            coordinate_srid=self.coordinate_srid,
            valid_from=self.valid_from,
            valid_until=self.valid_until,
            previous_version_id=self.previous_version_id,
            metadata=self.metadata,
        )


class GeographicDatasetRepository:
    """Persist staged geographic versions and their features atomically."""

    def __init__(
        self,
        configuration: PostgresArchiveConfig,
        network: OptionalServiceNetworkConfig,
        *,
        connector: PostgresConnector | None = None,
    ) -> None:
        if not network.enabled:
            raise ValueError("PostgreSQL geographic access is disabled by configuration")
        self._schema: str = _identifier(configuration.schema, "database.postgres.schema")
        self._network: OptionalServiceNetworkConfig = network
        self._connector: PostgresConnector = connector or default_postgres_connector()

    def stage_import(
        self,
        version: GeographicDatasetVersion,
        features: Sequence[GeographicFeature],
    ) -> GeographicDatasetVersion:
        """Write one staged version and all features in one transaction."""

        if version.lifecycle_status is not GeographicLifecycleStatus.STAGED:
            raise ValueError("Only staged geographic versions may be imported")
        if version.validation_status is not GeographicValidationStatus.PENDING:
            raise ValueError("New geographic imports must have pending validation")
        normalized = tuple(features)
        if len(normalized) > _MAX_IMPORT_FEATURES:
            raise ValueError("Geographic import exceeds its feature bound")
        if sum(_feature_size(feature) for feature in normalized) > _MAX_IMPORT_BYTES:
            raise ValueError("Geographic import exceeds its byte bound")
        statement = (
            "INSERT INTO geographic_dataset_versions "
            "(id, dataset_type, source_identity, source_version, import_id, imported_at, "
            "valid_from, valid_until, coordinate_srid, validation_status, lifecycle_status, "
            "previous_version_id, metadata) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        )
        with self.transaction() as connection:
            connection.execute(
                statement,
                (
                    version.id,
                    version.dataset_type.value,
                    version.source_identity,
                    version.source_version,
                    version.import_id,
                    version.imported_at,
                    version.valid_from,
                    version.valid_until,
                    version.coordinate_srid,
                    version.validation_status.value,
                    version.lifecycle_status.value,
                    version.previous_version_id,
                    Jsonb(version.metadata),
                ),
            )
            for feature in normalized:
                self._insert_feature(connection, version, feature)
        return version

    def query_features(self, query: GeographicFeatureQuery) -> tuple[GeographicFeature, ...]:
        connection = self._connector.connect(self._network)
        try:
            self._set_search_path(connection)
            if query.source_feature_ids:
                result = connection.execute(
                    "SELECT id, source_feature_id, ST_AsText(geometry), properties "
                    "FROM geographic_features WHERE dataset_version_id = %s "
                    "AND source_feature_id = ANY(%s) ORDER BY source_feature_id LIMIT %s",
                    (query.dataset_version_id, list(query.source_feature_ids), query.limit),
                )
            else:
                result = connection.execute(
                    "SELECT id, source_feature_id, ST_AsText(geometry), properties "
                    "FROM geographic_features WHERE dataset_version_id = %s "
                    "ORDER BY source_feature_id LIMIT %s",
                    (query.dataset_version_id, query.limit),
                )
            rows = cast(Sequence[Sequence[object]], result.fetchall())
            return tuple(
                GeographicFeature(
                    id=cast(UUID, row[0]),
                    source_feature_id=str(row[1]),
                    geometry_wkt=None if row[2] is None else str(row[2]),
                    properties=cast(dict[str, object], row[3]),
                )
                for row in rows
            )
        finally:
            connection.close()

    def feature_count(self, version_id: UUID) -> int:
        connection = self._connector.connect(self._network)
        try:
            self._set_search_path(connection)
            row = cast(
                tuple[object, ...] | None,
                connection.execute(
                    "SELECT count(*) FROM geographic_features WHERE dataset_version_id = %s", (version_id,)
                ).fetchone(),
            )
            return int(str(row[0])) if row else 0
        finally:
            connection.close()

    def _insert_feature(
        self,
        connection: PostgresConnection,
        version: GeographicDatasetVersion,
        feature: GeographicFeature,
    ) -> None:
        if feature.geometry_wkt is None:
            statement = (
                "INSERT INTO geographic_features "
                "(id, dataset_version_id, source_feature_id, geometry, properties) VALUES "
                "(%s, %s, %s, %s, %s)"
            )
        else:
            statement = (
                "INSERT INTO geographic_features "
                "(id, dataset_version_id, source_feature_id, geometry, properties) VALUES "
                "(%s, %s, %s, ST_GeomFromText(%s, %s), %s)"
            )
        values: tuple[object, ...]
        if feature.geometry_wkt is None:
            values = (feature.id, version.id, feature.source_feature_id, None, Jsonb(feature.properties))
        else:
            values = (
                feature.id,
                version.id,
                feature.source_feature_id,
                feature.geometry_wkt,
                version.coordinate_srid,
                Jsonb(feature.properties),
            )
        connection.execute(statement, values)

    def _set_search_path(self, connection: PostgresConnection) -> None:
        connection.execute(
            "SELECT pg_catalog.set_config('search_path', %s, true)",
            (f'"{self._schema}", pg_temp',),
        )

    @contextmanager
    def transaction(self) -> Generator[PostgresConnection, None, None]:
        connection = self._connector.connect(self._network)
        try:
            self._set_search_path(connection)
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


class GeographicImportPipeline:
    """Turn bounded import requests into staged database versions only."""

    def __init__(self, repository: GeographicDatasetRepository) -> None:
        self.repository: GeographicDatasetRepository = repository

    def stage(self, request: GeographicImportRequest) -> GeographicDatasetVersion:
        version = request.version()
        return self.repository.stage_import(version, request.features)


def _validate_version_source(source_identity: str, source_version: str) -> None:
    _bounded_text(source_identity, _MAX_SOURCE_IDENTITY, "geographic source identity")
    _bounded_text(source_version, _MAX_SOURCE_VERSION, "geographic source version")


def _validate_version_timestamps(
    imported_at: datetime,
    valid_from: datetime | None,
    valid_until: datetime | None,
) -> None:
    if imported_at.utcoffset() is None:
        raise ValueError("Geographic import timestamps must include a timezone")
    for name, value in (("valid_from", valid_from), ("valid_until", valid_until)):
        if value is not None and value.utcoffset() is None:
            raise ValueError(f"Geographic {name} must include a timezone")
    if valid_from is not None and valid_until is not None and valid_until < valid_from:
        raise ValueError("Geographic validity cannot end before it starts")


def _validate_version_srid(coordinate_srid: int) -> None:
    if not 0 <= coordinate_srid <= 998_999:
        raise ValueError("Geographic coordinate SRID is out of range")


def _bounded_text(value: str, maximum: int, label: str) -> None:
    if not value.strip() or len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{label} is empty or too large")


def _json_object_bytes(value: dict[str, object], maximum: int, label: str) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is not serializable") from exc
    if len(encoded) > maximum:
        raise ValueError(f"{label} is too large")


def _feature_size(feature: GeographicFeature) -> int:
    geometry = 0 if feature.geometry_wkt is None else len(feature.geometry_wkt.encode("utf-8"))
    properties = json.dumps(feature.properties, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    return len(feature.source_feature_id.encode("utf-8")) + geometry + len(properties)


def _identifier(value: str, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized or len(normalized) > 63 or not normalized.replace("_", "").isalnum() or normalized[0].isdigit():
        raise ValueError(f"{field_name} must be a lowercase PostgreSQL identifier")
    return normalized


__all__ = [
    "GeographicDatasetRepository",
    "GeographicDatasetType",
    "GeographicDatasetVersion",
    "GeographicFeature",
    "GeographicFeatureQuery",
    "GeographicImportPipeline",
    "GeographicImportRequest",
    "GeographicLifecycleStatus",
    "GeographicValidationStatus",
]
