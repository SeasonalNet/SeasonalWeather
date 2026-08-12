"""Optional remote TTS provider adapters.

The adapter package owns provider transport, credentials, and wire schemas.
The synthesis service owns preprocessing, output acceptance, fallback, and
publication fencing.
"""

from .base import ProviderAdapter
from .models import OpenAICompatibleConfig, SeasonalTtsdConfig
from .remote import OpenAICompatibleAdapter, SeasonalTtsdAdapter

__all__ = [
    "OpenAICompatibleAdapter",
    "OpenAICompatibleConfig",
    "ProviderAdapter",
    "SeasonalTtsdAdapter",
    "SeasonalTtsdConfig",
]
