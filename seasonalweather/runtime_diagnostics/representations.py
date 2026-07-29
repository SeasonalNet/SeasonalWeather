"""Pure bounded occurrence representations reserved for later API services."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from seasonalweather.diagnostics import load_catalog

from .models import OccurrenceRecord, OccurrenceTransition, thaw_json, timestamp


def occurrence_summary(record: OccurrenceRecord) -> dict[str, Any]:
    definition = load_catalog().definition(record.code)
    context = record.latest_instance.get("context")
    if not isinstance(context, Mapping):
        raise ValueError("occurrence instance context is invalid")
    return {
        "occurrence_id": record.occurrence_id,
        "code": record.code,
        "title": definition.title if definition is not None else None,
        "catalog_resolved": definition is not None,
        "state": record.state.value,
        "component": context.get("component"),
        "severity": record.latest_instance.get("severity"),
        "first_seen": timestamp(record.first_seen),
        "last_seen": timestamp(record.last_seen),
        "count": record.count,
        "resolved_at": timestamp(record.resolved_at) if record.resolved_at is not None else None,
        "duration_seconds": record.duration_seconds,
        "diagnostic_schema_version": record.diagnostic_schema_version,
        "catalog_version": record.catalog_version,
        "occurrence_schema_version": record.occurrence_schema_version,
        "fingerprint_version": record.fingerprint_version,
    }


def occurrence_detail(
    record: OccurrenceRecord,
    *,
    transitions: tuple[OccurrenceTransition, ...] = (),
) -> dict[str, Any]:
    return occurrence_summary(record) | {
        "initial_instance": thaw_json(record.initial_instance),
        "latest_instance": thaw_json(record.latest_instance),
        "resolution_reason": record.resolution_reason,
        "resolution_evidence": (
            thaw_json(record.resolution_evidence) if record.resolution_evidence is not None else None
        ),
        "prior_occurrence_id": record.prior_occurrence_id,
        "fingerprint": record.fingerprint,
        "fingerprint_version": record.fingerprint_version,
        "transitions": [
            {
                "transition_type": transition.transition_type,
                "observed_at": timestamp(transition.observed_at),
                "evidence": thaw_json(transition.evidence),
            }
            for transition in transitions
        ],
    }
