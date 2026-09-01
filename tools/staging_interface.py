"""Fail-closed orchestration for the isolated P3-07 staging Compose stack."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import cast

_IMMUTABLE_IMAGE = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")

ROOT = Path(__file__).resolve().parents[1]
BASE_COMPOSE = ROOT / "compose.yaml"
STAGING_COMPOSE = ROOT / "compose.staging.yaml"
PROJECT = "seasonalweather-staging"
STAGING_CONFIG_ENV = "SEASONALWEATHER_STAGING_CONFIG_FILE"
STAGING_SECRET_ENV = "SEASONALWEATHER_STAGING_SECRET_DIR"
REQUIRED_SECRETS = ("ICECAST_SOURCE_PASSWORD", "SEASONAL_API_TOKEN", "SEASONAL_WORKER_TOKEN")
ROLLBACK_IMAGE_KEYS = (
    "SEASONALWEATHER_CONTROLLER_IMAGE",
    "SEASONALWEATHER_ROUTINE_WORKER_IMAGE",
    "SEASONALWEATHER_MAINTENANCE_WORKER_IMAGE",
    "SEASONALWEATHER_PIPER_WORKER_IMAGE",
    "SEASONALWEATHER_ESPEAK_WORKER_IMAGE",
    "SEASONALWEATHER_FESTIVAL_WORKER_IMAGE",
    "SEASONALWEATHER_DECTALK_WORKER_IMAGE",
    "SEASONALWEATHER_LEGACY_TTS_WORKER_IMAGE",
    "SEASONALWEATHER_VOICETEXT_PAUL_WORKER_IMAGE",
    "SEASONALWEATHER_SPFY_WORKER_IMAGE",
    "SEASONALWEATHER_LIQUIDSOAP_IMAGE",
    "SEASONALWEATHER_ICECAST_IMAGE",
)
REQUIRED_ROLLBACK_IMAGE_KEYS = (
    "SEASONALWEATHER_CONTROLLER_IMAGE",
    "SEASONALWEATHER_ROUTINE_WORKER_IMAGE",
    "SEASONALWEATHER_LIQUIDSOAP_IMAGE",
    "SEASONALWEATHER_ICECAST_IMAGE",
)
OPTIONAL_PROFILES = ("maintenance", "espeak", "piper", "festival", "dectalk", "legacy-tts", "voicetext-paul", "spfy")
PROFILE_IMAGE_KEYS = {
    "maintenance": "SEASONALWEATHER_MAINTENANCE_WORKER_IMAGE",
    "espeak": "SEASONALWEATHER_ESPEAK_WORKER_IMAGE",
    "piper": "SEASONALWEATHER_PIPER_WORKER_IMAGE",
    "festival": "SEASONALWEATHER_FESTIVAL_WORKER_IMAGE",
    "dectalk": "SEASONALWEATHER_DECTALK_WORKER_IMAGE",
    "legacy-tts": "SEASONALWEATHER_LEGACY_TTS_WORKER_IMAGE",
    "voicetext-paul": "SEASONALWEATHER_VOICETEXT_PAUL_WORKER_IMAGE",
    "spfy": "SEASONALWEATHER_SPFY_WORKER_IMAGE",
}


def _required_path(name: str, *, directory: bool) -> Path:
    raw = os.environ.get(name, "")
    if not raw:
        raise SystemExit(f"{name} is required for the isolated staging stack")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise SystemExit(f"{name} must be an absolute path outside the repository")
    try:
        path = path.resolve(strict=True)
    except OSError as exc:
        raise SystemExit(f"{name} is not a usable path: {path}") from exc
    if path == ROOT or ROOT in path.parents:
        raise SystemExit(f"{name} must point outside the repository")
    if directory and not path.is_dir():
        raise SystemExit(f"{name} is not a directory: {path}")
    if not directory and not path.is_file():
        raise SystemExit(f"{name} is not a regular file: {path}")
    return path


def validate_environment(*, rollback_env_file: Path | None = None, profiles: tuple[str, ...] = ()) -> tuple[Path, Path]:
    """Validate staging-only paths without reading secret contents."""

    config = _required_path(STAGING_CONFIG_ENV, directory=False)
    secret_dir = _required_path(STAGING_SECRET_ENV, directory=True)
    for secret_name in REQUIRED_SECRETS:
        secret = secret_dir / secret_name
        if not secret.is_file():
            raise SystemExit(f"staging secret is missing: {secret_name}")
        if secret.stat().st_mode & 0o077:
            raise SystemExit(f"staging secret must not be group/world accessible: {secret}")
    if rollback_env_file is not None:
        if not rollback_env_file.is_absolute():
            raise SystemExit(f"rollback env file must be an existing absolute file: {rollback_env_file}")
        try:
            rollback_env_file = rollback_env_file.resolve(strict=True)
        except OSError as exc:
            raise SystemExit(f"rollback env file must be an existing absolute file: {rollback_env_file}") from exc
        if not rollback_env_file.is_file():
            raise SystemExit(f"rollback env file must be an existing absolute file: {rollback_env_file}")
        if rollback_env_file == ROOT or ROOT in rollback_env_file.parents:
            raise SystemExit("rollback env file must be outside the repository")
        values: dict[str, str] = {}
        for line_number, raw_line in enumerate(rollback_env_file.read_text(encoding="utf-8").splitlines(), 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise SystemExit(f"rollback env file has a malformed line: {line_number}")
            key, value = (part.strip() for part in line.split("=", 1))
            if key not in ROLLBACK_IMAGE_KEYS:
                raise SystemExit(f"rollback env file contains non-image keys: {key}")
            if _IMMUTABLE_IMAGE.fullmatch(value) is None:
                raise SystemExit(f"rollback env file has an invalid immutable image value: {key}")
            if key in values:
                raise SystemExit(f"rollback env file contains a duplicate key: {key}")
            values[key] = value
        required_keys: set[str] = set(REQUIRED_ROLLBACK_IMAGE_KEYS)
        for profile in profiles:
            image_key = PROFILE_IMAGE_KEYS.get(profile)
            if image_key is None:
                raise ValueError(f"unsupported staging profile: {profile}")
            required_keys.add(image_key)
        missing = required_keys - set(values)
        if missing:
            joined = ", ".join(sorted(missing))
            raise SystemExit(f"rollback env file is missing core image keys: {joined}")
    return config, secret_dir


def compose_command(
    *args: str,
    rollback_env_file: Path | None = None,
    profiles: tuple[str, ...] = (),
) -> list[str]:
    """Build every staging command with the fixed project and selected profiles."""

    command = [
        "docker",
        "compose",
        "--project-name",
        PROJECT,
        "--profile",
        "icecast",
    ]
    for profile in profiles:
        if profile not in OPTIONAL_PROFILES:
            raise ValueError(f"unsupported staging profile: {profile}")
        command.extend(("--profile", profile))
    if rollback_env_file is not None:
        command.extend(("--env-file", str(rollback_env_file)))
    command.extend(("--file", str(BASE_COMPOSE), "--file", str(STAGING_COMPOSE)))
    command.extend(args)
    return command


def _service_snapshot_is_healthy(raw_output: str) -> bool:
    """Return false for empty, malformed, stopped, or unhealthy Compose output."""

    try:
        decoded = cast(object, json.loads(raw_output))
        values = cast(list[object], decoded) if isinstance(decoded, list) else [decoded]
    except json.JSONDecodeError:
        values: list[object] = []
        for line in raw_output.splitlines():
            try:
                values.append(cast(object, json.loads(line)))
            except json.JSONDecodeError:
                return False
    if not values or any(not isinstance(value, dict) for value in values):
        return False
    rows = []
    for value in values:
        raw_row = cast(dict[object, object], value)
        rows.append({key: item for key, item in raw_row.items() if isinstance(key, str)})
    for row in rows:
        state = row.get("State", row.get("state"))
        if not isinstance(state, str) or state.lower() != "running":
            return False
        health = row.get("Health", row.get("health"))
        if isinstance(health, str) and health.lower() == "unhealthy":
            return False
    return True


def _run(
    args: tuple[str, ...],
    *,
    rollback_env_file: Path | None = None,
    profiles: tuple[str, ...] = (),
    capture_output: bool = False,
) -> int:
    _ = validate_environment(rollback_env_file=rollback_env_file, profiles=profiles)
    try:
        completed = subprocess.run(
            compose_command(*args, rollback_env_file=rollback_env_file, profiles=profiles),
            cwd=ROOT,
            check=False,
            capture_output=capture_output,
            text=True,
        )
        if capture_output and completed.returncode == 0:
            stdout = completed.stdout or ""
            if not _service_snapshot_is_healthy(stdout):
                return 1
        return completed.returncode
    except OSError as exc:
        raise SystemExit(f"cannot start Docker Compose: {exc}") from exc


def _soak(*, duration: int, interval: int, profiles: tuple[str, ...] = ()) -> int:
    if duration < 60 or duration > 7 * 24 * 60 * 60:
        raise SystemExit("soak duration must be between 60 seconds and 7 days")
    if interval < 10 or interval > 60:
        raise SystemExit("soak interval must be between 10 and 60 seconds")
    _ = validate_environment()
    deadline = time.monotonic() + duration
    snapshots = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        result = _run(("ps", "--format", "json"), profiles=profiles, capture_output=True)
        snapshots += 1
        if result:
            return result
        _ = time.sleep(min(interval, remaining))
    print(f"staging soak completed: {snapshots} bounded service snapshots")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Operate only the isolated P3-07 staging Compose project.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("config", "up", "down", "restart", "recreate", "ps", "logs"):
        command = commands.add_parser(name)
        _ = command.add_argument(
            "--profile",
            action="append",
            choices=OPTIONAL_PROFILES,
            default=[],
            dest="profiles",
            help="enable an optional worker profile (repeatable)",
        )
        if name == "logs":
            _ = command.add_argument("services", nargs="*")
    rollback = commands.add_parser("rollback")
    _ = rollback.add_argument("--env-file", type=Path, required=True, dest="rollback_env_file")
    _ = rollback.add_argument(
        "--profile",
        action="append",
        choices=OPTIONAL_PROFILES,
        default=[],
        dest="profiles",
        help="enable an optional worker profile (repeatable)",
    )
    soak = commands.add_parser("soak")
    _ = soak.add_argument("--duration-seconds", type=int, required=True)
    _ = soak.add_argument("--interval-seconds", type=int, default=30)
    _ = soak.add_argument(
        "--profile",
        action="append",
        choices=OPTIONAL_PROFILES,
        default=[],
        dest="profiles",
        help="enable an optional worker profile (repeatable)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    command = cast(str, args.command)
    profiles = tuple(cast(list[str], args.profiles))
    if command == "config":
        return _run(("config", "--quiet"), profiles=profiles)
    if command == "up":
        return _run(("up", "--detach", "--remove-orphans"), profiles=profiles)
    if command == "down":
        return _run(("down", "--remove-orphans"), profiles=profiles)
    if command == "restart":
        return _run(("restart",), profiles=profiles)
    if command == "recreate":
        return _run(("up", "--detach", "--force-recreate", "--remove-orphans"), profiles=profiles)
    if command == "ps":
        return _run(("ps",), profiles=profiles)
    if command == "logs":
        services = cast(list[str], args.services)
        return _run(("logs", "--no-color", "--timestamps", *services), profiles=profiles)
    if command == "rollback":
        rollback_env_file = cast(Path, args.rollback_env_file).expanduser()
        return _run(
            ("up", "--detach", "--force-recreate", "--remove-orphans"),
            rollback_env_file=rollback_env_file,
            profiles=profiles,
        )
    return _soak(
        duration=cast(int, args.duration_seconds),
        interval=cast(int, args.interval_seconds),
        profiles=profiles,
    )


if __name__ == "__main__":
    raise SystemExit(main())
