"""Deterministic, secret-safe complete candidate identity framing."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence


def source_manifest_sha256(
    source_manifest: Sequence[Mapping[str, object]],
) -> str | None:
    """Hash one canonical manifest of stable names, lengths, and exact digests."""

    if not source_manifest or any(
        item.get("sha256") is None or item.get("byte_length") is None for item in source_manifest
    ):
        return None
    framed = {
        "framing": "seasonalweather.configuration-source-manifest.canonical-json.v1",
        "sources": list(source_manifest),
    }
    return _canonical_sha256(framed)


def complete_candidate_sha256(
    *,
    source_manifest: Sequence[Mapping[str, object]],
    config_schema_version: int | None,
    origin_manifest: Sequence[Mapping[str, object]],
    environment_inputs: Sequence[Mapping[str, object]],
) -> str:
    """Hash every admitted candidate-input identity with explicit framing."""

    framed = {
        "framing": "seasonalweather.configuration-candidate.canonical-json.v1",
        "source_manifest": list(source_manifest),
        "config_schema_version": config_schema_version,
        "origin_manifest": list(origin_manifest),
        "environment_inputs": list(environment_inputs),
    }
    return _canonical_sha256(framed)


def canonical_report_sha256(report: Mapping[str, object]) -> str:
    """Return the out-of-band binding for one canonical complete report."""

    return _canonical_sha256(report)


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
