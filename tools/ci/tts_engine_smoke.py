"""Run one real synthesis inside a built optional-engine worker image."""

from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
import time
import wave
from array import array
from pathlib import Path

from seasonalweather.tts.local import SpfyHandler, VoiceTextPaulHandler
from seasonalweather.tts.models import LocalEngineOptions
from seasonalweather.tts.subprocess import run_bounded


def _verify_spfy_inventory(options: LocalEngineOptions, deadline: float) -> None:
    inventory = run_bounded(
        [options.spfy.executable, "--no-update-check", "--list-voices", "--json"],
        input_bytes=None,
        deadline=deadline,
        environment={
            "SPFY_VOICE_DIR": options.spfy.voice_dir,
            "SPFY_NO_UPDATE_CHECK": "1",
        },
    )
    payload = json.loads(inventory.stdout)
    voices = payload.get("voices") if isinstance(payload, dict) else None
    if not isinstance(voices, list):
        raise RuntimeError("spfy returned a malformed voice inventory")
    discovered = sorted(
        voice["name"] for voice in voices if isinstance(voice, dict) and isinstance(voice.get("name"), str)
    )
    print(json.dumps({"profile": "spfy", "discovered_voices": discovered}, sort_keys=True), file=sys.stderr)
    if options.voice.casefold() not in {voice.casefold() for voice in discovered}:
        raise RuntimeError(f"spfy smoke voice {options.voice!r} is unavailable; discovered={discovered!r}")


def _synthesize(profile: str, output_dir: Path) -> Path:
    deadline = time.monotonic() + 150.0
    if profile == "spfy":
        options = LocalEngineOptions(engine="spfy", voice="tom", rate_wpm=180)
        _verify_spfy_inventory(options, deadline)
        result = SpfyHandler().synthesize(
            '<speak>Seasonal Weather synthesis smoke test.<break time="250ms"/>Audio is operational.</speak>',
            options=options,
            output_dir=output_dir,
            deadline=deadline,
            cancellation=None,
        )
    else:
        result = VoiceTextPaulHandler().synthesize(
            "Seasonal Weather synthesis smoke test. Winds west at ten knots.",
            options=LocalEngineOptions(engine="voicetext_paul", voice="9", rate_wpm=165),
            output_dir=output_dir,
            deadline=deadline,
            cancellation=None,
        )
    return result.output_path


def _inspect_audio(profile: str, path: Path) -> dict[str, float | int | str]:
    with wave.open(str(path), "rb") as reader:
        channels = reader.getnchannels()
        sample_width = reader.getsampwidth()
        sample_rate = reader.getframerate()
        frame_count = reader.getnframes()
        frames = reader.readframes(frame_count)

    if channels < 1 or sample_width != 2 or sample_rate < 8_000 or frame_count < 1:
        raise RuntimeError(
            f"{profile} produced an unsupported WAV: channels={channels} width={sample_width} "
            f"rate={sample_rate} frames={frame_count}"
        )
    samples = array("h")
    samples.frombytes(frames)
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        raise RuntimeError(f"{profile} produced an empty PCM stream")

    peak = max(abs(sample) for sample in samples)
    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
    duration = frame_count / sample_rate
    if not 0.25 <= duration <= 30.0:
        raise RuntimeError(f"{profile} produced implausible duration: {duration:.3f}s")
    if peak < 128 or rms < 16.0:
        raise RuntimeError(f"{profile} produced silent or near-silent audio: peak={peak} rms={rms:.2f}")

    return {
        "profile": profile,
        "duration_seconds": round(duration, 3),
        "sample_rate_hz": sample_rate,
        "channels": channels,
        "peak": peak,
        "rms": round(rms, 2),
        "bytes": path.stat().st_size,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, choices=("spfy", "voicetext-paul"))
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix=f"{args.profile}-smoke-", dir="/tmp") as temporary:
        result = _inspect_audio(args.profile, _synthesize(args.profile, Path(temporary)))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
