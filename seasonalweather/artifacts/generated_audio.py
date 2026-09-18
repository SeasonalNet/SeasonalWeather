"""Canonical configuration-derived policy for generated PCM WAV artifacts."""

from __future__ import annotations

import math
from dataclasses import dataclass

MINIMUM_GENERATED_AUDIO_SECONDS = 900.0
DEFAULT_GENERATED_AUDIO_SECONDS = MINIMUM_GENERATED_AUDIO_SECONDS
GENERATED_AUDIO_CHANNELS = 2
GENERATED_AUDIO_SAMPLE_WIDTH_BYTES = 2
WAV_CONTAINER_ALLOWANCE_BYTES = 4096


def normalize_generated_duration(value: object | None) -> tuple[float, bool]:
    """Return the duration floor and whether the configured value was clamped."""

    if value is None:
        return DEFAULT_GENERATED_AUDIO_SECONDS, False
    if isinstance(value, bool):
        raise ValueError("audio.generated_max_duration_seconds must be a finite number")
    if not isinstance(value, (str, int, float)):
        raise ValueError("audio.generated_max_duration_seconds must be a finite number")
    try:
        duration = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("audio.generated_max_duration_seconds must be a finite number") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("audio.generated_max_duration_seconds must be a finite positive number")
    if duration < MINIMUM_GENERATED_AUDIO_SECONDS:
        return MINIMUM_GENERATED_AUDIO_SECONDS, True
    return duration, False


def generated_wav_maximum_bytes(
    *,
    sample_rate_hz: int,
    maximum_duration_seconds: float,
    channels: int = GENERATED_AUDIO_CHANNELS,
    sample_width_bytes: int = GENERATED_AUDIO_SAMPLE_WIDTH_BYTES,
) -> int:
    """Derive a conservative PCM payload plus bounded WAV container allowance."""

    if sample_rate_hz < 1 or channels < 1 or sample_width_bytes < 1:
        raise ValueError("generated WAV format values must be positive")
    duration, _clamped = normalize_generated_duration(maximum_duration_seconds)
    frames = math.ceil(duration * sample_rate_hz)
    return frames * channels * sample_width_bytes + WAV_CONTAINER_ALLOWANCE_BYTES


@dataclass(frozen=True)
class GeneratedAudioPolicy:
    """One policy shared by synthesis, workers, and controller artifacts."""

    sample_rate_hz: int
    maximum_duration_seconds: float = DEFAULT_GENERATED_AUDIO_SECONDS
    channels: int = GENERATED_AUDIO_CHANNELS
    sample_width_bytes: int = GENERATED_AUDIO_SAMPLE_WIDTH_BYTES

    def __post_init__(self) -> None:
        duration, _clamped = normalize_generated_duration(self.maximum_duration_seconds)
        object.__setattr__(self, "maximum_duration_seconds", duration)
        generated_wav_maximum_bytes(
            sample_rate_hz=self.sample_rate_hz,
            maximum_duration_seconds=duration,
            channels=self.channels,
            sample_width_bytes=self.sample_width_bytes,
        )

    @property
    def maximum_bytes(self) -> int:
        return generated_wav_maximum_bytes(
            sample_rate_hz=self.sample_rate_hz,
            maximum_duration_seconds=self.maximum_duration_seconds,
            channels=self.channels,
            sample_width_bytes=self.sample_width_bytes,
        )
