from __future__ import annotations

import asyncio
import datetime as dt
import itertools
from pathlib import Path
from typing import Any

import pytest

from seasonalweather.capabilities.models import (
    CapabilityRecord,
    CompatibilityState,
    OperationalState,
    ParameterValue,
)
from seasonalweather.capabilities.qualification import WorkerQualificationView
from seasonalweather.configuration import compile_path
from seasonalweather.jobs.policies import JobType
from seasonalweather.validation import (
    CapabilityAnalysis,
    CapabilityNeed,
    ValidationContext,
    analyze_capabilities,
    validate_compiled,
)
from seasonalweather.validation.capability import CapabilityDisposition
from seasonalweather.validation.pipeline import (
    _compatibility_issues,
    current_compatibility_identity,
    default_supported_compatibility,
)

NOW = dt.datetime(2026, 7, 29, 16, tzinfo=dt.UTC)
EXAMPLE = Path(__file__).resolve().parents[1] / "config/config.yaml"


def _record(
    name: str,
    *,
    state: OperationalState = OperationalState.HEALTHY,
    accepting: bool = True,
    compatible: CompatibilityState = CompatibilityState.COMPATIBLE,
    available: int = 1,
    parameters: dict[str, ParameterValue] | None = None,
) -> CapabilityRecord:
    return CapabilityRecord(
        name=name,
        implemented=True,
        compatibility=compatible,
        operational_state=state,
        accepting_new_jobs=accepting,
        total_capacity=1,
        reported_available=available,
        parameters=parameters or {"format": "wav"},
        validity_seconds=60,
        observed_at=NOW,
        published_at=NOW,
    )


def _view(records: tuple[CapabilityRecord, ...], **changes) -> WorkerQualificationView:
    values = {
        "worker_id": "worker_1",
        "worker_instance_id": "instance_1",
        "session_id": "session_1",
        "epoch": 3,
        "digest": "sha256:" + "a" * 64,
        "records": records,
        "authorized_capabilities": frozenset(item.name for item in records),
        "authorized_job_types": frozenset({JobType.TTS_SYNTHESIZE}),
        "payload_versions": {JobType.TTS_SYNTHESIZE: 1},
        "result_versions": {JobType.TTS_SYNTHESIZE: 1},
        "effective_capacity": {item.name: item.reported_available for item in records},
    }
    values.update(changes)
    return WorkerQualificationView(**values)


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"trusted": False}, "stale_or_unknown"),
        ({"connected": False}, "stale_or_unknown"),
        ({"probe_required": True}, "stale_or_unknown"),
    ],
)
def test_stale_unknown_qualification_is_distinct_and_does_not_mutate(changes, expected: str) -> None:
    view = _view((_record("tts.synthesis.v1"),), **changes)
    before = repr(view)

    result = analyze_capabilities((view,), (CapabilityNeed("tts.synthesis.v1"),))

    assert result[0].disposition.value == expected
    assert result[0].blocking
    assert repr(view) == before


def test_optional_requirement_and_viable_fallback_have_explicit_policy() -> None:
    primary = _record(
        "tts.primary.v1",
        state=OperationalState.UNAVAILABLE,
        accepting=False,
        available=0,
    )
    fallback = _record("tts.fallback.v1")
    view = _view((primary, fallback))

    fallback_result = analyze_capabilities(
        (view,),
        (CapabilityNeed("tts.primary.v1", fallback="tts.fallback.v1"),),
    )[0]
    optional_result = analyze_capabilities(
        (),
        (CapabilityNeed("tts.optional.v1", required=False),),
    )[0]

    assert fallback_result.disposition.value == "fallback"
    assert fallback_result.matched_capability == "tts.fallback.v1"
    assert not fallback_result.blocking
    assert optional_result.disposition.value == "not_implemented"
    assert not optional_result.blocking


def test_parameter_and_capacity_requirements_fail_without_reservation() -> None:
    view = _view((_record("tts.synthesis.v1"),), effective_capacity={"tts.synthesis.v1": 0})
    capacity = analyze_capabilities((view,), (CapabilityNeed("tts.synthesis.v1"),))[0]
    mismatch = analyze_capabilities(
        (_view((_record("tts.synthesis.v1"),)),),
        (CapabilityNeed("tts.synthesis.v1", parameters=(("format", "ogg"),)),),
    )[0]

    assert capacity.disposition.value == "no_capacity"
    assert mismatch.disposition.value == "parameter_mismatch"


def test_accepting_degraded_capability_remains_usable_but_not_satisfied() -> None:
    view = _view(
        (
            _record(
                "tts.synthesis.v1",
                state=OperationalState.DEGRADED,
                accepting=True,
                available=1,
            ),
        )
    )
    analysis = analyze_capabilities(
        (view,),
        (CapabilityNeed("tts.synthesis.v1"),),
    )[0]

    assert analysis.disposition.value == "degraded"
    assert analysis.usable
    assert not analysis.blocking
    assert analysis.matched_capability == "tts.synthesis.v1"
    issue = _compatibility_issues(
        current_compatibility_identity(1),
        default_supported_compatibility(),
        (analysis,),
    )[0]
    assert issue.code == "SWCFG4002"
    assert not issue.blocking


