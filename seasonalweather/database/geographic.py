"""Versioned geographic datasets, validation, and safe lifecycle transitions.

Staging, validation, promotion, and rollback are deliberately separate
operations.  PostGIS is optional and callers must retain the code-based
SAME/UGC path when it is unavailable or a dataset has no usable geometry.
"""

from __future__ import annotations

import json
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from math import isfinite
from typing import cast
from uuid import UUID, uuid4

from psycopg.errors import InternalError_
from psycopg.types.json import Jsonb

from ..config import OptionalServiceNetworkConfig, PostgresArchiveConfig
from ..diagnostics.bindings import FOUNDATION_CODES
from .postgresql import PostgresConnection, PostgresConnector, default_postgres_connector

_MAX_SOURCE_IDENTITY = 512
_MAX_SOURCE_VERSION = 256
_MAX_FEATURE_ID = 512
_MAX_WKT_BYTES = 1_000_000
_MAX_PROPERTY_BYTES = 1_000_000
_MAX_IMPORT_FEATURES = 100_000
_MAX_IMPORT_BYTES = 100_000_000
_MAX_QUERY_LIMIT = 10_000
_DEFAULT_MAX_COUNT_CHANGE_RATIO = 0.5
_MAX_VALIDATION_ERRORS = 8


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


class GeographicDiagnosticRule(StrEnum):
    INVALID_INPUT = "geography.invalid_input"
    POLICY_REJECTED = "geography.policy_rejected"


@dataclass(frozen=True, kw_only=True)
class GeographicDiagnostic:
    """A sanitized geographic finding, without source payloads or database details."""

    rule_id: GeographicDiagnosticRule
    message: str

    @property
    def code(self) -> str:
        return FOUNDATION_CODES[self.rule_id]

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "rule_id": self.rule_id.value, "message": self.message}


class GeographicInputError(ValueError):
    """A malformed geographic input rejected at its admission boundary."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.diagnostic = GeographicDiagnostic(rule_id=GeographicDiagnosticRule.INVALID_INPUT, message=message)


class GeographicPolicyError(ValueError):
    """A geographic operation disallowed by lifecycle or import policy."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.diagnostic = GeographicDiagnostic(rule_id=GeographicDiagnosticRule.POLICY_REJECTED, message=message)


class GeographicValidationUnavailable(RuntimeError):
    """PostGIS validation could not be completed."""


