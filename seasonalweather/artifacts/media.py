"""Bounded standard-library WAV inspection for controller-owned claimed bytes."""

from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path

from .models import MediaMetadata


@dataclass(frozen=True)
class WavPolicy:
    maximum_duration_seconds: float = 3600.0
    allowed_sample_widths: tuple[int, ...] = (2,)
    allowed_channels: tuple[int, ...] = (1, 2)


def validate_wav(path: Path, claimed: MediaMetadata, *, policy: WavPolicy | None = None) -> MediaMetadata:
    policy = policy or WavPolicy()
    computed = _read_wav(path, policy)
    if not _matches_claim(claimed, computed):
        raise ValueError("claimed WAV metadata does not match controller validation")
    return computed


def inspect_wav(path: Path, *, policy: WavPolicy | None = None) -> MediaMetadata:
    """Inspect WAV bytes using the same authoritative P1-10 parser."""
    return _read_wav(path, policy or WavPolicy())


def _read_wav(path: Path, policy: WavPolicy) -> MediaMetadata:
    try:
        with wave.open(str(path), "rb") as reader:
            channels = reader.getnchannels()
            width = reader.getsampwidth()
            rate = reader.getframerate()
            frames = reader.getnframes()
            compression = reader.getcomptype()
            if (
                compression != "NONE"
                or channels not in policy.allowed_channels
                or width not in policy.allowed_sample_widths
            ):
                raise ValueError("WAV encoding is not permitted")
            if rate < 8000 or rate > 192000 or frames < 1:
                raise ValueError("WAV metadata is out of bounds")
            duration = frames / rate
            if duration > policy.maximum_duration_seconds:
                raise ValueError("WAV duration exceeds policy")
            decoded = reader.readframes(frames)
            if len(decoded) != frames * channels * width:
                raise ValueError("WAV data is truncated")
    except (wave.Error, EOFError) as exc:
        raise ValueError("invalid or truncated WAV artifact") from exc
    return MediaMetadata(
        media_type="audio/wav",
        encoding="pcm_s",
        sample_width_bytes=width,
        sample_rate_hz=rate,
        channels=channels,
        frame_count=frames,
        duration_seconds=round(duration, 6),
    )


def _matches_claim(claimed: MediaMetadata, computed: MediaMetadata) -> bool:
    return claimed.media_type == computed.media_type and not any(
        given is not None and given != actual
        for given, actual in (
            (claimed.sample_rate_hz, computed.sample_rate_hz),
            (claimed.channels, computed.channels),
            (claimed.frame_count, computed.frame_count),
            (claimed.encoding, computed.encoding),
            (claimed.sample_width_bytes, computed.sample_width_bytes),
            (
                round(claimed.duration_seconds, 6) if claimed.duration_seconds is not None else None,
                computed.duration_seconds,
            ),
        )
    )
