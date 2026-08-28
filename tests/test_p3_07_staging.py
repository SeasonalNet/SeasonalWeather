from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tools.staging_interface import PROJECT, _service_snapshot_is_healthy, compose_command, validate_environment

ROOT = Path(__file__).resolve().parents[1]


def _yaml(path: Path) -> dict[str, Any]:
    yaml = importlib.import_module("yaml")
    loader = type("ComposeLoader", (yaml.SafeLoader,), {})
    _ = loader.add_constructor("!override", lambda current, node: current.construct_sequence(node))
    value = yaml.load(path.read_text(encoding="utf-8"), Loader=loader)
    assert isinstance(value, dict)
    return value


def _expect_system_exit(action: Callable[[], object], message: str) -> None:
    try:
        _ = action()
    except SystemExit as exc:
        assert message in str(exc)
    else:
        raise AssertionError("expected SystemExit")


def test_staging_overlay_isolated_from_the_default_compose_project() -> None:
    raw = (ROOT / "compose.staging.yaml").read_text(encoding="utf-8")
    overlay = _yaml(ROOT / "compose.staging.yaml")

    assert overlay["name"] == PROJECT
    assert "SEASONALWEATHER_COMPOSE_PROJECT" not in raw
    assert "./config/config.yaml" not in raw
    assert "./secrets" not in raw
    assert "SEASONALWEATHER_STAGING_CONFIG_FILE" in raw
    assert "SEASONALWEATHER_STAGING_SECRET_DIR" in raw
    assert "ports: !override" in raw


def test_staging_overlay_uses_an_alternate_loopback_api_and_stream() -> None:
    services = _yaml(ROOT / "compose.staging.yaml")["services"]
    assert isinstance(services, dict)
    controller = services["controller"]
    liquidsoap = services["liquidsoap"]
    icecast = services["icecast"]
    assert controller["ports"] == ["127.0.0.1:${SEASONALWEATHER_STAGING_API_PORT:-19080}:9080"]
    assert icecast["ports"] == ["127.0.0.1:${SEASONALWEATHER_STAGING_ICECAST_PORT:-18000}:8000"]
    assert liquidsoap["environment"] == {
        "SEASONALWEATHER_ICECAST_HOST": "icecast",
        "SEASONALWEATHER_ICECAST_PORT": "8000",
    }
    assert liquidsoap["depends_on"]["icecast"]["condition"] == "service_started"


def test_staging_command_pins_project_profile_and_compose_files() -> None:
    command = compose_command("up", "--detach")
    assert command[:8] == [
        "docker",
        "compose",
        "--project-name",
        PROJECT,
        "--profile",
        "icecast",
        "--file",
        str(ROOT / "compose.yaml"),
    ]
    assert command[8:10] == ["--file", str(ROOT / "compose.staging.yaml")]
    assert "-v" not in command


def test_staging_command_can_select_optional_worker_profiles() -> None:
    command = compose_command("up", "--detach", profiles=("piper", "spfy"))
    assert command[4:12] == [
        "--profile",
        "icecast",
        "--profile",
        "piper",
        "--profile",
        "spfy",
        "--file",
        str(ROOT / "compose.yaml"),
    ]


def test_staging_soak_snapshot_rejects_empty_stopped_and_unhealthy_output() -> None:
    assert not _service_snapshot_is_healthy("")
    assert not _service_snapshot_is_healthy('[{"State":"running"}, 1]')
    assert not _service_snapshot_is_healthy('{"State":"exited","Health":""}')
    assert not _service_snapshot_is_healthy('{"State":"running","Health":"unhealthy"}')
    assert _service_snapshot_is_healthy('[{"State":"running","Health":"healthy"}]')
    assert _service_snapshot_is_healthy('{"State":"running"}\n')


