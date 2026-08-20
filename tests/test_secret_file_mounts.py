from __future__ import annotations

from pathlib import Path

from seasonalweather.configuration.compiler import compile_path
from seasonalweather.configuration.loader import load_runtime_config
from seasonalweather.secret_files import SecretFileError, merge_secret_files

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config/config.yaml"


def _candidate(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        EXAMPLE.read_text(encoding="utf-8").replace(
            '  secret_dir: "/run/secrets"',
            f'  secret_dir: "{tmp_path / "secrets"}"',
            1,
        ),
        encoding="utf-8",
    )
    (tmp_path / "secrets").mkdir()
    return path


def _write_secret(directory: Path, name: str, value: str) -> None:
    path = directory / name
    path.write_text(value, encoding="utf-8")
    path.chmod(0o400)


def test_mounted_secret_files_overlay_environment_compatibility(tmp_path: Path) -> None:
    config_path = _candidate(tmp_path)
    _write_secret(tmp_path / "secrets", "ICECAST_SOURCE_PASSWORD", "mounted-source")
    _write_secret(tmp_path / "secrets", "SEASONAL_API_TOKEN", "mounted-api-token")

    config = load_runtime_config(
        str(config_path),
        environ={"ICECAST_SOURCE_PASSWORD": "environment-source", "SEASONAL_API_TOKEN": "environment-api-token"},
    )

    assert config.secrets.icecast_source_password == "mounted-source"
    assert config.secrets.api_token == "mounted-api-token"


def test_environment_values_remain_supported_without_secret_files(tmp_path: Path) -> None:
    config_path = _candidate(tmp_path)
    config = load_runtime_config(
        str(config_path),
        environ={"ICECAST_SOURCE_PASSWORD": "environment-source", "SEASONAL_API_TOKEN": "environment-api-token"},
    )

    assert config.secrets.icecast_source_password == "environment-source"


def test_mounted_secret_files_supply_values_without_environment_fallback(tmp_path: Path) -> None:
    config_path = _candidate(tmp_path)
    _write_secret(tmp_path / "secrets", "ICECAST_SOURCE_PASSWORD", "mounted-source")
    _write_secret(tmp_path / "secrets", "SEASONAL_API_TOKEN", "mounted-api-token")

    config = load_runtime_config(str(config_path), environ={})

    assert config.secrets.icecast_source_password == "mounted-source"
    assert config.secrets.api_token == "mounted-api-token"


def test_secret_files_are_not_added_to_compiler_report_values(tmp_path: Path) -> None:
    config_path = _candidate(tmp_path)
    _write_secret(tmp_path / "secrets", "ICECAST_SOURCE_PASSWORD", "report-secret-sentinel")
    compiled = compile_path(config_path, environ={})

    assert compiled.valid
    rendered = compiled.report.to_json()
    assert "report-secret-sentinel" not in rendered


def test_secret_file_permissions_fail_closed(tmp_path: Path) -> None:
    for mode in (0o000, 0o440, 0o600, 0o644):
        directory = tmp_path / f"secrets-{mode:o}"
        directory.mkdir()
        path = directory / "ICECAST_SOURCE_PASSWORD"
        path.write_text("secret", encoding="utf-8")
        path.chmod(mode)

        try:
            merge_secret_files({}, config_value={"paths": {"secret_dir": str(directory)}})
        except SecretFileError as exc:
            assert "mode 0400" in str(exc)
        else:
            raise AssertionError(f"mode {mode:o} was accepted")


def test_secret_symlink_fails_closed(tmp_path: Path) -> None:
    directory = tmp_path / "secrets"
    directory.mkdir()
    target = tmp_path / "outside-secret"
    target.write_text("secret", encoding="utf-8")
    target.chmod(0o400)
    (directory / "ICECAST_SOURCE_PASSWORD").symlink_to(target)

    try:
        merge_secret_files({}, config_value={"paths": {"secret_dir": str(directory)}})
    except SecretFileError as exc:
        assert "regular file" in str(exc)
    else:
        raise AssertionError("symlink secret file was accepted")


def test_secret_directory_symlink_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / "real-secrets"
    target.mkdir()
    directory = tmp_path / "secrets"
    directory.symlink_to(target, target_is_directory=True)

    try:
        merge_secret_files({}, config_value={"paths": {"secret_dir": str(directory)}})
    except SecretFileError as exc:
        assert "directory must not be a symlink" in str(exc)
    else:
        raise AssertionError("symlink secret directory was accepted")


def test_secret_newline_is_limited_to_one_terminal_linebreak(tmp_path: Path) -> None:
    directory = tmp_path / "secrets"
    directory.mkdir()
    path = directory / "ICECAST_SOURCE_PASSWORD"
    path.write_text("secret\n", encoding="utf-8")
    path.chmod(0o400)

    assert (
        merge_secret_files({}, config_value={"paths": {"secret_dir": str(directory)}})["ICECAST_SOURCE_PASSWORD"]
        == "secret"
    )

    path.chmod(0o600)
    path.write_text("secret\nsecond", encoding="utf-8")
    path.chmod(0o400)
    try:
        merge_secret_files({}, config_value={"paths": {"secret_dir": str(directory)}})
    except SecretFileError as exc:
        assert "embedded line break" in str(exc)
    else:
        raise AssertionError("embedded line break was accepted")
