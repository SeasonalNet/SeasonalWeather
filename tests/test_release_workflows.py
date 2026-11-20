import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def test_release_workflows_require_all_gates_and_publish_release_directory() -> None:
    for provider in (".forgejo", ".github"):
        release = (ROOT / provider / "workflows" / "release.yml").read_text()
        assert "tags:" in release
        assert "workflow_call" not in release
        assert "uses: ./" + provider + "/workflows/ci.yml" in release
        assert "uses: ./" + provider + "/workflows/security.yml" in release
        assert "uses: ./" + provider + "/workflows/semver.yml" in release
        assert "needs: [ci, security, semver]" in release
        assert "dist/release" in release
        assert "release-images" in release
        assert "IMAGE_REPOSITORY_BASE" in release
        assert "IMAGE_TAG" in release
        if provider == ".github":
            assert "ghcr.io/seasonalnet/seasonalweather" in release
        else:
            assert "registry.lan.seasonalnet.org/seasonalnet/seasonalweather" in release
            assert "secrets.HARBOR_CI_USERNAME" in release
            assert "secrets.HARBOR_CI_PASSWORD" in release


def test_release_gate_workflows_are_reusable() -> None:
    for provider in (".forgejo", ".github"):
        for workflow in ("ci.yml", "security.yml", "semver.yml"):
            content = (ROOT / provider / "workflows" / workflow).read_text()
            assert "workflow_call:" in content


def test_github_release_image_publishing_is_gated_before_release_creation() -> None:
    release = (ROOT / ".github/workflows/release.yml").read_text()
    image_step = release.index("release-images")
    provider_release = release.index("Create GitHub release")
    assert image_step < provider_release


def test_forgejo_release_publishes_images_to_harbor_and_release_assets() -> None:
    release = (ROOT / ".forgejo/workflows/release.yml").read_text()
    assert "runs-on: [docker, normal, builder]" in release
    assert "needs: [ci, security, semver]" in release
    assert "Publish Forgejo release" in release
    assert "bootstrap_docker.sh" in release
    assert "registry.lan.seasonalnet.org/seasonalnet/seasonalweather" in release
    assert "secrets.HARBOR_CI_USERNAME" in release
    assert "secrets.HARBOR_CI_PASSWORD" in release
    assert "release-images" in release
    assert "PACKAGE_REGISTRY_" not in release


def test_release_workflows_use_bootstrap_uv_after_activating_release_venv() -> None:
    setup = (
        'export PATH="$PWD/.venv-bootstrap/bin:$PATH"\n'
        "          uv python install 3.15.2\n"
        "          uv venv --clear --python 3.15.2 .venv-release\n"
        "          . .venv-release/bin/activate\n"
        "          uv sync --frozen --active"
    )
    for provider in (".forgejo", ".github"):
        release = (ROOT / provider / "workflows" / "release.yml").read_text()
        assert setup in release


def test_github_release_exposes_bootstrap_uv_to_artifact_build_step() -> None:
    release = (ROOT / ".github/workflows/release.yml").read_text()
    install, artifacts = release.split("- name: Build release artifacts", maxsplit=1)
    assert 'echo "$PWD/.venv-bootstrap/bin" >> "$GITHUB_PATH"' in install
    assert "make release-artifacts" in artifacts


def test_ci_workflows_enable_best_effort_dependency_and_provider_build_caches() -> None:
    github_ci = (ROOT / ".github/workflows/ci.yml").read_text()
    forgejo_ci = (ROOT / ".forgejo/workflows/ci.yml").read_text()

    for workflow in (github_ci, forgejo_ci):
        assert "actions/cache@v4" in workflow
        assert "path: ~/.cache/uv" in workflow
        assert "uv.lock" in workflow

    assert "type=gha,scope=seasonalweather-{profile}" in github_ci
    assert "mode=max,ignore-error=true" in github_ci
    assert "SW_BUILD_CACHE_FROM" not in forgejo_ci
    assert "SW_BUILD_CACHE_TO" not in forgejo_ci
    assert "seasonalweather-cache" not in forgejo_ci


