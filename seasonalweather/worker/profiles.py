"""Truthful worker profiles and SWWP registration construction.

Profiles describe what a worker process contains.  They do not grant
controller authority: authorization, compatibility, qualification, capacity,
and durable job state remain controller-owned.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from ..build_metadata import current_build_info
from ..capabilities.manifest import MANIFEST_SCHEMA_VERSION
from ..capabilities.manifest import CapabilityManifest as DomainManifest
from ..capabilities.models import CapabilityRecord, CompatibilityState, OperationalState, ParameterValue
from ..jobs.policies import JobType, QueueClass
from ..jobs.registry import policy_for
from ..swwp.capability_adapter import record_to_wire
from ..swwp.constants import PROTOCOL_VERSION
from ..swwp.messages import (
    CapabilityManifest,
    Register,
    VersionSupport,
)


class WorkerProfile(StrEnum):
    ROUTINE = "routine-worker"
    PIPER = "piper"
    LEGACY_TTS = "legacy-tts"
    MAINTENANCE = "maintenance"
    DEVELOPMENT = "development"


@dataclass(frozen=True)
class WorkerProfileSpec:
    profile: WorkerProfile
    queues: tuple[QueueClass, ...]
    job_types: tuple[JobType, ...]
    capabilities: tuple[str, ...]
    tts_profile: str | None
    required_executables: tuple[str, ...] = ()


_ROUTINE_JOBS = (
    JobType.SEGMENT_BUILD,
    JobType.TTS_SYNTHESIZE,
    JobType.AUDIO_CONVERT,
    JobType.CYCLE_REGENERATE,
    JobType.ALERT_ARTIFACT_GENERATE,
)
_ROUTINE_CAPABILITIES = (
    "segment.build.v1",
    "tts.synthesis.v1",
    "audio.convert.wav.v1",
    "cycle.regenerate.v1",
    "audio.alert_artifact.v1",
)

_PROFILES = {
    WorkerProfile.ROUTINE: WorkerProfileSpec(
        profile=WorkerProfile.ROUTINE,
        queues=(QueueClass.ROUTINE,),
        job_types=_ROUTINE_JOBS,
        capabilities=_ROUTINE_CAPABILITIES,
        tts_profile="standard",
        required_executables=("ffmpeg", "espeak-ng"),
    ),
    WorkerProfile.PIPER: WorkerProfileSpec(
        profile=WorkerProfile.PIPER,
        queues=(QueueClass.ROUTINE,),
        job_types=(JobType.TTS_SYNTHESIZE, JobType.ALERT_ARTIFACT_GENERATE),
        capabilities=("tts.synthesis.v1", "audio.alert_artifact.v1"),
        tts_profile="piper",
        required_executables=("piper",),
    ),
    WorkerProfile.LEGACY_TTS: WorkerProfileSpec(
        profile=WorkerProfile.LEGACY_TTS,
        queues=(QueueClass.ROUTINE,),
        job_types=(JobType.TTS_SYNTHESIZE, JobType.ALERT_ARTIFACT_GENERATE),
        capabilities=("tts.synthesis.v1", "audio.alert_artifact.v1"),
        tts_profile="legacy-tts",
        required_executables=("text2wave",),
    ),
    WorkerProfile.MAINTENANCE: WorkerProfileSpec(
        profile=WorkerProfile.MAINTENANCE,
        queues=(QueueClass.MAINTENANCE,),
        job_types=(JobType.MAINTENANCE_RECONCILE,),
        capabilities=("maintenance.reconcile.v1",),
        tts_profile=None,
    ),
    WorkerProfile.DEVELOPMENT: WorkerProfileSpec(
        profile=WorkerProfile.DEVELOPMENT,
        queues=(QueueClass.ROUTINE, QueueClass.MAINTENANCE),
        job_types=(*_ROUTINE_JOBS, JobType.MAINTENANCE_RECONCILE),
        capabilities=(*_ROUTINE_CAPABILITIES, "maintenance.reconcile.v1"),
        tts_profile="development",
    ),
}


def profile_spec(profile: WorkerProfile | str) -> WorkerProfileSpec:
    try:
        return _PROFILES[WorkerProfile(profile)]
    except (KeyError, ValueError) as exc:
        raise ValueError(f"unknown worker profile: {profile}") from exc


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:20]}"


def _dependency_available(spec: WorkerProfileSpec) -> bool:
    return all(shutil.which(executable) for executable in spec.required_executables)


def _capability_parameters(name: str, spec: WorkerProfileSpec) -> dict[str, ParameterValue]:
    if name == "tts.synthesis.v1":
        return {
            "format": "wav",
            "profiles": spec.tts_profile or "none",
            "sample_rates": (48_000,),
            "max_input_bytes": 65_536,
        }
    if name == "audio.convert.wav.v1":
        return {"format": "wav", "sample_rates": (48_000,)}
    return {}


def _capability_record(
    name: str,
    *,
    spec: WorkerProfileSpec,
    now: dt.datetime,
    slots: int,
    dependency_available: bool,
    handler_ready: bool,
) -> CapabilityRecord:
    job_restrictions = tuple(
        sorted(
            job_type.value
            for job_type in spec.job_types
            if name in {requirement.name for requirement in policy_for(job_type).capabilities}
        )
    )
    implemented = handler_ready
    available = implemented and dependency_available
    return CapabilityRecord(
        name=name,
        implemented=implemented,
        compatibility=CompatibilityState.UNKNOWN,
        operational_state=OperationalState.HEALTHY if available else OperationalState.UNAVAILABLE,
        accepting_new_jobs=available,
        total_capacity=slots if implemented else 0,
        reported_available=slots if available else 0,
        job_restrictions=job_restrictions,
        parameters=_capability_parameters(name, spec),
        validity_seconds=60,
        observed_at=now,
        published_at=now,
    )


def capability_manifest(
    profile: WorkerProfile | str,
    *,
    epoch: int = 1,
    slots: int = 1,
    now: dt.datetime | None = None,
    handler_ready: bool = False,
    dependency_probe: Callable[[WorkerProfileSpec], bool] = _dependency_available,
) -> CapabilityManifest:
    spec = profile_spec(profile)
    if epoch < 1 or not 1 <= slots <= 128:
        raise ValueError("worker epoch and slots must be positive and bounded")
    observed = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC).replace(microsecond=0)
    available = dependency_probe(spec)
    records = tuple(
        _capability_record(
            name,
            spec=spec,
            now=observed,
            slots=slots,
            dependency_available=available,
            handler_ready=handler_ready,
        )
        for name in spec.capabilities
    )
    domain_manifest = DomainManifest.create(
        schema_version=MANIFEST_SCHEMA_VERSION,
        epoch=epoch,
        records=records,
    )
    return CapabilityManifest(
        schema_version=domain_manifest.schema_version,
        epoch=domain_manifest.epoch,
        digest=domain_manifest.digest,
        records=tuple(record_to_wire(record) for record in domain_manifest.records),
    )


def registration_for_profile(
    profile: WorkerProfile | str,
    *,
    worker_id: str,
    worker_instance_id: str | None = None,
    worker_epoch: int = 1,
    slots: int = 1,
    now: dt.datetime | None = None,
    handler_ready: bool = False,
    dependency_probe: Callable[[WorkerProfileSpec], bool] = _dependency_available,
) -> Register:
    spec = profile_spec(profile)
    info = current_build_info()
    manifest = capability_manifest(
        spec.profile,
        epoch=worker_epoch,
        slots=slots,
        now=now,
        handler_ready=handler_ready,
        dependency_probe=dependency_probe,
    )
    supported: dict[JobType, tuple[int, ...]] = {
        job_type: (policy_for(job_type).payload_schema_version,) for job_type in spec.job_types
    }
    results: dict[JobType, tuple[int, ...]] = {
        job_type: (policy_for(job_type).result_schema_version,) for job_type in spec.job_types
    }
    return Register(
        worker_id=worker_id,
        worker_instance_id=worker_instance_id or _id("instance"),
        worker_epoch=worker_epoch,
        software_version=info.software_version,
        build_identity=info.build_identity,
        requested_queues=spec.queues,
        requested_slots=slots,
        capability_manifest=manifest,
        supported_versions=VersionSupport(
            swwp=(PROTOCOL_VERSION,),
            job_payloads=supported,
            job_results=results,
            diagnostics=(info.diagnostic_schema_version,),
            capability_manifest=(info.capability_manifest_version,),
            configuration_schema=tuple(sorted(set(info.configuration_schema))),
        ),
    )


def worker_id_from_environment(profile: WorkerProfile | str) -> str:
    value = os.environ.get("SEASONALWEATHER_WORKER_ID", "").strip()
    return value or f"{WorkerProfile(profile).value}-{os.uname().nodename[:32]}"
