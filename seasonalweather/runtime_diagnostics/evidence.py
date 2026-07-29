"""Bounded exception evidence that preserves Python chain and group topology."""

from __future__ import annotations

import json
import traceback
from typing import Any

from .redaction import redact_text

MAX_CHAIN_DEPTH = 8
MAX_GROUP_DEPTH = 6
MAX_GROUP_MEMBERS = 16
MAX_FRAMES = 128
MAX_NOTES = 16
MAX_TOTAL_BYTES = 32_768


def capture_exception(exc: BaseException) -> dict[str, Any]:
    seen: set[int] = set()
    try:
        evidence = _capture(exc, seen=seen, chain_depth=0, group_depth=0)
    except Exception:
        evidence = {
            "type": _type_name(exc),
            "message": "[exception evidence unavailable]",
            "truncated": {"capture_failure": True},
        }
    encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    if len(encoded) <= MAX_TOTAL_BYTES:
        return evidence
    return {
        "type": evidence["type"],
        "message": redact_text(evidence.get("message", ""), limit=256),
        "truncated": {"total_bytes": True, "original_bytes": len(encoded)},
    }


def _capture(
    exc: BaseException,
    *,
    seen: set[int],
    chain_depth: int,
    group_depth: int,
) -> dict[str, Any]:
    if id(exc) in seen:
        return {"type": _type_name(exc), "message": "[cycle]", "truncated": {"cycle": True}}
    seen.add(id(exc))
    result: dict[str, Any] = {
        "type": _type_name(exc),
        "message": _message(exc),
        "frames": _frames(exc),
    }
    if len(result["frames"]) == MAX_FRAMES:
        result["truncated"] = {"frames": True}
    _add_notes(exc, result)
    _add_group(exc, result, seen=seen, chain_depth=chain_depth, group_depth=group_depth)
    _add_chain(exc, result, seen=seen, chain_depth=chain_depth, group_depth=group_depth)
    return result


def _frames(exc: BaseException) -> list[dict[str, Any]]:
    frames = traceback.extract_tb(exc.__traceback__, limit=MAX_FRAMES)
    return [
        {
            "filename": redact_text(frame.filename, limit=512),
            "line": frame.lineno,
            "function": redact_text(frame.name, limit=256),
            "source": redact_text(frame.line or "", limit=512),
        }
        for frame in frames
    ]


def _add_notes(exc: BaseException, result: dict[str, Any]) -> None:
    notes = getattr(exc, "__notes__", ())
    if notes:
        result["notes"] = [redact_text(note, limit=512) for note in tuple(notes)[:MAX_NOTES]]
        if len(notes) > MAX_NOTES:
            result.setdefault("truncated", {})["notes"] = True


def _add_group(
    exc: BaseException,
    result: dict[str, Any],
    *,
    seen: set[int],
    chain_depth: int,
    group_depth: int,
) -> None:
    if isinstance(exc, BaseExceptionGroup):
        if group_depth >= MAX_GROUP_DEPTH:
            result.setdefault("truncated", {})["group_depth"] = True
        else:
            members = exc.exceptions[:MAX_GROUP_MEMBERS]
            result["members"] = [
                _capture(
                    member,
                    seen=seen,
                    chain_depth=chain_depth,
                    group_depth=group_depth + 1,
                )
                for member in members
            ]
            if len(exc.exceptions) > MAX_GROUP_MEMBERS:
                result.setdefault("truncated", {})["group_members"] = len(exc.exceptions) - len(members)


def _add_chain(
    exc: BaseException,
    result: dict[str, Any],
    *,
    seen: set[int],
    chain_depth: int,
    group_depth: int,
) -> None:
    if chain_depth >= MAX_CHAIN_DEPTH:
        if exc.__cause__ is not None or (exc.__context__ is not None and not exc.__suppress_context__):
            result.setdefault("truncated", {})["chain_depth"] = True
        return
    if exc.__cause__ is not None:
        result["cause"] = _capture(
            exc.__cause__,
            seen=seen,
            chain_depth=chain_depth + 1,
            group_depth=group_depth,
        )
    elif exc.__context__ is not None and not exc.__suppress_context__:
        result["context"] = _capture(
            exc.__context__,
            seen=seen,
            chain_depth=chain_depth + 1,
            group_depth=group_depth,
        )


def _type_name(exc: BaseException) -> str:
    return f"{type(exc).__module__}.{type(exc).__qualname__}"[:256]


def _message(exc: BaseException) -> str:
    try:
        return redact_text(str(exc), limit=1024)
    except Exception:
        return "[exception message unavailable]"
