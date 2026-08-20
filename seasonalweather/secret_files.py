"""Fail-closed loading of known configuration secrets from mounted files."""

from __future__ import annotations

import stat
from collections.abc import Mapping
from pathlib import Path

SECRET_FILE_MAX_BYTES = 64 * 1024
SECRET_ENVIRONMENT_NAMES = (
    "NWWS_JID",
    "NWWS_PASSWORD",
    "ICECAST_SOURCE_PASSWORD",
    "ICECAST_ADMIN_PASSWORD",
    "ICECAST_RELAY_PASSWORD",
    "SEASONAL_API_TOKEN",
    "SEASONAL_API_TOKENS_JSON",
    "SEASONAL_DISCORD_ALERTS_WEBHOOK",
    "SEASONAL_DISCORD_OPS_WEBHOOK",
    "SEASONAL_DISCORD_API_WEBHOOK",
    "SEASONAL_DISCORD_ERRORS_WEBHOOK",
)


class SecretFileError(ValueError):
    """A configured secret file is unsafe or malformed."""


def _secret_directory(value: Mapping[str, object] | None) -> Path:
    paths = value.get("paths") if isinstance(value, Mapping) else None
    configured = paths.get("secret_dir") if isinstance(paths, Mapping) else None
    return Path(str(configured or "/run/secrets"))


def _read_secret_bytes(path: Path, name: str) -> bytes:
    try:
        metadata = path.stat()
    except OSError as exc:
        raise SecretFileError(f"cannot stat secret file {name!r}") from exc
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise SecretFileError(f"secret file {name!r} must be a regular file")
    if stat.S_IMODE(metadata.st_mode) != 0o400:
        raise SecretFileError(f"secret file {name!r} must have mode 0400")
    if metadata.st_size > SECRET_FILE_MAX_BYTES:
        raise SecretFileError(f"secret file {name!r} exceeds the size limit")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SecretFileError(f"cannot read secret file {name!r}") from exc
    if len(raw) > SECRET_FILE_MAX_BYTES:
        raise SecretFileError(f"secret file {name!r} exceeds the size limit")
    return raw


def _decode_secret(raw: bytes, name: str) -> str:
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SecretFileError(f"secret file {name!r} is not UTF-8") from exc
    value = value.rstrip("\r\n")
    if "\x00" in value or "\r" in value or "\n" in value:
        raise SecretFileError(f"secret file {name!r} contains an embedded line break")
    return value


def _read_secret(path: Path, name: str) -> str:
    return _decode_secret(_read_secret_bytes(path, name), name)


def merge_secret_files(
    environ: Mapping[str, str],
    *,
    config_value: Mapping[str, object] | None,
) -> dict[str, str]:
    """Overlay independently validated mounted secrets onto environment values."""

    merged = dict(environ)
    directory = _secret_directory(config_value)
    if directory.is_symlink():
        raise SecretFileError("secret directory must not be a symlink")
    if not directory.exists():
        return merged
    if not directory.is_dir():
        raise SecretFileError("secret directory must be a directory")
    for name in SECRET_ENVIRONMENT_NAMES:
        path = directory / name
        if path.exists() or path.is_symlink():
            merged[name] = _read_secret(path, name)
    return merged
