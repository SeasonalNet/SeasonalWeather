from __future__ import annotations

import datetime as dt
import json
import tomllib
from pathlib import Path
from typing import Any

from seasonalweather.diagnostics.loader import load_catalog
from tools.quality.governance import ROOT, load_toml, parse_review_date


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read {path.relative_to(ROOT)}") from exc


def _catalog_codes(path: Path) -> set[str]:
    try:
        raw = json.loads(_text(path))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid diagnostic catalog: {path.relative_to(ROOT)}") from exc
    definitions = raw.get("definitions") if isinstance(raw, dict) else None
    if not isinstance(definitions, list):
        raise ValueError(f"diagnostic catalog definitions are not an array: {path.relative_to(ROOT)}")
    codes = {item["code"] for item in definitions if isinstance(item, dict) and isinstance(item.get("code"), str)}
    if len(codes) != len(definitions):
        raise ValueError(f"diagnostic catalog contains an invalid code: {path.relative_to(ROOT)}")
    return codes


def _metadata_group(document: dict[str, Any], group: str) -> list[str]:
    project = document.get("project")
    optional = project.get("optional-dependencies") if isinstance(project, dict) else None
    values = optional.get(group, []) if isinstance(optional, dict) else []
    dependency_groups = document.get("dependency-groups")
    if not values and isinstance(dependency_groups, dict):
        values = dependency_groups.get(group, [])
    return [str(value).lower() for value in values if isinstance(value, str)]


def _metadata(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(_text(path))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid project metadata: {path.relative_to(ROOT)}") from exc


def _check_active(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    definition = ROOT / str(config.get("controller_definition", ""))
    requirements = ROOT / str(config.get("controller_requirements", ""))
    context_ignore = ROOT / str(config.get("build_context_ignore", ""))
    catalog_source = ROOT / str(config.get("controller_catalog_source", ""))
    catalog_compiled = ROOT / str(config.get("controller_catalog_compiled", ""))
    catalog_explanations = ROOT / str(config.get("controller_catalog_explanations", ""))
    if not definition.is_file():
        errors.append(f"missing controller image definition: {definition.relative_to(ROOT)}")
        return errors
    if not requirements.is_file():
        errors.append(f"missing controller project metadata: {requirements.relative_to(ROOT)}")
        return errors
    if not context_ignore.is_file():
        errors.append(f"missing build-context exclusion: {context_ignore.relative_to(ROOT)}")
        return errors
    if not catalog_source.is_file() or not catalog_compiled.is_file() or not catalog_explanations.is_dir():
        errors.append("controller diagnostic catalog source, compiled data, and explanations are required")
        return errors

    dockerfile = _text(definition).lower()
    for token in config.get("required_dockerfile_tokens", []):
        if str(token).lower() not in dockerfile:
            errors.append(f"controller Dockerfile missing required boundary: {token}")
    for token in config.get("forbidden_dockerfile_tokens", []):
        if str(token).lower() in dockerfile:
            errors.append(f"controller Dockerfile contains worker-only content: {token}")

    try:
        document = _metadata(requirements)
    except ValueError as exc:
        errors.append(str(exc))
        return errors
    lock = "\n".join(_metadata_group(document, "controller"))
    for package in config.get("required_controller_packages", []):
        if str(package).lower() not in lock:
            errors.append(f"controller dependency lock missing required package: {package}")
    for package in config.get("forbidden_controller_packages", []):
        if str(package).lower() in lock:
            errors.append(f"controller dependency lock contains worker-only package: {package}")

    ignored = _text(context_ignore).lower()
    for token in config.get("required_context_ignore_tokens", []):
        if str(token).lower() not in ignored:
            errors.append(f"build context does not exclude sensitive or worker-only path: {token}")

    try:
        catalog = load_catalog()
        source_codes = _catalog_codes(catalog_source)
        compiled_codes = _catalog_codes(catalog_compiled)
    except Exception as exc:
        errors.append(f"controller diagnostic catalog cannot load: {type(exc).__name__}")
    else:
        loaded_codes = {str(definition_item.code) for definition_item in catalog.definitions}
        if not catalog.definitions:
            errors.append("controller diagnostic catalog has no active definitions")
        if source_codes != compiled_codes or compiled_codes != loaded_codes:
            errors.append("controller diagnostic source, compiled, and packaged code sets differ")
        explanation_files = {path.name for path in catalog_explanations.glob("*.md")}
        expected_explanations = {Path(item.explanation_path).name for item in catalog.definitions}
        if explanation_files != expected_explanations:
            errors.append("controller diagnostic explanation files do not match the complete catalog")
        for definition_item in catalog.definitions:
            explanation = catalog_explanations / Path(definition_item.explanation_path).name
            if not explanation.is_file():
                errors.append(f"missing diagnostic explanation: {explanation.relative_to(ROOT)}")
    return errors


def _check_workers(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    definition = ROOT / str(config.get("worker_definition", ""))
    if not definition.is_file():
        return [f"missing worker image definition: {definition.relative_to(ROOT)}"]
    dockerfile = _text(definition).lower()
    for token in config.get("required_worker_dockerfile_tokens", []):
        if str(token).lower() not in dockerfile:
            errors.append(f"worker Dockerfile missing required boundary: {token}")
    for token in config.get("forbidden_worker_dockerfile_tokens", []):
        if str(token).lower() in dockerfile:
            errors.append(f"worker Dockerfile contains controller-only or exposed content: {token}")

    metadata_path = ROOT / str(config.get("controller_requirements", ""))
    try:
        document = _metadata(metadata_path)
    except ValueError as exc:
        errors.append(str(exc))
        document = {}
    worker_groups = config.get("worker_dependency_groups", ["piper"])
    worker_values = [dependency for group in worker_groups for dependency in _metadata_group(document, str(group))]
    project = document.get("project")
    if isinstance(project, dict) and isinstance(project.get("dependencies"), list):
        worker_values.extend(str(value).lower() for value in project["dependencies"] if isinstance(value, str))
    lock = "\n".join(worker_values)
    for token in config.get("forbidden_worker_dependency_tokens", []):
        if str(token).lower() in lock:
            errors.append(f"worker project metadata contains controller-only package: {token}")
    profiles = tuple(str(item) for item in config.get("worker_profiles", []))
    for profile in profiles:
        if profile not in dockerfile:
            errors.append(f"worker Dockerfile does not declare profile: {profile}")
    return errors


def main() -> int:
    config = load_toml(ROOT / "quality/image-boundaries.toml")
    try:
        review_date = parse_review_date(config.get("review_date"), context="quality/image-boundaries.toml")
    except ValueError:
        review_date = None
    if review_date is None or review_date < dt.date.today():
        print("image-boundaries-check: declaration review_date is missing or expired")
        return 1

    if config.get("status") != "active":
        print("image-boundaries-check: active controller declaration is required once image definitions exist")
        return 1

    errors = [*_check_active(config), *_check_workers(config)]
    if errors:
        print("image-boundaries-check: controller or worker boundary failed")
        for error in errors:
            print(f"- {error}")
        return 1
    print("image-boundaries-check: controller and worker image boundaries satisfied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
