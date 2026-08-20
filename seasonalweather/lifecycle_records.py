"""Bounded structured startup and lifecycle records shared by runtimes."""

from __future__ import annotations

import datetime as dt
import json
import platform
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from .build_metadata import BuildInfo


class LifecycleStage(StrEnum):
    SERVICE_STARTING = "service_starting"
    CONFIGURATION_VALIDATED = "configuration_validated"
    STORAGE_READY = "storage_ready"
    CONTROL_PLANE_READY = "control_plane_ready"
    BROADCAST_PATH_READY = "broadcast_path_ready"
    SOURCES_STARTING = "sources_starting"
    SERVICE_READY = "service_ready"
    SERVICE_STARTED_DEGRADED = "service_started_degraded"
    BACKGROUND_WARMUP_COMPLETE = "background_warmup_complete"
    SERVICE_DRAINING = "service_draining"
    SERVICE_STOPPED = "service_stopped"


@dataclass(frozen=True)
class LifecycleRecordWriter:
    """Write bounded JSON records without depending on logging configuration."""

    role: str
    instance_id: str
    build_info: BuildInfo
    output: Callable[[str], object] | None = None
    clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC)
    last_stage: LifecycleStage | None = field(default=None, init=False, compare=False)

    def startup_identity(self, *, image_profile: str | None = None) -> None:
        info = self.build_info
        self._write(
            {
                "event": "startup_identity",
                "role": self.role,
                "instance_id": self.instance_id,
                "software_version": info.software_version,
                "source_revision": info.git_commit,
                "build_id": info.build_id,
                "build_identity": info.build_identity,
                "image_profile": image_profile or info.image_profile,
                "python_version": info.python_version,
                "platform": platform.platform()[:128],
                "swwp_protocol_versions": info.swwp_protocol_versions,
                "job_payload_schema_versions": info.job_payload_schema_versions,
                "job_result_schema_versions": info.job_result_schema_versions,
                "validation_protocol_versions": info.validation_protocol_versions,
                "configuration_schema": info.configuration_schema,
                "diagnostic_schema_version": info.diagnostic_schema_version,
                "diagnostic_catalog_version": info.diagnostic_catalog_version,
                "capability_manifest_version": info.capability_manifest_version,
            }
        )

    def stage(self, stage: LifecycleStage, *, ready: bool | None = None, reason: str | None = None) -> None:
        object.__setattr__(self, "last_stage", stage)
        record: dict[str, object] = {"event": stage.value, "role": self.role, "instance_id": self.instance_id}
        if ready is not None:
            record["ready"] = ready
        if reason is not None:
            record["reason"] = reason[:64]
        self._write(record)

    def _write(self, record: dict[str, object]) -> None:
        record["observed_at"] = self._timestamp()
        payload = json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        if self.output is not None:
            self.output(payload)
            return
        print(payload, file=sys.stdout, flush=True)

    def _timestamp(self) -> str:
        return self.clock().astimezone(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
