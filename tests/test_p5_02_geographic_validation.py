from __future__ import annotations

import asyncio
from collections.abc import Sequence
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from psycopg.types.json import Jsonb

from seasonalweather.config import OptionalServiceNetworkConfig, PostgresArchiveConfig
from seasonalweather.database.geographic import (
    GeographicDatasetRepository,
    GeographicDatasetType,
    GeographicLifecycleStatus,
    GeographicPolicyError,
    GeographicValidationResult,
    GeographicValidationStatus,
    GeographicValidationUnavailable,
)
from seasonalweather.same.targeting import SameTargetResolver


class _Connection:
    def __init__(
        self,
        *,
        version_row: tuple[object, ...],
        aggregate: tuple[object, ...] = (2, 0, 0),
        candidate_row: tuple[object, ...] | None = None,
        rollback_row: tuple[object, ...] | None = None,
        previous_row: tuple[object, ...] | None = None,
        previous_count: int = 0,
        active_predecessor: tuple[object, ...] | None = None,
        active_version_row: tuple[object, ...] | None = None,
        active_rows: tuple[tuple[object, ...], ...] = (),
        fail_validation: bool = False,
    ) -> None:
        self.version_row = version_row
        self.candidate_row = candidate_row
        self.rollback_row = rollback_row
        self.aggregate = aggregate
        self.previous_row = previous_row
        self.previous_count = previous_count
        self.active_predecessor = active_predecessor
        self.active_version_row = active_version_row
        self.active_rows = active_rows
        self.fail_validation = fail_validation
        self.calls: list[tuple[str, tuple[object, ...] | None]] = []
        self.events: list[str] = []

    def execute(self, operation: str, parameters: Sequence[object] | None = None) -> SimpleNamespace:
        params = None if parameters is None else tuple(parameters)
        self.calls.append((operation, params))
        if operation.startswith("SELECT pg_catalog.set_config"):
            return SimpleNamespace(fetchone=lambda: (None,))
        return self._execute_select(operation)

    def _execute_select(self, operation: str) -> SimpleNamespace:
        if operation.startswith("SELECT id, dataset_type"):
            return SimpleNamespace(fetchone=lambda: self.version_row)
        if operation.startswith("SELECT dataset_type, validation_status, lifecycle_status, previous_version_id"):
            row = self.candidate_row if self.candidate_row is not None else self.version_row
            return SimpleNamespace(fetchone=lambda: row)
        if operation.startswith("SELECT dataset_type, validation_status, lifecycle_status"):
            row = self.rollback_row if self.rollback_row is not None else self.version_row
            return SimpleNamespace(fetchone=lambda: row)
        if operation.startswith("SELECT count(*),"):
            if self.fail_validation:
                raise RuntimeError("postgis unavailable")
            return SimpleNamespace(fetchone=lambda: self.aggregate)
        if operation.startswith("SELECT dataset_type, lifecycle_status"):
            return SimpleNamespace(fetchone=lambda: self.previous_row)
        if operation.startswith("SELECT count(*) FROM geographic_features"):
            return SimpleNamespace(fetchone=lambda: (self.previous_count,))
        if operation.startswith("SELECT id FROM geographic_dataset_versions"):
            return self._execute_version_select(operation)
        return SimpleNamespace(fetchone=lambda: None, fetchall=lambda: ())

    def _execute_version_select(self, operation: str) -> SimpleNamespace:
        if "LIMIT 1 FOR SHARE" in operation:
            return SimpleNamespace(fetchone=lambda: self.active_predecessor)
        if "ORDER BY imported_at DESC LIMIT 1" in operation:
            return SimpleNamespace(fetchone=lambda: self.active_version_row)
        return SimpleNamespace(fetchall=lambda: self.active_rows)

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
_CANDIDATE = uuid4()
_PREVIOUS = uuid4()
_ACTIVE = uuid4()


def _repository(connection: _Connection) -> GeographicDatasetRepository:
    return GeographicDatasetRepository(_CONFIG, _NET, connector=_Connector(connection))


def test_staged_validation_accepts_null_geometry_and_persists_valid_evidence() -> None:
    connection = _Connection(
        version_row=(_CANDIDATE, "counties", 4326, "pending", "staged", _PREVIOUS),
        aggregate=(2, 0, 0),
        previous_row=("counties", "superseded"),
        previous_count=2,
    )

    result = _repository(connection).validate_staged(_CANDIDATE, expected_feature_count=2, expected_srid=4326)

    assert isinstance(result, GeographicValidationResult)
    assert result.valid
    assert result.feature_count == 2
    assert result.previous_feature_count == 2
    assert result.errors == ()
    assert result.diagnostics == ()
    update = next(call for call in connection.calls if call[0].startswith("UPDATE geographic_dataset_versions"))
    assert update[1] is not None
    assert update[1][0] == GeographicValidationStatus.VALID.value
    assert isinstance(update[1][1], Jsonb)
    assert connection.events == ["commit", "close"]


