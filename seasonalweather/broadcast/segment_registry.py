"""The authoritative static segment policy registry.

This module owns descriptive segment policy only.  Configuration remains the
owner of configuration values, the capability subsystem remains the owner of
runtime capability truth, and the conductor/refresher remain runtime owners.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import StrEnum
from types import MappingProxyType
from typing import NoReturn

from ..diagnostics.models import DiagnosticSeverity
from ..jobs.policies import CapabilityRequirement
from ..validation.admission import segment_field
from ..validation.issues import ValidationIssue, ValidationStage


class SegmentFailurePolicy(StrEnum):
    """How a failed refresh is represented to the cycle runtime."""

    RETAIN_LAST_KNOWN_GOOD = "retain_last_known_good"


class SegmentBuilderKind(StrEnum):
    """Existing execution seam used to produce a segment."""

    REFRESHER_ID = "refresher_id"
    REFRESHER_STATUS = "refresher_status"
    CYCLE_BUILDER_SEGMENTS = "cycle_builder_segments"
    CONDUCTOR_LIVE_TIME = "conductor_live_time"


# This is the complete set of P1-19 execution seams.  It deliberately lives
# with the authoritative declarations so a valid enum value cannot be paired
# with a made-up owner or operation, or with a segment role that the existing
# consumer cannot execute.
_BUILDER_SEAM_CONTRACT = MappingProxyType(
    {
        SegmentBuilderKind.REFRESHER_ID: (
            "seasonalweather.broadcast.segment_refresher",
            "SegmentRefresher._refresh_id",
            frozenset({"id"}),
        ),
        SegmentBuilderKind.REFRESHER_STATUS: (
            "seasonalweather.broadcast.segment_refresher",
            "SegmentRefresher._refresh_status",
            frozenset({"status"}),
        ),
        SegmentBuilderKind.CONDUCTOR_LIVE_TIME: (
            "seasonalweather.broadcast.conductor",
            "CycleConductor._push_live_time",
            frozenset({"time"}),
        ),
        SegmentBuilderKind.CYCLE_BUILDER_SEGMENTS: (
            "seasonalweather.broadcast.cycle",
            "CycleBuilder.build_segments",
            frozenset(
                {
                    "health",
                    "hwo",
                    "spc",
                    "zfp",
                    "fcst",
                    "cwf",
                    "obs",
                    "marine_obs",
                    "outro",
                }
            ),
        ),
    }
)


class SegmentFocusPolicy(StrEnum):
    """Whether a segment is core or deferred while alert focus is active."""

    CORE = "core"
    DEFERRED = "deferred"
    NOT_AIRABLE = "not_airable"


def _freeze_value(value: object) -> object:
    """Recursively freeze registry-owned descriptive values."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_value(item) for item in value)
    return value


@dataclass(frozen=True)
class SegmentCapabilityRequirement:
    """Deeply immutable registry declaration for one runtime capability."""

    name: str
    required: bool = True
    parameters: Mapping[str, str | int | bool] = dataclass_field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", _freeze_value(self.parameters))

    def to_runtime_requirement(self) -> CapabilityRequirement:
        """Convert at the existing capability boundary to the runtime model."""
        if (
            not isinstance(self.name, str)
            or not isinstance(self.required, bool)
            or not isinstance(self.parameters, Mapping)
        ):
            raise TypeError("malformed segment capability requirement")
        return CapabilityRequirement(
            name=self.name,
            required=self.required,
            parameters=dict(self.parameters),
        )


@dataclass(frozen=True)
class SegmentBuilderReference:
    """Stable identity for an existing builder/adapter boundary.

    P1-19 records the existing implementation identity.  It does not change
    the P1-20 builder result or provenance architecture.
    """

    owner: str
    operation: str
    kind: SegmentBuilderKind


@dataclass(frozen=True)
class SegmentEnablement:
    """Mapping from a registry entry to an existing typed config value."""

    config_path: tuple[str, ...] | None = None
    default: bool = True

    def __post_init__(self) -> None:
        path = tuple(self.config_path or ())
        object.__setattr__(self, "config_path", path)
        if any(not part.strip() for part in path):
            raise ValueError("segment enablement path must contain non-empty names")

    def resolve(self, config: object | None, *, use_default_when_config_absent: bool = True) -> bool:
        if config is None and self.config_path and not use_default_when_config_absent:
            return False
        return _resolve_declared_bool(self.config_path or (), config, self.default, "enablement")