@dataclass(frozen=True, kw_only=True)
class GeographicValidationResult:
    """Bounded validation evidence persisted with a staged version."""

    version_id: UUID
    validation_status: GeographicValidationStatus
    feature_count: int
    expected_feature_count: int | None = None
    previous_feature_count: int | None = None
    count_change_ratio: float | None = None
    errors: tuple[str, ...] = ()
    diagnostics: tuple[GeographicDiagnostic, ...] = ()

    @property
    def valid(self) -> bool:
        return self.validation_status is GeographicValidationStatus.VALID


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
                raise GeographicInputError("Geographic feature geometry cannot be empty")
            if len(self.geometry_wkt.encode("utf-8")) > _MAX_WKT_BYTES:
                raise GeographicInputError("Geographic feature geometry is too large")


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
            raise GeographicInputError("A geographic version cannot roll back to itself")
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
            raise GeographicInputError("Geographic feature query limit is invalid")
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
            raise GeographicInputError("Geographic import feature bound is invalid")
        if self.max_bytes < 1 or self.max_bytes > _MAX_IMPORT_BYTES:
            raise GeographicInputError("Geographic import byte bound is invalid")
        if len(features) > self.max_features:
            raise GeographicPolicyError("Geographic import exceeds its feature bound")
        size = sum(_feature_size(feature) for feature in features)
        if size > self.max_bytes:
            raise GeographicPolicyError("Geographic import exceeds its byte bound")

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
            raise GeographicPolicyError("Only staged geographic versions may be imported")
        if version.validation_status is not GeographicValidationStatus.PENDING:
            raise GeographicPolicyError("New geographic imports must have pending validation")
        normalized = tuple(features)
        if len(normalized) > _MAX_IMPORT_FEATURES:
            raise GeographicPolicyError("Geographic import exceeds its feature bound")
        if sum(_feature_size(feature) for feature in normalized) > _MAX_IMPORT_BYTES:
            raise GeographicPolicyError("Geographic import exceeds its byte bound")
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

    def validate_staged(
        self,
        version_id: UUID,
        *,
        expected_feature_count: int | None = None,
        expected_srid: int | None = None,
        max_count_change_ratio: float = _DEFAULT_MAX_COUNT_CHANGE_RATIO,
    ) -> GeographicValidationResult:
        """Validate a staged version and persist its outcome atomically.

        Null geometry is allowed because code-based SAME/UGC targeting remains the
        safety authority. Present geometry must be non-empty, valid, and carry the
        version SRID. Database/PostGIS errors leave the version pending.
        """
        _validate_count_policy(expected_feature_count, max_count_change_ratio)
        if expected_srid is not None:
            _validate_version_srid(expected_srid)

        try:
            with self.transaction() as connection:
                dataset_type, coordinate_srid, previous_version_id = _load_validation_version(connection, version_id)
                feature_count, invalid_geometry_count, srid_mismatch_count = _geometry_counts(
                    connection, version_id, coordinate_srid
                )
                active_predecessor_id = _active_validation_predecessor(connection, dataset_type)
                comparison_version_id = active_predecessor_id or previous_version_id
                previous_feature_count, predecessor_diagnostic = _previous_validation_state(
                    connection, comparison_version_id, dataset_type
                )
                count_change_ratio, count_error = _count_change(
                    feature_count, previous_feature_count, max_count_change_ratio
                )
                diagnostics = [predecessor_diagnostic] if predecessor_diagnostic is not None else []
                diagnostics.extend(
                    _geometry_diagnostics(
                        coordinate_srid=coordinate_srid,
                        expected_srid=expected_srid,
                        feature_count=feature_count,
                        invalid_geometry_count=invalid_geometry_count,
                        srid_mismatch_count=srid_mismatch_count,
                    )
                )
                diagnostics.extend(_count_diagnostics(feature_count, expected_feature_count, count_error))
                diagnostics = diagnostics[:_MAX_VALIDATION_ERRORS]
                status = GeographicValidationStatus.INVALID if diagnostics else GeographicValidationStatus.VALID
                result = GeographicValidationResult(
                    version_id=version_id,
                    validation_status=status,
                    feature_count=feature_count,
                    expected_feature_count=expected_feature_count,
                    previous_feature_count=previous_feature_count,
                    count_change_ratio=count_change_ratio,
                    errors=tuple(diagnostic.message for diagnostic in diagnostics),
                    diagnostics=tuple(diagnostics),
                )
                connection.execute(
                    """UPDATE geographic_dataset_versions
                       SET validation_status = %s,
                           metadata = COALESCE(metadata, '{}'::jsonb) || %s
                       WHERE id = %s""",
                    (status.value, Jsonb(_validation_evidence(result, expected_srid)), version_id),
                )
                return result
        except (LookupError, ValueError):
            raise
        except Exception as exc:
            raise GeographicValidationUnavailable("PostGIS geographic validation is unavailable") from exc

    def promote(self, version_id: UUID) -> UUID:
        """Make a validated staged version active in one transaction."""
        with self.transaction() as connection:
            candidate = cast(
                tuple[object, ...] | None,
                connection.execute(
                    "SELECT dataset_type, validation_status, lifecycle_status, previous_version_id "
                    "FROM geographic_dataset_versions WHERE id = %s FOR UPDATE",
                    (version_id,),
                ).fetchone(),
            )
            if not candidate:
                raise LookupError("Geographic dataset version was not found")
            dataset_type = str(candidate[0])
            if str(candidate[1]) != GeographicValidationStatus.VALID.value:
                raise GeographicPolicyError("Only valid geographic versions may be promoted")
            if str(candidate[2]) != GeographicLifecycleStatus.STAGED.value:
                raise GeographicPolicyError("Only staged geographic versions may be promoted")

            active_rows = cast(
                Sequence[Sequence[object]],
                connection.execute(
                    "SELECT id FROM geographic_dataset_versions "
                    "WHERE dataset_type = %s AND lifecycle_status = %s AND id <> %s "
                    "ORDER BY imported_at DESC FOR UPDATE",
                    (dataset_type, GeographicLifecycleStatus.ACTIVE.value, version_id),
                ).fetchall(),
            )
            active_ids = tuple(cast(UUID, row[0]) for row in active_rows)
            for active_id in active_ids:
                connection.execute(
                    "UPDATE geographic_dataset_versions SET lifecycle_status = %s WHERE id = %s",
                    (GeographicLifecycleStatus.SUPERSEDED.value, active_id),
                )
            if active_ids:
                connection.execute(
                    "UPDATE geographic_dataset_versions SET lifecycle_status = %s, previous_version_id = %s "
                    "WHERE id = %s",
                    (GeographicLifecycleStatus.ACTIVE.value, active_ids[0], version_id),
                )
            else:
                connection.execute(
                    "UPDATE geographic_dataset_versions SET lifecycle_status = %s WHERE id = %s",
                    (GeographicLifecycleStatus.ACTIVE.value, version_id),
                )
        return version_id

    def rollback_to(self, version_id: UUID) -> UUID:
        """Reactivate a retained valid version without deleting the current one."""
        with self.transaction() as connection:
            target = cast(
                tuple[object, ...] | None,
                connection.execute(
                    "SELECT dataset_type, validation_status, lifecycle_status "
                    "FROM geographic_dataset_versions WHERE id = %s FOR UPDATE",
                    (version_id,),
                ).fetchone(),
            )
            if not target:
                raise LookupError("Geographic dataset version was not found")
            dataset_type = str(target[0])
            if str(target[1]) != GeographicValidationStatus.VALID.value:
                raise GeographicPolicyError("Only valid geographic versions may be used for rollback")
            if str(target[2]) not in {
                GeographicLifecycleStatus.ACTIVE.value,
                GeographicLifecycleStatus.SUPERSEDED.value,
            }:
                raise GeographicPolicyError("Only retained geographic versions may be used for rollback")

            active_rows = cast(
                Sequence[Sequence[object]],
                connection.execute(
                    "SELECT id FROM geographic_dataset_versions "
                    "WHERE dataset_type = %s AND lifecycle_status = %s AND id <> %s "
                    "ORDER BY imported_at DESC FOR UPDATE",
                    (dataset_type, GeographicLifecycleStatus.ACTIVE.value, version_id),
                ).fetchall(),
            )
            for row in active_rows:
                connection.execute(
                    "UPDATE geographic_dataset_versions SET lifecycle_status = %s WHERE id = %s",
                    (GeographicLifecycleStatus.SUPERSEDED.value, cast(UUID, row[0])),
                )
            connection.execute(
                "UPDATE geographic_dataset_versions SET lifecycle_status = %s WHERE id = %s",
                (GeographicLifecycleStatus.ACTIVE.value, version_id),
            )
        return version_id

    def active_version(self, dataset_type: GeographicDatasetType) -> UUID | None:
        """Return the currently active version for a dataset type, if any."""
        connection = self._connector.connect(self._network)
        try:
            self._set_search_path(connection)
            row = cast(
                tuple[object, ...] | None,
                connection.execute(
                    "SELECT id FROM geographic_dataset_versions WHERE dataset_type = %s "
                    "AND lifecycle_status = %s ORDER BY imported_at DESC LIMIT 1",
                    (dataset_type.value, GeographicLifecycleStatus.ACTIVE.value),
                ).fetchone(),
            )
            return cast(UUID, row[0]) if row else None
        finally:
            connection.close()

    def query_active_features(
        self,
        dataset_type: GeographicDatasetType,
        *,
        source_feature_ids: Sequence[str] = (),
        limit: int = 1_000,
    ) -> tuple[GeographicFeature, ...]:
        """Read active geometry when available; callers must retain code fallback."""
        version_id = self.active_version(dataset_type)
        if version_id is None:
            return ()
        return self.query_features(
            GeographicFeatureQuery(
                dataset_version_id=version_id,
                source_feature_ids=tuple(source_feature_ids),
                limit=limit,
            )
        )

    def _insert_feature(
        self,
        connection: PostgresConnection,
        version: GeographicDatasetVersion,
        feature: GeographicFeature,
    ) -> None:
        if feature.geometry_wkt is None:
            connection.execute(
                "INSERT INTO geographic_features "
                "(id, dataset_version_id, source_feature_id, geometry, properties) VALUES "
                "(%s, %s, %s, %s, %s)",
                (feature.id, version.id, feature.source_feature_id, None, Jsonb(feature.properties)),
            )
            return
        try:
            connection.execute(
                "INSERT INTO geographic_features "
                "(id, dataset_version_id, source_feature_id, geometry, properties) VALUES "
                "(%s, %s, %s, ST_GeomFromText(%s, %s), %s)",
                (
                    feature.id,
                    version.id,
                    feature.source_feature_id,
                    feature.geometry_wkt,
                    version.coordinate_srid,
                    Jsonb(feature.properties),
                ),
            )
        except InternalError_ as exc:
            # PostGIS reports WKT parser rejection as XX000 via pg_parser_errhint.
            # The SQLSTATE alone also covers unrelated database failures.
            diagnostic = exc.diag
            if diagnostic.source_function == "pg_parser_errhint" and diagnostic.source_file == "lwgeom_pg.c":
                raise GeographicInputError("Geographic feature geometry is not valid WKT") from exc
            raise

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
    """Coordinate bounded staging, validation, promotion, and rollback."""

    def __init__(self, repository: GeographicDatasetRepository) -> None:
        self.repository: GeographicDatasetRepository = repository

    def stage(self, request: GeographicImportRequest) -> GeographicDatasetVersion:
        version = request.version()
        return self.repository.stage_import(version, request.features)

    def validate(
        self,
        version_id: UUID,
        *,
        expected_feature_count: int | None = None,
        expected_srid: int | None = None,
        max_count_change_ratio: float = _DEFAULT_MAX_COUNT_CHANGE_RATIO,
    ) -> GeographicValidationResult:
        return self.repository.validate_staged(
            version_id,
            expected_feature_count=expected_feature_count,
            expected_srid=expected_srid,
            max_count_change_ratio=max_count_change_ratio,
        )

    def promote(self, version_id: UUID) -> UUID:
        return self.repository.promote(version_id)

    def rollback_to(self, version_id: UUID) -> UUID:
        return self.repository.rollback_to(version_id)


