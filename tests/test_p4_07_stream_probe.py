import math
import struct
from io import BytesIO

import pytest

from tools.p4_gate_probe import detect_tone


def _tone(frequency: int) -> bytes:
    return b"".join(
        struct.pack("<h", round(3000 * math.sin(math.tau * frequency * sample / 8000))) for sample in range(8000)
    )


def test_detects_checkpoint_tone_behind_audio_in_queue() -> None:
    preceding_audio = _tone(440) + bytes(16_000 * 75)
    assert detect_tone(BytesIO(preceding_audio + _tone(770)), 770) == pytest.approx(3000, abs=1)


def test_silence_and_previous_checkpoint_do_not_pass() -> None:
    assert detect_tone(BytesIO(bytes(16_000) + _tone(440)), 770) is None
