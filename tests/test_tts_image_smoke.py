from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_tts_image_smoke_runs_real_engines_under_hardened_containers() -> None:
    script = (ROOT / "tools/ci/tts_image_smoke.sh").read_text(encoding="utf-8")

    assert "seasonalweather-worker:spfy" in script
    assert "seasonalweather-worker:voicetext-paul" in script
    assert "--interactive" in script
    assert "--read-only" in script
    assert "--network none" in script
    assert "--cap-drop ALL" in script
    assert "--security-opt no-new-privileges" in script
    assert "--profile spfy" in script
    assert "--profile voicetext-paul" in script
    assert "Xvfb :99" in script
    assert "type=bind" not in script
    assert "python - --profile voicetext-paul" in script
    assert '"${spfy_image}" - --profile spfy < "${smoke_script}"' in script


def test_tts_image_smoke_decodes_and_rejects_silent_wavs() -> None:
    smoke = (ROOT / "tools/ci/tts_engine_smoke.py").read_text(encoding="utf-8")

    assert "SpfyHandler().synthesize(" in smoke
    assert "VoiceTextPaulHandler().synthesize(" in smoke
    assert 'wave.open(str(path), "rb")' in smoke
    assert "peak < 128 or rms < 16.0" in smoke
    assert 'voice="tom"' in smoke
    assert '"--list-voices", "--json"' in smoke
    assert '"discovered_voices": discovered' in smoke
