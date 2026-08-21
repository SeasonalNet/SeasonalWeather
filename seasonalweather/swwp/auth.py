"""Pre-registration authentication, authorization, and version negotiation."""

from __future__ import annotations

import datetime as dt
import secrets
from dataclasses import dataclass
from typing import Protocol

from ..jobs.policies import JobType, QueueClass
from .constants import PROTOCOL_VERSION, SUBPROTOCOL
from .messages import Register, SelectedVersions


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    principal_id: str
    worker_id: str
    enabled: bool
    revoked: bool
    expires_at: dt.datetime | None
    queues: frozenset[QueueClass]
    job_types: frozenset[JobType]
    capabilities: frozenset[str]

    def is_current(self, now: dt.datetime) -> bool:
        return self.enabled and not self.revoked and (self.expires_at is None or now < self.expires_at)


class RegistrationPolicy(Protocol):
    def principal(self) -> AuthenticatedPrincipal | None: ...

    def authorize(self, registration: Register, now: dt.datetime) -> AuthenticatedPrincipal: ...


@dataclass(frozen=True)
class StaticRegistrationPolicy:
    authenticated: AuthenticatedPrincipal | None

    def principal(self) -> AuthenticatedPrincipal | None:
        return self.authenticated

    def authorize(self, registration: Register, now: dt.datetime) -> AuthenticatedPrincipal:
        principal = self.authenticated
        if principal is None:
            raise PermissionError("transport principal is unauthenticated")
        if not principal.is_current(now):
            raise PermissionError("transport principal is not current")
        if principal.worker_id != registration.worker_id:
            raise PermissionError("transport principal does not match worker identity")
        return principal


@dataclass(frozen=True)
class BearerTokenRegistrationPolicy:
    """Authorize a live worker transport without putting credentials on SWWP."""

    expected_token: str
    presented_token: str
    queues: frozenset[QueueClass]
    job_types: frozenset[JobType]
    capabilities: frozenset[str]

    def principal(self) -> AuthenticatedPrincipal | None:
        return None

    def authorize(self, registration: Register, now: dt.datetime) -> AuthenticatedPrincipal:
        del now
        if not self.expected_token or not secrets.compare_digest(self.presented_token, self.expected_token):
            raise PermissionError("worker transport bearer token is invalid")
        return AuthenticatedPrincipal(
            principal_id="worker-bearer-token",
            worker_id=registration.worker_id,
            enabled=True,
            revoked=False,
            expires_at=None,
            queues=self.queues,
            job_types=self.job_types,
            capabilities=self.capabilities,
        )


@dataclass(frozen=True)
class ControllerVersionSupport:
    swwp: frozenset[int] = frozenset({PROTOCOL_VERSION})
    job_payloads: dict[JobType, frozenset[int]] | None = None
    job_results: dict[JobType, frozenset[int]] | None = None
    diagnostics: frozenset[int] = frozenset({1})
    capability_manifest: frozenset[int] = frozenset({1})
    configuration_schema: frozenset[int] = frozenset({1})


def negotiate_subprotocol(offered: tuple[str, ...]) -> str:
    if offered != (SUBPROTOCOL,):
        raise ValueError("exact SWWP/1 subprotocol is required")
    return SUBPROTOCOL


def _highest(left: set[int] | frozenset[int], right: tuple[int, ...], name: str) -> int:
    common = set(left).intersection(right)
    if not common:
        raise ValueError(f"no compatible {name} version")
    return max(common)


def negotiate_versions(registration: Register, support: ControllerVersionSupport) -> SelectedVersions:
    worker = registration.supported_versions
    payload_support = support.job_payloads or {job_type: frozenset({1}) for job_type in JobType}
    result_support = support.job_results or {job_type: frozenset({1}) for job_type in JobType}
    return SelectedVersions(
        swwp=_highest(support.swwp, worker.swwp, "SWWP"),
        job_payloads=_job_versions(worker.job_payloads, payload_support, "payload"),
        job_results=_job_versions(worker.job_results, result_support, "result"),
        diagnostics=_highest(support.diagnostics, worker.diagnostics, "diagnostic"),
        capability_manifest=_highest(
            support.capability_manifest,
            worker.capability_manifest,
            "capability manifest",
        ),
        configuration_schema=_highest(
            support.configuration_schema,
            worker.configuration_schema,
            "configuration schema",
        ),
    )


def _job_versions(
    worker: dict[JobType, tuple[int, ...]],
    controller: dict[JobType, frozenset[int]],
    name: str,
) -> dict[JobType, int]:
    selected: dict[JobType, int] = {}
    for job_type, versions in worker.items():
        supported = controller.get(job_type, frozenset())
        if set(versions).intersection(supported):
            selected[job_type] = _highest(supported, versions, f"{job_type.value} {name}")
    return selected
