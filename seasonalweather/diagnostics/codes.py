"""Single authority for diagnostic code parsing and formatting."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from .namespaces import NAMESPACE_BY_TOKEN, NamespaceState


class ConditionClass(IntEnum):
    GENERAL = 0
    INVALID_INPUT = 1
    UNSUPPORTED_STATE = 2
    DEPENDENCY = 3
    DEGRADATION = 4
    PERMANENT_FAILURE = 5
    SECURITY = 6
    RESOURCE = 7
    LIFECYCLE = 8
    RESERVED = 9


CLASS_MEANINGS = {
    ConditionClass.GENERAL: "namespace-wide, catalog, or genuinely general condition",
    ConditionClass.INVALID_INPUT: "invalid, malformed, incomplete, or undecodable input",
    ConditionClass.UNSUPPORTED_STATE: "unsupported, incompatible, contradictory, or policy-invalid state",
    ConditionClass.DEPENDENCY: "external dependency, transport, or protocol communication failure",
    ConditionClass.DEGRADATION: "temporary degradation, retry exhaustion, fallback, or availability loss",
    ConditionClass.PERMANENT_FAILURE: "permanent failure, violated invariant, corruption, or fatal condition",
    ConditionClass.SECURITY: "authentication, authorization, trust, credential, or security condition",
    ConditionClass.RESOURCE: "resource, capacity, quota, size, timeout, or deadline condition",
    ConditionClass.LIFECYCLE: "lifecycle, startup, restart, recovery, reconciliation, drain, or shutdown condition",
    ConditionClass.RESERVED: "reserved and unassignable in catalog version 1",
}


class DiagnosticCodeError(ValueError):
    """Bounded exact-code failure suitable for CLI handling."""

    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        super().__init__(message)


@dataclass(frozen=True, order=True)
class DiagnosticCode:
    namespace: str
    condition_class: ConditionClass
    ordinal: int

    def __post_init__(self) -> None:
        namespace = NAMESPACE_BY_TOKEN.get(self.namespace)
        if namespace is None:
            raise DiagnosticCodeError("unknown_namespace", "Diagnostic code uses an unknown namespace.")
        if namespace.state is NamespaceState.RESERVED:
            raise DiagnosticCodeError(
                "reserved_namespace",
                f"{self.namespace} is reserved and has no assigned catalog codes.",
            )
        if self.condition_class is ConditionClass.RESERVED:
            raise DiagnosticCodeError("reserved_class", "The 9xxx band is unassignable in catalog version 1.")
        if not 1 <= self.ordinal <= 999:
            raise DiagnosticCodeError("class_boundary", "Diagnostic ordinals must be between 001 and 999.")

    def __str__(self) -> str:
        return f"{self.namespace}{int(self.condition_class)}{self.ordinal:03d}"

    @classmethod
    def parse(cls, value: str) -> DiagnosticCode:
        namespace = _namespace_from_code(value)
        suffix = _decimal_suffix(value, namespace.token)
        _require_active(namespace.token, namespace.state)
        condition_class = ConditionClass(int(suffix[0]))
        ordinal = int(suffix[1:])
        return cls(namespace.token, condition_class, ordinal)


def format_code(namespace: str, condition_class: ConditionClass | int, ordinal: int) -> str:
    return str(DiagnosticCode(namespace, ConditionClass(condition_class), ordinal))


def _namespace_from_code(value: str):
    if not isinstance(value, str) or not value:
        raise DiagnosticCodeError("malformed", "Diagnostic code must be a nonempty string.")
    matches = [
        namespace
        for token, namespace in NAMESPACE_BY_TOKEN.items()
        if value.startswith(token) and len(value) == len(token) + 4
    ]
    if len(matches) != 1:
        raise DiagnosticCodeError("unknown_namespace", "Diagnostic code uses an unknown namespace.")
    return matches[0]


def _decimal_suffix(value: str, namespace: str) -> str:
    suffix = value[len(namespace) :]
    if len(suffix) != 4 or not suffix.isascii() or not suffix.isdecimal():
        raise DiagnosticCodeError("malformed", "Diagnostic code must end in exactly four decimal digits.")
    return suffix


def _require_active(token: str, state: NamespaceState) -> None:
    if state is NamespaceState.RESERVED:
        raise DiagnosticCodeError(
            "reserved_namespace",
            f"{token} is reserved and has no assigned catalog codes.",
        )
