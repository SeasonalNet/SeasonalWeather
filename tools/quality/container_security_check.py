from __future__ import annotations

import datetime as dt
import re
import tomllib
from pathlib import Path
from typing import Any

from seasonalweather.secret_files import SECRET_ENVIRONMENT_NAMES
from tools.quality.governance import ROOT, load_toml, parse_review_date

_SECRET_DECLARATION_PATTERN = re.compile(r"(?im)^\s*(?:ARG|ENV)\s+[^\n]*(?:password|secret|token|api[_-]?key)\b")


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read {path.relative_to(ROOT)}") from exc


def _required_tokens(text: str, tokens: list[Any], *, context: str) -> list[str]:
    lowered = text.lower()
    return [f"{context} missing required boundary: {token}" for token in tokens if str(token).lower() not in lowered]


def _forbidden_tokens(text: str, tokens: list[Any], *, context: str) -> list[str]:
    lowered = text.lower()
    return [f"{context} contains forbidden boundary: {token}" for token in tokens if str(token).lower() in lowered]


def _check_contract(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if config.get("status") != "active":
        errors.append("quality/container-security.toml must be active")
    for field in ("owner", "rationale", "scope", "review_date", "removal_condition"):
        if not config.get(field):
            errors.append(f"quality/container-security.toml missing governance field: {field}")
    try:
        review_date = parse_review_date(config.get("review_date"), context="quality/container-security.toml")
    except ValueError as exc:
        errors.append(str(exc))
    else:
        if review_date < dt.date.today():
            errors.append("quality/container-security.toml review_date has expired")

    expected = {
        "controller",
        "routine-worker",
        "espeak",
        "piper",
        "festival",
        "dectalk",
        "legacy-tts",
        "voicetext-paul",
        "spfy",
        "maintenance",
        "development",
    }
    profiles = {str(profile) for profile in config.get("profiles", [])}
    if profiles != expected:
        errors.append(f"security contract profiles must be exactly {sorted(expected)}")
    if config.get("service_user") != "seasonalweather":
        errors.append("security contract must use the seasonalweather service user")
    if config.get("service_uid") != 10001 or config.get("service_gid") != 10001:
        errors.append("security contract must pin UID and GID 10001")
    if config.get("read_only_root") is not True:
        errors.append("security contract must require a read-only root filesystem")
    if config.get("no_new_privileges") is not True:
        errors.append("security contract must require no-new-privileges")
    if config.get("cap_drop") != ["ALL"]:
        errors.append("security contract must drop ALL capabilities")
    if config.get("tmpfs") != ["/tmp", "/run"]:
        errors.append("security contract must provide tmpfs /tmp and /run")
    if config.get("runtime_flags") != [
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--tmpfs=/tmp:rw,nosuid,nodev,noexec",
        "--tmpfs=/run:rw,nosuid,nodev,noexec",
    ]:
        errors.append("security contract runtime flags must match the hardened profile")
    if config.get("config_mount") != "/etc/seasonalweather/config.yaml":
        errors.append("security contract must use the canonical read-only config mount")
    if config.get("config_mount_mode") != "read-only":
        errors.append("security contract must make the configuration mount read-only")
    if config.get("secret_mount_root") != "/run/secrets":
        errors.append("security contract must use the canonical secret mount root")
    if config.get("secret_mount_mode") != "read-only" or config.get("secret_file_mode") != "0400":
        errors.append("security contract must require read-only 0400 secret files")
    if config.get("diagnostic_export_root") != "/usr/share/seasonalweather/diagnostics":
        errors.append("security contract must use the canonical diagnostic export root")
    if config.get("diagnostic_mount_mode") != "read-only":
        errors.append("security contract must make the diagnostic export mount read-only")
    return errors


def _check_dockerfile(path: Path, config: dict[str, Any], *, role: str) -> list[str]:
    text = _text(path)
    image = config["image"]
    errors = _required_tokens(
        text,
        [
            f"USER {config['service_user']}",
            f'io.seasonalweather.security.profile="{role}"',
            f'io.seasonalweather.security.user="{config["service_user"]}:{config["service_uid"]}:{config["service_gid"]}"',
            'io.seasonalweather.security.read-only-root="required"',
            'io.seasonalweather.security.no-new-privileges="required"',
            'io.seasonalweather.security.cap-drop="ALL"',
            'io.seasonalweather.security.tmpfs="/tmp,/run"',
            'io.seasonalweather.security.secrets="read-only-per-service"',
        ],
        context=path.name,
    )
    errors.extend(_forbidden_tokens(text, image["forbidden_secret_tokens"], context=path.name))
    errors.extend(_forbidden_tokens(text, ["setuid", "setgid"], context=path.name))
    if _SECRET_DECLARATION_PATTERN.search(text):
        errors.append(f"{path.name} declares a secret-shaped ARG or ENV")
    return errors


def _check_context(config: dict[str, Any]) -> list[str]:
    dockerignore = _text(ROOT / ".dockerignore")
    return _required_tokens(dockerignore, config["image"]["required_context_excludes"], context=".dockerignore")


def _check_mount_policy(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    controller = config["roles"]["controller"]
    worker = config["roles"]["worker"]
    controller_secret_names = tuple(name for name in SECRET_ENVIRONMENT_NAMES if name != "SEASONAL_WORKER_TOKEN")
    if tuple(controller["secret_files"]) != controller_secret_names:
        errors.append("controller secret allowlist must match the known secret file bindings")
    if worker["secret_files"] != ["SEASONAL_WORKER_TOKEN"]:
        errors.append("worker secret allowlist must contain only the SWWP worker credential")
    if any(path in controller["writable_mounts"] for path in controller["read_only_mounts"]):
        errors.append("controller read-only and writable mount policies overlap")
    if any(path in worker["writable_mounts"] for path in worker["read_only_mounts"]):
        errors.append("worker read-only and writable mount policies overlap")
    for path in controller["writable_mounts"]:
        if str(path) in {str(item) for item in controller["forbidden_mounts"]}:
            errors.append(f"controller mount is both writable and forbidden: {path}")
    worker_writable = {str(path) for path in worker["writable_mounts"]}
    for path in worker["forbidden_mounts"]:
        if str(path) in worker_writable or not str(path).startswith("/var/"):
            errors.append(f"worker forbidden mount policy is invalid: {path}")
    for path in (config["config_mount"], config["secret_mount_root"], config["diagnostic_export_root"]):
        if any(str(path) == str(item) for item in (*controller["writable_mounts"], *worker["writable_mounts"])):
            errors.append(f"read-only mount is writable in a service policy: {path}")
    return errors


def _check_dependencies(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    metadata_path = ROOT / "pyproject.toml"
    try:
        document = tomllib.loads(_text(metadata_path))
    except tomllib.TOMLDecodeError:
        return ["pyproject.toml is not valid project metadata"]
    project = document.get("project")
    optional = project.get("optional-dependencies") if isinstance(project, dict) else None
    dependency_groups = document.get("dependency-groups")
    controller_group = str(config.get("controller_dependency_group", "controller"))
    if isinstance(optional, dict):
        controller_values = optional.get(controller_group, [])
    elif isinstance(dependency_groups, dict):
        controller_values = dependency_groups.get(controller_group, [])
    else:
        controller_values = []
    controller_lock = "\n".join(str(value) for value in controller_values if isinstance(value, str))
    errors.extend(
        _forbidden_tokens(
            controller_lock, config["image"]["forbidden_controller_dependencies"], context="pyproject.toml[controller]"
        )
    )
    project_values = project.get("dependencies", []) if isinstance(project, dict) else []
    worker_values = [str(value) for value in project_values if isinstance(value, str)]
    worker_groups = config.get("worker_dependency_groups", ["piper"])
    for group in worker_groups:
        if isinstance(optional, dict):
            values = optional.get(str(group), [])
        elif isinstance(dependency_groups, dict):
            values = dependency_groups.get(str(group), [])
        else:
            values = []
        if isinstance(values, list):
            worker_values.extend(str(value) for value in values if isinstance(value, str))
    errors.extend(
        _forbidden_tokens(
            "\n".join(worker_values),
            config["image"]["forbidden_worker_dependencies"],
            context="pyproject.toml worker metadata",
        )
    )
    return errors


def main() -> int:
    config = load_toml(ROOT / "quality/container-security.toml")
    errors = [
        *_check_contract(config),
        *_check_dockerfile(ROOT / "Dockerfile", config, role="controller"),
        *_check_dockerfile(ROOT / "Dockerfile.worker", config, role="worker"),
        *_check_context(config),
        *_check_mount_policy(config),
        *_check_dependencies(config),
    ]
    if errors:
        print("container-security-check: failed")
        for error in errors:
            print(f"- {error}")
        return 1
    print("container-security-check: controller and worker profiles satisfy P2-05 contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