def _load_validation_version(connection: PostgresConnection, version_id: UUID) -> tuple[str, int, UUID | None]:
    row = cast(
        tuple[object, ...] | None,
        connection.execute(
            """SELECT id, dataset_type, coordinate_srid, validation_status,
                      lifecycle_status, previous_version_id
               FROM geographic_dataset_versions
               WHERE id = %s FOR UPDATE""",
            (version_id,),
        ).fetchone(),
    )
    if not row:
        raise LookupError("Geographic dataset version was not found")
    if str(row[4]) != GeographicLifecycleStatus.STAGED.value:
        raise GeographicPolicyError("Only staged geographic versions may be validated")
    if str(row[3]) != GeographicValidationStatus.PENDING.value:
        raise GeographicPolicyError("Only pending geographic versions may be validated")
    return str(row[1]), int(str(row[2])), cast(UUID | None, row[5])


def _geometry_counts(connection: PostgresConnection, version_id: UUID, coordinate_srid: int) -> tuple[int, int, int]:
    row = cast(
        tuple[object, ...] | None,
        connection.execute(
            """SELECT count(*),
                      count(*) FILTER (WHERE geometry IS NOT NULL AND
                                       (ST_IsEmpty(geometry) OR NOT ST_IsValid(geometry))),
                      count(*) FILTER (WHERE geometry IS NOT NULL AND ST_SRID(geometry) <> %s)
               FROM geographic_features
               WHERE dataset_version_id = %s""",
            (coordinate_srid, version_id),
        ).fetchone(),
    )
    if not row:
        return (0, 0, 0)
    return int(str(row[0])), int(str(row[1])), int(str(row[2]))


