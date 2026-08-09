"""Deterministic secret-safe active-versus-candidate diffing."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast
from urllib.parse import urlsplit, urlunsplit

from seasonalweather.configuration.compiler import CompiledConfiguration
from seasonalweather.configuration.origins import ENVIRONMENT_BINDINGS, OriginKind
from seasonalweather.configuration.paths import ConfigPath
from seasonalweather.configuration.redaction import is_secret_path

from .models import ChangeKind, DiffEntry, ReloadDiff
from .policy import classify_path

_MISSING = object()
_REDACTED = "<redacted:presence-changed>"
_MAX_DISPLAY = 160


def build_reload_diff(
    active: CompiledConfiguration,
    candidate: CompiledConfiguration,
    *,
    active_generation: int,
    active_identity_sha256: str,
    candidate_identity_sha256: str,
    report_sha256: str,
    warning_paths: frozenset[str] = frozenset(),
    active_environment_inputs: Sequence[Mapping[str, object]] = (),
    candidate_environment_inputs: Sequence[Mapping[str, object]] = (),
) -> ReloadDiff:
    if active.value is None or candidate.value is None:
        raise ValueError("reload diff requires two valid typed configurations")
    raw: list[tuple[ConfigPath, object, object]] = []
    _walk(ConfigPath(), active.value, candidate.value, raw)
    _environment_changes(
        active,
        candidate,
        raw,
        active_environment_inputs=active_environment_inputs,
        candidate_environment_inputs=candidate_environment_inputs,
    )
    entries = tuple(
        sorted(
            (_entry(active, candidate, path, old, new, warning_paths) for path, old, new in raw),
            key=lambda item: item.path,
        )
    )
    return ReloadDiff(
        active_generation=active_generation,
        active_identity_sha256=active_identity_sha256,
        candidate_identity_sha256=candidate_identity_sha256,
        report_sha256=report_sha256,
        entries=entries,
        source_only_change=not entries and active_identity_sha256 != candidate_identity_sha256,
    )


def _walk(path: ConfigPath, old: object, new: object, output: list[tuple[ConfigPath, object, object]]) -> None:
    if isinstance(old, Mapping) and isinstance(new, Mapping):
        _walk_mappings(path, old, new, output)
        return
    if _sequence(old) and _sequence(new):
        _walk_sequences(path, cast(Sequence[object], old), cast(Sequence[object], new), output)
        return
    if old is _MISSING:
        _walk_one_sided(path, new, old_missing=True, output=output)
        return
    if new is _MISSING:
        _walk_one_sided(path, old, old_missing=False, output=output)
        return
    if old != new:
        output.append((path, old, new))


def _walk_mappings(
    path: ConfigPath,
    old: Mapping[object, object],
    new: Mapping[object, object],
    output: list[tuple[ConfigPath, object, object]],
) -> None:
    for key in sorted(set(old) | set(new), key=str):
        _walk(path.field(str(key)), old.get(key, _MISSING), new.get(key, _MISSING), output)


def _walk_sequences(
    path: ConfigPath,
    old: Sequence[object],
    new: Sequence[object],
    output: list[tuple[ConfigPath, object, object]],
) -> None:
    for index in range(max(len(old), len(new))):
        _walk(
            path.index(index),
            old[index] if index < len(old) else _MISSING,
            new[index] if index < len(new) else _MISSING,
            output,
        )


def _walk_one_sided(
    path: ConfigPath,
    value: object,
    *,
    old_missing: bool,
    output: list[tuple[ConfigPath, object, object]],
) -> None:
    if isinstance(value, Mapping):
        items = ((path.field(str(key)), item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0])))
    elif _sequence(value):
        items = ((path.index(index), item) for index, item in enumerate(cast(Sequence[object], value)))
    else:
        output.append((path, _MISSING, value) if old_missing else (path, value, _MISSING))
        return
    for child_path, item in items:
        _walk(child_path, _MISSING, item, output) if old_missing else _walk(child_path, item, _MISSING, output)


def _environment_changes(
    active: CompiledConfiguration,
    candidate: CompiledConfiguration,
    output: list[tuple[ConfigPath, object, object]],
    *,
    active_environment_inputs: Sequence[Mapping[str, object]],
    candidate_environment_inputs: Sequence[Mapping[str, object]],
) -> None:
    active_origins = {origin.path: origin for origin in active.report.origins}
    candidate_origins = {origin.path: origin for origin in candidate.report.origins}
    active_inputs = _environment_identity_map(active_environment_inputs)
    candidate_inputs = _environment_identity_map(candidate_environment_inputs)
    existing_paths = {path for path, _old, _new in output}
    for path, variable, _default in ENVIRONMENT_BINDINGS:
        old = active_origins.get(path)
        new = candidate_origins.get(path)
        old_present = bool(old and old.kind is OriginKind.ENVIRONMENT)
        new_present = bool(new and new.kind is OriginKind.ENVIRONMENT)
        old_identity = active_inputs.get(variable, (old_present, None))
        new_identity = candidate_inputs.get(variable, (new_present, None))
        if path not in existing_paths and old_identity != new_identity:
            output.append((path, old_identity, new_identity))


def _environment_identity_map(
    values: Sequence[Mapping[str, object]],
) -> dict[str, tuple[bool, str | None]]:
    return {
        str(item.get("variable")): (
            bool(item.get("present")),
            str(item["opaque_change_identity"]) if item.get("opaque_change_identity") is not None else None,
        )
        for item in values
    }


def _entry(
    active: CompiledConfiguration,
    candidate: CompiledConfiguration,
    path: ConfigPath,
    old: object,
    new: object,
    warning_paths: frozenset[str],
) -> DiffEntry:
    rule = classify_path(path)
    secret = is_secret_path(path) or path.segments[:1] == ("secrets",)
    if old is _MISSING:
        kind = ChangeKind.ADD
    elif new is _MISSING:
        kind = ChangeKind.REMOVE
    else:
        kind = ChangeKind.REPLACE
    old_origin = active.origins.get(path)
    new_origin = candidate.origins.get(path)
    located = candidate.parsed.locations.get(path) if candidate.parsed else None
    location = located.value.to_dict() if located is not None else None
    return DiffEntry(
        path=path,
        classification=rule.disposition,
        policy_id=rule.identity,
        kind=kind,
        secret=secret,
        old=_REDACTED if secret else _safe_value(old),
        new=_REDACTED if secret else _safe_value(new),
        old_origin=old_origin.kind.value if old_origin else None,
        new_origin=new_origin.kind.value if new_origin else None,
        source_location=location,
        acknowledgment_required=path.to_pointer() in warning_paths,
    )


def _safe_value(value: object) -> object:
    if value is _MISSING:
        return "<absent>"
    if value is None or isinstance(value, bool | int | float):
        return value
    text = str(value)
    if "://" in text:
        text = _sanitize_url(text)
    return text if len(text) <= _MAX_DISPLAY else f"{text[: _MAX_DISPLAY - 12]}…<{len(text)}>"


def _sanitize_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        return "<redacted:endpoint>"
    if not parsed.scheme or not host:
        return "<redacted:endpoint>"
    return urlunsplit((parsed.scheme, f"{host}{port}", parsed.path, "", ""))


def _sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray)
