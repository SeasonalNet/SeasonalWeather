"""Crash-safe local controller marker and next-start reconciliation."""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import stat
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from seasonalweather import __version__
from seasonalweather.diagnostics.bindings import RUNTIME_CODES

from .models import CorrelationContext, DiagnosticRole, PromotionReason, timestamp
from .redaction import redact_text
from .service import RuntimeDiagnosticService

MARKER_SCHEMA_VERSION = 1
MAX_MARKER_BYTES = 4096
MARKER_STAGES = frozenset({"starting", "running", "draining", "stopping", "stopped", "failed"})


@dataclass(frozen=True)
class ProcessMarker:
    role: DiagnosticRole
    instance_id: str
    process_id: int
    started_at: dt.datetime
    application_version: str
    configuration_generation: int | None
    lifecycle_stage: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "marker_schema_version": MARKER_SCHEMA_VERSION,
            "role": self.role.value,
            "instance_id": self.instance_id,
            "process_id": self.process_id,
            "started_at": timestamp(self.started_at),
            "application_version": self.application_version,
            "configuration_generation": self.configuration_generation,
            "lifecycle_stage": self.lifecycle_stage,
        }


@dataclass(frozen=True)
class PriorControllerEvidence:
    role: DiagnosticRole
    prior_controller_instance_id: str
    advisory_pid: int
    started_at: str
    application_identity: str
    configuration_generation: int | None
    lifecycle_stage: str

    @classmethod
    def from_marker(cls, marker: Mapping[str, Any]) -> PriorControllerEvidence:
        required = {
            "marker_schema_version",
            "role",
            "instance_id",
            "process_id",
            "started_at",
            "application_version",
            "configuration_generation",
            "lifecycle_stage",
        }
        if set(marker) != required:
            raise RuntimeError("prior process marker fields are invalid")
        started_at = _marker_timestamp(marker["started_at"])
        role = DiagnosticRole(marker["role"])
        return cls(
            role=role,
            prior_controller_instance_id=redact_text(marker["instance_id"], limit=128),
            advisory_pid=_bounded_marker_integer(marker["process_id"], "process ID", minimum=1),
            started_at=started_at,
            application_identity=redact_text(marker["application_version"], limit=128),
            configuration_generation=(
                _bounded_marker_integer(
                    marker["configuration_generation"],
                    "configuration generation",
                    minimum=0,
                )
                if marker["configuration_generation"] is not None
                else None
            ),
            lifecycle_stage=str(marker["lifecycle_stage"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role.value,
            "prior_controller_instance_id": self.prior_controller_instance_id,
            "advisory_pid": self.advisory_pid,
            "started_at": self.started_at,
            "application_identity": self.application_identity,
            "configuration_generation": self.configuration_generation,
            "lifecycle_stage": self.lifecycle_stage,
        }


class ProcessMarkerStore:
    def __init__(self, state_root: Path) -> None:
        self.state_root = state_root
        self.current_path = state_root / "controller-runtime.json"
        self.pending_path = state_root / "controller-runtime.previous.json"
        self.lock_path = state_root / ".controller-runtime.lock"
        self._instance_id: str | None = None
        self._lock_fd: int | None = None

    def start(self, marker: ProcessMarker) -> dict[str, Any] | None:
        self._prepare_state_root()
        self._acquire_lifetime_lock()
        try:
            prior = self._read_marker(self.current_path) if self.current_path.exists() else None
            if prior is not None:
                if self.pending_path.exists():
                    pending = self._read_marker(self.pending_path)
                    if pending != prior:
                        raise RuntimeError("unreconciled prior marker already exists")
                else:
                    self._atomic_write(self.pending_path, prior)
            self._atomic_write(self.current_path, marker.to_dict())
            self._instance_id = marker.instance_id
            return prior
        except BaseException:
            self._release_lifetime_lock()
            raise

    def update_stage(self, stage: str) -> None:
        if self._instance_id is None:
            raise RuntimeError("process marker has not started")
        if stage not in MARKER_STAGES:
            raise ValueError("process marker lifecycle stage is invalid")
        current = self._read_marker(self.current_path)
        if current.get("instance_id") != self._instance_id:
            raise RuntimeError("current marker belongs to another instance")
        current["lifecycle_stage"] = stage
        self._atomic_write(self.current_path, current)

    def mark_clean(self) -> None:
        if self._instance_id is None:
            return
        try:
            current = self._read_marker(self.current_path)
            if current.get("instance_id") != self._instance_id:
                raise RuntimeError("refusing to remove another instance marker")
            self.current_path.unlink()
            _fsync_directory(self.state_root)
            self._instance_id = None
        finally:
            self._release_lifetime_lock()

    def reconcile_pending(
        self,
        service: RuntimeDiagnosticService,
        *,
        current_context: CorrelationContext,
    ):
        if not self.pending_path.exists():
            return None
        prior = self._read_marker(self.pending_path)
        prior_evidence = PriorControllerEvidence.from_marker(prior)
        instance = service.build(
            code=RUNTIME_CODES["prior_incomplete_shutdown"],
            context=CorrelationContext(
                role=current_context.role,
                instance_id=current_context.instance_id,
                component="controller-lifecycle",
                build_identity=current_context.build_identity,
                configuration_generation=current_context.configuration_generation,
                reason_code="prior_incomplete_shutdown",
            ),
            message=(
                "The previous controller instance did not complete clean shutdown "
                f"after lifecycle stage {prior['lifecycle_stage']}."
            ),
            operational_effect="Prior in-process shutdown reporting may be incomplete.",
            recovery_action="Review external service and kernel records without assuming an exact cause.",
            promotion_reason=PromotionReason.RECONCILIATION,
            exception_evidence={"prior_controller": prior_evidence.to_dict()},
        )
        result = service.promote(instance)
        if self.pending_path.exists() and self._read_marker(self.pending_path) == prior:
            self.pending_path.unlink()
            _fsync_directory(self.state_root)
        return result

    def _read_marker(self, path: Path) -> dict[str, Any]:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise RuntimeError("unsafe process marker type")
        if info.st_size > MAX_MARKER_BYTES or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise RuntimeError("unsafe process marker size, ownership, or permissions")
        data = path.read_bytes()
        try:
            raw = json.loads(data)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("process marker is malformed") from exc
        return _validate_marker(raw)

    def _atomic_write(self, path: Path, value: dict[str, Any]) -> None:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if len(encoded) > MAX_MARKER_BYTES:
            raise ValueError("process marker exceeds byte limit")
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=self.state_root)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=True) as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            _fsync_directory(self.state_root)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)

    def _acquire_lifetime_lock(self) -> None:
        if self._lock_fd is not None:
            raise RuntimeError("process marker is already owned")
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise RuntimeError("unsafe process marker lock")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise RuntimeError("another controller startup owns the process marker") from exc
        except BaseException:
            os.close(fd)
            raise
        self._lock_fd = fd

    def _prepare_state_root(self) -> None:
        try:
            info = self.state_root.lstat()
        except FileNotFoundError:
            self.state_root.mkdir(parents=True, mode=0o700)
            info = self.state_root.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o022
        ):
            raise RuntimeError("unsafe process marker state root")

    def _release_lifetime_lock(self) -> None:
        if self._lock_fd is None:
            return
        fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        os.close(self._lock_fd)
        self._lock_fd = None