@pytest.mark.parametrize(
    ("record", "need", "expected"),
    [
        (
            _record(
                "tts.synthesis.v1",
                state=OperationalState.DEGRADED,
                accepting=False,
            ),
            CapabilityNeed("tts.synthesis.v1"),
            "no_capacity",
        ),
        (
            _record(
                "tts.synthesis.v1",
                state=OperationalState.DEGRADED,
                parameters={"format": "ogg"},
            ),
            CapabilityNeed("tts.synthesis.v1", parameters=(("format", "wav"),)),
            "parameter_mismatch",
        ),
    ],
)
def test_degraded_capability_still_requires_admission_qualification(
    record: CapabilityRecord,
    need: CapabilityNeed,
    expected: str,
) -> None:
    analysis = analyze_capabilities((_view((record,)),), (need,))[0]

    assert analysis.disposition.value == expected
    assert not analysis.usable
    assert analysis.blocking


def test_degraded_capability_with_zero_effective_capacity_is_not_usable() -> None:
    name = "tts.synthesis.v1"
    record = _record(name, state=OperationalState.DEGRADED)
    analysis = analyze_capabilities(
        (_view((record,), effective_capacity={name: 0}),),
        (CapabilityNeed(name),),
    )[0]

    assert analysis.disposition.value == "no_capacity"
    assert not analysis.usable
    assert analysis.blocking


def test_usable_views_rank_satisfied_then_degraded_above_unusable_views() -> None:
    name = "tts.synthesis.v1"
    no_capacity = _view((_record(name),), effective_capacity={name: 0})
    degraded = _view(
        (_record(name, state=OperationalState.DEGRADED),),
        epoch=4,
        digest="sha256:" + "b" * 64,
    )
    satisfied = _view(
        (_record(name),),
        epoch=5,
        digest="sha256:" + "c" * 64,
    )

    degraded_selected = analyze_capabilities(
        (no_capacity, degraded),
        (CapabilityNeed(name),),
    )[0]
    satisfied_selected = analyze_capabilities(
        (degraded, satisfied, no_capacity),
        (CapabilityNeed(name),),
    )[0]

    assert degraded_selected.disposition.value == "degraded"
    assert degraded_selected.evidence == ("epoch=4", "digest=sha256:" + "b" * 64)
    assert satisfied_selected.disposition.value == "satisfied"
    assert satisfied_selected.evidence == ("epoch=5", "digest=sha256:" + "c" * 64)


def test_capability_view_permutations_produce_identical_analysis_evidence_and_report() -> None:
    name = "tts.synthesis.v1"
    views = (
        _view(
            (_record(name),),
            worker_id="worker_a",
            worker_instance_id="instance_a",
            session_id="session_a",
            epoch=3,
            digest="sha256:" + "a" * 64,
        ),
        _view(
            (_record(name),),
            worker_id="worker_b",
            worker_instance_id="instance_b",
            session_id="session_b",
            epoch=5,
            digest="sha256:" + "b" * 64,
        ),
        _view(
            (_record(name, state=OperationalState.DEGRADED),),
            worker_id="worker_c",
            worker_instance_id="instance_c",
            session_id="session_c",
            epoch=8,
            digest="sha256:" + "c" * 64,
        ),
    )
    need = CapabilityNeed(name)
    compiled = compile_path(EXAMPLE, environ={})
    analyses = set()
    reports = set()

    for ordering in itertools.permutations(views):
        analysis = analyze_capabilities(ordering, (need,))
        analyses.add(analysis)
        report = asyncio.run(
            validate_compiled(
                compiled,
                context=ValidationContext(
                    clock=lambda: NOW,
                    capability_views=ordering,
                    capability_needs=(need,),
                ),
            )
        )
        reports.add(report.to_json())

    assert len(analyses) == 1
    assert len(reports) == 1
    selected = next(iter(analyses))[0]
    assert selected.evidence == ("epoch=5", "digest=sha256:" + "b" * 64)


def test_degraded_fallback_is_usable_but_remains_explicitly_degraded() -> None:
    primary = _record(
        "tts.primary.v1",
        state=OperationalState.UNAVAILABLE,
        accepting=False,
        available=0,
    )
    fallback = _record("tts.fallback.v1", state=OperationalState.DEGRADED)
    analysis = analyze_capabilities(
        (_view((primary, fallback)),),
        (CapabilityNeed("tts.primary.v1", fallback="tts.fallback.v1"),),
    )[0]

    assert analysis.disposition.value == "degraded_fallback"
    assert analysis.usable
    assert not analysis.blocking
    assert analysis.matched_capability == "tts.fallback.v1"
    assert analysis.evidence


def test_validation_context_defensively_freezes_capability_snapshot_maps() -> None:
    view = _view((_record("tts.synthesis.v1"),))
    context = ValidationContext(capability_views=(view,))
    view.effective_capacity["tts.synthesis.v1"] = 0

    assert context.capability_views[0].effective_capacity["tts.synthesis.v1"] == 1
    with pytest.raises(TypeError):
        context.capability_views[0].effective_capacity["tts.synthesis.v1"] = 0


def test_capability_need_and_analysis_copy_public_collections() -> None:
    parameter: Any = ["format", "wav"]
    parameters = [parameter]
    need = CapabilityNeed("tts.synthesis.v1", parameters=parameters)
    evidence = ["epoch=1"]
    analysis = CapabilityAnalysis(
        need,
        CapabilityDisposition.SATISFIED,
        False,
        "tts.synthesis.v1",
        evidence,
    )
    parameters.append(("voice", "default"))
    parameter[1] = "mutated"
    evidence.append("mutated")

    assert need.parameters == (("format", "wav"),)
    assert analysis.evidence == ("epoch=1",)
