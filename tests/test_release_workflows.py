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


def test_release_gate_workflows_are_reusable() -> None:
    for provider in (".forgejo", ".github"):
        for workflow in ("ci.yml", "security.yml", "semver.yml"):
            content = (ROOT / provider / "workflows" / workflow).read_text()
            assert "workflow_call:" in content
