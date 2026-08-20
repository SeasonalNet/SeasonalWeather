"""Validation-owned construction of read-only probes from typed configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping

from seasonalweather.configuration.compiler import CompiledConfiguration

from .preflight import (
    PreflightProbe,
    local_executable_probe,
    local_file_separation_probe,
    local_path_probe,
)


def configured_preflight_probes(compiled: CompiledConfiguration) -> tuple[PreflightProbe, ...]:
    """Construct the shared P1-14 probe set without importing CLI authority."""

    if not compiled.valid or compiled.value is None:
        return ()
    return (
        *_path_probes(compiled.value.get("paths")),
        *_job_database_separation_probes(compiled.value),
        *_tts_probes(compiled.value.get("tts")),
        *_api_probes(compiled.value.get("api")),
    )


def _path_probes(paths: object) -> tuple[PreflightProbe, ...]:
    if not isinstance(paths, dict):
        return ()
    probes: list[PreflightProbe] = []
    optional = {"job_state_dir", "diagnostic_export_dir", "temporary_dir", "runtime_dir", "secret_dir"}
    for name in (
        "work_dir",
        "operational_state_dir",
        "job_state_dir",
        "artifact_dir",
        "audio_dir",
        "cache_dir",
        "config_dir",
        "log_dir",
        "diagnostic_export_dir",
        "temporary_dir",
        "runtime_dir",
        "secret_dir",
    ):
        path = paths.get(name)
        if isinstance(path, str) and path:
            probes.append(
                local_path_probe(
                    identifier=f"path.{name}",
                    owner="configuration",
                    path=path,
                    required=name not in optional,
                    directory=True,
                )
            )
    return tuple(probes)


def _job_database_separation_probes(value: Mapping[str, object]) -> tuple[PreflightProbe, ...]:
    jobs = value.get("jobs")
    database = value.get("database")
    paths = value.get("paths")
    if not isinstance(jobs, dict) or not jobs.get("enabled"):
        return ()
    job_path = jobs.get("path")
    database_path = _operational_database_path(database, paths)
    if not isinstance(job_path, str) or not job_path or not isinstance(database_path, str):
        return ()
    return (
        local_file_separation_probe(
            identifier="jobs.database_separation",
            owner="configuration",
            first_path=job_path,
            second_path=database_path,
        ),
    )


def _operational_database_path(database: object, paths: object) -> object:
    selected = database.get("path") if isinstance(database, dict) else None
    if selected or not isinstance(paths, dict):
        return selected
    state_dir = paths.get("operational_state_dir") or paths.get("work_dir")
    if not isinstance(state_dir, str) or not state_dir:
        return None
    return os.path.join(state_dir, "seasonalweather.sqlite3")


def _tts_probes(tts: object) -> tuple[PreflightProbe, ...]:
    if not isinstance(tts, dict):
        return ()
    backend = tts.get("backend")
    fallback = tts.get("fallback_backend")
    remote_probes = _remote_tts_probes(tts, backend, fallback)
    if backend == "local":
        local = tts.get("local")
        if isinstance(local, dict):
            backend = local.get("engine", "espeak-ng")
    commands = {
        "espeak": ("espeak-ng",),
        "espeak-ng": ("espeak-ng",),
        "espeak_ng": ("espeak-ng",),
        "festival": ("text2wave",),
        "piper": ("piper",),
        "dectalk": ("dectalk-env", "/opt/dectalk/dectalk/dist/say"),
        "voicetext_paul": ("sudo",),
    }
    selected = commands.get(backend) if isinstance(backend, str) else None
    if not selected:
        return tuple(remote_probes)
    probes = [
        local_executable_probe(
            identifier=f"tts.backend.{index}",
            owner="tts",
            command=command,
            required=True,
        )
        for index, command in enumerate(selected)
    ]
    return tuple((*remote_probes, *probes))


def _remote_tts_probes(tts: dict[object, object], backend: object, fallback: object) -> list[PreflightProbe]:
    probes: list[PreflightProbe] = []
    definitions = (
        ("seasonal_ttsd", "client_credential_file", "tts.seasonal_ttsd.credential"),
        ("openai_compatible", "api_key_file", "tts.openai_compatible.api_key"),
    )
    for provider, credential_key, identifier in definitions:
        if backend != provider and fallback != provider:
            continue
        section = tts.get(provider)
        path = section.get(credential_key) if isinstance(section, dict) else None
        if isinstance(path, str) and path:
            probes.append(
                local_path_probe(
                    identifier=identifier,
                    owner="tts",
                    path=path,
                    required=backend == provider,
                    directory=False,
                    regular_file=True,
                    fallback_available=backend != provider,
                )
            )
    return probes


def _api_probes(api: object) -> tuple[PreflightProbe, ...]:
    if not isinstance(api, dict) or not isinstance(api.get("ffmpeg_bin"), str):
        return ()
    return (
        local_executable_probe(
            identifier="api.ffmpeg",
            owner="upload",
            command=api["ffmpeg_bin"],
            required=False,
        ),
    )
