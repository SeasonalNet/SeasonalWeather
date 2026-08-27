"""Phase 2 image and runtime-boundary exit-gate validation."""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from seasonalweather.jobs.policies import ExecutorClass, QueueClass
from seasonalweather.jobs.registry import JOB_TYPE_POLICIES
from tools.build_interface import IMAGE_TARGETS

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ImageSpec:
    """One declared Phase 2 image target and its runtime boundary."""

    profile: str
    tag: str
    role: str
    entrypoint: tuple[str, ...]
    healthcheck: tuple[str, ...]


IMAGE_SPECS = (
    ImageSpec(
        profile="controller",
        tag="seasonalweather:standard",
        role="controller",
        entrypoint=("python", "-m", "seasonalweather.api.server"),
        healthcheck=("CMD", "python", "-m", "seasonalweather", "health", "controller", "--mode", "readiness"),
    ),
    ImageSpec(
        profile="routine-worker",
        tag="seasonalweather-worker:standard",
        role="worker",
        entrypoint=("python", "-m", "seasonalweather", "worker"),
        healthcheck=("CMD", "python", "-m", "seasonalweather", "health", "worker", "--mode", "liveness"),
    ),
    ImageSpec(
        profile="piper",
        tag="seasonalweather-worker:piper",
        role="worker",
        entrypoint=("python", "-m", "seasonalweather", "worker"),
        healthcheck=("CMD", "python", "-m", "seasonalweather", "health", "worker", "--mode", "liveness"),
    ),
    ImageSpec(
        profile="legacy-tts",
        tag="seasonalweather-worker:legacy-tts",
        role="worker",
        entrypoint=("python", "-m", "seasonalweather", "worker"),
        healthcheck=("CMD", "python", "-m", "seasonalweather", "health", "worker", "--mode", "liveness"),
    ),
    ImageSpec(
        profile="voicetext-paul",
        tag="seasonalweather-worker:voicetext-paul",
        role="worker",
        entrypoint=("python", "-m", "seasonalweather", "worker"),
        healthcheck=("CMD", "python", "-m", "seasonalweather", "health", "worker", "--mode", "liveness"),
    ),
    ImageSpec(
        profile="spfy",
        tag="seasonalweather-worker:spfy",
        role="worker",
        entrypoint=("python", "-m", "seasonalweather", "worker"),
        healthcheck=("CMD", "python", "-m", "seasonalweather", "health", "worker", "--mode", "liveness"),
    ),
    ImageSpec(
        profile="maintenance",
        tag="seasonalweather-worker:maintenance",
        role="worker",
        entrypoint=("python", "-m", "seasonalweather", "worker"),
        healthcheck=("CMD", "python", "-m", "seasonalweather", "health", "worker", "--mode", "liveness"),
    ),
    ImageSpec(
        profile="development",
        tag="seasonalweather:development",
        role="worker",
        entrypoint=("python", "-m", "seasonalweather", "worker"),
        healthcheck=("CMD", "python", "-m", "seasonalweather", "health", "worker", "--mode", "liveness"),
    ),
)

_CONTROLLER_FORBIDDEN_MODULES = ("seasonalweather.worker",)
_WORKER_FORBIDDEN_MODULES = (
    "seasonalweather.api",
    "seasonalweather.broadcast",
    "seasonalweather.database",
    "seasonalweather.main",
    "seasonalweather.nwws",
)
_WORKER_IMPORTS = ("fastapi", "slixmpp", "sqlalchemy", "uvicorn")
_STABLE_BUILD_FIELDS = (
    "project",
    "software_version",
    "git_commit",
    "git_describe",
    "dirty_tree",
    "build_source_timestamp",
    "source_date_epoch",
    "build_identity",
    "target_platform",
    "python_version",
    "swwp_protocol_versions",
    "job_payload_schema_versions",
    "job_result_schema_versions",
    "validation_protocol_versions",
    "configuration_schema",
    "diagnostic_schema_version",
    "diagnostic_catalog_version",
    "capability_manifest_version",
)


class GateError(RuntimeError):
    """Raised when a built-image gate operation cannot complete safely."""


