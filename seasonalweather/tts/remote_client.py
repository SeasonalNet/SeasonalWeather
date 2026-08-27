"""Controller-owned client for explicitly configured remote TTS providers."""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Any

from .tts import TTS


class RemoteSynthesisClient:
    """Keep the accepted remote-provider path without restoring local execution.

    Local synthesis is a worker responsibility.  This client is constructed
    only for the two external provider backends and rejects a local backend or
    local fallback before a request can be issued.
    """

    def __init__(
        self,
        *,
        configuration: Any,
        admission_check: Callable[[], None] | None = None,
        activity_context: Callable[[], AbstractContextManager[None]] | None = None,
    ) -> None:
        self.configuration = configuration
        self.configuration_generation = 0
        self._admission_check = admission_check
        self._activity_context = activity_context
        self._tts = self._build_tts(configuration)

    def _build_tts(self, configuration: Any) -> TTS:
        backend = str(configuration.tts.backend)
        fallback = configuration.tts.fallback_backend
        if backend == "local" or fallback == "local":
            raise ValueError("local TTS execution is worker-owned and cannot be configured in the controller")
        if backend not in {"seasonal_ttsd", "openai_compatible"}:
            raise ValueError(f"unsupported controller TTS backend: {backend}")
        return TTS(
            backend=backend,
            local_engine=str(configuration.tts.local.engine),
            voice=str(configuration.tts.voice),
            rate_wpm=int(configuration.tts.rate_wpm),
            volume=float(configuration.tts.volume),
            sample_rate=int(configuration.audio.sample_rate),
            text_overrides=configuration.tts.text_overrides,
            vtp_cfg=configuration.tts.voicetext_paul,
            fallback_backend=None,
            configuration_generation=self.configuration_generation,
            generation_provider=lambda: self.configuration_generation,
            current_generation=lambda expected: expected is None or expected == self.configuration_generation,
            admission_check=self._admission_check,
            activity_context=self._activity_context,
            seasonal_ttsd_config=configuration.tts.seasonal_ttsd,
            openai_compatible_config=configuration.tts.openai_compatible,
            tts_data_base=configuration.paths.operational_state_dir,
        )

    def reconfigure(self, configuration: Any) -> None:
        old = self._tts
        self.configuration = configuration
        self._tts = self._build_tts(configuration)
        old.close()

    def availability(self) -> tuple[bool, str]:
        return self._tts.availability()

    def close(self) -> None:
        self._tts.close()

    async def synthesize(
        self,
        text: str,
        output_path: Path,
        *,
        purpose: str = "routine",
        deadline_at: dt.datetime | None = None,
        cancellation: asyncio.Event | None = None,
        source_identity: str | None = None,
        event_identity: str | None = None,
        content_identity: str | None = None,
    ) -> None:
        del cancellation, source_identity, event_identity, content_identity
        context = self._activity_context() if self._activity_context is not None else nullcontext()

        def render() -> None:
            with context:
                self._tts.synth_to_wav(
                    text,
                    Path(output_path),
                    purpose=purpose,
                    deadline_at=deadline_at,
                )

        await asyncio.to_thread(render)