def _active_validation_predecessor(connection: PostgresConnection, dataset_type: str) -> UUID | None:
    row = cast(
        tuple[object, ...] | None,
        connection.execute(
            "SELECT id FROM geographic_dataset_versions "
            "WHERE dataset_type = %s AND lifecycle_status = %s "
            "ORDER BY imported_at DESC LIMIT 1 FOR SHARE",
            (dataset_type, GeographicLifecycleStatus.ACTIVE.value),
        ).fetchone(),
    )
    return cast(UUID, row[0]) if row else None


def _previous_validation_state(
    connection: PostgresConnection, previous_version_id: UUID | None, dataset_type: str
) -> tuple[int | None, GeographicDiagnostic | None]:
    if previous_version_id is None:
        return None, None
    row = cast(
        tuple[object, ...] | None,
        connection.execute(
            """SELECT dataset_type, lifecycle_status
               FROM geographic_dataset_versions
               WHERE id = %s FOR SHARE""",
            (previous_version_id,),
        ).fetchone(),
    )
    if not row:
        return None, GeographicDiagnostic(
            rule_id=GeographicDiagnosticRule.POLICY_REJECTED,
            message="previous geographic dataset version was not found",
        )
    if str(row[0]) != dataset_type:
        return None, GeographicDiagnostic(
            rule_id=GeographicDiagnosticRule.INVALID_INPUT,
            message="previous geographic dataset version has a different dataset type",
        )
    if str(row[1]) not in {
        GeographicLifecycleStatus.ACTIVE.value,
        GeographicLifecycleStatus.SUPERSEDED.value,
    }:
        return None, GeographicDiagnostic(
            rule_id=GeographicDiagnosticRule.POLICY_REJECTED,
            message="previous geographic dataset version is not retained",
        )
    count_row = cast(
        tuple[object, ...] | None,
        connection.execute(
            "SELECT count(*) FROM geographic_features WHERE dataset_version_id = %s",
            (previous_version_id,),
        ).fetchone(),
    )
    return (int(str(count_row[0])) if count_row else 0), None


