"""Pure P1-14 semantic predicates shared by reports and runtime delegates."""

from __future__ import annotations

import math
import posixpath
import re
from collections.abc import Mapping
from typing import cast
from urllib.parse import SplitResult, urlsplit

REMOTE_TOKEN_TTL_MAX = 86_400
REMOTE_REFRESH_MARGIN_MAX = 3_600
REMOTE_CONNECT_TIMEOUT_MAX = 30.0
REMOTE_TOKEN_TIMEOUT_MAX = 60.0
REMOTE_SYNTHESIS_TIMEOUT_MAX = 600.0
REMOTE_INPUT_BYTES_MAX = 1_048_576
REMOTE_RESPONSE_BYTES_MAX = 128 * 1024 * 1024
REMOTE_ERROR_BYTES_MAX = 1 * 1024 * 1024
_REMOTE_HOSTNAME_RE = re.compile(
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.?"
)


def remote_tts_configuration_errors(
    provider: str,
    config: Mapping[str, object],
    *,
    selected: bool,
) -> tuple[tuple[str, str], ...]:
    """Return pure, source-relative errors for one selected remote provider."""

    if not selected:
        return ()
    errors: list[tuple[str, str]] = []
    base_url = str(config.get("base_url", "") or "").strip()
    credential_key = "api_key_file" if provider == "openai_compatible" else "client_credential_file"
    url_error = remote_tts_base_url_error(provider, base_url)
    if url_error is not None:
        errors.append(("base_url", url_error))
    errors.extend(_remote_credential_errors(config, credential_key))
    errors.extend(_remote_profile_errors(provider, config))
    errors.extend(_remote_numeric_errors(provider, config))
    return tuple(errors)


def remote_tts_base_url_error(provider: str, value: str) -> str | None:
    """Return one bounded URL error without exposing parser exceptions."""

    if _remote_url_has_control(value):
        return "must be a well-formed HTTPS origin"
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return "must be a well-formed HTTPS origin"
    if not _valid_remote_hostname(parsed, hostname) or not _valid_remote_port(parsed, port):
        return "must be a well-formed HTTPS origin"
    if _remote_origin_is_invalid(parsed):
        return "must be an HTTPS origin without credentials, query, or fragment"
    path = parsed.path.rstrip("/")
    expected = "/v1" if provider == "openai_compatible" else ""
    if path != expected:
        message = "must use the /v1 API path" if expected else "must use the provider origin without an API path"
        return message
    return None


def _remote_url_has_control(value: str) -> bool:
    return any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)


def _remote_origin_is_invalid(parsed: SplitResult) -> bool:
    return any(
        (
            parsed.scheme != "https",
            not parsed.netloc,
            parsed.username is not None,
            parsed.password is not None,
            bool(parsed.query),
            bool(parsed.fragment),
        )
    )


def _valid_remote_hostname(parsed: SplitResult, hostname: str | None) -> bool:
    if not hostname:
        return False
    if ":" in hostname:
        return parsed.netloc.startswith("[") and parsed.netloc.count("[") == 1 and parsed.netloc.count("]") == 1
    return bool(_REMOTE_HOSTNAME_RE.fullmatch(hostname))


def _valid_remote_port(parsed: SplitResult, port: int | None) -> bool:
    authority = parsed.netloc.rsplit("@", 1)[-1]
    if authority.startswith("["):
        closing = authority.find("]")
        suffix = authority[closing + 1 :] if closing >= 0 else ""
        return suffix == "" or (
            suffix.startswith(":") and suffix[1:].isdigit() and port is not None and 1 <= port <= 65_535
        )
    if ":" not in authority:
        return port is None
    suffix = authority.rsplit(":", 1)[1]
    return suffix.isdigit() and port is not None and 1 <= port <= 65_535


def _remote_credential_errors(config: Mapping[str, object], field: str) -> tuple[tuple[str, str], ...]:
    if not str(config.get(field, "") or "").strip():
        return ((field, "must be nonempty when the provider is selected"),)
    return ()