def _validate_marker(raw: object) -> dict[str, Any]:
    required = {
        "marker_schema_version",
        "role",
        "instance_id",
        "process_id",
        "started_at",
        "application_version",
        "configuration_generation",
        "lifecycle_stage",
    }
    if not isinstance(raw, dict) or set(raw) != required or raw["marker_schema_version"] != MARKER_SCHEMA_VERSION:
        raise RuntimeError("process marker version or fields are invalid")
    _validate_marker_identity(raw)
    return raw


def _validate_marker_identity(raw: dict[str, Any]) -> None:
    if raw["role"] not in {item.value for item in DiagnosticRole}:
        raise RuntimeError("process marker role is invalid")
    _bounded_marker_integer(raw["process_id"], "process ID", minimum=1)
    for key in ("instance_id", "started_at", "application_version", "lifecycle_stage"):
        _validate_marker_text(raw[key])
    _marker_timestamp(raw["started_at"])
    if raw["lifecycle_stage"] not in MARKER_STAGES:
        raise RuntimeError("process marker lifecycle stage is invalid")
    if not _valid_generation(raw["configuration_generation"]):
        raise RuntimeError("process marker configuration generation is invalid")


def _validate_marker_text(value: object) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise RuntimeError("process marker text is invalid")


def _valid_generation(value: object) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2_147_483_647)


def _bounded_marker_integer(value: object, name: str, *, minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= 2_147_483_647:
        raise RuntimeError(f"process marker {name} is invalid")
    return value


def _marker_timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise RuntimeError("process marker start timestamp is invalid")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("process marker start timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise RuntimeError("process marker start timestamp is invalid")
    return timestamp(parsed)


def controller_marker(
    *,
    instance_id: str,
    configuration_generation: int | None = None,
    now: dt.datetime | None = None,
) -> ProcessMarker:
    return ProcessMarker(
        role=DiagnosticRole.CONTROLLER,
        instance_id=instance_id,
        process_id=os.getpid(),
        started_at=now or dt.datetime.now(dt.UTC),
        application_version=__version__,
        configuration_generation=configuration_generation,
        lifecycle_stage="starting",
    )


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
