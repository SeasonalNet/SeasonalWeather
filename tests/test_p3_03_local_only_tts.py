from __future__ import annotations

import importlib
from pathlib import Path
from typing import cast

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
    loaded = cast(
        object,
        importlib.import_module("yaml").safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8")),
    )
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

    assert _strings(_service(compose, "espeak-worker")["profiles"]) == ["espeak"]
    assert _strings(_service(compose, "piper-worker")["profiles"]) == ["piper"]
    assert _strings(_service(compose, "festival-worker")["profiles"]) == ["festival"]
    assert _strings(_service(compose, "dectalk-worker")["profiles"]) == ["dectalk"]
    assert _strings(_service(compose, "legacy-tts-worker")["profiles"]) == ["legacy-tts"]
    assert _strings(_service(compose, "voicetext-paul-worker")["profiles"]) == ["voicetext-paul"]
    assert _strings(_service(compose, "spfy-worker")["profiles"]) == ["spfy"]
    assert _service(compose, "espeak-worker")["image"] == (
        "${SEASONALWEATHER_ESPEAK_WORKER_IMAGE:-seasonalweather-worker:espeak}"
    )
    assert _service(compose, "piper-worker")["image"] == (
        "${SEASONALWEATHER_PIPER_WORKER_IMAGE:-seasonalweather-worker:piper}"
    )
    assert _service(compose, "festival-worker")["image"] == (
        "${SEASONALWEATHER_FESTIVAL_WORKER_IMAGE:-seasonalweather-worker:festival}"
    )
    assert _service(compose, "dectalk-worker")["image"] == (
        "${SEASONALWEATHER_DECTALK_WORKER_IMAGE:-seasonalweather-worker:dectalk}"
    )
    assert _service(compose, "legacy-tts-worker")["image"] == (
        "${SEASONALWEATHER_LEGACY_TTS_WORKER_IMAGE:-seasonalweather-worker:legacy-tts}"
    )
    assert _service(compose, "voicetext-paul-worker")["image"] == (
        "${SEASONALWEATHER_VOICETEXT_PAUL_WORKER_IMAGE:-seasonalweather-worker:voicetext-paul}"
    )
    assert _service(compose, "spfy-worker")["image"] == (
        "${SEASONALWEATHER_SPFY_WORKER_IMAGE:-seasonalweather-worker:spfy}"
    )
    assert not {"seasonal-ttsd", "openai-tts", "openai-compatible-tts"}.intersection(services)


def test_local_tts_profiles_use_the_worker_boundary_and_shared_staging() -> None:
    for name, profile in (
        ("espeak-worker", "espeak"),
        ("piper-worker", "piper"),
        ("festival-worker", "festival"),
        ("dectalk-worker", "dectalk"),
        ("legacy-tts-worker", "legacy-tts"),
        ("voicetext-paul-worker", "voicetext-paul"),
        ("spfy-worker", "spfy"),
    ):
        worker = _service(_compose(), name)
        expected_entrypoint = (
            ["/usr/local/bin/voicetext-paul-entrypoint"]
            if profile == "voicetext-paul"
            else ["python", "-m", "seasonalweather", "worker"]
        )
        assert _strings(worker["entrypoint"]) == expected_entrypoint
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


def test_specialized_workers_mount_only_engine_runtime_state() -> None:
    compose = _compose()
    voicetext = _service(compose, "voicetext-paul-worker")
    spfy = _service(compose, "spfy-worker")
    voicetext_environment = _mapping(voicetext["environment"])
    spfy_environment = _mapping(spfy["environment"])

    assert voicetext_environment["DISPLAY"] == ":99"
    assert voicetext_environment["VOICETEXT_PAUL_TMPDIR"] == "/tmp/voicetext"
    assert voicetext_environment["VOICETEXT_PAUL_LOCK_PATH"] == "/tmp/voicetext/voicetext.lock"
    voicetext_mounts = _mounts(voicetext)
    assert voicetext_mounts["/var/lib/seasonalweather/voices/voicetext_paul"]["source"] == (
        "seasonalweather-voicetext-paul-voices"
    )
    assert voicetext_mounts["/var/lib/seasonalweather/wineprefixes"]["source"] == (
        "seasonalweather-voicetext-paul-wineprefixes"
    )
    assert spfy_environment["SPFY_VOICE_DIR"] == "/opt/spfy"
    assert spfy_environment["SPFY_NO_UPDATE_CHECK"] == "1"

    piper_environment = _mapping(_service(compose, "piper-worker")["environment"])
    assert piper_environment["PIPER_MODEL_DIR"] == "/opt/piper/models"
    piper_mounts = _mounts(_service(compose, "piper-worker"))
    assert piper_mounts["/opt/piper/models"]["source"] == (
        "${SEASONALWEATHER_PIPER_MODEL_DIR:-/var/lib/seasonalweather/piper-models}"
    )


def test_local_profiles_advertise_only_local_tts_and_alert_work() -> None:
    for profile in (
        WorkerProfile.ROUTINE,
        WorkerProfile.ESPEAK,
        WorkerProfile.PIPER,
        WorkerProfile.FESTIVAL,
        WorkerProfile.DECTALK,
        WorkerProfile.LEGACY_TTS,
        WorkerProfile.VOICETEXT_PAUL,
        WorkerProfile.SPFY,
    ):
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
