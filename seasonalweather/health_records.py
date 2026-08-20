"""Shared bounded file-backed health records for process health probes."""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

DEFAULT_HEALTH_PATH = str(Path(tempfile.gettempdir()) / "seasonalweather-worker-health.json")
MAX_HEALTH_BYTES = 8192
MAX_HEALTH_AGE_SECONDS = 120.0


def health_path(value: str | None = None) -> Path:
    return Path(value or os.environ.get("SEASONALWEATHER_WORKER_HEALTH_FILE", DEFAULT_HEALTH_PATH))


class HealthRecordStore:
    """Atomically publish process state for an exec health probe."""

    def __init__(self, path: Path, *, clock: Callable[[], dt.datetime]) -> None:
        self.path = path
        self.clock = clock

    def write(
        self,
        *,
        state: str,
        ready: bool,
        registered: bool,
        accepting_new_jobs: bool,
        active_leases: int,
        reason: str,
    ) -> None:
        payload = {
            "schema_version": 1,
            "state": state[:32],
            "ready": bool(ready),
            "registered": bool(registered),
            "accepting_new_jobs": bool(accepting_new_jobs),
            "active_leases": max(0, min(int(active_leases), 32)),
            "reason": reason[:64],
            "updated_at": self._timestamp(),
        }
        encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
        if len(encoded.encode("utf-8")) > MAX_HEALTH_BYTES:
            raise ValueError("health record exceeds the bounded size")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def _timestamp(self) -> str:
        return self.clock().astimezone(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_payload(path: Path) -> tuple[dict[str, object] | None, str | None]:
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_HEALTH_BYTES:
            return None, "health_record_oversized"
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, "health_record_unavailable"
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return None, "health_record_invalid"
    required = {"state", "ready", "registered", "accepting_new_jobs", "updated_at"}
    if set(payload) != required | {"active_leases", "reason", "schema_version"}:
        return None, "health_record_invalid"
    if not all(isinstance(payload.get(key), bool) for key in ("ready", "registered", "accepting_new_jobs")):
        return None, "health_record_invalid"
    if not isinstance(payload.get("state"), str):
        return None, "health_record_invalid"
    return payload, None


def _record_is_fresh(payload: dict[str, object], now: dt.datetime | None) -> bool:
    try:
        updated = dt.datetime.fromisoformat(str(payload["updated_at"]).replace("Z", "+00:00"))
        observed = (now or dt.datetime.now(dt.UTC)) - updated.astimezone(dt.UTC)
    except (TypeError, ValueError):
        return False
    return -5 <= observed.total_seconds() <= MAX_HEALTH_AGE_SECONDS


def _record_status(payload: dict[str, object]) -> tuple[bool, str]:
    if payload["state"] in {"active", "ready"} and payload["ready"] and payload["registered"]:
        return True, "worker_ready"
    if payload["state"] == "stopped":
        return False, "worker_stopped"
    if payload["state"] == "failed":
        return False, "worker_failed"
    if payload["state"] == "draining":
        return False, "worker_draining"
    return False, str(payload.get("reason") or "worker_not_ready")[:64]


def read_health(path: Path, *, now: dt.datetime | None = None) -> tuple[bool, str]:
    payload, error = _read_payload(path)
    if error is not None:
        return False, error
    if payload is None:
        return False, "health_record_invalid"
    if not _record_is_fresh(payload, now):
        return False, "health_record_stale"
    return _record_status(payload)
