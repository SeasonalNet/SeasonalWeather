"""Bounded application service for API health and readiness reporting."""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class ComponentState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


_UNREADY_STATES = {
    ComponentState.DEGRADED,
    ComponentState.UNAVAILABLE,
    ComponentState.DISABLED,
    ComponentState.UNKNOWN,
}
_DEGRADED_STATES = {
    ComponentState.DEGRADED,
    ComponentState.UNAVAILABLE,
    ComponentState.UNKNOWN,
}
_MAX_COMPONENTS = 24
_MAX_DETAIL_ITEMS = 8
_MAX_REQUIRED_CAPABILITIES = 64
_MAX_AGE_SECONDS = 31_536_000


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _bounded_seconds(value: float | int) -> float:
    return round(max(0.0, min(float(value), float(_MAX_AGE_SECONDS))), 3)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).replace(microsecond=0).isoformat()


def _safe_details(raw: dict[str, Any]) -> dict[str, bool | int | float | str]:
    details: dict[str, bool | int | float | str] = {}
    for key in sorted(raw)[:_MAX_DETAIL_ITEMS]:
        if not key.replace("_", "").isalnum() or len(key) > 40:
            continue
        value = raw[key]
        if isinstance(value, bool):
            details[key] = value
        elif isinstance(value, int):
            details[key] = max(-1_000_000_000, min(value, 1_000_000_000))
        elif isinstance(value, float):
            details[key] = round(max(-1_000_000_000.0, min(value, 1_000_000_000.0)), 3)
        elif isinstance(value, str) and len(value) <= 64:
            details[key] = value
    return details


@dataclass(frozen=True)
class HealthComponent:
    name: str
    state: ComponentState
    required: bool
    reason: str
    observed_at: dt.datetime | None = None
    age_seconds: float | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, detailed: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "state": self.state.value,
            "required": self.required,
            "reason": self.reason,
        }
        if detailed:
            if self.observed_at is not None:
                payload["observed_at"] = _iso(self.observed_at)
            if self.age_seconds is not None:
                payload["age_seconds"] = _bounded_seconds(self.age_seconds)
            details = _safe_details(self.details)
            if details:
                payload["details"] = details
        return payload


Probe = Callable[[], Awaitable[HealthComponent]]


@dataclass(frozen=True)
class ComponentProbe:
    name: str
    required: bool
    evaluate: Probe


@dataclass(frozen=True)
class HealthReport:
    checked_at: dt.datetime
    duration_ms: float
    ready: bool
    state: ComponentState
    lifecycle_state: str
    components: tuple[HealthComponent, ...]

    def to_dict(self, *, detailed: bool) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "lifecycle_state": self.lifecycle_state,
            "ready": self.ready,
            "checked_at": _iso(self.checked_at),
            "duration_ms": round(max(0.0, min(self.duration_ms, 60_000.0)), 3),
            "components": [component.to_dict(detailed=detailed) for component in self.components[:_MAX_COMPONENTS]],
        }