def test_staged_validation_blocks_bad_geometry_srid_and_implausible_counts() -> None:
    connection = _Connection(
        version_row=(_CANDIDATE, "counties", 3857, "pending", "staged", _PREVIOUS),
        aggregate=(3, 1, 1),
        previous_row=("counties", "active"),
        previous_count=2,
    )

    result = _repository(connection).validate_staged(
        _CANDIDATE,
        expected_feature_count=2,
        expected_srid=4326,
        max_count_change_ratio=0.25,
    )

    assert result.validation_status is GeographicValidationStatus.INVALID
    assert any("SRID" in error for error in result.errors)
    assert any("invalid or empty" in error for error in result.errors)
    assert any("does not match expected" in error for error in result.errors)
    assert any("exceeding" in error for error in result.errors)
    assert tuple(item.message for item in result.diagnostics) == result.errors
    assert [item.code for item in result.diagnostics] == ["SWGEO1001"] * 3 + ["SWGEO2001"] * 2
    (_, params) = next(call for call in connection.calls if call[0].startswith("UPDATE geographic_dataset_versions"))
    assert params is not None
    assert isinstance(params[1], Jsonb)
    assert params[1].obj["validation"]["diagnostics"] == [item.to_dict() for item in result.diagnostics]
    assert connection.events == ["commit", "close"]


@pytest.mark.parametrize("declared_previous", (None, _PREVIOUS))
def test_validation_compares_against_current_active_predecessor(declared_previous: object) -> None:
    connection = _Connection(
        version_row=(_CANDIDATE, "counties", 4326, "pending", "staged", declared_previous),
        aggregate=(5, 0, 0),
        active_predecessor=(_ACTIVE,),
        previous_row=("counties", "active"),
        previous_count=2,
    )

    result = _repository(connection).validate_staged(_CANDIDATE)

    assert result.validation_status is GeographicValidationStatus.INVALID
    assert any("exceeding" in error for error in result.errors)
    previous_lookup = next(
        params
        for operation, params in connection.calls
        if operation.startswith("SELECT dataset_type, lifecycle_status")
    )
    assert previous_lookup == (_ACTIVE,)


@pytest.mark.parametrize(
    ("expected_feature_count", "max_count_change_ratio"),
    [(-1, 0.5), (None, float("nan")), (None, 1.1)],
)
def test_validation_rejects_invalid_count_policy(
    expected_feature_count: int | None, max_count_change_ratio: float
) -> None:
    connection = _Connection(version_row=(_CANDIDATE, "counties", 4326, "pending", "staged", None))

    with pytest.raises(ValueError, match="count"):
        _ = _repository(connection).validate_staged(
            _CANDIDATE,
            expected_feature_count=expected_feature_count,
            max_count_change_ratio=max_count_change_ratio,
        )

    assert not connection.calls


def test_validation_rejects_missing_or_non_pending_versions() -> None:
    missing = _Connection(version_row=())
    with pytest.raises(LookupError, match="not found"):
        _ = _repository(missing).validate_staged(_CANDIDATE)

    non_pending = _Connection(version_row=(_CANDIDATE, "counties", 4326, "valid", "staged", None))
    with pytest.raises(ValueError, match="pending"):
        _ = _repository(non_pending).validate_staged(_CANDIDATE)


def test_validation_records_nonretained_predecessor_as_evidence() -> None:
    connection = _Connection(
        version_row=(_CANDIDATE, "counties", 4326, "pending", "staged", _PREVIOUS),
        previous_row=("counties", "staged"),
    )

    result = _repository(connection).validate_staged(_CANDIDATE)

    assert result.validation_status is GeographicValidationStatus.INVALID
    assert result.errors == ("previous geographic dataset version is not retained",)
    assert [item.code for item in result.diagnostics] == ["SWGEO2001"]


def test_active_query_returns_empty_when_no_version_is_active() -> None:
    connection = _Connection(version_row=(_CANDIDATE, "counties", 4326, "pending", "staged", None))

    repository = _repository(connection)
    assert repository.active_version(GeographicDatasetType.COUNTIES) is None
    assert repository.query_active_features(GeographicDatasetType.COUNTIES) == ()


def test_postgis_validation_failure_rolls_back_and_never_marks_candidate_valid() -> None:
    connection = _Connection(
        version_row=(_CANDIDATE, "counties", 4326, "pending", "staged", None),
        fail_validation=True,
    )

    with pytest.raises(GeographicValidationUnavailable) as exc:
        _ = _repository(connection).validate_staged(_CANDIDATE)

    assert isinstance(exc.value.__cause__, RuntimeError)
    assert str(exc.value.__cause__) == "postgis unavailable"
    assert not any(call[0].startswith("UPDATE geographic_dataset_versions") for call in connection.calls)
    assert connection.events == ["rollback", "close"]


