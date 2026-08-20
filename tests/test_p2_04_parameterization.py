from __future__ import annotations

from pathlib import Path
from typing import Protocol

from seasonalweather.config import load_config

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "config/config.yaml"


class _MonkeyPatch(Protocol):
    def setenv(self, name: str, value: str) -> None: ...


def _environment(monkeypatch: _MonkeyPatch) -> None:
    monkeypatch.setenv("ICECAST_SOURCE_PASSWORD", "synthetic-source")
    monkeypatch.setenv("SEASONAL_API_TOKEN", "synthetic-api-token")
    monkeypatch.setenv("NWWS_JID", "changeme@nwws-oi.weather.gov")
    monkeypatch.setenv("NWWS_PASSWORD", "CHANGEME")


def _write_variant(
    tmp_path: Path,
    *,
    replacements: tuple[tuple[str, str], ...] = (),
    removals: tuple[str, ...] = (),
    name: str,
) -> Path:
    text = EXAMPLE.read_text(encoding="utf-8")
    for old, new in replacements:
        if text.count(old) != 1:
            raise AssertionError(f"expected one configuration occurrence for {old!r}")
        text = text.replace(old, new)
    for value in removals:
        if text.count(value) != 1:
            raise AssertionError(f"expected one configuration line for {value!r}")
        text = text.replace(value, "")
    candidate = tmp_path / name
    candidate.write_text(text, encoding="utf-8")
    return candidate


def _assert_invalid_network_config(candidate: Path) -> None:
    try:
        load_config(str(candidate))
    except ValueError as exc:
        assert "network" in str(exc)
    else:
        raise AssertionError("invalid network configuration was accepted")


def test_example_declares_explicit_p2_04_paths_and_network(monkeypatch: _MonkeyPatch) -> None:
    _environment(monkeypatch)

    config = load_config(str(EXAMPLE))

    assert config.paths.operational_state_dir == "/var/lib/seasonalweather/state"
    assert config.paths.job_state_dir == "/var/lib/seasonalweather/jobs"
    assert config.paths.artifact_dir == "/var/lib/seasonalweather/artifacts"
    assert config.paths.diagnostic_export_dir == "/usr/share/seasonalweather/diagnostics"
    assert config.paths.temporary_dir == "/tmp"
    assert config.paths.runtime_dir == "/run/seasonalweather"
    assert config.paths.secret_dir == "/run/secrets"
    assert config.network.api.bind_host == "127.0.0.1"
    assert config.network.api.port == 9080
    assert config.network.liquidsoap.host == "127.0.0.1"
    assert config.network.swwp.controller_path == "/v1/workers/connect"
    assert config.network.postgresql.enabled is False
    assert config.network.redis.enabled is False


def test_custom_network_values_are_typed_and_bounded(tmp_path: Path, monkeypatch: _MonkeyPatch) -> None:
    _environment(monkeypatch)
    candidate = _write_variant(
        tmp_path,
        name="config.yaml",
        replacements=(
            (
                '  api:\n    bind_host: "127.0.0.1"\n    port: 9080',
                '  api:\n    bind_host: "controller"\n    port: 19080',
            ),
            (
                '  liquidsoap:\n    host: "127.0.0.1"\n    port: 1234\n    timeout_seconds: 3.0',
                '  liquidsoap:\n    host: "liquidsoap"\n    port: 21234\n    timeout_seconds: 4.5',
            ),
            (
                '    worker_controller_url: ""',
                '    worker_controller_url: "wss://controller.example/v1/workers/connect"',
            ),
            (
                '  postgresql:\n    enabled: false\n    address: ""\n    port: 5432\n    database: ""\n    tls: true\n    connect_timeout_seconds: 5.0',
                '  postgresql:\n    enabled: true\n    address: "postgres.example"\n    port: 55432\n    database: "seasonalweather"\n    tls: true\n    connect_timeout_seconds: 7.0',
            ),
            (
                '  operational_state_dir: "/var/lib/seasonalweather/state"',
                f'  operational_state_dir: "{tmp_path / "state"}"',
            ),
        ),
    )

    config = load_config(str(candidate))

    assert config.network.api.bind_host == "controller"
    assert config.network.api.port == 19080
    assert config.network.liquidsoap.timeout_seconds == 4.5
    assert config.network.swwp.worker_controller_url == "wss://controller.example/v1/workers/connect"
    assert config.network.postgresql.address == "postgres.example"
    assert config.network.postgresql.port == 55432


def test_invalid_api_port_zero_fails_closed(tmp_path: Path, monkeypatch: _MonkeyPatch) -> None:
    _environment(monkeypatch)
    candidate = _write_variant(
        tmp_path,
        name="invalid-zero.yaml",
        replacements=(
            ('  api:\n    bind_host: "127.0.0.1"\n    port: 9080', '  api:\n    bind_host: "127.0.0.1"\n    port: 0'),
        ),
    )
    _assert_invalid_network_config(candidate)


def test_invalid_api_port_overflow_fails_closed(tmp_path: Path, monkeypatch: _MonkeyPatch) -> None:
    _environment(monkeypatch)
    candidate = _write_variant(
        tmp_path,
        name="invalid-overflow.yaml",
        replacements=(
            (
                '  api:\n    bind_host: "127.0.0.1"\n    port: 9080',
                '  api:\n    bind_host: "127.0.0.1"\n    port: 65536',
            ),
        ),
    )
    _assert_invalid_network_config(candidate)


def test_invalid_api_host_fails_closed(tmp_path: Path, monkeypatch: _MonkeyPatch) -> None:
    _environment(monkeypatch)
    candidate = _write_variant(
        tmp_path,
        name="invalid-host.yaml",
        replacements=(
            (
                '  api:\n    bind_host: "127.0.0.1"\n    port: 9080',
                '  api:\n    bind_host: "bad\\naddress"\n    port: 9080',
            ),
        ),
    )
    _assert_invalid_network_config(candidate)


def test_legacy_paths_derive_new_roots_from_work_dir(tmp_path: Path, monkeypatch: _MonkeyPatch) -> None:
    _environment(monkeypatch)
    candidate = _write_variant(
        tmp_path,
        name="legacy.yaml",
        replacements=(
            ('  work_dir: "/var/lib/seasonalweather"', f'  work_dir: "{tmp_path / "legacy-root"}"'),
            (
                '  path: "/var/lib/seasonalweather/state/seasonalweather.sqlite3"  # also stores exchange clients, tokens, and auth audit',
                '  path: ""',
            ),
        ),
        removals=(
            '  operational_state_dir: "/var/lib/seasonalweather/state"\n',
            '  job_state_dir: "/var/lib/seasonalweather/jobs"\n',
            '  artifact_dir: "/var/lib/seasonalweather/artifacts"\n',
            '  diagnostic_export_dir: "/usr/share/seasonalweather/diagnostics"\n',
            '  temporary_dir: "/tmp"\n',
            '  runtime_dir: "/run/seasonalweather"\n',
            '  secret_dir: "/run/secrets"\n',
        ),
    )

    config = load_config(str(candidate))

    legacy_root = tmp_path / "legacy-root"
    assert config.paths.operational_state_dir == str(legacy_root / "state")
    assert config.paths.job_state_dir == str(legacy_root / "jobs")
    assert config.paths.artifact_dir == str(legacy_root / "artifacts")
    assert config.database.path == str(legacy_root / "state" / "seasonalweather.sqlite3")