class HealthService:
    """Collect typed component state with one timeout per component."""

    def __init__(
        self,
        probes: Iterable[ComponentProbe],
        *,
        timeout_seconds: float = 1.0,
        clock: Callable[[], dt.datetime] = _utc_now,
        lifecycle_state: Callable[[], str] = lambda: "running",
    ) -> None:
        self._probes = tuple(probes)[:_MAX_COMPONENTS]
        self._timeout_seconds = max(0.01, min(float(timeout_seconds), 5.0))
        self._clock = clock
        self._lifecycle_state = lifecycle_state

    async def _evaluate(self, probe: ComponentProbe) -> HealthComponent:
        try:
            component = await asyncio.wait_for(
                probe.evaluate(),
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            return HealthComponent(
                probe.name,
                ComponentState.UNAVAILABLE if probe.required else ComponentState.DEGRADED,
                probe.required,
                "probe_timeout",
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return HealthComponent(
                probe.name,
                ComponentState.UNAVAILABLE if probe.required else ComponentState.DEGRADED,
                probe.required,
                "probe_exception",
            )
        if component.name != probe.name or component.required != probe.required:
            return HealthComponent(
                probe.name,
                ComponentState.UNAVAILABLE if probe.required else ComponentState.UNKNOWN,
                probe.required,
                "invalid_probe_result",
            )
        return component

    async def collect(self) -> HealthReport:
        started = time.monotonic()
        components = tuple(
            sorted(
                await asyncio.gather(*(self._evaluate(probe) for probe in self._probes)),
                key=lambda component: component.name,
            )
        )
        lifecycle_state = str(self._lifecycle_state())
        ready = lifecycle_state == "running" and not any(
            component.required and component.state in _UNREADY_STATES for component in components
        )
        if not ready:
            state = ComponentState.UNAVAILABLE
        elif any(not component.required and component.state in _DEGRADED_STATES for component in components):
            state = ComponentState.DEGRADED
        else:
            state = ComponentState.HEALTHY
        return HealthReport(
            checked_at=self._clock(),
            duration_ms=(time.monotonic() - started) * 1000.0,
            ready=ready,
            state=state,
            lifecycle_state=lifecycle_state[:32],
            components=components,
        )


def _component(
    name: str,
    state: ComponentState,
    required: bool,
    reason: str,
    **kwargs: Any,
) -> HealthComponent:
    return HealthComponent(name, state, required, reason, **kwargs)


def _constant_probe(
    name: str,
    state: ComponentState,
    *,
    required: bool = False,
    reason: str,
    details: dict[str, Any] | None = None,
) -> ComponentProbe:
    async def evaluate() -> HealthComponent:
        return _component(
            name,
            state,
            required,
            reason,
            details=details or {},
        )

    return ComponentProbe(name, required, evaluate)


def _source_probe(runtime: Any, source: str) -> ComponentProbe:
    name = f"source_{source}"

    async def evaluate() -> HealthComponent:
        snapshot = runtime.health_state.source_snapshot(source)
        if snapshot is None:
            return _component(name, ComponentState.UNKNOWN, False, "state_unavailable")
        state = ComponentState(str(snapshot["state"]))
        return _component(
            name,
            state,
            False,
            str(snapshot["reason"]),
            observed_at=snapshot.get("observed_at"),
            age_seconds=snapshot.get("age_seconds"),
            details={
                "consecutive_failures": snapshot.get("consecutive_failures", 0),
                "role": snapshot.get("role", "general"),
            },
        )

    return ComponentProbe(name, False, evaluate)


def build_runtime_health_service(
    runtime: Any,
    *,
    command_store: Any,
    auth_service: Any,
    job_service: Any = None,
    capability_registry: Any = None,
    artifact_service: Any = None,
    artifacts_required: bool = False,
    required_capabilities: Iterable[str] = (),
    timeout_seconds: float = 1.0,
) -> HealthService:
    """Build probes from current controller-owned runtime capabilities."""

    cfg = runtime.cfg
    probes: list[ComponentProbe] = []

    async def artifact_probe() -> HealthComponent:
        if artifact_service is None:
            return _component("artifacts", ComponentState.NOT_APPLICABLE, False, "not_configured")
        snapshot = artifact_service.health_snapshot()
        pending = int(snapshot.get("pending_reconciliation", 0))
        temporary = int(snapshot.get("temporary_backlog", 0))
        storage_available = bool(snapshot.get("storage_available", 0))
        required_missing = int(snapshot.get("required_active_missing", 0))
        closed = snapshot.get("state") == "closed"
        unavailable = closed or not storage_available or required_missing > 0
        state = (
            ComponentState.UNAVAILABLE
            if unavailable
            else ComponentState.DEGRADED
            if pending
            else ComponentState.HEALTHY
        )
        reason = (
            "service_closed"
            if closed
            else "storage_unavailable"
            if not storage_available
            else "required_active_missing"
            if required_missing
            else "reconciliation_pending"
            if pending
            else "artifacts_ready"
        )
        return _component(
            "artifacts",
            state,
            artifacts_required,
            reason,
            details={
                "pending_reconciliation": pending,
                "temporary_backlog": temporary,
                "required_active_missing": required_missing,
                "last_reason": str(snapshot.get("last_reason", "none")),
            },
        )

    probes.append(ComponentProbe("artifacts", artifacts_required, artifact_probe))

    async def database_probe() -> HealthComponent:
        database = getattr(runtime, "database", None)
        if not cfg.database.enabled:
            return _component(
                "sqlite",
                ComponentState.DISABLED,
                False,
                "disabled_by_configuration",
            )
        if database is None:
            return _component(
                "sqlite",
                ComponentState.UNAVAILABLE,
                True,
                "database_unavailable",
            )
        operational = database.is_operational()
        return _component(
            "sqlite",
            ComponentState.HEALTHY if operational else ComponentState.UNAVAILABLE,
            True,
            "database_operational" if operational else "database_unavailable",
        )

    probes.append(ComponentProbe("sqlite", bool(cfg.database.enabled), database_probe))

    async def directories_probe() -> HealthComponent:
        paths = tuple(Path(path) for path in runtime._paths())

        def inspect() -> tuple[bool, int]:
            writable = all(path.is_dir() and os.access(path, os.W_OK | os.X_OK) for path in paths)
            free_mib = (
                min(
                    (os.statvfs(path).f_bavail * os.statvfs(path).f_frsize) // (1024 * 1024)
                    for path in paths
                    if path.is_dir()
                )
                if all(path.is_dir() for path in paths)
                else 0
            )
            return writable, int(free_mib)

        writable, free_mib = inspect()
        return _component(
            "runtime_directories",
            ComponentState.HEALTHY if writable else ComponentState.UNAVAILABLE,
            True,
            "directories_writable" if writable else "directory_unavailable",
            details={"minimum_free_mib": free_mib},
        )

    probes.append(ComponentProbe("runtime_directories", True, directories_probe))

    async def conductor_probe() -> HealthComponent:
        running = any(task.get_name() == "conductor" and not task.done() for task in asyncio.all_tasks())
        return _component(
            "conductor",
            ComponentState.HEALTHY if running else ComponentState.UNAVAILABLE,
            True,
            "conductor_running" if running else "conductor_not_running",
        )

    probes.append(ComponentProbe("conductor", True, conductor_probe))

    async def liquidsoap_probe() -> HealthComponent:
        reachable = runtime.telnet.ping(timeout_seconds=min(timeout_seconds, 1.0))
        queue_details = {
            f"{name}_queue_depth": int(getattr(runtime, f"{name}_queue").qsize())
            for name in ("nwws", "cap", "ipaws", "ern")
            if getattr(runtime, f"{name}_queue", None) is not None
        }
        return _component(
            "liquidsoap",
            ComponentState.HEALTHY if reachable else ComponentState.UNAVAILABLE,
            True,
            "control_reachable" if reachable else "control_unreachable",
            details=queue_details,
        )

    probes.append(ComponentProbe("liquidsoap", True, liquidsoap_probe))

    async def tts_probe() -> HealthComponent:
        available, reason = runtime.tts.availability()
        return _component(
            "tts",
            ComponentState.HEALTHY if available else ComponentState.UNAVAILABLE,
            True,
            reason,
        )

    probes.append(ComponentProbe("tts", True, tts_probe))

    async def segments_probe() -> HealthComponent:
        snapshot = runtime._seg_store.health_snapshot()
        count = int(snapshot["count"])
        stale = int(snapshot["stale_count"])
        placeholders = int(snapshot["placeholder_count"])
        if count == 0:
            state = ComponentState.UNKNOWN
            reason = "segments_not_initialized"
        elif stale or placeholders:
            state = ComponentState.DEGRADED
            reason = "segments_need_refresh"
        else:
            state = ComponentState.HEALTHY
            reason = "segments_current"
        return _component(
            "segments",
            state,
            False,
            reason,
            age_seconds=float(snapshot["oldest_age_seconds"]),
            details={
                "count": count,
                "ready_count": int(snapshot["ready_count"]),
                "stale_count": stale,
                "placeholder_count": placeholders,
            },
        )

    probes.append(ComponentProbe("segments", False, segments_probe))
    lifecycle = runtime.lifecycle

    async def lifecycle_probe() -> HealthComponent:
        running = bool(getattr(lifecycle, "startup_ready", lifecycle.ready))
        return _component(
            "lifecycle",
            ComponentState.HEALTHY if running else ComponentState.UNAVAILABLE,
            True,
            "running" if running else "initialization_incomplete",
            details={
                "state": lifecycle.state.value,
                "startup_ready": running,
            },
        )

    probes.append(ComponentProbe("lifecycle", True, lifecycle_probe))

    async def command_admission_probe() -> HealthComponent:
        available = command_store is not None and lifecycle.ready
        return _component(
            "command_admission",
            ComponentState.HEALTHY if available else ComponentState.UNAVAILABLE,
            True,
            "admission_available" if available else "admission_closed",
        )

    probes.append(ComponentProbe("command_admission", True, command_admission_probe))

    jobs_cfg = getattr(cfg, "jobs", None)
    jobs_enabled = bool(getattr(jobs_cfg, "enabled", False))
    jobs_required = bool(getattr(jobs_cfg, "required", False))

    async def job_repository_probe() -> HealthComponent:
        if not jobs_enabled:
            return _component(
                "job_repository",
                ComponentState.DISABLED,
                False,
                "disabled_by_configuration",
            )
        if job_service is None:
            return _component(
                "job_repository",
                ComponentState.UNAVAILABLE,
                jobs_required,
                "repository_unavailable",
            )
        summary = job_service.health()
        healthy = (
            summary.initialized and summary.wal and summary.schema_version > 0 and summary.reconciliation_required == 0
        )
        return _component(
            "job_repository",
            ComponentState.HEALTHY if healthy else ComponentState.DEGRADED,
            jobs_required,
            "repository_operational" if healthy else "reconciliation_required",
            details=summary.details(),
        )

    probes.append(
        ComponentProbe(
            "job_repository",
            jobs_enabled and jobs_required,
            job_repository_probe,
        )
    )

    auth_mode = str(getattr(cfg.api.auth.mode, "value", cfg.api.auth.mode))
    exchange_required = auth_mode in {"exchange", "hybrid"}
    auth_available = not exchange_required or auth_service is not None
    probes.append(
        _constant_probe(
            "authentication",
            ComponentState.HEALTHY if auth_available else ComponentState.UNAVAILABLE,
            required=exchange_required,
            reason="authentication_available" if auth_available else "exchange_repository_unavailable",
            details={"mode": auth_mode},
        )
    )

    probes.extend(_source_probe(runtime, source) for source in ("nwws_oi", "cap_api", "nws_api"))
    for source, enabled in (
        ("ipaws", bool(cfg.ipaws.enabled)),
        ("ern", bool(cfg.ern.enabled)),
    ):
        probes.append(
            _constant_probe(
                f"source_{source}",
                ComponentState.UNKNOWN if enabled else ComponentState.DISABLED,
                reason="state_unavailable" if enabled else "disabled_by_configuration",
            )
        )

    required_capability_names = tuple(sorted(set(required_capabilities)))
    if len(required_capability_names) > _MAX_REQUIRED_CAPABILITIES:
        raise ValueError("required worker capabilities exceed supported maximum")

    async def workers_probe() -> HealthComponent:
        if capability_registry is None:
            return _component(
                "workers",
                ComponentState.UNAVAILABLE if required_capability_names else ComponentState.NOT_APPLICABLE,
                bool(required_capability_names),
                "required_worker_registry_unavailable" if required_capability_names else "simulated_only",
            )
        now = _utc_now()
        summary = capability_registry.health(now)
        snapshots = capability_registry.snapshots(now)
        satisfied = {
            record.name
            for snapshot in snapshots
            if snapshot.connected and snapshot.trusted and not snapshot.probe_required
            for record in snapshot.records
            if snapshot.effective_capacity.get(record.name, 0) > 0
        }
        missing = set(required_capability_names) - satisfied
        if missing:
            state = ComponentState.UNAVAILABLE
            reason = "required_capability_unavailable"
        elif summary.connected_workers == 0:
            state = ComponentState.NOT_APPLICABLE
            reason = "no_simulated_workers"
        elif summary.unknown_workers or summary.stale_capabilities:
            state = ComponentState.DEGRADED
            reason = "capability_requalification_required"
        else:
            state = ComponentState.HEALTHY
            reason = "capabilities_qualified"
        return _component(
            "workers",
            state,
            bool(required_capability_names),
            reason,
            details={
                "active_assignments": summary.active_assignments,
                "connected_workers": summary.connected_workers,
                "effective_capacity": summary.effective_capacity,
                "missing_required": len(missing),
                "outstanding_probes": summary.outstanding_probes,
                "qualified_workers": summary.qualified_workers,
                "stale_capabilities": summary.stale_capabilities,
                "unknown_workers": summary.unknown_workers,
            },
        )

    probes.append(
        ComponentProbe(
            "workers",
            bool(required_capability_names),
            workers_probe,
        )
    )
    probes.extend(
        (
            _constant_probe(
                "postgresql",
                ComponentState.NOT_APPLICABLE,
                reason="not_implemented",
            ),
            _constant_probe(
                "redis",
                ComponentState.NOT_APPLICABLE,
                reason="not_introduced",
            ),
            _constant_probe(
                "swwp",
                ComponentState.NOT_APPLICABLE,
                reason="not_implemented",
            ),
        )
    )
    return HealthService(
        probes,
        timeout_seconds=timeout_seconds,
        lifecycle_state=lambda: lifecycle.state.value,
    )
