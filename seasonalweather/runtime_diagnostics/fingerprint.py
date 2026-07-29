"""Deterministic bounded operator-relevant occurrence fingerprinting."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .models import FINGERPRINT_VERSION, RuntimeDiagnostic


@dataclass(frozen=True)
class Fingerprint:
    version: int
    digest: str
    canonical_key: str


def fingerprint(instance: RuntimeDiagnostic) -> Fingerprint:
    evidence = instance.exception_evidence or {}
    frames = evidence.get("frames") if isinstance(evidence, Mapping) else None
    top_frame = (
        frames[-1]
        if isinstance(frames, Sequence) and not isinstance(frames, str | bytes | bytearray) and frames
        else None
    )
    material = {
        "version": FINGERPRINT_VERSION,
        "code": instance.code,
        "context": dict(instance.context.fingerprint_fields()),
        "exception_type": evidence.get("type") if isinstance(evidence, Mapping) else None,
        "top_frame": (
            {
                "filename": top_frame.get("filename"),
                "function": top_frame.get("function"),
                "line": top_frame.get("line"),
            }
            if isinstance(top_frame, Mapping)
            else None
        ),
    }
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return Fingerprint(
        version=FINGERPRINT_VERSION,
        digest=hashlib.sha256(canonical.encode()).hexdigest(),
        canonical_key=canonical,
    )
