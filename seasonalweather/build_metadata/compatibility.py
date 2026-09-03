"""Runtime admission checks for immutable build and release identity."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ..capabilities.manifest import MANIFEST_SCHEMA_VERSION
from ..configuration.schema import SUPPORTED_CONFIG_SCHEMAS
from ..diagnostics.models import DIAGNOSTIC_CATALOG_VERSION, DIAGNOSTIC_SCHEMA_VERSION
from ..jobs.registry import JOB_TYPE_POLICIES
from ..swwp.constants import PROTOCOL_VERSION
from ..validation.compatibility import (
    CompatibilityDisposition,
    CompatibilityFinding,
    CompatibilityIdentity,
    IntegerRange,
    SupportedCompatibility,
    analyze_compatibility,
)
from ..validation.constants import VALIDATION_PROTOCOL_VERSION
from .build_info import BuildInfo

RUNTIME_SOFTWARE_MINIMUM = "0.17.0"
RUNTIME_SOFTWARE_MAXIMUM_EXCLUSIVE = "0.19.0"
CONTROLLER_BUILD_PROFILES = frozenset({"controller", "source"})
WORKER_BUILD_PROFILES = frozenset(
    {
        "development",
        "dectalk",
        "espeak",
        "festival",
        "legacy-tts",
        "maintenance",
        "piper",
        "routine-worker",
        "source",
        "spfy",
        "voicetext-paul",
    }
)


@dataclass(frozen=True)
class BuildCompatibility:
    """Immutable result of comparing one build record with this runtime."""

    findings: tuple[CompatibilityFinding, ...]

    @property
    def compatible(self) -> bool:
        return all(finding.disposition.compatible for finding in self.findings)

    @property
    def incompatible_findings(self) -> tuple[CompatibilityFinding, ...]:
        return tuple(finding for finding in self.findings if not finding.disposition.compatible)

    def to_dict(self) -> dict[str, object]:
        return {
            "compatible": self.compatible,
            "findings": [finding.to_dict() for finding in self.findings],
        }


class BuildCompatibilityError(ValueError):
    """Raised when a build cannot safely enter the selected runtime role."""

    def __init__(self, result: BuildCompatibility) -> None:
        self.result = result
        fields = ", ".join(finding.field for finding in result.incompatible_findings)
        super().__init__(f"build is incompatible with runtime ({fields})")


def _supported() -> SupportedCompatibility:
    payload_versions = frozenset(policy.payload_schema_version for policy in JOB_TYPE_POLICIES.values())
    result_versions = frozenset(policy.result_schema_version for policy in JOB_TYPE_POLICIES.values())
    return SupportedCompatibility(
        software_minimum=RUNTIME_SOFTWARE_MINIMUM,
        software_maximum_exclusive=RUNTIME_SOFTWARE_MAXIMUM_EXCLUSIVE,
        validation_protocol=IntegerRange(1, VALIDATION_PROTOCOL_VERSION),
        config_schema=IntegerRange(min(SUPPORTED_CONFIG_SCHEMAS), max(SUPPORTED_CONFIG_SCHEMAS)),
        swwp_protocol=IntegerRange(1, PROTOCOL_VERSION),
        job_payload_schemas=payload_versions,
        job_result_schemas=result_versions,
        diagnostic_schema=IntegerRange(1, DIAGNOSTIC_SCHEMA_VERSION),
        diagnostic_catalog=IntegerRange(1, DIAGNOSTIC_CATALOG_VERSION),
        capability_manifest=IntegerRange(1, MANIFEST_SCHEMA_VERSION),
        report_schema=IntegerRange(1, 1),
    )


def _set_finding(field: str, supplied: Iterable[int], supported: frozenset[int]) -> CompatibilityFinding:
    values = tuple(supplied)
    if not values:
        disposition = CompatibilityDisposition.MISSING
    elif any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in values):
        disposition = CompatibilityDisposition.MALFORMED
    elif tuple(sorted(set(values))) != values:
        disposition = CompatibilityDisposition.CONTRADICTORY
    elif set(values).intersection(supported):
        disposition = CompatibilityDisposition.COMPATIBLE
    elif max(values) < min(supported):
        disposition = CompatibilityDisposition.UNSUPPORTED_OLDER
    else:
        disposition = CompatibilityDisposition.UNSUPPORTED_NEWER
    return CompatibilityFinding(field, list(values), disposition, sorted(supported))


def _range_finding(field: str, minimum: int, maximum: int, supported: IntegerRange) -> CompatibilityFinding:
    supplied = {"minimum": minimum, "maximum": maximum}
    if maximum < supported.minimum:
        disposition = CompatibilityDisposition.UNSUPPORTED_OLDER
    elif minimum > supported.maximum:
        disposition = CompatibilityDisposition.UNSUPPORTED_NEWER
    else:
        disposition = CompatibilityDisposition.COMPATIBLE
    return CompatibilityFinding(field, supplied, disposition, supported.to_dict())


def _scalar_finding(field: str, value: int, supported: IntegerRange) -> CompatibilityFinding:
    return CompatibilityFinding(field, value, supported.classify(value), supported.to_dict())


def _software_finding(info: BuildInfo, supported: SupportedCompatibility) -> CompatibilityFinding:
    identity = CompatibilityIdentity(
        software_version=info.software_version,
        build_identity=info.build_identity,
        validation_protocol_version=VALIDATION_PROTOCOL_VERSION,
        config_schema_version=max(SUPPORTED_CONFIG_SCHEMAS),
        swwp_protocol_version=PROTOCOL_VERSION,
        job_payload_schema_versions=tuple(
            sorted(policy.payload_schema_version for policy in JOB_TYPE_POLICIES.values())
        ),
        job_result_schema_versions=tuple(sorted(policy.result_schema_version for policy in JOB_TYPE_POLICIES.values())),
        diagnostic_schema_version=DIAGNOSTIC_SCHEMA_VERSION,
        diagnostic_catalog_version=DIAGNOSTIC_CATALOG_VERSION,
        capability_manifest_version=MANIFEST_SCHEMA_VERSION,
        report_schema_version=1,
    )
    return analyze_compatibility(identity, supported)[0]


def _schema_findings(info: BuildInfo) -> tuple[CompatibilityFinding, ...]:
    payload_schemas = frozenset(policy.payload_schema_version for policy in JOB_TYPE_POLICIES.values())
    result_schemas = frozenset(policy.result_schema_version for policy in JOB_TYPE_POLICIES.values())
    return (
        _set_finding("swwp_protocol_versions", info.swwp_protocol_versions, frozenset(range(1, PROTOCOL_VERSION + 1))),
        _set_finding(
            "validation_protocol_versions",
            info.validation_protocol_versions,
            frozenset(range(1, VALIDATION_PROTOCOL_VERSION + 1)),
        ),
        _set_finding("job_payload_schema_versions", info.job_payload_schema_versions, payload_schemas),
        _set_finding("job_result_schema_versions", info.job_result_schema_versions, result_schemas),
        _range_finding(
            "configuration_schema",
            info.configuration_schema[0],
            info.configuration_schema[1],
            IntegerRange(min(SUPPORTED_CONFIG_SCHEMAS), max(SUPPORTED_CONFIG_SCHEMAS)),
        ),
        _scalar_finding(
            "diagnostic_schema_version",
            info.diagnostic_schema_version,
            IntegerRange(1, DIAGNOSTIC_SCHEMA_VERSION),
        ),
        _scalar_finding(
            "diagnostic_catalog_version",
            info.diagnostic_catalog_version,
            IntegerRange(1, DIAGNOSTIC_CATALOG_VERSION),
        ),
        _scalar_finding(
            "capability_manifest_version",
            info.capability_manifest_version,
            IntegerRange(1, MANIFEST_SCHEMA_VERSION),
        ),
    )


def _identity_findings(
    info: BuildInfo,
    *,
    role: str,
    expected_profile: str | None,
) -> tuple[CompatibilityFinding, ...]:
    findings: list[CompatibilityFinding] = []
    if info.project != "seasonalweather":
        findings.append(
            CompatibilityFinding(
                "project",
                info.project,
                CompatibilityDisposition.CONTRADICTORY,
                "seasonalweather",
            )
        )
    allowed_profiles = CONTROLLER_BUILD_PROFILES if role == "controller" else WORKER_BUILD_PROFILES
    if info.image_profile not in allowed_profiles:
        findings.append(
            CompatibilityFinding(
                "image_profile",
                info.image_profile,
                CompatibilityDisposition.CONTRADICTORY,
                sorted(allowed_profiles),
            )
        )
    if expected_profile is not None and info.image_profile not in {"source", expected_profile}:
        findings.append(
            CompatibilityFinding(
                "image_profile.expected",
                info.image_profile,
                CompatibilityDisposition.CONTRADICTORY,
                {"expected": expected_profile, "development_fallback": "source"},
            )
        )
    return tuple(findings)


def check_runtime_compatibility(
    info: BuildInfo,
    *,
    role: str,
    expected_profile: str | None = None,
) -> BuildCompatibility:
    """Compare immutable build metadata with the current runtime contract."""

    if role not in {"controller", "worker"}:
        raise ValueError(f"unsupported runtime role: {role}")
    supported = _supported()
    findings = [_software_finding(info, supported)]
    findings.extend(_schema_findings(info))
    findings.extend(_identity_findings(info, role=role, expected_profile=expected_profile))
    return BuildCompatibility(tuple(findings))


def ensure_runtime_compatibility(
    info: BuildInfo,
    *,
    role: str,
    expected_profile: str | None = None,
) -> None:
    result = check_runtime_compatibility(info, role=role, expected_profile=expected_profile)
    if not result.compatible:
        raise BuildCompatibilityError(result)


__all__ = [
    "BuildCompatibility",
    "BuildCompatibilityError",
    "CONTROLLER_BUILD_PROFILES",
    "RUNTIME_SOFTWARE_MAXIMUM_EXCLUSIVE",
    "RUNTIME_SOFTWARE_MINIMUM",
    "WORKER_BUILD_PROFILES",
    "check_runtime_compatibility",
    "ensure_runtime_compatibility",
]
