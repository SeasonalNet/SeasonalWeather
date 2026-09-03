"""Shared artifact contracts with lazy controller-owned service exports.

Importing a worker-safe leaf must not load persistence or publication owners.
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .fencing import ExpectedResultFence, FenceDecision, evaluate_fence
    from .models import ArtifactClass, ArtifactReference, ArtifactResult, MediaMetadata
    from .service import ArtifactService

_EXPORTS = {
    "ArtifactClass": "models",
    "ArtifactReference": "models",
    "ArtifactResult": "models",
    "ArtifactService": "service",
    "ExpectedResultFence": "fencing",
    "FenceDecision": "fencing",
    "MediaMetadata": "models",
    "evaluate_fence": "fencing",
}

__all__ = [
    "ArtifactClass",
    "ArtifactReference",
    "ArtifactResult",
    "ArtifactService",
    "ExpectedResultFence",
    "FenceDecision",
    "MediaMetadata",
    "evaluate_fence",
]


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value
    return value
