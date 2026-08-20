"""SWWP/1 identity, limits, state, and protocol-local vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

PROTOCOL_NAME = "SWWP"
PROTOCOL_VERSION = 1
SUBPROTOCOL = "seasonalweather.worker.v1"


@dataclass(frozen=True)
class ProtocolLimits:
    max_message_bytes: int = 65_536
    max_string_chars: int = 2_048
    max_collection_items: int = 64
    max_map_items: int = 64
    max_depth: int = 12
    max_version_entries: int = 32
    max_heartbeat_leases: int = 32
    max_reconciliation_items: int = 64
    max_retained_errors: int = 16
    min_heartbeat_seconds: int = 5
    max_heartbeat_seconds: int = 300

    def __post_init__(self) -> None:
        if any(value <= 0 for value in vars(self).values()):
            raise ValueError("SWWP limits must be positive")


DEFAULT_LIMITS = ProtocolLimits()


class ControllerState(StrEnum):
    AWAITING_REGISTRATION = "awaiting_registration"
    ACTIVE = "active"
    DRAINING = "draining"
    CLOSED = "closed"
    REJECTED = "rejected"
    FAILED = "failed"


class WorkerState(StrEnum):
    DISCONNECTED = "disconnected"
    REGISTERING = "registering"
    ACTIVE = "active"
    DRAINING = "draining"
    RECONCILING = "reconciling"
    CLOSED = "closed"
    FAILED = "failed"


class WorkerReadinessState(StrEnum):
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    DRAINING = "draining"
    STOPPED = "stopped"
    FAILED = "failed"


class ProtocolErrorCategory(StrEnum):
    MALFORMED_JSON = "malformed_json"
    INVALID_ENVELOPE = "invalid_envelope"
    INVALID_PAYLOAD = "invalid_payload"
    UNKNOWN_MESSAGE_TYPE = "unknown_message_type"
    UNSUPPORTED_VERSION = "unsupported_version"
    UNAUTHENTICATED = "unauthenticated"
    UNAUTHORIZED = "unauthorized"
    REGISTRATION_REQUIRED = "registration_required"
    STATE_VIOLATION = "state_violation"
    STALE_SESSION = "stale_session"
    UNKNOWN_JOB = "unknown_job"
    STALE_LEASE = "stale_lease"
    SCHEMA_MISMATCH = "schema_mismatch"
    OVERSIZED = "oversized"
    RATE_SEQUENCE = "rate_sequence"
    INTERNAL_REJECTION = "internal_rejection"


class ReconcileDisposition(StrEnum):
    RESUME = "resume"
    RENEW = "renew"
    CANCEL = "cancel"
    RESEND_RESULT = "resend_result"
    ALREADY_COMMITTED = "already_committed"
    REVALIDATION_REQUIRED = "revalidation_required"
    DISCARD_STALE = "discard_stale"
    UNKNOWN = "unknown"
