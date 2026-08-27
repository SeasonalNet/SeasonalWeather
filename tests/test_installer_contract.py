from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_bootstrap_has_interactive_profiles_and_tui_fallback() -> None:
    script = (ROOT / "scripts/00-bootstrap.sh").read_text()

    assert "configure_interactive_features" in script
    assert "dialog/whiptail not found, falling back to stdout/stdin and disabling Terminal User Interface" in script
    assert "SEASONAL_INSTALL_PROFILE=minimal|standard|voicetext-paul|dectalk|custom" in script
    assert "SEASONAL_PIPER=0|1" in script


def test_bootstrap_prunes_optional_tts_from_base_packages() -> None:
    script = (ROOT / "scripts/00-bootstrap.sh").read_text()

    base_section = script.split("BASE_APT_PACKAGES=(", 1)[1].split(")", 1)[0]
    assert "espeak-ng" not in base_section
    assert "festival" not in base_section
    assert "festvox-kallpc16k" not in base_section
    assert "libasound2-dev" not in base_section
    assert "SEASONAL_PIPER:-0" in script
    assert "--group piper" in script


def test_configure_assistant_script_is_shell_valid() -> None:
    subprocess.run(["bash", "-n", str(ROOT / "scripts/configure-seasonalweather")], check=True)


def test_configure_defaults_to_preserving_existing_config(monkeypatch, tmp_path) -> None:
    import io
    import sys

    from seasonalweather.cli import configure as configure_mod

    existing = tmp_path / "config.yaml"
    output = tmp_path / "config.yaml.new"
    existing.write_text(
        """
station:
  name: ExistingStation
  service_area_name: Existing area
  timezone: America/New_York
  deployment_type: land_marine
stream:
  icecast_mount: seasonalweather.ogg
nwws:
  enabled: true
  allowed_wfos: [KLWX]
observations:
  stations: [KDCA]
api:
  allow_remote: false
tts:
  backend: voicetext_paul
  voice: '9'
  rate_wpm: 165
logs:
  runtime:
    color: never
""".lstrip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(configure_mod, "validate_candidate", lambda path: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n" * 16))
    rc = configure_mod.main(
        [
            "--tui",
            "never",
            "--config",
            str(existing),
            "--output",
            str(output),
        ]
    )

    assert rc == 0
    rendered = output.read_text(encoding="utf-8")
    assert "backend: voicetext_paul" in rendered
    assert "deployment_type: land_marine" in rendered


def test_configure_profile_can_intentionally_override_existing_config(monkeypatch, tmp_path) -> None:
    import io
    import sys

    from seasonalweather.cli import configure as configure_mod

    existing = tmp_path / "config.yaml"
    output = tmp_path / "config.yaml.new"
    existing.write_text(
        """
station:
  name: ExistingStation
  service_area_name: Existing area
  timezone: America/New_York
  deployment_type: land_marine
stream:
  icecast_mount: seasonalweather.ogg
nwws:
  enabled: true
  allowed_wfos: [KLWX]
observations:
  stations: [KDCA]
api:
  allow_remote: false
tts:
  backend: voicetext_paul
  voice: '9'
  rate_wpm: 165
logs:
  runtime:
    color: never
""".lstrip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(configure_mod, "validate_candidate", lambda path: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n" * 16))
    rc = configure_mod.main(
        [
            "--tui",
            "never",
            "--profile",
            "standard",
            "--config",
            str(existing),
            "--output",
            str(output),
        ]
    )

    assert rc == 0
    rendered = output.read_text(encoding="utf-8")
    assert "backend: espeak-ng" in rendered
