"""Optional remote TTS provider adapters.

The adapter package owns provider transport, credentials, and wire schemas.
The synthesis service owns preprocessing, output acceptance, fallback, and
publication fencing.
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .base import ProviderAdapter
    from .models import OpenAICompatibleConfig, SeasonalTtsdConfig
    from .remote import OpenAICompatibleAdapter, SeasonalTtsdAdapter

_EXPORTS = {
    "OpenAICompatibleAdapter": "remote",
    "OpenAICompatibleConfig": "models",
    "ProviderAdapter": "base",
    "SeasonalTtsdAdapter": "remote",
    "SeasonalTtsdConfig": "models",
}

__all__ = [
    "OpenAICompatibleAdapter",
    "OpenAICompatibleConfig",
    "ProviderAdapter",
    "SeasonalTtsdAdapter",
    "SeasonalTtsdConfig",
]


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value
    return value
