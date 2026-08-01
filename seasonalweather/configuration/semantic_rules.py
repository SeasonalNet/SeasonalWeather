"""Pure P1-14 semantic predicates shared by reports and runtime delegates."""

from __future__ import annotations

import posixpath


def current_and_legacy_auth_conflict(
    *,
    auth_present: bool,
    legacy_fields: frozenset[str] | set[str],
) -> bool:
    return auth_present and bool(legacy_fields)


def static_credential_sources_conflict(single_token: str, tokens_json: str) -> bool:
    return bool(single_token and tokens_json)


def exchange_ttls_are_ordered(
    minimum: int,
    default: int,
    maximum_write: int,
    maximum_read: int,
) -> bool:
    return 0 < minimum <= default <= maximum_write <= maximum_read


def job_repository_identity_errors(
    *,
    enabled: bool,
    required: bool,
    path: str,
    operational_database_path: str,
) -> tuple[str, ...]:
    errors: list[str] = []
    if enabled and not path.strip():
        errors.append("jobs.path must be explicitly configured when jobs are enabled")
    if (
        enabled
        and path.strip()
        and lexical_configuration_path(path) == lexical_configuration_path(operational_database_path)
    ):
        errors.append("jobs.path must be separate from database.path")
    if required and not enabled:
        errors.append("jobs.required cannot be true when jobs are disabled")
    return tuple(errors)


def lexical_configuration_path(value: str) -> str:
    """Normalize a configured path lexically without consulting the filesystem.

    Relative paths remain relative to the configured working-directory contract.
    Separators, ``.`` components, and purely lexical ``..`` components are
    normalized. Symlink and real-filesystem equivalence belongs to opt-in
    environmental preflight.
    """

    normalized = posixpath.normpath(value.replace("\\", "/").strip())
    return normalized.rstrip("/") or "."


def job_repository_timing_errors(
    *,
    busy_timeout_ms: int,
    assignment_ack_seconds: int,
    lease_seconds: int,
    shutdown_reconciliation_seconds: float,
) -> tuple[str, ...]:
    errors: list[str] = []
    if not 100 <= busy_timeout_ms <= 30_000:
        errors.append("jobs.busy_timeout_ms must be between 100 and 30000")
    if not 1 <= assignment_ack_seconds < lease_seconds <= 3600:
        errors.append("jobs lease timing must satisfy 1 <= assignment_ack_seconds < lease_seconds <= 3600")
    if not 0.1 <= shutdown_reconciliation_seconds <= 30.0:
        errors.append("jobs.shutdown_reconciliation_seconds must be between 0.1 and 30")
    return tuple(errors)


def lifecycle_timeout_error(
    *,
    total_seconds: float,
    stage_seconds: tuple[float, ...],
) -> str | None:
    if total_seconds <= 0 or any(value <= 0 for value in stage_seconds):
        return "lifecycle timeout values must be positive"
    if total_seconds < max(stage_seconds):
        return "lifecycle.total_seconds must cover every stage timeout"
    return None
