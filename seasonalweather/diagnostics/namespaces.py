"""Authoritative immutable diagnostic namespace registry."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class NamespaceState(StrEnum):
    ACTIVE = "active"
    RESERVED = "reserved"


@dataclass(frozen=True, order=True)
class DiagnosticNamespace:
    token: str
    state: NamespaceState
    scope: str
    owner: str
    remediation_domain: str


NAMESPACES = (
    DiagnosticNamespace(
        "SWBUILD",
        NamespaceState.ACTIVE,
        "build identity, provenance, image profile, and release compatibility",
        "release engineering",
        "build and release compatibility",
    ),
    DiagnosticNamespace(
        "SWCACHE",
        NamespaceState.RESERVED,
        "implementation-neutral cache population, invalidation, expiry, and coherence",
        "architecture",
        "future cache architecture",
    ),
    DiagnosticNamespace(
        "SWCAP",
        NamespaceState.ACTIVE,
        "CAP, IPAWS, JSON-LD/API ingest, normalization, and lifecycle",
        "alert ingestion",
        "CAP and IPAWS source operation",
    ),
    DiagnosticNamespace(
        "SWCFG",
        NamespaceState.ACTIVE,
        "configuration compilation, validation, and reload",
        "configuration",
        "configuration authoring and activation",
    ),
    DiagnosticNamespace(
        "SWDB",
        NamespaceState.ACTIVE,
        "SQLite, PostgreSQL, relational schema, outbox, archive, and migration",
        "persistence",
        "database operation and recovery",
    ),
    DiagnosticNamespace(
        "SWERN",
        NamespaceState.ACTIVE,
        "ERN continuous audio, FFmpeg supervision, SAME AFSK, and lifecycle",
        "ERN ingestion",
        "ERN audio transport and decoding",
    ),
    DiagnosticNamespace(
        "SWJOB",
        NamespaceState.ACTIVE,
        "command, job, lease, execution, cancellation, and result state",
        "job orchestration",
        "command and job processing",
    ),
    DiagnosticNamespace(
        "SWLQS",
        NamespaceState.ACTIVE,
        "Liquidsoap control, queue mutation, and final publication",
        "broadcast publication",
        "Liquidsoap control and publication",
    ),
    DiagnosticNamespace(
        "SWNWWS",
        NamespaceState.ACTIVE,
        "NWWS-OI transport, authentication, MUC membership, and ingest",
        "NWWS ingestion",
        "NWWS-OI connectivity and product ingest",
    ),
    DiagnosticNamespace(
        "SWOBS",
        NamespaceState.ACTIVE,
        "logging, metrics, tracing, syslog, and notification outputs",
        "observability",
        "operational visibility and notification",
    ),
    DiagnosticNamespace(
        "SWREDIS",
        NamespaceState.RESERVED,
        "Redis-specific connectivity, keyspace, eviction, persistence, replication, and coordination",
        "architecture",
        "future Redis-specific operation",
    ),
    DiagnosticNamespace(
        "SWRUN",
        NamespaceState.ACTIVE,
        "process lifecycle, supervision, readiness, and fatal runtime state",
        "runtime",
        "process lifecycle and recovery",
    ),
    DiagnosticNamespace(
        "SWSEG",
        NamespaceState.ACTIVE,
        "segment generation, registry, freshness, and artifact semantics",
        "broadcast segments",
        "segment generation and freshness",
    ),
    DiagnosticNamespace(
        "SWTTS",
        NamespaceState.ACTIVE,
        "synthesis, audio validation, backend selection, and capability",
        "speech synthesis",
        "TTS backend and audio recovery",
    ),
    DiagnosticNamespace(
        "SWWP",
        NamespaceState.ACTIVE,
        "worker protocol, sessions, capabilities, and compatibility",
        "worker protocol",
        "controller and worker interoperability",
    ),
)

NAMESPACE_BY_TOKEN = MappingProxyType({namespace.token: namespace for namespace in NAMESPACES})


def namespace_for_token(token: str) -> DiagnosticNamespace:
    """Resolve one exact canonical namespace token."""
    try:
        return NAMESPACE_BY_TOKEN[token]
    except KeyError as exc:
        raise ValueError(f"unknown diagnostic namespace: {token}") from exc
