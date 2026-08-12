"""Narrow provider boundary consumed by the backend-neutral service."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..models import SynthesisRequest
from .models import ProviderAudio


class ProviderAdapter(Protocol):
    backend_id: str

    def synthesize(
        self,
        request: SynthesisRequest,
        text: str,
        *,
        output_dir: Path,
        deadline: float,
        cancellation: object,
    ) -> ProviderAudio: ...

    def close(self) -> None: ...
