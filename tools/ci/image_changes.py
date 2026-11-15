"""Classify changed paths and select the smallest safe CI gate."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC_FILES = {"CONTRIBUTING.md", "LICENSE", "README.md"}
IMAGE_FILES = {
    ".dockerignore",
    "Dockerfile",
    "Dockerfile.worker",
    "Makefile",
    "docker-bake.hcl",
    "pyproject.toml",
    "uv.lock",
}
IMAGE_PREFIXES = (
    "docker/",
    "scripts/worker/",
    "scripts/wrappers/",
    "seasonalweather/",
    "tools/build_interface.py",
    "tools/ci/",
    "tools/quality/phase2_exit_gate.py",
    "tools/samegen/",
)
DOC_PREFIXES = ("docs/",)
CI_PREFIXES = (".forgejo/workflows/", ".github/workflows/")
RUNTIME_PREFIXES = ("seasonalweather/",)
CHECK_PREFIXES = ("config/", "scripts/", "systemd/", "typings/")


def classify_path(path: str) -> str:
    # README changes affect package metadata, not runtime image behavior.
    if path in DOC_FILES or path.startswith(DOC_PREFIXES) or path.endswith((".md", ".rst")):
        return "docs"
    if path in CI_PREFIXES or path.startswith(CI_PREFIXES) or path == "tools/ci/image_changes.py":
        return "ci"
    if path.startswith("tests/"):
        return "tests"
    if path == "tools/quality/phase2_exit_gate.py":
        return "image"
    if path.startswith("quality/") or path.startswith("tools/quality/"):
        return "quality"
    if path in IMAGE_FILES or path.startswith(IMAGE_PREFIXES):
        return "image"
    if path.startswith(RUNTIME_PREFIXES):
        return "runtime"
    if path.startswith(CHECK_PREFIXES):
        return "config"
    return "unknown"


def classify_paths(paths: list[str]) -> set[str]:
    return {classify_path(path) for path in paths}


def gate_for_paths(paths: list[str], *, force: bool = False) -> tuple[bool, bool]:
    """Return ``(run_checks, run_images)`` for the changed paths."""
    if force:
        return True, True
    categories = classify_paths(paths)
    if categories == {"docs"} or not categories:
        return False, False
    return True, bool(categories & {"image", "runtime", "unknown"})


def requires_image_gate(paths: list[str]) -> bool:
    return gate_for_paths(paths)[1]


def changed_paths(base: str, head: str) -> list[str]:
    completed = subprocess.run(
        ["git", "diff", "--name-only", "-z", base, head, "--"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [item.decode("utf-8", errors="surrogateescape") for item in completed.stdout.split(b"\0") if item]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    args = parser.parse_args()
    paths = changed_paths(args.base, args.head)
    run_checks, run_images = gate_for_paths(paths)
    print(f"scope={','.join(sorted(classify_paths(paths))) or 'docs'}")
    print(f"run_checks={'true' if run_checks else 'false'}")
    print(f"run_images={'true' if run_images else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