def test_promotion_supersedes_active_and_retains_it_as_rollback_target() -> None:
    connection = _Connection(
        version_row=(_CANDIDATE, "counties", "valid", "staged", None),
        candidate_row=("counties", "valid", "staged", _PREVIOUS),
        active_rows=((_ACTIVE,),),
    )

    assert _repository(connection).promote(_CANDIDATE) == _CANDIDATE

    updates = [call for call in connection.calls if call[0].startswith("UPDATE geographic_dataset_versions")]
    assert len(updates) == 2
    assert updates[0][1] == (GeographicLifecycleStatus.SUPERSEDED.value, _ACTIVE)
    assert updates[1][1] == (GeographicLifecycleStatus.ACTIVE.value, _ACTIVE, _CANDIDATE)
    assert connection.events == ["commit", "close"]


def test_promotion_rejects_invalid_candidate_without_active_mutation() -> None:
    connection = _Connection(
        version_row=(_CANDIDATE, "counties", "invalid", "staged", None),
        candidate_row=("counties", "invalid", "staged", None),
        active_rows=((_ACTIVE,),),
    )

    with pytest.raises(GeographicPolicyError, match="valid") as exc:
        _ = _repository(connection).promote(_CANDIDATE)

    assert exc.value.diagnostic.code == "SWGEO2001"
    assert not any(call[0].startswith("SELECT id FROM") for call in connection.calls)
    assert connection.events == ["rollback", "close"]


def test_rollback_reactivates_retained_version_transactionally() -> None:
    connection = _Connection(
        version_row=(_PREVIOUS, "counties", "valid", "superseded"),
        rollback_row=("counties", "valid", "superseded"),
        active_rows=((_ACTIVE,),),
    )

    assert _repository(connection).rollback_to(_PREVIOUS) == _PREVIOUS

    updates = [call for call in connection.calls if call[0].startswith("UPDATE geographic_dataset_versions")]
    assert updates == [
        (updates[0][0], (GeographicLifecycleStatus.SUPERSEDED.value, _ACTIVE)),
        (updates[1][0], (GeographicLifecycleStatus.ACTIVE.value, _PREVIOUS)),
    ]
    assert connection.events == ["commit", "close"]


def test_rollback_policy_does_not_mutate_active_version() -> None:
    connection = _Connection(version_row=(), rollback_row=("counties", "valid", "staged"), active_rows=((_ACTIVE,),))

    with pytest.raises(GeographicPolicyError, match="retained") as exc:
        _ = _repository(connection).rollback_to(_PREVIOUS)

    assert exc.value.diagnostic.code == "SWGEO2001"
    assert not any(call[0].startswith("UPDATE") for call in connection.calls)
    assert connection.events == ["rollback", "close"]


def test_same_target_resolver_uses_optional_gis_only_after_code_fallback_fails() -> None:
    class _Resolver(SameTargetResolver):
        async def _ugc_zone_to_same(self, zone_id: str) -> list[str]:
            return []

    async def geographic_lookup(zones: Sequence[str]) -> Sequence[str]:
        assert tuple(zones) == ("MDC031",)
        return ("024031", "024033")

    resolver = object.__new__(_Resolver)
    resolver._same_fips_allow_set = {"024031"}
    resolver._geographic_same_lookup = geographic_lookup
    resolver._tz = ZoneInfo("UTC")

    result = asyncio.run(resolver._nwws_same_targets_from_texts("MDC031-012130-", ""))

    assert result[0] == ["MDC031"]
    assert result[1] == ["024031"]
    assert result[3] is True


def test_same_target_resolver_keeps_code_mapping_authoritative() -> None:
    class _Resolver(SameTargetResolver):
        async def _ugc_zone_to_same(self, zone_id: str) -> list[str]:
            return ["024031"]

    calls: list[Sequence[str]] = []

    async def unexpected_lookup(zones: Sequence[str]) -> Sequence[str]:
        calls.append(zones)
        return ("024033",)

    resolver = object.__new__(_Resolver)
    resolver._same_fips_allow_set = {"024031"}
    resolver._geographic_same_lookup = unexpected_lookup
    resolver._tz = ZoneInfo("UTC")

    result = asyncio.run(resolver._nwws_same_targets_from_texts("MDC031-012130-", ""))

    assert result[1] == ["024031"]
    assert not calls


def test_same_target_resolver_keeps_code_path_safe_when_gis_is_unavailable(caplog: pytest.LogCaptureFixture) -> None:
    async def failing_lookup(_zones: Sequence[str]) -> Sequence[str]:
        raise RuntimeError("database unavailable")

    resolver = object.__new__(SameTargetResolver)
    resolver._same_fips_allow_set = {"024031"}
    resolver._geographic_same_lookup = failing_lookup
    resolver._tz = ZoneInfo("UTC")

    for _ in range(2):
        assert asyncio.run(resolver._same_targets_from_geographic_fallback(("MDC031",))) == []

    assert not caplog.records