def _count_change(
    feature_count: int, previous_feature_count: int | None, max_count_change_ratio: float
) -> tuple[float | None, str | None]:
    if previous_feature_count is None:
        return None, None
    if previous_feature_count == 0:
        if feature_count:
            return float("inf"), "feature count changed from zero to a non-zero value"
        return 0.0, None
    ratio = abs(feature_count - previous_feature_count) / previous_feature_count
    if ratio > max_count_change_ratio:
        return (
            ratio,
            f"feature count changed by {ratio:.3f}, exceeding the allowed ratio {max_count_change_ratio:.3f}",
        )
    return ratio, None


def _validation_evidence(result: GeographicValidationResult, expected_srid: int | None) -> dict[str, object]:
    ratio = result.count_change_ratio
    return {
        "validation": {
            "feature_count": result.feature_count,
            "expected_feature_count": result.expected_feature_count,
            "previous_feature_count": result.previous_feature_count,
            "count_change_ratio": "infinite" if ratio is not None and not isfinite(ratio) else ratio,
            "expected_srid": expected_srid,
            "errors": list(result.errors),
            "diagnostics": [diagnostic.to_dict() for diagnostic in result.diagnostics],
        }
    }


def _geometry_diagnostics(
    *,
    coordinate_srid: int,
    expected_srid: int | None,
    feature_count: int,
    invalid_geometry_count: int,
    srid_mismatch_count: int,
) -> list[GeographicDiagnostic]:
    invalid_inputs: list[str] = []
    if expected_srid is not None and coordinate_srid != expected_srid:
        invalid_inputs.append(f"coordinate SRID {coordinate_srid} does not match expected SRID {expected_srid}")
    if coordinate_srid == 0 and feature_count:
        invalid_inputs.append("geographic features require a non-zero coordinate SRID")
    if invalid_geometry_count:
        invalid_inputs.append(f"{invalid_geometry_count} geographic features have invalid or empty geometry")
    if srid_mismatch_count:
        invalid_inputs.append(f"{srid_mismatch_count} geographic features have an SRID mismatch")
    return [
        GeographicDiagnostic(rule_id=GeographicDiagnosticRule.INVALID_INPUT, message=message)
        for message in invalid_inputs
    ]


