"""Controlled build identity and reproducible provenance records."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import subprocess  # nosec B404 - invokes the fixed git executable with argv-only arguments
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .. import __version__
from ..capabilities.manifest import MANIFEST_SCHEMA_VERSION
from ..configuration.schema import SUPPORTED_CONFIG_SCHEMAS
from ..diagnostics.models import DIAGNOSTIC_CATALOG_VERSION, DIAGNOSTIC_SCHEMA_VERSION
from ..jobs.registry import JOB_TYPE_POLICIES
from ..swwp.constants import PROTOCOL_VERSION
from ..validation.constants import VALIDATION_PROTOCOL_VERSION

BUILD_INFO_SCHEMA_VERSION = 1
BUILD_INFO_PATH = "/usr/share/seasonalweather/build-info.json"
BUILD_INFO_PATH_ENV = "SEASONALWEATHER_BUILD_INFO_PATH"
BUILD_PROFILE_ENV = "SEASONALWEATHER_BUILD_PROFILE"
BUILD_TARGET_PLATFORM_ENV = "SEASONALWEATHER_TARGET_PLATFORM"
BUILD_ID_ENV = "SEASONALWEATHER_BUILD_ID"
SOURCE_DATE_EPOCH_ENV = "SOURCE_DATE_EPOCH"
_PROJECT = "seasonalweather"
_MAX_TEXT = 256


class BuildInfoError(ValueError):
    """Raised when a build-info record is malformed or contradictory."""


def _bounded_text(value: object, *, name: str, maximum: int = _MAX_TEXT) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise BuildInfoError(f"{name} must be a non-empty bounded string")
    return value


def _bounded_optional_text(value: object, *, name: str, maximum: int = _MAX_TEXT) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, name=name, maximum=maximum)


def _bounded_int(value: object, *, name: str, maximum: int = 255) -> int:
    if type(value) is not int or value < 1 or value > maximum:
        raise BuildInfoError(f"{name} must be a bounded positive integer")
    return value


def _optional_epoch(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise BuildInfoError("source_date_epoch must be a non-negative integer or null")
    return value


def _version_tuple(value: object, *, name: str) -> tuple[int, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(type(item) is not int or item < 1 or item > 255 for item in value)
    ):
        raise BuildInfoError(f"{name} must be a non-empty list of bounded positive integers")
    result = tuple(value)
    if result != tuple(sorted(set(result))):
        raise BuildInfoError(f"{name} must be sorted and unique")
    return result


def _schema_range(value: object) -> tuple[int, int]:
    if not isinstance(value, dict) or set(value) != {"minimum", "maximum"}:
        raise BuildInfoError("configuration schema range is malformed")
    minimum = value["minimum"]
    maximum = value["maximum"]
    if type(minimum) is not int or type(maximum) is not int or minimum < 1 or maximum < minimum:
        raise BuildInfoError("configuration schema range is invalid")
    return minimum, maximum


def _utc_timestamp(epoch: int) -> str:
    try:
        value = dt.datetime.fromtimestamp(epoch, tz=dt.UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise BuildInfoError("SOURCE_DATE_EPOCH is outside the supported timestamp range") from exc
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class BuildInfo:
    """Immutable build identity shared by every runtime identity surface."""

    schema_version: int
    project: str
    software_version: str
    git_commit: str
    git_describe: str
    dirty_tree: bool
    build_source_timestamp: str | None
    source_date_epoch: int | None
    build_id: str
    image_profile: str
    target_platform: str
    python_version: str
    swwp_protocol_versions: tuple[int, ...]
    job_payload_schema_versions: tuple[int, ...]
    job_result_schema_versions: tuple[int, ...]
    validation_protocol_versions: tuple[int, ...]
    configuration_schema: tuple[int, int]
    diagnostic_schema_version: int
    diagnostic_catalog_version: int
    capability_manifest_version: int

    def __post_init__(self) -> None:
        if self.schema_version != BUILD_INFO_SCHEMA_VERSION:
            raise BuildInfoError("unsupported build-info schema version")
        _bounded_text(self.project, name="project")
        _bounded_text(self.software_version, name="software_version")
        _bounded_text(self.git_commit, name="git_commit")
        _bounded_text(self.git_describe, name="git_describe")
        _bounded_optional_text(self.build_source_timestamp, name="build_source_timestamp")
        _bounded_text(self.build_id, name="build_id")
        _bounded_text(self.image_profile, name="image_profile", maximum=64)
        _bounded_text(self.target_platform, name="target_platform", maximum=128)
        _bounded_text(self.python_version, name="python_version", maximum=64)
        if type(self.dirty_tree) is not bool:
            raise BuildInfoError("dirty_tree must be a boolean")
        _validate_build_info_constraints(self)

    @property
    def build_identity(self) -> str:
        if self.git_commit == "unknown":
            return f"{self.project}-{self.software_version}"
        suffix = "-dirty" if self.dirty_tree else ""
        return f"{self.project}-{self.software_version}-{self.git_commit[:12]}{suffix}"

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "project": self.project,
            "software_version": self.software_version,
            "build_identity": self.build_identity,
            "git_commit": self.git_commit,
            "git_describe": self.git_describe,
            "dirty_tree": self.dirty_tree,
            "build_source_timestamp": self.build_source_timestamp,
            "source_date_epoch": self.source_date_epoch,
            "build_id": self.build_id,
            "image_profile": self.image_profile,
            "target_platform": self.target_platform,
            "python_version": self.python_version,
            "swwp_protocol_versions": list(self.swwp_protocol_versions),
            "job_payload_schema_versions": list(self.job_payload_schema_versions),
            "job_result_schema_versions": list(self.job_result_schema_versions),
            "validation_protocol_versions": list(self.validation_protocol_versions),
            "configuration_schema": {
                "minimum": self.configuration_schema[0],
                "maximum": self.configuration_schema[1],
            },
            "diagnostic_schema_version": self.diagnostic_schema_version,
            "diagnostic_catalog_version": self.diagnostic_catalog_version,
            "capability_manifest_version": self.capability_manifest_version,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    def oci_labels(self) -> dict[str, str]:
        values = self.to_dict()
        return {
            "org.opencontainers.image.title": self.project,
            "org.opencontainers.image.version": self.software_version,
            "org.opencontainers.image.revision": self.git_commit,
            "org.opencontainers.image.created": self.build_source_timestamp or "",
            "org.opencontainers.image.ref.name": self.git_describe,
            "io.seasonalweather.build.id": self.build_id,
            "io.seasonalweather.build.identity": self.build_identity,
            "io.seasonalweather.build.dirty": "true" if self.dirty_tree else "false",
            "io.seasonalweather.build.profile": self.image_profile,
            "io.seasonalweather.build.target-platform": self.target_platform,
            "io.seasonalweather.build.info-sha256": hashlib.sha256(self.to_json().encode()).hexdigest(),
            "io.seasonalweather.schema.configuration": json.dumps(values["configuration_schema"], sort_keys=True),
            "io.seasonalweather.schema.swwp": ",".join(map(str, self.swwp_protocol_versions)),
            "io.seasonalweather.schema.validation": ",".join(map(str, self.validation_protocol_versions)),
            "io.seasonalweather.schema.diagnostics": str(self.diagnostic_schema_version),
            "io.seasonalweather.schema.catalog": str(self.diagnostic_catalog_version),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> BuildInfo:
        expected = {
            "schema_version",
            "project",
            "software_version",
            "build_identity",
            "git_commit",
            "git_describe",
            "dirty_tree",
            "build_source_timestamp",
            "source_date_epoch",
            "build_id",
            "image_profile",
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
        }
        if set(raw) != expected:
            raise BuildInfoError("build-info fields are not exact")
        dirty_tree = raw["dirty_tree"]
        if type(dirty_tree) is not bool:
            raise BuildInfoError("dirty_tree must be a boolean")
        info = cls(
            schema_version=_bounded_int(raw["schema_version"], name="schema_version"),
            project=_bounded_text(raw["project"], name="project"),
            software_version=_bounded_text(raw["software_version"], name="software_version"),
            git_commit=_bounded_text(raw["git_commit"], name="git_commit"),
            git_describe=_bounded_text(raw["git_describe"], name="git_describe"),
            dirty_tree=dirty_tree,
            build_source_timestamp=_bounded_optional_text(raw["build_source_timestamp"], name="build_source_timestamp"),
            source_date_epoch=_optional_epoch(raw["source_date_epoch"]),
            build_id=_bounded_text(raw["build_id"], name="build_id"),
            image_profile=_bounded_text(raw["image_profile"], name="image_profile", maximum=64),
            target_platform=_bounded_text(raw["target_platform"], name="target_platform", maximum=128),
            python_version=_bounded_text(raw["python_version"], name="python_version", maximum=64),
            swwp_protocol_versions=_version_tuple(raw["swwp_protocol_versions"], name="swwp_protocol_versions"),
            job_payload_schema_versions=_version_tuple(
                raw["job_payload_schema_versions"], name="job_payload_schema_versions"
            ),
            job_result_schema_versions=_version_tuple(
                raw["job_result_schema_versions"], name="job_result_schema_versions"
            ),
            validation_protocol_versions=_version_tuple(
                raw["validation_protocol_versions"], name="validation_protocol_versions"
            ),
            configuration_schema=_schema_range(raw["configuration_schema"]),
            diagnostic_schema_version=_bounded_int(raw["diagnostic_schema_version"], name="diagnostic_schema_version"),
            diagnostic_catalog_version=_bounded_int(
                raw["diagnostic_catalog_version"], name="diagnostic_catalog_version"
            ),
            capability_manifest_version=_bounded_int(
                raw["capability_manifest_version"], name="capability_manifest_version"
            ),
        )
        if raw["build_identity"] != info.build_identity:
            raise BuildInfoError("build_identity contradicts the build metadata")
        return info


def _validate_build_info_constraints(info: BuildInfo) -> None:
    _validate_source_date_epoch(info.source_date_epoch)
    _validate_version_sequences(info)
    _validate_schema_versions(info)


def _validate_source_date_epoch(value: int | None) -> None:
    if value is not None and (type(value) is not int or value < 0):
        raise BuildInfoError("source_date_epoch must be a non-negative integer or null")


def _validate_version_sequences(info: BuildInfo) -> None:
    for name, versions in (
        ("swwp_protocol_versions", info.swwp_protocol_versions),
        ("job_payload_schema_versions", info.job_payload_schema_versions),
        ("job_result_schema_versions", info.job_result_schema_versions),
        ("validation_protocol_versions", info.validation_protocol_versions),
    ):
        if (
            not versions
            or tuple(versions) != tuple(sorted(set(versions)))
            or any(item < 1 or item > 255 for item in versions)
        ):
            raise BuildInfoError(f"{name} must be sorted, unique, and positive")


def _validate_schema_versions(info: BuildInfo) -> None:
    minimum, maximum = info.configuration_schema
    if minimum < 1 or maximum < minimum:
        raise BuildInfoError("configuration schema range is invalid")
    for name, value in (
        ("diagnostic_schema_version", info.diagnostic_schema_version),
        ("diagnostic_catalog_version", info.diagnostic_catalog_version),
        ("capability_manifest_version", info.capability_manifest_version),
    ):
        if type(value) is not int or value < 1 or value > 255:
            raise BuildInfoError(f"{name} must be a bounded positive integer")


def _run_git(repo_root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(  # nosec B603 - argv is fixed to git metadata queries; shell execution is disabled
            ("git", "-C", str(repo_root), *args),
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if value else None


def _git_metadata(repo_root: Path) -> tuple[str, str, bool, int | None]:
    commit = _run_git(repo_root, "rev-parse", "HEAD") or "unknown"
    describe = _run_git(repo_root, "describe", "--tags", "--always") or commit
    status = _run_git(repo_root, "status", "--porcelain=v1", "--untracked-files=all")
    dirty = bool(status)
    commit_epoch = _run_git(repo_root, "show", "-s", "--format=%ct", "HEAD")
    try:
        source_epoch = int(commit_epoch) if commit_epoch is not None else None
    except ValueError:
        source_epoch = None
    return commit, describe, dirty, source_epoch


def _parse_epoch(value: str | int | None) -> int | None:
    if value is None:
        value = os.environ.get(SOURCE_DATE_EPOCH_ENV)
    if value is None or value == "":
        return None
    try:
        epoch = int(value)
    except (TypeError, ValueError) as exc:
        raise BuildInfoError("SOURCE_DATE_EPOCH must be a non-negative integer") from exc
    if epoch < 0:
        raise BuildInfoError("SOURCE_DATE_EPOCH must be a non-negative integer")
    return epoch


def collect_build_info(
    *,
    repo_root: Path,
    image_profile: str = "source",
    target_platform: str = "unknown",
    source_date_epoch: str | int | None = None,
    build_id: str | None = None,
    python_version: str | None = None,
) -> BuildInfo:
    commit, describe, dirty, commit_epoch = _git_metadata(repo_root)
    epoch = _parse_epoch(source_date_epoch)
    if epoch is None:
        epoch = commit_epoch
    timestamp = _utc_timestamp(epoch) if epoch is not None else None
    project = _PROJECT
    software_version = __version__
    python_version = python_version or platform.python_version()
    swwp_protocol_versions = (PROTOCOL_VERSION,)
    job_payload_schema_versions = tuple(
        sorted({policy.payload_schema_version for policy in JOB_TYPE_POLICIES.values()})
    )
    job_result_schema_versions = tuple(sorted({policy.result_schema_version for policy in JOB_TYPE_POLICIES.values()}))
    validation_protocol_versions = (VALIDATION_PROTOCOL_VERSION,)
    configuration_schema = (min(SUPPORTED_CONFIG_SCHEMAS), max(SUPPORTED_CONFIG_SCHEMAS))
    payload: dict[str, object] = {
        "project": project,
        "software_version": software_version,
        "git_commit": commit,
        "git_describe": describe,
        "dirty_tree": dirty,
        "build_source_timestamp": timestamp,
        "source_date_epoch": epoch,
        "image_profile": image_profile,
        "target_platform": target_platform,
        "python_version": python_version,
        "swwp_protocol_versions": swwp_protocol_versions,
        "job_payload_schema_versions": job_payload_schema_versions,
        "job_result_schema_versions": job_result_schema_versions,
        "validation_protocol_versions": validation_protocol_versions,
        "configuration_schema": configuration_schema,
        "diagnostic_schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "diagnostic_catalog_version": DIAGNOSTIC_CATALOG_VERSION,
        "capability_manifest_version": MANIFEST_SCHEMA_VERSION,
    }
    if build_id is None:
        build_id = (
            "bld-"
            + hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]
        )
    return BuildInfo(
        schema_version=BUILD_INFO_SCHEMA_VERSION,
        project=project,
        software_version=software_version,
        git_commit=commit,
        git_describe=describe,
        dirty_tree=dirty,
        build_source_timestamp=timestamp,
        source_date_epoch=epoch,
        build_id=build_id,
        image_profile=image_profile,
        target_platform=target_platform,
        python_version=python_version,
        swwp_protocol_versions=swwp_protocol_versions,
        job_payload_schema_versions=job_payload_schema_versions,
        job_result_schema_versions=job_result_schema_versions,
        validation_protocol_versions=validation_protocol_versions,
        configuration_schema=configuration_schema,
        diagnostic_schema_version=DIAGNOSTIC_SCHEMA_VERSION,
        diagnostic_catalog_version=DIAGNOSTIC_CATALOG_VERSION,
        capability_manifest_version=MANIFEST_SCHEMA_VERSION,
    )


def _fallback_build_info() -> BuildInfo:
    return BuildInfo(
        schema_version=BUILD_INFO_SCHEMA_VERSION,
        project=_PROJECT,
        software_version=__version__,
        git_commit="unknown",
        git_describe="source",
        dirty_tree=False,
        build_source_timestamp=None,
        source_date_epoch=None,
        build_id="source",
        image_profile="source",
        target_platform="unknown",
        python_version=platform.python_version(),
        swwp_protocol_versions=(PROTOCOL_VERSION,),
        job_payload_schema_versions=tuple(
            sorted({policy.payload_schema_version for policy in JOB_TYPE_POLICIES.values()})
        ),
        job_result_schema_versions=tuple(
            sorted({policy.result_schema_version for policy in JOB_TYPE_POLICIES.values()})
        ),
        validation_protocol_versions=(VALIDATION_PROTOCOL_VERSION,),
        configuration_schema=(min(SUPPORTED_CONFIG_SCHEMAS), max(SUPPORTED_CONFIG_SCHEMAS)),
        diagnostic_schema_version=DIAGNOSTIC_SCHEMA_VERSION,
        diagnostic_catalog_version=DIAGNOSTIC_CATALOG_VERSION,
        capability_manifest_version=MANIFEST_SCHEMA_VERSION,
    )


def load_build_info(path: Path | None = None) -> BuildInfo:
    candidates = [path] if path is not None else []
    if path is None:
        configured = os.environ.get(BUILD_INFO_PATH_ENV)
        if configured:
            candidates.append(Path(configured))
        candidates.extend(
            (
                Path(BUILD_INFO_PATH),
                Path(__file__).resolve().parents[1] / "build" / "build-info.json",
            )
        )
    for candidate in candidates:
        if candidate is None or not candidate.is_file():
            continue
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BuildInfoError(f"cannot load build-info from {candidate}") from exc
        if not isinstance(raw, dict):
            raise BuildInfoError("build-info root must be an object")
        return BuildInfo.from_dict(raw)
    return _fallback_build_info()


_current_build_info: BuildInfo | None = None


def current_build_info() -> BuildInfo:
    global _current_build_info
    if _current_build_info is None:
        _current_build_info = load_build_info()
    return _current_build_info


def reset_current_build_info() -> None:
    global _current_build_info
    _current_build_info = None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate or inspect controlled SeasonalWeather build provenance.")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--profile", default="source")
    parser.add_argument("--target-platform", default="unknown")
    parser.add_argument("--source-date-epoch")
    parser.add_argument("--build-id")
    parser.add_argument("--json", action="store_true", dest="machine_readable")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.output is None:
        print(current_build_info().to_json() if args.machine_readable else current_build_info().build_identity)
        return 0
    info = collect_build_info(
        repo_root=args.repo_root.resolve(),
        image_profile=args.profile,
        target_platform=args.target_platform,
        source_date_epoch=args.source_date_epoch,
        build_id=args.build_id,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.tmp-{os.getpid()}")
    temporary.write_text(info.to_json() + "\n", encoding="utf-8")
    os.replace(temporary, args.output)
    if args.machine_readable:
        print(info.to_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
