"""Controller-owned uploaded-audio staging and asset references."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import subprocess
import uuid
import wave
from pathlib import Path
from typing import Any

from seasonalweather.application.errors import ControlError, DependencyUnavailableError, NotFoundError
from seasonalweather.database.assets import AudioAssetRepository
from seasonalweather.validation.admission import validate_wav_upload


class AudioAssetService:
    """Own bounded upload normalization and controller asset lookup."""

    def __init__(self, orchestrator: Any) -> None:
        self.orch = orchestrator
        db = getattr(orchestrator, "database", None)
        self._repository = AudioAssetRepository(db) if db is not None else None

    def _now_utc(self) -> dt.datetime:
        return dt.datetime.now(dt.UTC)

    def _work_paths(self) -> tuple[Path, Path, Path, Path]:
        return self.orch._paths()

    def _asset_dir(self) -> Path:
        work_dir, _audio_dir, _cache_dir, _logs_dir = self._work_paths()
        path = work_dir / "api" / "uploads"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _sha256_file(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _maximum_bytes(self) -> int:
        return max(1024, int(self.orch.cfg.api.audio_max_bytes))

    def upload_max_bytes(self) -> int:
        return self._maximum_bytes()

    def _maximum_duration(self) -> float:
        return max(1.0, min(float(self.orch.cfg.api.audio_max_seconds), 3600.0))

    def _expiry_seconds(self) -> int:
        return max(300, min(int(self.orch.cfg.api.audio_ttl_seconds), 7 * 86400))

    def _sample_rate(self) -> int:
        try:
            return int(getattr(self.orch.cfg.audio, "sample_rate", 16000) or 16000)
        except Exception:
            return 16000

    def _ffmpeg(self) -> str:
        return self.orch.cfg.api.ffmpeg_bin or "ffmpeg"

    def _require_ffmpeg(self) -> str:
        executable = self._ffmpeg()
        resolved = shutil.which(executable)
        if not resolved:
            raise DependencyUnavailableError(
                "ffmpeg_missing",
                "ffmpeg is required for uploaded-audio normalization but was not found in PATH.",
                details={"binary": executable},
            )
        return resolved

    def _probe(self, path: Path) -> dict[str, Any]:
        try:
            with wave.open(str(path), "rb") as wav:
                channels = int(wav.getnchannels())
                sample_rate_hz = int(wav.getframerate())
                frames = int(wav.getnframes())
                sample_width_bytes = int(wav.getsampwidth())
        except wave.Error as exc:
            raise ControlError(
                "invalid_wav", "Normalized WAV could not be parsed.", details={"path": str(path)}
            ) from exc

        duration_seconds = float(frames) / float(sample_rate_hz or 1)
        expected_rate = self._sample_rate()
        if channels != 2 or sample_width_bytes != 2 or sample_rate_hz != expected_rate:
            raise ControlError(
                "normalized_wav_mismatch",
                "Normalized WAV does not match the station playout format.",
                details={
                    "path": str(path),
                    "channels": channels,
                    "sample_width_bytes": sample_width_bytes,
                    "sample_rate_hz": sample_rate_hz,
                    "expected_channels": 2,
                    "expected_sample_width_bytes": 2,
                    "expected_sample_rate_hz": expected_rate,
                },
            )
        if duration_seconds <= 0.0:
            raise ControlError("invalid_wav_duration", "Uploaded WAV has zero duration.")
        if duration_seconds > self._maximum_duration():
            raise ControlError(
                "wav_too_long",
                "Uploaded WAV exceeds the configured duration limit.",
                details={"max_seconds": self._maximum_duration(), "duration_seconds": duration_seconds},
            )
        return {
            "duration_seconds": duration_seconds,
            "sample_rate_hz": sample_rate_hz,
            "channels": channels,
            "frames": frames,
            "sample_width_bytes": sample_width_bytes,
        }

    def _normalize(self, *, source: Path, destination: Path) -> dict[str, Any]:
        command = [
            self._require_ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-map_metadata",
            "-1",
            "-vn",
            "-ac",
            "2",
            "-ar",
            str(self._sample_rate()),
            "-c:a",
            "pcm_s16le",
            str(destination),
        ]
        process = subprocess.run(command, capture_output=True, text=True)
        if process.returncode != 0 or not destination.exists():
            detail = (process.stderr or process.stdout or "").strip()
            raise ControlError(
                "audio_normalization_failed",
                "Uploaded WAV could not be normalized to the station playout format.",
                details={"stderr": detail[-1200:]},
            )
        return self._probe(destination)

    async def stage_upload(self, *, filename: str, content_type: str, data: bytes, actor: str) -> dict[str, Any]:
        if self._repository is None:
            raise ControlError(
                "database_unavailable",
                "Uploaded audio requires the controller-owned database.",
                status_code=503,
            )
        rejection = validate_wav_upload(
            filename=filename,
            content_type=content_type,
            size_bytes=len(data),
            maximum_bytes=self._maximum_bytes(),
        )
        if rejection is not None:
            details = {"max_bytes": rejection.maximum_bytes} if rejection.maximum_bytes is not None else None
            raise ControlError(
                rejection.reason_code,
                rejection.message,
                status_code=rejection.status_code,
                details=details,
            )

        clean_filename = Path(filename or "upload.wav").name or "upload.wav"
        asset_id = f"aud_{uuid.uuid4().hex[:20]}"
        asset_dir = self._asset_dir()
        source_path = asset_dir / f"{asset_id}.upload.wav"
        wav_path = asset_dir / f"{asset_id}.wav"
        source_path.write_bytes(data)
        try:
            probe = self._normalize(source=source_path, destination=wav_path)
        finally:
            source_path.unlink(missing_ok=True)

        uploaded_at = self._now_utc().replace(microsecond=0)
        expires_at = uploaded_at + dt.timedelta(seconds=self._expiry_seconds())
        metadata = {
            "asset_id": asset_id,
            "filename": clean_filename,
            "content_type": content_type or "audio/wav",
            "duration_seconds": round(float(probe["duration_seconds"]), 3),
            "sample_rate_hz": int(probe["sample_rate_hz"]),
            "target_sample_rate_hz": self._sample_rate(),
            "channels": int(probe["channels"]),
            "sample_width_bytes": int(probe["sample_width_bytes"]),
            "frames": int(probe["frames"]),
            "normalized": True,
            "sha256": self._sha256_file(wav_path),
            "uploaded_at": uploaded_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "path": str(wav_path),
            "actor": actor,
        }
        self._repository.upsert_asset(metadata)
        return {key: value for key, value in metadata.items() if key not in {"path", "actor"}}

    def load(self, asset_id: str) -> dict[str, Any]:
        if self._repository is None:
            raise ControlError(
                "database_unavailable", "Audio assets require the controller-owned database.", status_code=503
            )
        metadata = self._repository.get_asset(asset_id)
        if metadata is None:
            legacy_path = self._asset_dir() / f"{asset_id}.json"
            if legacy_path.exists():
                try:
                    legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
                    if isinstance(legacy, dict):
                        metadata = legacy
                        self._repository.upsert_asset(metadata)
                        legacy_path.unlink()
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    metadata = None
        if metadata is None:
            raise NotFoundError(
                "audio_asset_not_found", "Audio asset was not found.", details={"audio_asset_id": asset_id}
            )

        expires_at = dt.datetime.fromisoformat(metadata["expires_at"])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=dt.UTC)
        if self._now_utc() > expires_at.astimezone(dt.UTC):
            raise NotFoundError("audio_asset_expired", "Audio asset has expired.", details={"audio_asset_id": asset_id})
        wav_path = Path(metadata["path"])
        if not wav_path.exists():
            raise NotFoundError("audio_asset_missing_file", "Audio asset metadata exists but the WAV file is missing.")
        return metadata

    def copy_to(self, *, asset_id: str, destination: Path) -> dict[str, Any]:
        metadata = self.load(asset_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(metadata["path"]), destination)
        return metadata
