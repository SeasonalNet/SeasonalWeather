"""Verify a quiet synthetic marker reaches the disposable Icecast stream."""

from __future__ import annotations

import array
import math
import subprocess
import sys
import wave
from typing import IO

from seasonalweather.liquidsoap_telnet import LiquidsoapTelnet

_RATE = 8000


def detect_tone(pcm: IO[bytes], frequency: int) -> float | None:
    sine = [math.sin(math.tau * frequency * sample / _RATE) for sample in range(_RATE)]
    cosine = [math.cos(math.tau * frequency * sample / _RATE) for sample in range(_RATE)]
    while len(window := pcm.read(2 * _RATE)) == 2 * _RATE:
        samples = array.array("h", window)
        real = sum(value * reference for value, reference in zip(samples, cosine, strict=True))
        imag = sum(value * reference for value, reference in zip(samples, sine, strict=True))
        amplitude = 2 * math.hypot(real, imag) / _RATE
        if amplitude > 500:
            return amplitude
    return None


def main() -> None:
    frequency = int(sys.argv[1])
    path = f"/var/lib/seasonalweather/artifacts/p4-gate-probe-{frequency}.wav"
    # Match routine audio to avoid changing decoder format at the probe boundary.
    clip_rate = 48000
    period = (round(3000 * math.sin(math.tau * frequency * sample / clip_rate)) for sample in range(clip_rate))
    stereo = array.array("h", (value for sample in period for value in (sample, sample)))
    with wave.open(path, "wb") as clip:
        clip.setparams((2, 2, clip_rate, 0, "NONE", "not compressed"))
        clip.writeframes(stereo.tobytes() * 12)
    command = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-rw_timeout",
        "6000000",
        "-i",
        # The Ogg mount starts a new logical stream at metadata boundaries.
        # Use the parallel MP3 mount so one decoder spans queued tracks.
        "http://icecast:8000/seasonalweather.mp3",
        "-t",
        "105",
        "-ac",
        "1",
        "-ar",
        str(_RATE),
        "-f",
        "s16le",
        "pipe:1",
    ]
    client = LiquidsoapTelnet("liquidsoap", 1234)
    with subprocess.Popen(command, stdout=subprocess.PIPE) as decoder:
        try:
            # The outage gate checks audible output, not routine FIFO latency.
            request_id = client.push_voice_alert(path)
            if request_id is None:
                raise SystemExit("Liquidsoap did not accept the synthetic voice-plane marker")
            assert decoder.stdout is not None
            amplitude = detect_tone(decoder.stdout, frequency)
            if amplitude is not None:
                print(f"Icecast decoded probe: {frequency} Hz, peak amplitude {amplitude:.0f}/32768")
                return
            if decoder.wait():
                raise SystemExit("Icecast stream decode failed")
        finally:
            if decoder.poll() is None:
                decoder.kill()
    snapshot = client.publication_snapshot([request_id])
    statuses = snapshot.get("statuses") if snapshot else None
    state = statuses.get(request_id, "absent") if isinstance(statuses, dict) else "unknown"
    raise SystemExit(f"No audible {frequency} Hz probe in Icecast audio (Liquidsoap request {request_id}: {state})")


if __name__ == "__main__":
    main()
