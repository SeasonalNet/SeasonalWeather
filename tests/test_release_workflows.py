from pathlib import Path

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
        if provider == ".github":
            assert "release-images" in release
            assert "IMAGE_REPOSITORY_BASE" in release
            assert "IMAGE_TAG" in release
        else:
            assert "release-images" not in release
            assert "IMAGE_REPOSITORY_BASE" not in release
            assert "IMAGE_TAG" not in release


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


def test_forgejo_release_publishes_assets_without_image_registry_access() -> None:
    release = (ROOT / ".forgejo/workflows/release.yml").read_text()
    assert "Publish Forgejo release" in release
    assert "release-images" not in release
    assert "bootstrap_docker.sh" not in release
    assert "PACKAGE_REGISTRY_" not in release


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


def test_github_release_publishes_images_to_ghcr() -> None:
    forgejo_release = (ROOT / ".forgejo/workflows/release.yml").read_text()
    github_release = (ROOT / ".github/workflows/release.yml").read_text()

    assert "SW_IMAGE_PUSH_MODE" not in forgejo_release
    assert "ghcr.io/seasonalnet/seasonalweather" in github_release
    assert "release-images" in github_release
