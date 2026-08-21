"""Thin, controlled orchestration for image and Compose build interfaces."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from seasonalweather.build_metadata import BuildInfo, BuildInfoError

ROOT = Path(__file__).resolve().parents[1]
BAKE_FILE = ROOT / "docker-bake.hcl"
IMAGE_TARGETS = ("controller", "routine-worker", "piper", "legacy-tts", "maintenance", "development")
DOCKER_ENVIRONMENT_KEYS = (
    "DOCKER_CONFIG",
    "DOCKER_CONTEXT",
    "DOCKER_HOST",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
    "BUILDX_CONFIG",
)
_BUILD_NETWORK_ENVIRONMENT = "SEASONALWEATHER_DOCKER_BUILD_NETWORK"
_BUILD_NETWORKS = frozenset(("default", "host", "none"))


def _load(path: Path) -> BuildInfo:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise BuildInfoError("build-info root must be an object")
        return BuildInfo.from_dict(raw)
    except (OSError, json.JSONDecodeError, BuildInfoError) as exc:
        raise SystemExit(f"invalid build-info: {exc}") from exc


def _controlled_environment(info: BuildInfo, *, build_network: str | None = None) -> dict[str, str]:
    """Pass only build inputs explicitly represented in the build record."""

    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "SW_PROJECT": info.project,
        "SW_VERSION": info.software_version,
        "SW_BUILD_ID": info.build_id,
        "SW_BUILD_IDENTITY": info.build_identity,
        "SW_GIT_COMMIT": info.git_commit,
        "SW_GIT_DESCRIBE": info.git_describe,
        "SW_DIRTY_TREE": "true" if info.dirty_tree else "false",
        "SW_BUILD_SOURCE_TIMESTAMP": info.build_source_timestamp or "",
        "SW_SOURCE_DATE_EPOCH": str(info.source_date_epoch or ""),
        "SW_IMAGE_PROFILE": info.image_profile,
        "SW_TARGET_PLATFORM": info.target_platform,
        "SW_PYTHON_VERSION": info.python_version,
        "SW_SWWP_PROTOCOL_VERSIONS": ",".join(map(str, info.swwp_protocol_versions)),
        "SW_JOB_PAYLOAD_SCHEMA_VERSIONS": ",".join(map(str, info.job_payload_schema_versions)),
        "SW_JOB_RESULT_SCHEMA_VERSIONS": ",".join(map(str, info.job_result_schema_versions)),
        "SW_VALIDATION_PROTOCOL_VERSIONS": ",".join(map(str, info.validation_protocol_versions)),
        "SW_CONFIG_SCHEMA_MIN": str(info.configuration_schema[0]),
        "SW_CONFIG_SCHEMA_MAX": str(info.configuration_schema[1]),
        "SW_DIAGNOSTIC_SCHEMA_VERSION": str(info.diagnostic_schema_version),
        "SW_DIAGNOSTIC_CATALOG_VERSION": str(info.diagnostic_catalog_version),
        "SW_CAPABILITY_MANIFEST_VERSION": str(info.capability_manifest_version),
    }
    environment.update({key: os.environ[key] for key in DOCKER_ENVIRONMENT_KEYS if key in os.environ})
    if build_network is not None:
        environment[_BUILD_NETWORK_ENVIRONMENT] = build_network
    return environment


def run_image(*, build_info: Path, targets: tuple[str, ...]) -> int:
    if not BAKE_FILE.is_file():
        raise SystemExit(f"image matrix is missing: {BAKE_FILE}")
    info = _load(build_info)
    network = os.environ.get(_BUILD_NETWORK_ENVIRONMENT, "default")
    if network not in _BUILD_NETWORKS:
        raise SystemExit(f"unsupported Docker build network: {network}")
    command = ["docker", "buildx", "bake", "--load", "--file", str(BAKE_FILE)]
    if network == "host":
        command.append("--allow=network.host")
    command.extend(targets)
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=_controlled_environment(info, build_network=network),
            check=False,
        )
    except OSError as exc:
        raise SystemExit(f"cannot start Docker Buildx: {exc}") from exc
    return completed.returncode


def compose_check() -> int:
    compose_files = tuple(ROOT.glob("compose*.yaml")) + tuple(ROOT.glob("compose*.yml"))
    compose_files += tuple(ROOT.glob("docker-compose*.yaml")) + tuple(ROOT.glob("docker-compose*.yml"))
    if not compose_files:
        print("compose-check: no Compose definition; deferred to Phase 3")
        return 0
    command = ["docker", "compose", "config", "--quiet"]
    try:
        return subprocess.run(command, cwd=ROOT, check=False).returncode
    except OSError as exc:
        raise SystemExit(f"cannot start Docker Compose: {exc}") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the controlled SeasonalWeather build interface.")
    commands = parser.add_subparsers(dest="command", required=True)
    image = commands.add_parser("image")
    image.add_argument("--build-info", type=Path, required=True)
    image.add_argument("--target", choices=IMAGE_TARGETS)
    image.add_argument("--all", action="store_true")
    commands.add_parser("compose-check")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "compose-check":
        return compose_check()
    if args.all == (args.target is not None):
        raise SystemExit("choose exactly one of --target or --all")
    targets = IMAGE_TARGETS if args.all else (args.target,)
    return run_image(build_info=args.build_info, targets=targets)


if __name__ == "__main__":
    raise SystemExit(main())