def _remote_profile_errors(provider: str, config: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    if provider == "seasonal_ttsd":
        return _seasonal_profile_errors(config)
    return _openai_profile_errors(config)


def _seasonal_profile_errors(config: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    errors = []
    if str(config.get("voice", "") or "").strip() != "voicetext-paul":
        errors.append(("voice", "must be voicetext-paul for the approved production profile"))
    if str(config.get("profile", "") or "").strip() != "wav-48k-stereo":
        errors.append(("profile", "must be wav-48k-stereo for the approved production profile"))
    return tuple(errors)


def _openai_profile_errors(config: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    errors = [
        (field, "must be nonempty when the provider is selected")
        for field in ("model", "voice")
        if not str(config.get(field, "") or "").strip()
    ]
    if str(config.get("response_format", "wav") or "").strip().lower() not in {
        "wav",
        "mp3",
        "flac",
        "opus",
        "aac",
    }:
        errors.append(("response_format", "is unsupported"))
    speed = config.get("speed", 1.0)
    if not _valid_number(speed) or not 0 < float(cast(int | float, speed)) <= 4:
        errors.append(("speed", "must be finite and in (0, 4]"))
    return tuple(errors)


def _remote_numeric_errors(provider: str, config: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    fields: list[tuple[str, bool, int | float, float | None]] = [
        ("connect_timeout_seconds", False, REMOTE_CONNECT_TIMEOUT_MAX, None),
        ("synthesis_timeout_seconds", False, REMOTE_SYNTHESIS_TIMEOUT_MAX, None),
        ("max_input_bytes", True, REMOTE_INPUT_BYTES_MAX, None),
        ("max_response_bytes", True, REMOTE_RESPONSE_BYTES_MAX, None),
        ("max_error_bytes", True, REMOTE_ERROR_BYTES_MAX, None),
    ]
    if provider == "seasonal_ttsd":
        fields = [
            ("token_ttl_seconds", True, REMOTE_TOKEN_TTL_MAX, None),
            ("refresh_margin_seconds", True, REMOTE_REFRESH_MARGIN_MAX, 0),
            ("token_timeout_seconds", False, REMOTE_TOKEN_TIMEOUT_MAX, None),
            *fields,
        ]
    errors = [
        error
        for field, integer, maximum, minimum in fields
        for error in _remote_number_errors(config, field, integer=integer, maximum=maximum, minimum=minimum)
    ]
    if (
        provider == "seasonal_ttsd"
        and _valid_number(config.get("refresh_margin_seconds", 120), integer=True)
        and _valid_number(config.get("token_ttl_seconds", 900), integer=True)
        and int(cast(int, config.get("refresh_margin_seconds", 120)))
        >= int(cast(int, config.get("token_ttl_seconds", 900)))
    ):
        errors.append(("refresh_margin_seconds", "must be less than token_ttl_seconds"))
    if config.get("verify_tls", True) is not True:
        errors.append(("verify_tls", "must remain true for remote TTS"))
    return tuple(errors)


def _valid_number(value: object, *, integer: bool = False) -> bool:
    if isinstance(value, bool):
        return False
    if integer and not isinstance(value, int):
        return False
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def _remote_number_errors(
    config: Mapping[str, object],
    field: str,
    *,
    integer: bool = False,
    maximum: int | float,
    minimum: float | None = None,
) -> tuple[tuple[str, str], ...]:
    value = config.get(field)
    if not _valid_number(value, integer=integer):
        return ((field, "must be finite and numeric"),)
    number = float(cast(int | float, value))
    if (minimum is None and number <= 0) or (minimum is not None and number < minimum) or number > maximum:
        lower = "greater than 0" if minimum is None else f"at least {minimum:g}"
        return ((field, f"must be {lower} and at most {maximum:g}"),)
    return ()


def current_and_legacy_auth_conflict(
    *,
    auth_present: bool,
    legacy_fields: frozenset[str] | set[str],
) -> bool:
    return auth_present and bool(legacy_fields)


def static_credential_sources_conflict(single_token: str, tokens_json: str) -> bool:
    return bool(single_token and tokens_json)


def exchange_ttls_are_ordered(
    minimum: int,
    default: int,
    maximum_write: int,
    maximum_read: int,
) -> bool:
    return 0 < minimum <= default <= maximum_write <= maximum_read


def job_repository_identity_errors(
    *,
    enabled: bool,
    required: bool,
    path: str,
    operational_database_path: str,
) -> tuple[str, ...]:
    errors: list[str] = []
    if enabled and not path.strip():
        errors.append("jobs.path must be explicitly configured when jobs are enabled")
    if (
        enabled
        and path.strip()
        and lexical_configuration_path(path) == lexical_configuration_path(operational_database_path)
    ):
        errors.append("jobs.path must be separate from database.path")
    if required and not enabled:
        errors.append("jobs.required cannot be true when jobs are disabled")
    return tuple(errors)


def lexical_configuration_path(value: str) -> str:
    """Normalize a configured path lexically without consulting the filesystem.

    Relative paths remain relative to the configured working-directory contract.
    Separators, ``.`` components, and purely lexical ``..`` components are
    normalized. Symlink and real-filesystem equivalence belongs to opt-in
    environmental preflight.
    """

    normalized = posixpath.normpath(value.replace("\\", "/").strip())
    return normalized.rstrip("/") or "."


def job_repository_timing_errors(
    *,
    busy_timeout_ms: int,
    assignment_ack_seconds: int,
    lease_seconds: int,
    shutdown_reconciliation_seconds: float,
) -> tuple[str, ...]:
    errors: list[str] = []
    if not 100 <= busy_timeout_ms <= 30_000:
        errors.append("jobs.busy_timeout_ms must be between 100 and 30000")
    if not 1 <= assignment_ack_seconds < lease_seconds <= 3600:
        errors.append("jobs lease timing must satisfy 1 <= assignment_ack_seconds < lease_seconds <= 3600")
    if not 0.1 <= shutdown_reconciliation_seconds <= 30.0:
        errors.append("jobs.shutdown_reconciliation_seconds must be between 0.1 and 30")
    return tuple(errors)


def lifecycle_timeout_error(
    *,
    total_seconds: float,
    stage_seconds: tuple[float, ...],
) -> str | None:
    if total_seconds <= 0 or any(value <= 0 for value in stage_seconds):
        return "lifecycle timeout values must be positive"
    if total_seconds < max(stage_seconds):
        return "lifecycle.total_seconds must cover every stage timeout"
    return None