def test_github_ci_pins_the_wine_compatible_runner() -> None:
    github_ci = (ROOT / ".github/workflows/ci.yml").read_text()

    assert github_ci.count("runs-on: ubuntu-24.04") == 2
    assert "ubuntu-latest" not in github_ci


def test_release_workflows_enable_best_effort_dependency_and_provider_build_caches() -> None:
    github_release = (ROOT / ".github/workflows/release.yml").read_text()
    forgejo_release = (ROOT / ".forgejo/workflows/release.yml").read_text()

    for workflow in (github_release, forgejo_release):
        assert "actions/cache@v4" in workflow
        assert "path: ~/.cache/uv" in workflow
        assert "uv.lock" in workflow

    assert "type=gha,scope=seasonalweather-{profile}" in github_release
    assert "mode=max,ignore-error=true" in github_release
    assert "SW_BUILD_CACHE_FROM" not in forgejo_release
    assert "SW_BUILD_CACHE_TO" not in forgejo_release
    assert "seasonalweather-cache" not in forgejo_release


def test_ci_suppression_base_falls_back_after_rewritten_push() -> None:
    for provider in (".forgejo", ".github"):
        ci = (ROOT / provider / "workflows" / "ci.yml").read_text()
        assert 'if ! git cat-file -e "${base}^{commit}" 2>/dev/null; then' in ci
        assert 'base="HEAD^"' in ci
        assert "after a rewritten push" in ci


def test_github_release_publishes_images_to_ghcr() -> None:
    forgejo_release = (ROOT / ".forgejo/workflows/release.yml").read_text()
    github_release = (ROOT / ".github/workflows/release.yml").read_text()

    assert "ghcr.io/" not in forgejo_release
    assert "registry.lan.seasonalnet.org/seasonalnet/seasonalweather" in forgejo_release
    assert "ghcr.io/seasonalnet/seasonalweather" in github_release
    assert "release-images" in github_release


@pytest.mark.parametrize("failed_profile", ["", "piper"])
def test_release_images_wait_for_parallel_builds(tmp_path: Path, failed_profile: str) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    work = tmp_path / ".git"
    makefile = tmp_path / "Makefile"
    makefile.write_text((ROOT / "Makefile").read_text())
    (work / "info" / "exclude").write_text("/Makefile\n")
    fake_python = work / "fake-python"
    fake_python.write_text(
        """#!/bin/sh
set -eu
case "$2" in
    seasonalweather.build_metadata)
        printf '%s' "$8" > "$4"
        ;;
    tools.build_interface)
        profile="$7"
        test "$(cat "$5")" = "$profile"
        touch ".git/started-$profile"
        for i in $(seq 1 100); do
            set -- .git/started-*
            [ "$#" -lt 11 ] || break
            sleep 0.05
        done
        test "$#" -eq 11
        touch ".git/finished-$profile"
        test "$profile" != "$FAILED_PROFILE"
        ;;
    *) exit 2 ;;
esac
"""
    )
    fake_python.chmod(0o755)
    manifest = work / "references"
    manifest.write_text("previous\n")
    result = subprocess.run(
        [
            "make",
            "-f",
            str(makefile),
            f"PYTHON={fake_python}",
            "IMAGE_REPOSITORY_BASE=example.invalid/seasonalweather",
            "IMAGE_TAG=v1.2.3",
            f"IMAGE_REFERENCES_FILE={manifest}",
            "release-images",
        ],
        cwd=tmp_path,
        env={**os.environ, "FAILED_PROFILE": failed_profile},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert len(list(work.glob("finished-*"))) == 11
    if failed_profile:
        assert result.returncode != 0
        assert manifest.read_text() == "previous\n"
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        lines = manifest.read_text().splitlines()
        assert len(lines) == 11
        assert lines[0] == "controller\texample.invalid/seasonalweather:v1.2.3-controller"
        assert "piper\texample.invalid/seasonalweather-worker:v1.2.3-piper" in lines
    assert not list(work.glob(".release-images.*"))