@dataclass(frozen=True)
class SegmentFallbackPolicy:
    """Typed policy for an optional unavailable-content fallback."""

    config_path: tuple[str, ...] | None = None
    default: bool = False

    def __post_init__(self) -> None:
        path = tuple(self.config_path or ())
        object.__setattr__(self, "config_path", path)
        if any(not part.strip() for part in path):
            raise ValueError("segment fallback path must contain non-empty names")

    def resolve(self, config: object | None) -> bool:
        return _resolve_declared_bool(self.config_path or (), config, self.default, "fallback")


@dataclass(frozen=True)
class SegmentDefinition:
    """Immutable policy for one statically known broadcast segment."""

    key: str
    title: str
    builder: SegmentBuilderReference
    enablement: SegmentEnablement
    normal_order: int | None
    focus_order: int | None
    refresh_cadence_seconds: int
    max_age_seconds: int
    minimum_air_interval_seconds: int
    failure_policy: SegmentFailurePolicy
    capability_requirements: tuple[SegmentCapabilityRequirement, ...]
    policy_metadata: Mapping[str, object]
    fallback_policy: SegmentFallbackPolicy | None = None
    focus_policy: SegmentFocusPolicy = SegmentFocusPolicy.CORE

    def __post_init__(self) -> None:
        if not self.key.strip() or not self.title.strip():
            raise ValueError("segment key and title must be non-empty")
        for name, value in (
            ("refresh cadence", self.refresh_cadence_seconds),
            ("maximum age", self.max_age_seconds),
            ("minimum air interval", self.minimum_air_interval_seconds),
        ):
            if isinstance(value, bool) or value < 0:
                raise ValueError(f"segment {name} must be nonnegative")
        object.__setattr__(self, "capability_requirements", tuple(self.capability_requirements))
        object.__setattr__(self, "policy_metadata", _freeze_value(self.policy_metadata))


class SegmentRegistryError(ValueError):
    """A fail-closed registry definition error with a governed diagnostic."""

    def __init__(self, issue: ValidationIssue) -> None:
        self.issue = issue
        super().__init__(f"{issue.code}: {issue.message}")


def _resolve_declared_bool(path: tuple[str, ...], config: object | None, default: bool, field_name: str) -> bool:
    if not path or config is None:
        return default
    current = config
    for part in path:
        if not hasattr(current, part):
            raise SegmentRegistryError(
                _registry_issue(
                    "segment.registry.invalid_definition",
                    ".".join(path),
                    f"Declared segment {field_name} path is absent from the typed configuration: {'.'.join(path)}.",
                )
            )
        current = getattr(current, part)
    if not isinstance(current, bool):
        raise SegmentRegistryError(
            _registry_issue(
                "segment.registry.invalid_definition",
                ".".join(path),
                f"Declared segment {field_name} path is not a boolean typed configuration value: {'.'.join(path)}.",
            )
        )
    return current


def _registry_issue(rule_id: str, key: str, message: str) -> ValidationIssue:
    return ValidationIssue(
        rule_id=rule_id,
        phase=ValidationStage.SEMANTIC,
        severity=DiagnosticSeverity.ERROR,
        blocking=True,
        message=message,
        path=segment_field(key).path(),
        operational_effect="The broadcast segment registry cannot be used until its definitions are corrected.",
        help="Correct the authoritative segment definition and construct the registry again.",
        documentation_reference="docs/segment-registry.md",
    )


@dataclass(frozen=True)
class ResolvedSegment:
    definition: SegmentDefinition
    enabled: bool
    fallback_enabled: bool = False