def _docker(*arguments: str) -> str:
    try:
        result = subprocess.run(
            ["docker", *arguments],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise GateError(f"cannot start Docker: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
        raise GateError(f"Docker {' '.join(arguments[:3])} failed: {detail}")
    return result.stdout


def _docker_json(*arguments: str) -> Any:
    output = _docker(*arguments)
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise GateError(f"Docker {' '.join(arguments[:3])} returned invalid JSON") from exc


def _image_inspect(spec: ImageSpec) -> dict[str, Any]:
    payload = _docker_json("image", "inspect", spec.tag)
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise GateError(f"image inspect returned an invalid record for {spec.tag}")
    return payload[0]


def _embedded_json(spec: ImageSpec, *command: str) -> Any:
    return _docker_json("run", "--rm", "--entrypoint", "python", spec.tag, *command)


def _embedded_file_json(spec: ImageSpec, path: str) -> Any:
    script = f"import json; print(json.dumps(json.load(open({path!r}, encoding='utf-8')), sort_keys=True))"
    return _embedded_json(spec, "-c", script)


def _package_presence(spec: ImageSpec) -> dict[str, bool]:
    names = (*_CONTROLLER_FORBIDDEN_MODULES, *_WORKER_FORBIDDEN_MODULES, *_WORKER_IMPORTS)
    script = (
        "import importlib.util, json; "
        f"names={names!r}; "
        "print(json.dumps({name: importlib.util.find_spec(name) is not None for name in names}, sort_keys=True))"
    )
    payload = _embedded_json(spec, "-c", script)
    if not isinstance(payload, dict) or any(
        not isinstance(key, str) or not isinstance(value, bool) for key, value in payload.items()
    ):
        raise GateError(f"package-boundary probe returned an invalid record for {spec.tag}")
    return payload


def _label_expectations(info: dict[str, Any]) -> dict[str, str]:
    schema = info["configuration_schema"]
    return {
        "org.opencontainers.image.title": str(info["project"]),
        "org.opencontainers.image.version": str(info["software_version"]),
        "org.opencontainers.image.revision": str(info["git_commit"]),
        "org.opencontainers.image.created": str(info["build_source_timestamp"] or ""),
        "org.opencontainers.image.ref.name": str(info["git_describe"]),
        "io.seasonalweather.build.id": str(info["build_id"]),
        "io.seasonalweather.build.identity": str(info["build_identity"]),
        "io.seasonalweather.build.dirty": "true" if info["dirty_tree"] else "false",
        "io.seasonalweather.build.profile": str(info["image_profile"]),
        "io.seasonalweather.build.target-platform": str(info["target_platform"]),
        "io.seasonalweather.build.source-date-epoch": str(info["source_date_epoch"] or ""),
        "io.seasonalweather.schema.swwp": ",".join(str(item) for item in info["swwp_protocol_versions"]),
        "io.seasonalweather.schema.job-payload": ",".join(str(item) for item in info["job_payload_schema_versions"]),
        "io.seasonalweather.schema.job-result": ",".join(str(item) for item in info["job_result_schema_versions"]),
        "io.seasonalweather.schema.validation": ",".join(str(item) for item in info["validation_protocol_versions"]),
        "io.seasonalweather.schema.configuration": f"{schema['minimum']}-{schema['maximum']}",
        "io.seasonalweather.schema.diagnostics": str(info["diagnostic_schema_version"]),
        "io.seasonalweather.schema.catalog": str(info["diagnostic_catalog_version"]),
        "io.seasonalweather.schema.capability-manifest": str(info["capability_manifest_version"]),
    }


def validate_source_contract(root: Path = ROOT) -> list[str]:
    """Return P2-09 source/configuration boundary violations."""

    errors: list[str] = []
    if tuple(spec.profile for spec in IMAGE_SPECS) != IMAGE_TARGETS:
        errors.append("P2-01/P2-03 image targets and P2-09 inspection matrix differ")
    for relative in (
        "config/config.yaml",
        "config/example.env",
        "seasonalweather/config.py",
        "seasonalweather/configuration/schema.py",
    ):
        path = root / relative
        if "execution_mode" in path.read_text(encoding="utf-8").lower():
            errors.append(f"retired embedded execution configuration remains in {relative}")
    for relative in ("seasonalweather/main.py", "seasonalweather/api/server.py", "seasonalweather/control.py"):
        path = root / relative
        if "EmbeddedExecutionPort(" in path.read_text(encoding="utf-8"):
            errors.append(f"controller production composition constructs EmbeddedExecutionPort: {relative}")
    for policy in JOB_TYPE_POLICIES.values():
        if policy.queue is QueueClass.ROUTINE and policy.executor is not ExecutorClass.ROUTINE_WORKER:
            errors.append(f"routine job bypasses routine worker: {policy.job_type.value}")
        if policy.queue is QueueClass.MAINTENANCE and policy.executor is not ExecutorClass.MAINTENANCE_WORKER:
            errors.append(f"maintenance job bypasses maintenance worker: {policy.job_type.value}")
    return errors


def validate_image_record(
    spec: ImageSpec,
    inspect: dict[str, Any],
    info: dict[str, Any],
    package_presence: dict[str, bool],
) -> list[str]:
    """Validate one Docker image's declared metadata and package boundary."""

    errors: list[str] = []
    config = inspect.get("Config")
    if not isinstance(config, dict):
        return [f"{spec.tag}: Docker image has no Config record"]
    if config.get("User") not in {"seasonalweather", "10001:10001"}:
        errors.append(f"{spec.tag}: image is not pinned to the non-root service user")
    if tuple(config.get("Entrypoint") or ()) != spec.entrypoint:
        errors.append(f"{spec.tag}: entrypoint does not match the declared profile")
    healthcheck = config.get("Healthcheck")
    if not isinstance(healthcheck, dict) or tuple(healthcheck.get("Test") or ()) != spec.healthcheck:
        errors.append(f"{spec.tag}: healthcheck does not match the declared profile")
    exposed = config.get("ExposedPorts") or {}
    if spec.role == "controller" and set(exposed) != {"9080/tcp"}:
        errors.append(f"{spec.tag}: controller must expose only its API port")
    if spec.role == "worker" and exposed:
        errors.append(f"{spec.tag}: worker image exposes a controller-facing port")

    labels = config.get("Labels")
    if not isinstance(labels, dict):
        errors.append(f"{spec.tag}: OCI labels are missing")
    else:
        for name, expected in _label_expectations(info).items():
            if labels.get(name) != expected:
                errors.append(f"{spec.tag}: label {name} does not match embedded build-info")

    if info.get("image_profile") != spec.profile:
        errors.append(f"{spec.tag}: embedded image profile is not {spec.profile}")
    expected_modules = _CONTROLLER_FORBIDDEN_MODULES if spec.role == "controller" else _WORKER_FORBIDDEN_MODULES
    for module in expected_modules:
        if package_presence.get(module, False):
            errors.append(f"{spec.tag}: forbidden package root is present: {module}")
    if spec.role == "worker":
        for module in _WORKER_IMPORTS:
            if package_presence.get(module, False):
                errors.append(f"{spec.tag}: controller-only dependency is present: {module}")
    return errors


def validate_built_images() -> list[str]:
    """Inspect every built profile and compare immutable cross-image identity."""

    errors: list[str] = []
    reports: list[tuple[ImageSpec, dict[str, Any], dict[str, Any], dict[str, bool], Any]] = []
    for spec in IMAGE_SPECS:
        try:
            inspect = _image_inspect(spec)
            info = _embedded_file_json(spec, "/usr/share/seasonalweather/build-info.json")
            cli_info = _embedded_json(spec, "-m", "seasonalweather.build_metadata", "--json")
            package_presence = _package_presence(spec)
            catalog = _embedded_json(spec, "-m", "seasonalweather", "diagnostics", "list", "--format", "json")
        except GateError as exc:
            errors.append(str(exc))
            continue
        if info != cli_info:
            errors.append(f"{spec.tag}: build metadata CLI disagrees with the embedded record")
        reports.append((spec, inspect, info, package_presence, catalog))
        errors.extend(validate_image_record(spec, inspect, info, package_presence))

    if not reports:
        return errors
    baseline = reports[0][2]
    baseline_catalog = json.dumps(reports[0][4], sort_keys=True, separators=(",", ":"))
    for spec, _inspect, info, _packages, catalog in reports[1:]:
        for field in _STABLE_BUILD_FIELDS:
            if info.get(field) != baseline.get(field):
                errors.append(f"{spec.tag}: build field {field} differs from controller identity")
        if json.dumps(catalog, sort_keys=True, separators=(",", ":")) != baseline_catalog:
            errors.append(f"{spec.tag}: diagnostic catalog differs from controller catalog")
    try:
        definitions = reports[0][4]["diagnostics"]
        code = definitions[0]["code"]
        if not isinstance(code, str):
            raise TypeError
    except (KeyError, IndexError, TypeError):
        errors.append("controller diagnostic catalog has no usable definition for explain validation")
    else:
        for spec, _inspect, _info, _packages, _catalog in reports:
            try:
                explained = _embedded_json(
                    spec, "-m", "seasonalweather", "diagnostics", "explain", code, "--format", "json"
                )
            except GateError as exc:
                errors.append(str(exc))
            else:
                if (
                    not isinstance(explained, dict)
                    or not isinstance(explained.get("diagnostic"), dict)
                    or explained["diagnostic"].get("code") != code
                ):
                    errors.append(f"{spec.tag}: diagnostics explain did not resolve {code}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the Phase 2 image and runtime exit gate.")
    parser.add_argument("--images", action="store_true", help="inspect all locally built Phase 2 images")
    args = parser.parse_args(argv)
    errors = validate_source_contract()
    if args.images:
        errors.extend(validate_built_images())
    if errors:
        print("phase2-exit-gate: failed")
        for error in errors:
            print(f"- {error}")
        return 1
    mode = "source and built-image" if args.images else "source"
    print(f"phase2-exit-gate: {mode} boundaries satisfied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
