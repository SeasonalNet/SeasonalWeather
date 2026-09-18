from __future__ import annotations

import pytest

from seasonalweather.artifacts.generated_audio import (
    GeneratedAudioPolicy,
    generated_wav_maximum_bytes,
    normalize_generated_duration,
)
from seasonalweather.tts.models import SynthesisOutputPolicy


def test_generated_audio_policy_defaults_to_fifteen_minutes() -> None:
    policy = GeneratedAudioPolicy(sample_rate_hz=48_000)

    assert policy.maximum_duration_seconds == 900.0
    assert policy.maximum_bytes == 172_804_096
    assert SynthesisOutputPolicy().maximum_duration_seconds == 900.0
    assert SynthesisOutputPolicy().maximum_bytes >= policy.maximum_bytes


@pytest.mark.parametrize("value", (1, 899, 899.999))
def test_generated_audio_duration_below_floor_is_clamped(value: float) -> None:
    assert normalize_generated_duration(value) == (900.0, True)


def test_generated_audio_duration_at_or_above_floor_is_preserved() -> None:
    assert normalize_generated_duration(None) == (900.0, False)
    assert normalize_generated_duration(900) == (900.0, False)
    assert normalize_generated_duration(1800) == (1800.0, False)
    assert generated_wav_maximum_bytes(sample_rate_hz=48_000, maximum_duration_seconds=1800) == 345_604_096
    output = SynthesisOutputPolicy(maximum_duration_seconds=1800, maximum_bytes=1)
    assert output.maximum_bytes == 345_604_096


@pytest.mark.parametrize("value", (True, 0, -1, float("inf"), float("nan"), "broken"))
def test_generated_audio_duration_rejects_malformed_values(value: object) -> None:
    with pytest.raises(ValueError):
        normalize_generated_duration(value)