def test_staging_environment_requires_external_config_and_mode_0400_secrets(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "staging-config.yaml"
    config.write_text("config_schema: 1\n", encoding="utf-8")
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    for name in ("ICECAST_SOURCE_PASSWORD", "SEASONAL_API_TOKEN", "SEASONAL_WORKER_TOKEN"):
        secret = secret_dir / name
        _ = secret.write_text("test-only\n", encoding="utf-8")
        _ = secret.chmod(0o400)
    _ = monkeypatch.setenv("SEASONALWEATHER_STAGING_CONFIG_FILE", str(config))
    _ = monkeypatch.setenv("SEASONALWEATHER_STAGING_SECRET_DIR", str(secret_dir))

    assert validate_environment() == (config, secret_dir)


def test_staging_environment_rejects_repository_config_and_weak_secrets(monkeypatch, tmp_path: Path) -> None:
    _ = monkeypatch.setenv("SEASONALWEATHER_STAGING_CONFIG_FILE", str(ROOT / "config/config.yaml"))
    _ = monkeypatch.setenv("SEASONALWEATHER_STAGING_SECRET_DIR", str(tmp_path))
    _expect_system_exit(validate_environment, "outside the repository")

    config = tmp_path / "staging.yaml"
    _ = config.write_text("config_schema: 1\n", encoding="utf-8")
    monkeypatch.setenv("SEASONALWEATHER_STAGING_CONFIG_FILE", str(config))
    for name in ("ICECAST_SOURCE_PASSWORD", "SEASONAL_API_TOKEN", "SEASONAL_WORKER_TOKEN"):
        _ = (tmp_path / name).write_text("test-only\n", encoding="utf-8")
        _ = (tmp_path / name).chmod(0o644)
    _expect_system_exit(validate_environment, "group/world accessible")


def test_staging_environment_resolves_paths_before_repository_isolation_check(monkeypatch, tmp_path: Path) -> None:
    config_link = tmp_path / "config-link.yaml"
    config_link.symlink_to(ROOT / "config/config.yaml")
    secret_link = tmp_path / "secret-link"
    secret_link.symlink_to(ROOT, target_is_directory=True)
    _ = monkeypatch.setenv("SEASONALWEATHER_STAGING_CONFIG_FILE", str(config_link))
    _ = monkeypatch.setenv("SEASONALWEATHER_STAGING_SECRET_DIR", str(tmp_path))
    _expect_system_exit(validate_environment, "outside the repository")

    config = tmp_path / "staging.yaml"
    _ = config.write_text("config_schema: 1\n", encoding="utf-8")
    _ = monkeypatch.setenv("SEASONALWEATHER_STAGING_CONFIG_FILE", str(config))
    _ = monkeypatch.setenv("SEASONALWEATHER_STAGING_SECRET_DIR", str(secret_link))
    _expect_system_exit(validate_environment, "outside the repository")


def test_rollback_env_file_allows_only_image_identity(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "staging.yaml"
    config.write_text("config_schema: 1\n", encoding="utf-8")
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    for name in ("ICECAST_SOURCE_PASSWORD", "SEASONAL_API_TOKEN", "SEASONAL_WORKER_TOKEN"):
        secret = secret_dir / name
        _ = secret.write_text("test-only\n", encoding="utf-8")
        _ = secret.chmod(0o400)
    rollback = tmp_path / "rollback.env"
    _ = rollback.write_text(
        "\n".join(
            [
                "SEASONALWEATHER_CONTROLLER_IMAGE=registry.example/seasonalweather@sha256:" + "a" * 64,
                "SEASONALWEATHER_ROUTINE_WORKER_IMAGE=registry.example/seasonalweather-worker@sha256:" + "b" * 64,
                "SEASONALWEATHER_LIQUIDSOAP_IMAGE=savonet/liquidsoap@sha256:" + "c" * 64,
                "SEASONALWEATHER_ICECAST_IMAGE=ghcr.io/libretime/icecast@sha256:" + "d" * 64,
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _ = monkeypatch.setenv("SEASONALWEATHER_STAGING_CONFIG_FILE", str(config))
    _ = monkeypatch.setenv("SEASONALWEATHER_STAGING_SECRET_DIR", str(secret_dir))

    _ = validate_environment(rollback_env_file=rollback)
    assert "--env-file" in compose_command("up", rollback_env_file=rollback)

    _ = rollback.write_text(
        "\n".join(
            [
                "SEASONAL_API_TOKEN=must-not-be-here",
                "SEASONALWEATHER_ROUTINE_WORKER_IMAGE=registry.example/seasonalweather-worker@sha256:" + "b" * 64,
                "SEASONALWEATHER_LIQUIDSOAP_IMAGE=savonet/liquidsoap@sha256:" + "c" * 64,
                "SEASONALWEATHER_ICECAST_IMAGE=ghcr.io/libretime/icecast@sha256:" + "d" * 64,
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _expect_system_exit(lambda: validate_environment(rollback_env_file=rollback), "non-image keys")

    _ = rollback.write_text("not-an-env-line\n", encoding="utf-8")
    _expect_system_exit(lambda: validate_environment(rollback_env_file=rollback), "malformed line")

    _ = rollback.write_text(
        "\n".join(
            [
                "SEASONALWEATHER_CONTROLLER_IMAGE=registry.example/seasonalweather@sha256:" + "a" * 64,
                "SEASONALWEATHER_CONTROLLER_IMAGE=registry.example/seasonalweather@sha256:" + "e" * 64,
                "SEASONALWEATHER_ROUTINE_WORKER_IMAGE=registry.example/seasonalweather-worker@sha256:" + "b" * 64,
                "SEASONALWEATHER_LIQUIDSOAP_IMAGE=savonet/liquidsoap@sha256:" + "c" * 64,
                "SEASONALWEATHER_ICECAST_IMAGE=ghcr.io/libretime/icecast@sha256:" + "d" * 64,
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _expect_system_exit(lambda: validate_environment(rollback_env_file=rollback), "duplicate key")

    _ = rollback.write_text(
        "\n".join(
            [
                "SEASONALWEATHER_CONTROLLER_IMAGE=",
                "SEASONALWEATHER_ROUTINE_WORKER_IMAGE=registry.example/seasonalweather-worker@sha256:" + "b" * 64,
                "SEASONALWEATHER_LIQUIDSOAP_IMAGE=savonet/liquidsoap@sha256:" + "c" * 64,
                "SEASONALWEATHER_ICECAST_IMAGE=ghcr.io/libretime/icecast@sha256:" + "d" * 64,
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _expect_system_exit(lambda: validate_environment(rollback_env_file=rollback), "invalid immutable image value")

    _ = rollback.write_text(
        "\n".join(
            [
                "SEASONALWEATHER_CONTROLLER_IMAGE=seasonalweather:prior",
                "SEASONALWEATHER_ROUTINE_WORKER_IMAGE=registry.example/seasonalweather-worker@sha256:" + "b" * 64,
                "SEASONALWEATHER_LIQUIDSOAP_IMAGE=savonet/liquidsoap@sha256:" + "c" * 64,
                "SEASONALWEATHER_ICECAST_IMAGE=ghcr.io/libretime/icecast@sha256:" + "d" * 64,
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _expect_system_exit(lambda: validate_environment(rollback_env_file=rollback), "immutable image value")


def test_rollback_requires_the_selected_optional_worker_image(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "staging.yaml"
    config.write_text("config_schema: 1\n", encoding="utf-8")
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    for name in ("ICECAST_SOURCE_PASSWORD", "SEASONAL_API_TOKEN", "SEASONAL_WORKER_TOKEN"):
        secret = secret_dir / name
        _ = secret.write_text("test-only\n", encoding="utf-8")
        _ = secret.chmod(0o400)
    rollback = tmp_path / "rollback.env"
    _ = rollback.write_text(
        "\n".join(
            [
                "SEASONALWEATHER_CONTROLLER_IMAGE=registry.example/seasonalweather@sha256:" + "a" * 64,
                "SEASONALWEATHER_ROUTINE_WORKER_IMAGE=registry.example/seasonalweather-worker@sha256:" + "b" * 64,
                "SEASONALWEATHER_LIQUIDSOAP_IMAGE=savonet/liquidsoap@sha256:" + "c" * 64,
                "SEASONALWEATHER_ICECAST_IMAGE=ghcr.io/libretime/icecast@sha256:" + "d" * 64,
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _ = monkeypatch.setenv("SEASONALWEATHER_STAGING_CONFIG_FILE", str(config))
    _ = monkeypatch.setenv("SEASONALWEATHER_STAGING_SECRET_DIR", str(secret_dir))

    _expect_system_exit(
        lambda: validate_environment(rollback_env_file=rollback, profiles=("piper",)),
        "SEASONALWEATHER_PIPER_WORKER_IMAGE",
    )
    with rollback.open("a", encoding="utf-8") as stream:
        _ = stream.write(
            "SEASONALWEATHER_PIPER_WORKER_IMAGE=registry.example/seasonalweather-worker@sha256:" + "e" * 64 + "\n"
        )
    _ = validate_environment(rollback_env_file=rollback, profiles=("piper",))
