"""Controller client for worker-owned artifact jobs.

This module deliberately contains no synthesis implementation.  It persists a
bounded opaque input reference, admits a durable job, and waits for the
controller's already-authoritative artifact/result commitment path.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .policies import JobType


class WorkerJobUnavailable(RuntimeError):
    """Raised when worker-owned execution cannot be admitted or committed."""


class SynthesisClient(Protocol):
    """Minimal synthesis port used by controller-owned audio assembly."""

    configuration_generation: int

    def availability(self) -> tuple[bool, str]: ...

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
    ) -> None: ...


@dataclass(frozen=True)
class WorkerSynthesisConfiguration:
    engine: str
    voice: str
    rate_wpm: int
    volume: float
    sample_rate: int
    text_overrides: tuple[dict[str, Any], ...] = ()
    voicetext_paul: dict[str, Any] | None = None
    data_base: str = ""

    @classmethod
    def from_configuration(cls, configuration: Any) -> WorkerSynthesisConfiguration:
        tts = configuration.tts
        local = tts.local
        vtp = getattr(tts, "voicetext_paul", None)
        return cls(
            engine=str(local.engine),
            voice=str(tts.voice),
            rate_wpm=int(tts.rate_wpm),
            volume=float(tts.volume),
            sample_rate=int(configuration.audio.sample_rate),
            text_overrides=tuple(dict(item) for item in (tts.text_overrides or ())),
            voicetext_paul=(
                {
                    name: getattr(vtp, name)
                    for name in (
                        "run_as",
                        "retries",
                        "retry_sleep_ms",
                        "reset_every",
                        "kill_before",
                        "vtml_lexicon",
                        "alias_overrides",
                        "phoneme_overrides_x_cmu",
                    )
                    if hasattr(vtp, name)
                }
                if vtp is not None
                else None
            ),
            # The controller's state path is not mounted in workers. Local
            # engines resolve their own image/volume-local runtime roots.
            data_base="",
        )


class WorkerInputStore:
    """Persistent controller-written input descriptors for shared staging."""

    def __init__(self, root: Path, *, maximum_bytes: int = 131_072) -> None:
        self.root = Path(root)
        self.maximum_bytes = int(maximum_bytes)

    def put(self, *, text: str, configuration: WorkerSynthesisConfiguration) -> tuple[str, str]:
        if not text or len(text.encode("utf-8")) > self.maximum_bytes // 2:
            raise ValueError("worker synthesis input is empty or overlong")
        self.root.mkdir(mode=0o750, parents=True, exist_ok=True)
        content_ref = f"content_{uuid.uuid4().hex[:20]}"
        profile_ref = f"profile_{uuid.uuid4().hex[:20]}"
        descriptor = {
            "schema_version": 1,
            "content_ref": content_ref,
            "voice_profile_ref": profile_ref,
            "text": text,
            "engine": configuration.engine,
            "voice": configuration.voice,
            "rate_wpm": configuration.rate_wpm,
            "volume": configuration.volume,
            "sample_rate": configuration.sample_rate,
            "text_overrides": list(configuration.text_overrides),
            "voicetext_paul": configuration.voicetext_paul,
            "data_base": configuration.data_base,
        }
        encoded = json.dumps(descriptor, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(encoded) > self.maximum_bytes:
            raise ValueError("worker synthesis descriptor exceeds configured bounds")
        destination = self.root / f"{content_ref}.json"
        fd, temporary_name = tempfile.mkstemp(prefix=".input-", dir=self.root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o640)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return content_ref, profile_ref

    def read(self, content_ref: str) -> dict[str, Any]:
        if not content_ref.startswith("content_") or not content_ref.replace("_", "").isalnum():
            raise ValueError("worker input reference is invalid")
        path = self.root / f"{content_ref}.json"
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise WorkerJobUnavailable("worker synthesis input is unavailable") from exc
        if len(raw) > self.maximum_bytes:
            raise ValueError("worker synthesis descriptor exceeds configured bounds")
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("content_ref") != content_ref:
            raise ValueError("worker synthesis descriptor identity is invalid")
        return value


class WorkerSynthesisClient:
    """Admit TTS work and return only controller-accepted worker artifacts."""

    def __init__(self, *, configuration: Any, input_root: Path, configuration_generation: int = 0) -> None:
        self.configuration = configuration
        self.configuration_generation = int(configuration_generation)
        self.input_store = WorkerInputStore(input_root)
        self._job_service: Any | None = None
        self._repository: Any | None = None
        self._active_root: Path | None = None
        self._bound = False

    def bind(
        self,
        *,
        job_service: Any,
        repository: Any,
        active_root: Path,
        configuration_generation: int | None = None,
    ) -> None:
        self._job_service = job_service
        self._repository = repository
        self._active_root = Path(active_root)
        if configuration_generation is not None:
            self.configuration_generation = int(configuration_generation)
        self._bound = True

    def reconfigure(self, configuration: Any) -> None:
        self.configuration = configuration

    def availability(self) -> tuple[bool, str]:
        if not self._bound or self._job_service is None or self._repository is None or self._active_root is None:
            return False, "worker_job_client_unavailable"
        return True, "worker_execution_required"

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
        if self._job_service is None or self._repository is None or self._active_root is None:
            raise WorkerJobUnavailable("worker job service is not bound")
        if purpose not in {"routine", "alert", "administrative"}:
            raise ValueError("unsupported worker synthesis purpose")
        if purpose == "administrative":
            purpose = "routine"
        configuration = WorkerSynthesisConfiguration.from_configuration(self.configuration)
        content_ref, profile_ref = self.input_store.put(text=text, configuration=configuration)
        generation = self.configuration_generation
        now = dt.datetime.now(dt.UTC)
        deadline = deadline_at or (now + dt.timedelta(seconds=180 if purpose == "routine" else 90))
        job_type, payload, dedupe_key = self._job_request(
            purpose=purpose,
            content_ref=content_ref,
            profile_ref=profile_ref,
            generation=generation,
            source_identity=source_identity,
            event_identity=event_identity,
            content_identity=content_identity,
        )
        admission = await asyncio.to_thread(
            self._job_service.admit,
            job_type=job_type,
            payload=payload,
            deadline_at=deadline,
            dedupe_key=dedupe_key,
            config_generation=generation,
        )
        await self._wait_for_result(
            job_id=admission.job.job_id,
            output_path=output_path,
            deadline=deadline,
            cancellation=cancellation,
        )

    @staticmethod
    def _job_request(
        *,
        purpose: str,
        content_ref: str,
        profile_ref: str,
        generation: int,
        source_identity: str | None,
        event_identity: str | None,
        content_identity: str | None,
    ) -> tuple[JobType, dict[str, Any], str]:
        if purpose == "alert":
            payload = {
                "source_identity": source_identity or "source_controller",
                "event_identity": event_identity or f"event_{content_ref}",
                "content_identity": content_identity or content_ref,
                "content_ref": content_ref,
                "mode": "full",
                "config_generation": generation,
            }
            dedupe_key = f"alert:{payload['source_identity']}:{payload['event_identity']}:{payload['content_identity']}"
            return JobType.ALERT_ARTIFACT_GENERATE, payload, dedupe_key
        payload = {
            "content_ref": content_ref,
            "voice_profile_ref": profile_ref,
            "output_format": "wav",
            "config_generation": generation,
        }
        return JobType.TTS_SYNTHESIZE, payload, f"tts:{content_ref}:{profile_ref}"

    async def _wait_for_result(
        self,
        *,
        job_id: str,
        output_path: Path,
        deadline: dt.datetime,
        cancellation: asyncio.Event | None,
    ) -> None:
        job_service = self._job_service
        repository = self._repository
        if job_service is None or repository is None:
            raise WorkerJobUnavailable("worker job service is not bound")
        while True:
            await self._cancel_if_requested(job_service, job_id, cancellation)
            job = await asyncio.to_thread(repository.get, job_id)
            if job is None:
                raise WorkerJobUnavailable("admitted worker job disappeared")
            if job.status.value == "succeeded":
                await self._copy_committed_artifact(job, output_path)
                return
            if job.status.value in {"failed", "cancelled", "expired", "superseded"}:
                reason = job.error.code if job.error is not None else job.status.value
                raise WorkerJobUnavailable(f"worker synthesis did not complete: {reason}")
            if dt.datetime.now(dt.UTC) >= deadline:
                await asyncio.to_thread(job_service.reconcile)
                raise WorkerJobUnavailable("worker synthesis deadline expired")
            await asyncio.sleep(0.05)

    @staticmethod
    async def _cancel_if_requested(job_service: Any, job_id: str, cancellation: asyncio.Event | None) -> None:
        if cancellation is None or not cancellation.is_set():
            return
        await asyncio.to_thread(
            job_service.repository.request_cancellation,
            job_id,
            at=dt.datetime.now(dt.UTC),
        )
        raise WorkerJobUnavailable("worker synthesis was cancelled")

    async def _copy_committed_artifact(self, job: Any, output_path: Path) -> None:
        attempt_id = str(job.attempt_id or "")
        repository = self._repository
        if repository is None or self._active_root is None:
            raise WorkerJobUnavailable("worker repository is not bound")
        receipt = await asyncio.to_thread(repository.artifact_receipt, job.job_id, attempt_id)
        if receipt is None or receipt.disposition != "committed":
            raise WorkerJobUnavailable("worker result lacks a committed artifact receipt")
        source = self._active_root / receipt.target_key
        output_path = Path(output_path)
        output_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        temporary = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            await asyncio.to_thread(shutil.copyfile, source, temporary)
            await asyncio.to_thread(os.replace, temporary, output_path)
        finally:
            temporary.unlink(missing_ok=True)