def _count_diagnostics(
    feature_count: int, expected_feature_count: int | None, count_error: str | None
) -> list[GeographicDiagnostic]:
    policy_rejections: list[str] = []
    if expected_feature_count is not None and feature_count != expected_feature_count:
        policy_rejections.append(
            f"feature count {feature_count} does not match expected count {expected_feature_count}"
        )
    if count_error:
        policy_rejections.append(count_error)
    return [
        GeographicDiagnostic(rule_id=GeographicDiagnosticRule.POLICY_REJECTED, message=message)
        for message in policy_rejections
    ]


def _validate_version_source(source_identity: str, source_version: str) -> None:
    _bounded_text(source_identity, _MAX_SOURCE_IDENTITY, "geographic source identity")
    _bounded_text(source_version, _MAX_SOURCE_VERSION, "geographic source version")


def _validate_version_timestamps(
    imported_at: datetime,
    valid_from: datetime | None,
    valid_until: datetime | None,
) -> None:
    if imported_at.utcoffset() is None:
        raise GeographicInputError("Geographic import timestamps must include a timezone")
    for name, value in (("valid_from", valid_from), ("valid_until", valid_until)):
        if value is not None and value.utcoffset() is None:
            raise GeographicInputError(f"Geographic {name} must include a timezone")
    if valid_from is not None and valid_until is not None and valid_until < valid_from:
        raise GeographicInputError("Geographic validity cannot end before it starts")


def _validate_version_srid(coordinate_srid: int) -> None:
    if not 0 <= coordinate_srid <= 998_999:
        raise GeographicInputError("Geographic coordinate SRID is out of range")


def _validate_count_policy(expected_feature_count: int | None, max_count_change_ratio: float) -> None:
    if expected_feature_count is not None and expected_feature_count < 0:
        raise GeographicInputError("Expected geographic feature count cannot be negative")
    if not isfinite(max_count_change_ratio) or not 0 <= max_count_change_ratio <= 1:
        raise GeographicInputError("Geographic count change ratio must be between zero and one")


def _bounded_text(value: str, maximum: int, label: str) -> None:
    if not value.strip() or len(value.encode("utf-8")) > maximum:
        raise GeographicInputError(f"{label} is empty or too large")


def _json_object_bytes(value: dict[str, object], maximum: int, label: str) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise GeographicInputError(f"{label} is not serializable") from exc
    if len(encoded) > maximum:
        raise GeographicInputError(f"{label} is too large")


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
    "GeographicDiagnostic",
    "GeographicDiagnosticRule",
    "GeographicFeature",
    "GeographicFeatureQuery",
    "GeographicImportPipeline",
    "GeographicImportRequest",
    "GeographicInputError",
    "GeographicLifecycleStatus",
    "GeographicPolicyError",
    "GeographicValidationResult",
    "GeographicValidationStatus",
    "GeographicValidationUnavailable",
]
