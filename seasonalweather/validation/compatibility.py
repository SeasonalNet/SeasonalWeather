"""Pure typed software, schema, and protocol compatibility analysis."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import total_ordering
from types import MappingProxyType
from typing import Any

_PEP440_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:(?P<pre>a|b|rc)(?P<pre_number>0|[1-9]\d*))?"
    r"(?:\.dev(?P<development>0|[1-9]\d*))?"
    r"(?:\+(?P<local>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$",
    re.IGNORECASE,
)


class CompatibilityDisposition(StrEnum):
    COMPATIBLE = "compatible"
    ADVISORY = "compatible_with_advisory"
    UNSUPPORTED_OLDER = "unsupported_older"
    UNSUPPORTED_NEWER = "unsupported_newer"
    MALFORMED = "malformed_or_unknown"
    MISSING = "missing_required_stamp"
    CONTRADICTORY = "internally_contradictory"

    @property
    def compatible(self) -> bool:
        return self in {CompatibilityDisposition.COMPATIBLE, CompatibilityDisposition.ADVISORY}


@dataclass(frozen=True, order=True)
class IntegerRange:
    minimum: int
    maximum: int

    def __post_init__(self) -> None:
        if self.minimum < 1 or self.maximum < self.minimum:
            raise ValueError("compatibility range must be positive and ordered")

    def classify(self, value: int | None) -> CompatibilityDisposition:
        if value is None:
            return CompatibilityDisposition.MISSING
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            return CompatibilityDisposition.MALFORMED
        if value < self.minimum:
            return CompatibilityDisposition.UNSUPPORTED_OLDER
        if value > self.maximum:
            return CompatibilityDisposition.UNSUPPORTED_NEWER
        return CompatibilityDisposition.COMPATIBLE

    def to_dict(self) -> dict[str, int]:
        return {"minimum": self.minimum, "maximum": self.maximum}


@dataclass(frozen=True)
class CompatibilityIdentity:
    software_version: str | None
    build_identity: str | None
    validation_protocol_version: int | None
    config_schema_version: int | None
    swwp_protocol_version: int | None
    job_payload_schema_versions: tuple[int, ...]
    job_result_schema_versions: tuple[int, ...]
    diagnostic_schema_version: int | None
    diagnostic_catalog_version: int | None
    capability_manifest_version: int | None
    report_schema_version: int | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "job_payload_schema_versions", tuple(self.job_payload_schema_versions))
        object.__setattr__(self, "job_result_schema_versions", tuple(self.job_result_schema_versions))


@dataclass(frozen=True)
class SupportedCompatibility:
    software_minimum: str
    software_maximum_exclusive: str
    validation_protocol: IntegerRange
    config_schema: IntegerRange
    swwp_protocol: IntegerRange
    job_payload_schemas: frozenset[int]
    job_result_schemas: frozenset[int]
    diagnostic_schema: IntegerRange
    diagnostic_catalog: IntegerRange
    capability_manifest: IntegerRange
    report_schema: IntegerRange

    def __post_init__(self) -> None:
        object.__setattr__(self, "job_payload_schemas", frozenset(self.job_payload_schemas))
        object.__setattr__(self, "job_result_schemas", frozenset(self.job_result_schemas))
        minimum = _pep440(self.software_minimum)
        maximum = _pep440(self.software_maximum_exclusive)
        if minimum is None or maximum is None or minimum >= maximum:
            raise ValueError("software compatibility bounds must be ordered PEP 440 versions")
        if not self.job_payload_schemas or not self.job_result_schemas:
            raise ValueError("job compatibility schema sets cannot be empty")
        if any(item < 1 for item in (*self.job_payload_schemas, *self.job_result_schemas)):
            raise ValueError("job compatibility schema versions must be positive")


@dataclass(frozen=True)
class CompatibilityFinding:
    field: str
    supplied: object
    disposition: CompatibilityDisposition
    supported: object

    def __post_init__(self) -> None:
        object.__setattr__(self, "supplied", _freeze_json(self.supplied))
        object.__setattr__(self, "supported", _freeze_json(self.supported))

    def to_dict(self) -> dict[str, object]:
        return {
            "field": self.field,
            "supplied": _thaw_json(self.supplied),
            "disposition": self.disposition.value,
            "supported": _thaw_json(self.supported),
        }


@total_ordering
@dataclass(frozen=True)
class _Pep440Version:
    major: int
    minor: int
    patch: int
    pre: tuple[str, int] | None = None
    development: int | None = None

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, _Pep440Version):
            return NotImplemented
        core = (self.major, self.minor, self.patch)
        other_core = (other.major, other.minor, other.patch)
        if core != other_core:
            return core < other_core
        pre_order = _pre_order(self.pre, self.development)
        other_pre_order = _pre_order(other.pre, other.development)
        if pre_order != other_pre_order:
            return pre_order < other_pre_order
        if self.development is None:
            return False
        if other.development is None:
            return True
        return self.development < other.development


def _pre_order(pre: tuple[str, int] | None, development: int | None) -> tuple[int, int]:
    if pre is None:
        return (-1, 0) if development is not None else (3, 0)
    return ({"a": 0, "b": 1, "rc": 2}[pre[0]], pre[1])


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in sorted(value.items())})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _pep440(value: str | None) -> _Pep440Version | None:
    if value is None:
        return None
    match = _PEP440_RE.fullmatch(value)
    if match is None:
        return None
    pre = match.group("pre")
    return _Pep440Version(
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        (pre.lower(), int(match.group("pre_number"))) if pre is not None else None,
        int(match.group("development")) if match.group("development") else None,
    )


def _software_finding(value: str | None, supported: SupportedCompatibility) -> CompatibilityFinding:
    parsed = _pep440(value)
    minimum = _pep440(supported.software_minimum)
    maximum = _pep440(supported.software_maximum_exclusive)
    bound = {
        "minimum": supported.software_minimum,
        "maximum_exclusive": supported.software_maximum_exclusive,
    }
    if value is None:
        disposition = CompatibilityDisposition.MISSING
    elif parsed is None or minimum is None or maximum is None:
        disposition = CompatibilityDisposition.MALFORMED
    elif parsed < minimum:
        disposition = CompatibilityDisposition.UNSUPPORTED_OLDER
    elif parsed >= maximum:
        disposition = CompatibilityDisposition.UNSUPPORTED_NEWER
    elif (parsed.major, parsed.minor) != (minimum.major, minimum.minor):
        disposition = CompatibilityDisposition.ADVISORY
    else:
        disposition = CompatibilityDisposition.COMPATIBLE
    return CompatibilityFinding("software_version", value, disposition, bound)


def _range_finding(field: str, value: int | None, supported: IntegerRange) -> CompatibilityFinding:
    return CompatibilityFinding(field, value, supported.classify(value), supported.to_dict())


def _schema_set_disposition(values: tuple[int, ...], supported: frozenset[int]) -> CompatibilityDisposition:
    malformed = any(isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in values)
    if not values:
        return CompatibilityDisposition.MISSING
    if malformed:
        return CompatibilityDisposition.MALFORMED
    if tuple(sorted(set(values))) != values:
        return CompatibilityDisposition.CONTRADICTORY
    if min(values) < min(supported):
        return CompatibilityDisposition.UNSUPPORTED_OLDER
    if max(values) > max(supported):
        return CompatibilityDisposition.UNSUPPORTED_NEWER
    if not set(values).issubset(supported):
        return CompatibilityDisposition.MALFORMED
    return CompatibilityDisposition.COMPATIBLE


def _set_finding(field: str, values: tuple[int, ...], supported: frozenset[int]) -> CompatibilityFinding:
    return CompatibilityFinding(
        field,
        list(values),
        _schema_set_disposition(values, supported),
        sorted(supported),
    )


def analyze_compatibility(
    identity: CompatibilityIdentity,
    supported: SupportedCompatibility,
) -> tuple[CompatibilityFinding, ...]:
    """Analyze supplied immutable identities without I/O or controller mutation."""

    findings = [
        _software_finding(identity.software_version, supported),
        _range_finding(
            "validation_protocol_version",
            identity.validation_protocol_version,
            supported.validation_protocol,
        ),
        _range_finding("config_schema_version", identity.config_schema_version, supported.config_schema),
        _range_finding("swwp_protocol_version", identity.swwp_protocol_version, supported.swwp_protocol),
        _set_finding(
            "job_payload_schema_versions",
            identity.job_payload_schema_versions,
            supported.job_payload_schemas,
        ),
        _set_finding(
            "job_result_schema_versions",
            identity.job_result_schema_versions,
            supported.job_result_schemas,
        ),
        _range_finding(
            "diagnostic_schema_version",
            identity.diagnostic_schema_version,
            supported.diagnostic_schema,
        ),
        _range_finding(
            "diagnostic_catalog_version",
            identity.diagnostic_catalog_version,
            supported.diagnostic_catalog,
        ),
        _range_finding(
            "capability_manifest_version",
            identity.capability_manifest_version,
            supported.capability_manifest,
        ),
        _range_finding("report_schema_version", identity.report_schema_version, supported.report_schema),
    ]
    return tuple(findings)
