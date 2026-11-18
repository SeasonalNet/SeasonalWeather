from __future__ import annotations

import asyncio
import datetime as dt
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from seasonalweather.artifacts.audio_assets import AudioAssetService


def test_audio_upload_normalization_runs_off_the_event_loop(tmp_path: Path) -> None:
    service: Any = object.__new__(AudioAssetService)
    service._repository = SimpleNamespace(upsert_asset=lambda _metadata: None)
    service._asset_dir = lambda: tmp_path
    service._maximum_bytes = lambda: 1024
    service._expiry_seconds = lambda: 300
    service._now_utc = lambda: dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    caller = threading.get_ident()
    calls: list[int] = []

    def normalize(**_kwargs: Any) -> dict[str, Any]:
        calls.append(threading.get_ident())
        return {
            "duration_seconds": 1.0,
            "sample_rate_hz": 48_000,
            "channels": 2,
            "frames": 48_000,
            "sample_width_bytes": 2,
        }

    service._normalize = normalize
    service._sha256_file = lambda _path: "digest"

    result = asyncio.run(
        service.stage_upload(
            filename="upload.wav",
            content_type="audio/wav",
            data=b"RIFF",
            actor="test",
        )
    )

    assert len(calls) == 1 and calls[0] != caller
    assert result["sha256"] == "digest"


def test_cancelled_upload_releases_files_after_normalization(tmp_path: Path) -> None:
    service: Any = object.__new__(AudioAssetService)
    upserts: list[object] = []
    service._repository = SimpleNamespace(upsert_asset=upserts.append)
    service._asset_dir = lambda: tmp_path
    service._maximum_bytes = lambda: 1024

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()

        def normalize(*, source: Path, destination: Path) -> dict[str, Any]:
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5)
            destination.write_bytes(source.read_bytes())
            return {}

        service._normalize = normalize
        task = asyncio.create_task(
            service.stage_upload(filename="upload.wav", content_type="audio/wav", data=b"RIFF", actor="test")
        )
        try:
            await asyncio.wait_for(started.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert list(tmp_path.glob("*.upload.wav"))
        finally:
            release.set()
        async with asyncio.timeout(5):
            while list(tmp_path.iterdir()):
                await asyncio.sleep(0.01)

    asyncio.run(scenario())
    assert not upserts