class ResolvedSegmentRegistry:
    """A configuration-bound, immutable view of the canonical registry."""

    def __init__(self, definitions: tuple[ResolvedSegment, ...]) -> None:
        self._definitions = tuple(definitions)
        self._by_key = MappingProxyType({item.definition.key: item for item in self._definitions})

    @property
    def definitions(self) -> tuple[ResolvedSegment, ...]:
        return self._definitions

    def get(self, key: str) -> SegmentDefinition | None:
        item = self._by_key.get(_ALIASES.get(key, key))
        return item.definition if item else None

    def enabled(self, key: str) -> bool:
        item = self._by_key.get(_ALIASES.get(key, key))
        return bool(item and item.enabled)

    def title_for(self, key: str, fallback: str | None = None) -> str:
        definition = self.get(key)
        return definition.title if definition else (fallback or key)

    def refresh_cadence(self, key: str) -> int:
        definition = self.get(key)
        return definition.refresh_cadence_seconds if definition else 0

    def max_age(self, key: str) -> int:
        definition = self.get(key)
        return definition.max_age_seconds if definition else 0

    def fallback_enabled(self, key: str) -> bool:
        item = self._by_key.get(_ALIASES.get(key, key))
        return bool(item and item.fallback_enabled)

    def static_order(self, *, focus: bool) -> tuple[str, ...]:
        """Return the complete enabled static airable order for a mode."""
        order_name = "focus_order" if focus else "normal_order"
        entries = (item.definition for item in self._definitions if self._is_content_segment(item, focus=focus))
        return tuple(
            item.key
            for item in sorted(
                (item for item in entries if getattr(item, order_name) is not None),
                key=lambda item: getattr(item, order_name),
            )
        )

    def content_keys(self, *, focus: bool) -> tuple[str, ...]:
        """Return the historical content-only view after id and live time."""
        return tuple(key for key in self.static_order(focus=focus) if key not in {"id", "time"})

    @staticmethod
    def _is_content_segment(item: ResolvedSegment, *, focus: bool) -> bool:
        definition = item.definition
        return (
            item.enabled
            and definition.focus_policy is not SegmentFocusPolicy.NOT_AIRABLE
            and (not focus or definition.focus_policy is SegmentFocusPolicy.CORE)
        )

    def refresh_keys(self) -> tuple[str, ...]:
        return tuple(
            item.definition.key
            for item in self._definitions
            if item.enabled
            and item.definition.refresh_cadence_seconds > 0
            and item.definition.key not in {"time", "outro"}
        )

    def deferred_focus_keys(self) -> tuple[str, ...]:
        return tuple(
            item.key
            for item in sorted(
                (
                    item.definition
                    for item in self._definitions
                    if item.enabled and item.definition.focus_policy is SegmentFocusPolicy.DEFERRED
                ),
                key=lambda item: item.focus_order if item.focus_order is not None else -1,
            )
        )

    def minimum_air_interval(self, key: str) -> float:
        definition = self.get(key)
        return float(definition.minimum_air_interval_seconds) if definition else 0.0

    def is_managed(self, key: str) -> bool:
        return _ALIASES.get(key, key) in self._by_key


def _invalid_definition(key: str, message: str) -> NoReturn:
    raise SegmentRegistryError(_registry_issue("segment.registry.invalid_definition", key, message))


def _definition_key(definition: SegmentDefinition) -> str:
    return definition.key if isinstance(definition.key, str) and definition.key.strip() else "registry"


def _validate_definition_identity(definition: SegmentDefinition, key: str) -> None:
    if not isinstance(definition.key, str) or not definition.key.strip():
        _invalid_definition(key, "Authoritative segment key must be a non-empty string.")
    if not isinstance(definition.title, str) or not definition.title.strip():
        _invalid_definition(key, f"Authoritative segment title must be a non-empty string: {key}.")


def _validate_definition_builder(definition: SegmentDefinition, key: str) -> None:
    builder = definition.builder
    if not isinstance(builder, SegmentBuilderReference):
        _invalid_definition(key, f"Authoritative segment builder reference is malformed: {key}.")
    if (
        not isinstance(builder.owner, str)
        or not builder.owner.strip()
        or not isinstance(builder.operation, str)
        or not builder.operation.strip()
        or not isinstance(builder.kind, SegmentBuilderKind)
    ):
        _invalid_definition(key, f"Authoritative segment builder reference is invalid: {key}.")
    expected = _BUILDER_SEAM_CONTRACT.get(builder.kind)
    if expected is None:
        _invalid_definition(key, f"Authoritative segment builder kind is unavailable: {key}.")
    expected_owner, expected_operation, allowed_keys = expected
    if (builder.owner, builder.operation) != (expected_owner, expected_operation):
        _invalid_definition(
            key,
            f"Authoritative segment builder does not resolve to an existing P1-19 seam: {key}.",
        )
    if key not in allowed_keys:
        _invalid_definition(
            key,
            f"Authoritative segment builder seam is unavailable for this segment role: {key}.",
        )


