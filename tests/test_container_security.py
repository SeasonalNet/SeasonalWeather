from __future__ import annotations

from pathlib import Path

from tools.quality.container_security_check import main

ROOT = Path(__file__).resolve().parents[1]


def test_container_security_contract_passes() -> None:
    assert main() == 0


def test_dockerfiles_declare_runtime_hardening_labels() -> None:
    controller = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    worker = (ROOT / "Dockerfile.worker").read_text(encoding="utf-8")
    for text, profile in ((controller, "controller"), (worker, "worker")):
        assert f'io.seasonalweather.security.profile="{profile}"' in text
        assert 'io.seasonalweather.security.read-only-root="required"' in text
        assert 'io.seasonalweather.security.no-new-privileges="required"' in text
        assert 'io.seasonalweather.security.cap-drop="ALL"' in text
        assert 'io.seasonalweather.security.tmpfs="/tmp,/run"' in text
        assert 'io.seasonalweather.security.secrets="read-only-per-service"' in text


def test_build_context_excludes_secret_and_database_material() -> None:
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    for pattern in ("*.env", "*.pem", "*.key", "*.p12", "*.sqlite3"):
        assert pattern in dockerignore


def test_security_contract_records_runtime_flags_and_mount_modes() -> None:
    contract = (ROOT / "quality/container-security.toml").read_text(encoding="utf-8")
    for token in (
        '"--read-only"',
        '"--cap-drop=ALL"',
        '"--security-opt=no-new-privileges:true"',
        '"--tmpfs=/tmp:rw,nosuid,nodev,noexec"',
        '"--tmpfs=/run:rw,nosuid,nodev,noexec"',
        'config_mount_mode = "read-only"',
        'secret_mount_mode = "read-only"',
        'secret_file_mode = "0400"',
        'diagnostic_mount_mode = "read-only"',
    ):
        assert token in contract


def test_worker_dockerfile_does_not_create_controller_state_paths() -> None:
    worker = (ROOT / "Dockerfile.worker").read_text(encoding="utf-8")
    for forbidden in (
        "/var/lib/seasonalweather/state",
        "/var/lib/seasonalweather/jobs",
        "/var/log/seasonalweather",
    ):
        assert forbidden not in worker


def test_dockerfiles_do_not_declare_secret_shaped_build_inputs() -> None:
    for name in ("Dockerfile", "Dockerfile.worker"):
        text = (ROOT / name).read_text(encoding="utf-8").lower()
        assert "arg password" not in text
        assert "arg secret" not in text
        assert "arg token" not in text
        assert "env password" not in text
        assert "env secret" not in text
        assert "env token" not in text


def test_secret_allowlist_is_controller_only() -> None:
    contract = (ROOT / "quality/container-security.toml").read_text(encoding="utf-8")
    assert "secret_files = []" in contract
    assert '"ICECAST_SOURCE_PASSWORD"' in contract
    assert '"SEASONAL_API_TOKEN"' in contract
