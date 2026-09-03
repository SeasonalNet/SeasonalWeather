"""Shared capability contracts with lazy controller qualification exports."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .manifest import CapabilityManifest, CapabilityUpdate, manifest_digest
    from .models import CapabilityRecord, CompatibilityState, DependencyState, OperationalState
    from .qualification import QualificationReason, QualificationResult, qualify
    from .registry import CapabilityRegistry, WorkerCapabilitySnapshot

_EXPORTS = {
    "CapabilityManifest": "manifest",
    "CapabilityRecord": "models",
    "CapabilityRegistry": "registry",
    "CapabilityUpdate": "manifest",
    "CompatibilityState": "models",
    "DependencyState": "models",
    "OperationalState": "models",
    "QualificationReason": "qualification",
    "QualificationResult": "qualification",
    "WorkerCapabilitySnapshot": "registry",
    "manifest_digest": "manifest",
    "qualify": "qualification",
}

__all__ = [
    "CapabilityManifest",
    "CapabilityRecord",
    "CapabilityRegistry",
    "CapabilityUpdate",
    "CompatibilityState",
    "DependencyState",
    "OperationalState",
    "QualificationReason",
    "QualificationResult",
    "WorkerCapabilitySnapshot",
    "manifest_digest",
    "qualify",
]


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value
    return value
