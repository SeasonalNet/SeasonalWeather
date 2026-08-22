from __future__ import annotations

from pathlib import Path
from typing import cast

import yaml

from seasonalweather.artifacts.integration import ArtifactResultCoordinator
from seasonalweather.jobs.policies import FinalCommitAuthority, JobPriority, JobType
from seasonalweather.jobs.registry import policy_for as job_policy_for
from seasonalweather.tts.models import SynthesisPurpose
from seasonalweather.tts.policy import policy_for as synthesis_policy_for
from seasonalweather.worker.profiles import WorkerProfile, capability_manifest, profile_spec

ROOT = Path(__file__).resolve().parents[1]
ObjectMapping = dict[str, object]


def _mapping(value: object) -> ObjectMapping:
    assert isinstance(value, dict)
    return cast(ObjectMapping, value)


def _service(compose: ObjectMapping, name: str) -> ObjectMapping:
    return _mapping(_mapping(compose["services"])[name])


def _strings(value: object) -> list[str]:
    assert isinstance(value, list)
    items = cast(list[object], value)
    assert all(isinstance(item, str) for item in items)
    return cast(list[str], items)


def _compose() -> ObjectMapping:
    loaded = cast(object, yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8")))
    return _mapping(loaded)


def _mounts(service: ObjectMapping) -> dict[str, ObjectMapping]:
    mounts: dict[str, ObjectMapping] = {}
    volumes = service.get("volumes", [])
    assert isinstance(volumes, list)
    for raw_mount in cast(list[object], volumes):
        mount = _mapping(raw_mount)
        target = mount.get("target")
        assert isinstance(target, str)
        mounts[target] = mount
    return mounts


def test_local_tts_profiles_are_explicit_and_remote_backends_are_absent() -> None:
    compose = _compose()
    services = _mapping(compose["services"])

    assert _strings(_service(compose, "piper-worker")["profiles"]) == ["piper"]
    assert _strings(_service(compose, "legacy-tts-worker")["profiles"]) == ["legacy-tts"]
    assert _service(compose, "piper-worker")["image"] == (
        "${SEASONALWEATHER_PIPER_WORKER_IMAGE:-seasonalweather-worker:piper}"
    )
    assert _service(compose, "legacy-tts-worker")["image"] == (
        "${SEASONALWEATHER_LEGACY_TTS_WORKER_IMAGE:-seasonalweather-worker:legacy-tts}"
    )
    assert not {"seasonal-ttsd", "openai-tts", "openai-compatible-tts"}.intersection(services)


def test_local_tts_profiles_use_the_worker_boundary_and_shared_staging() -> None:
    for name, profile in (("piper-worker", "piper"), ("legacy-tts-worker", "legacy-tts")):
        worker = _service(_compose(), name)
        assert _strings(worker["entrypoint"]) == ["python", "-m", "seasonalweather", "worker"]
        command = _strings(worker["command"])
        assert command[command.index("--profile") + 1] == profile
        assert command[1] == "ws://controller:9080/v1/workers/connect"
        assert _mapping(_mapping(worker["depends_on"])["controller"])["condition"] == "service_started"
        assert worker["secrets"] == [
            {
                "source": "SEASONAL_WORKER_TOKEN",
                "target": "SEASONAL_WORKER_TOKEN",
                "uid": "10001",
                "gid": "10001",
                "mode": "0400",
            }
        ]
        assert worker["read_only"] is True
        assert _strings(worker["cap_drop"]) == ["ALL"]
        assert _strings(worker["security_opt"]) == ["no-new-privileges:true"]
        assert worker["user"] == "10001:10001"
        mounts = _mounts(worker)
        assert mounts["/var/lib/seasonalweather/artifacts"]["source"] == "seasonalweather-artifacts"
        assert mounts["/var/lib/seasonalweather/artifacts"]["read_only"] is True
        assert mounts["/var/lib/seasonalweather/artifacts/worker-artifacts/staging"]["source"] == (
            "seasonalweather-artifact-staging"
        )
        assert not mounts["/var/lib/seasonalweather/artifacts/worker-artifacts/staging"].get("read_only", False)


def test_local_profiles_advertise_only_local_tts_and_alert_work() -> None:
    for profile in (WorkerProfile.ROUTINE, WorkerProfile.PIPER, WorkerProfile.LEGACY_TTS):
        spec = profile_spec(profile)
        assert JobType.TTS_SYNTHESIZE in spec.job_types
        assert JobType.ALERT_ARTIFACT_GENERATE in spec.job_types
        assert spec.tts_profile is not None

        manifest = capability_manifest(profile, dependency_probe=lambda _spec: True, handler_ready=True)
        tts = next(record for record in manifest.records if record.name == "tts.synthesis.v1")
        assert tts.parameters["format"] == "wav"
        assert tts.parameters["profiles"] == spec.tts_profile
        assert tts.accepting_new_jobs is True


def test_local_tts_uses_existing_purpose_and_controller_commit_policy() -> None:
    alert = synthesis_policy_for(SynthesisPurpose.ALERT)
    routine = synthesis_policy_for(SynthesisPurpose.ROUTINE)
    assert alert.priority is JobPriority.SAFETY_CRITICAL
    assert alert.strict_deadline is True
    assert alert.job_type is JobType.ALERT_ARTIFACT_GENERATE
    assert routine.job_type is JobType.TTS_SYNTHESIZE
    assert alert.priority is not routine.priority
    assert job_policy_for(JobType.TTS_SYNTHESIZE).final_commit_authority is FinalCommitAuthority.CONTROLLER
    assert job_policy_for(JobType.ALERT_ARTIFACT_GENERATE).final_commit_authority is FinalCommitAuthority.CONTROLLER
    assert ArtifactResultCoordinator.supports(JobType.TTS_SYNTHESIZE)
    assert ArtifactResultCoordinator.supports(JobType.ALERT_ARTIFACT_GENERATE)