def _validate_definition_policies(definition: SegmentDefinition, key: str) -> None:
    if not isinstance(definition.enablement, SegmentEnablement):
        _invalid_definition(key, f"Authoritative segment enablement is malformed: {key}.")
    if definition.fallback_policy is not None and not isinstance(definition.fallback_policy, SegmentFallbackPolicy):
        _invalid_definition(key, f"Authoritative segment fallback policy is malformed: {key}.")
    if not isinstance(definition.failure_policy, SegmentFailurePolicy):
        _invalid_definition(key, f"Authoritative segment failure policy is invalid: {key}.")
    if not isinstance(definition.focus_policy, SegmentFocusPolicy):
        _invalid_definition(key, f"Authoritative segment focus policy is invalid: {key}.")


def _validate_definition_numbers(definition: SegmentDefinition, key: str) -> None:
    for name, value in (
        ("refresh cadence", definition.refresh_cadence_seconds),
        ("maximum age", definition.max_age_seconds),
        ("minimum air interval", definition.minimum_air_interval_seconds),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            _invalid_definition(key, f"Authoritative segment {name} must be a nonnegative integer: {key}.")
    for name, order_value in (("normal", definition.normal_order), ("focus", definition.focus_order)):
        if order_value is not None and (
            isinstance(order_value, bool) or not isinstance(order_value, int) or order_value < 0
        ):
            _invalid_definition(
                key, f"Authoritative segment {name} ordering must be a nonnegative integer or None: {key}."
            )


def _validate_definition_capabilities(definition: SegmentDefinition, key: str) -> None:
    if not isinstance(definition.capability_requirements, tuple):
        _invalid_definition(key, f"Authoritative segment capability declarations are malformed: {key}.")
    for requirement in definition.capability_requirements:
        if not isinstance(requirement, SegmentCapabilityRequirement):
            _invalid_definition(key, f"Authoritative segment capability declaration is malformed: {key}.")
        try:
            requirement.to_runtime_requirement()
        except Exception as exc:
            raise SegmentRegistryError(
                _registry_issue(
                    "segment.registry.invalid_definition",
                    key,
                    f"Authoritative segment capability declaration is invalid: {key}.",
                )
            ) from exc


class SegmentRegistry:
    """Validated, deterministic authority for static segment definitions."""

    def __init__(self, definitions: tuple[SegmentDefinition, ...] | list[SegmentDefinition]) -> None:
        ordered = tuple(definitions)
        seen: set[str] = set()
        for definition in ordered:
            if not isinstance(definition, SegmentDefinition):
                raise SegmentRegistryError(
                    _registry_issue(
                        "segment.registry.invalid_definition",
                        "registry",
                        "Authoritative segment registry entries must be SegmentDefinition values.",
                    )
                )
            self._validate_definition(definition)
            if definition.key in seen:
                raise SegmentRegistryError(
                    _registry_issue(
                        "segment.registry.invalid_definition",
                        definition.key,
                        f"Duplicate authoritative segment key: {definition.key}.",
                    )
                )
            seen.add(definition.key)
        for mode, field in (("normal", "normal_order"), ("focus", "focus_order")):
            self._validate_order_positions(ordered, mode=mode, field=field)
        self._definitions = ordered
        self._by_key = MappingProxyType({item.key: item for item in ordered})

    @staticmethod
    def _validate_definition(definition: SegmentDefinition) -> None:
        key = _definition_key(definition)
        _validate_definition_identity(definition, key)
        _validate_definition_builder(definition, key)
        _validate_definition_policies(definition, key)
        _validate_definition_numbers(definition, key)
        _validate_definition_capabilities(definition, key)
        if not isinstance(definition.policy_metadata, Mapping):
            _invalid_definition(key, f"Authoritative segment policy metadata must be a mapping: {key}.")
        if definition.refresh_cadence_seconds > definition.max_age_seconds:
            raise SegmentRegistryError(
                _registry_issue(
                    "segment.registry.policy_invariant",
                    definition.key,
                    f"Segment refresh cadence exceeds maximum age: {definition.key}.",
                )
            )
        if definition.focus_policy is not SegmentFocusPolicy.NOT_AIRABLE and (
            definition.normal_order is None or definition.focus_order is None
        ):
            raise SegmentRegistryError(
                _registry_issue(
                    "segment.registry.policy_invariant",
                    definition.key,
                    f"Airable segment is missing normal or focus ordering: {definition.key}.",
                )
            )

    @staticmethod
    def _validate_order_positions(definitions: tuple[SegmentDefinition, ...], *, mode: str, field: str) -> None:
        positions: dict[int, str] = {}
        for definition in definitions:
            position = getattr(definition, field)
            if position is None or definition.focus_policy is SegmentFocusPolicy.NOT_AIRABLE:
                continue
            previous = positions.get(position)
            if previous is not None:
                raise SegmentRegistryError(
                    _registry_issue(
                        "segment.registry.policy_invariant",
                        definition.key,
                        f"Ambiguous {mode} ordering position {position}: {previous} and {definition.key}.",
                    )
                )
            positions[position] = definition.key

    @property
    def definitions(self) -> tuple[SegmentDefinition, ...]:
        return self._definitions

    def get(self, key: str) -> SegmentDefinition | None:
        return self._by_key.get(_ALIASES.get(key, key))

    def resolve(
        self,
        config: object | None = None,
        *,
        use_default_when_config_absent: bool = True,
    ) -> ResolvedSegmentRegistry:
        return ResolvedSegmentRegistry(
            tuple(
                ResolvedSegment(
                    item,
                    item.enablement.resolve(
                        config,
                        use_default_when_config_absent=use_default_when_config_absent,
                    ),
                    item.fallback_policy.resolve(config) if item.fallback_policy is not None else False,
                )
                for item in self._definitions
            )
        )

    def resolve_for_cycle(self, config: object | None) -> ResolvedSegmentRegistry:
        """Resolve the legacy CycleBuilder(None) compatibility profile."""
        return self.resolve(config, use_default_when_config_absent=False)


_TTS = (SegmentCapabilityRequirement(name="tts.synthesis.v1", parameters={"format": "wav"}),)


def _definition(
    key: str,
    title: str,
    owner: str,
    operation: str,
    builder_kind: SegmentBuilderKind,
    normal: int | None,
    focus: int | None,
    cadence: int,
    *,
    enablement: tuple[str, ...] | None = None,
    focus_policy: SegmentFocusPolicy = SegmentFocusPolicy.CORE,
    minimum_air_interval: int = 0,
    metadata: Mapping[str, object] | None = None,
    fallback_policy: SegmentFallbackPolicy | None = None,
    capability_required: bool = True,
) -> SegmentDefinition:
    return SegmentDefinition(
        key=key,
        title=title,
        builder=SegmentBuilderReference(owner, operation, builder_kind),
        enablement=SegmentEnablement(enablement),
        normal_order=normal,
        focus_order=focus,
        refresh_cadence_seconds=cadence,
        max_age_seconds=cadence,
        minimum_air_interval_seconds=minimum_air_interval,
        failure_policy=SegmentFailurePolicy.RETAIN_LAST_KNOWN_GOOD,
        capability_requirements=_TTS if capability_required else (),
        policy_metadata=metadata or {},
        fallback_policy=fallback_policy,
        focus_policy=focus_policy,
    )


_ALIASES = MappingProxyType({"hwo-unavailable": "hwo"})

DEFAULT_SEGMENT_REGISTRY = SegmentRegistry(
    (
        _definition(
            "id",
            "Station identification.",
            "seasonalweather.broadcast.segment_refresher",
            "SegmentRefresher._refresh_id",
            SegmentBuilderKind.REFRESHER_ID,
            0,
            0,
            60,
            metadata={"live": False},
        ),
        _definition(
            "time",
            "The current time in our service area.",
            "seasonalweather.broadcast.conductor",
            "CycleConductor._push_live_time",
            SegmentBuilderKind.CONDUCTOR_LIVE_TIME,
            1,
            1,
            0,
            metadata={"live": True},
        ),
        _definition(
            "health",
            "Data feed status.",
            "seasonalweather.broadcast.cycle",
            "CycleBuilder.build_segments",
            SegmentBuilderKind.CYCLE_BUILDER_SEGMENTS,
            10,
            10,
            60,
        ),
        _definition(
            "status",
            "Overall station status and alerts.",
            "seasonalweather.broadcast.segment_refresher",
            "SegmentRefresher._refresh_status",
            SegmentBuilderKind.REFRESHER_STATUS,
            20,
            20,
            180,
        ),
        _definition(
            "hwo",
            "Hazardous weather outlook for the service area.",
            "seasonalweather.broadcast.cycle",
            "CycleBuilder.build_segments",
            SegmentBuilderKind.CYCLE_BUILDER_SEGMENTS,
            30,
            30,
            3600,
            fallback_policy=SegmentFallbackPolicy(("hwo", "speak_unavailable"), default=True),
        ),
        _definition(
            "spc",
            "Severe weather outlook for the service area.",
            "seasonalweather.broadcast.cycle",
            "CycleBuilder.build_segments",
            SegmentBuilderKind.CYCLE_BUILDER_SEGMENTS,
            40,
            40,
            1800,
            enablement=("spc", "enabled"),
        ),
        _definition(
            "zfp",
            "Weather synopsis for the area.",
            "seasonalweather.broadcast.cycle",
            "CycleBuilder.build_segments",
            SegmentBuilderKind.CYCLE_BUILDER_SEGMENTS,
            50,
            60,
            3600,
            focus_policy=SegmentFocusPolicy.DEFERRED,
            minimum_air_interval=20 * 60,
        ),
        _definition(
            "fcst",
            "The forecast for the service area.",
            "seasonalweather.broadcast.cycle",
            "CycleBuilder.build_segments",
            SegmentBuilderKind.CYCLE_BUILDER_SEGMENTS,
            60,
            70,
            1800,
            focus_policy=SegmentFocusPolicy.DEFERRED,
            minimum_air_interval=20 * 60,
        ),
        _definition(
            "cwf",
            "Coastal and marine weather forecast.",
            "seasonalweather.broadcast.cycle",
            "CycleBuilder.build_segments",
            SegmentBuilderKind.CYCLE_BUILDER_SEGMENTS,
            70,
            90,
            7200,
            enablement=("cwf", "enabled"),
            focus_policy=SegmentFocusPolicy.DEFERRED,
            minimum_air_interval=40 * 60,
        ),
        _definition(
            "obs",
            "Current conditions in our area.",
            "seasonalweather.broadcast.cycle",
            "CycleBuilder.build_segments",
            SegmentBuilderKind.CYCLE_BUILDER_SEGMENTS,
            80,
            50,
            900,
        ),
        _definition(
            "marine_obs",
            "Marine observations for the service area.",
            "seasonalweather.broadcast.cycle",
            "CycleBuilder.build_segments",
            SegmentBuilderKind.CYCLE_BUILDER_SEGMENTS,
            90,
            80,
            900,
            enablement=("marine_obs", "enabled"),
            focus_policy=SegmentFocusPolicy.DEFERRED,
            minimum_air_interval=30 * 60,
        ),
        _definition(
            "outro",
            "End of the current broadcast cycle.",
            "seasonalweather.broadcast.cycle",
            "CycleBuilder.build_segments",
            SegmentBuilderKind.CYCLE_BUILDER_SEGMENTS,
            None,
            None,
            0,
            focus_policy=SegmentFocusPolicy.NOT_AIRABLE,
            metadata={"airable": False},
            capability_required=False,
        ),
    )
)


__all__ = [
    "DEFAULT_SEGMENT_REGISTRY",
    "ResolvedSegment",
    "ResolvedSegmentRegistry",
    "SegmentBuilderReference",
    "SegmentBuilderKind",
    "SegmentCapabilityRequirement",
    "SegmentDefinition",
    "SegmentEnablement",
    "SegmentFallbackPolicy",
    "SegmentFailurePolicy",
    "SegmentFocusPolicy",
    "SegmentRegistry",
    "SegmentRegistryError",
]
