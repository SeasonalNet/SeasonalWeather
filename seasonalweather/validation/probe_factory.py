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
    for name in ("work_dir", "audio_dir", "cache_dir", "config_dir", "log_dir"):
        path = paths.get(name)
        if isinstance(path, str) and path:
            probes.append(
                local_path_probe(
                    identifier=f"path.{name}",
                    owner="configuration",
                    path=path,
                    required=True,
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
    work_dir = paths.get("work_dir")
    return os.path.join(work_dir, "seasonalweather.sqlite3") if isinstance(work_dir, str) and work_dir else None


def _tts_probes(tts: object) -> tuple[PreflightProbe, ...]:
    if not isinstance(tts, dict):
        return ()
    backend = tts.get("backend")
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
        return ()
    probes = [
        local_executable_probe(
            identifier=f"tts.backend.{index}",
            owner="tts",
            command=command,
            required=True,
        )
        for index, command in enumerate(selected)
    ]
    return tuple(probes)


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
