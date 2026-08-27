from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _yaml(path: Path) -> dict[str, Any]:
    value = importlib.import_module("yaml").safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_openai_compatible_is_an_explicit_external_overlay() -> None:
    base = _yaml(ROOT / "compose.yaml")
    overlay = _yaml(ROOT / "compose.openai-compatible.yaml")

    assert not {"openai-tts", "openai-compatible-tts"}.intersection(base["services"])
    assert "OPENAI_API_KEY" not in str(base)
    assert set(overlay["services"]) == {"controller"}
    assert overlay["services"]["controller"]["secrets"] == [
        {
            "source": "OPENAI_COMPATIBLE_API_KEY",
            "target": "OPENAI_COMPATIBLE_API_KEY",
            "uid": "10001",
            "gid": "10001",
            "mode": "0400",
        }
    ]
    assert overlay["secrets"]["OPENAI_COMPATIBLE_API_KEY"]["file"] == (
        "${SEASONALWEATHER_SECRET_DIR:-./secrets}/OPENAI_COMPATIBLE_API_KEY"
    )


def test_openai_compatible_overlay_preserves_controller_only_secret_authority() -> None:
    overlay = _yaml(ROOT / "compose.openai-compatible.yaml")
    service = overlay["services"]["controller"]

    assert "volumes" not in service
    assert "environment" not in service
    assert "profiles" not in service
    assert "OPENAI_COMPATIBLE_API_KEY" not in str(overlay["services"].get("routine-worker", {}))
    assert "OPENAI_COMPATIBLE_API_KEY" not in str(overlay["services"].get("liquidsoap", {}))


def test_openai_compatible_documentation_keeps_provider_and_common_authority_split() -> None:
    documentation = (ROOT / "docs/p3-05-openai-compatible.md").read_text(encoding="utf-8")

    assert "/run/secrets/OPENAI_COMPATIBLE_API_KEY" in documentation
    assert "/audio/speech" in documentation
    assert "controller-owned P1-10 validation" in documentation
    assert "raw synthesis\ntext" in documentation
    assert "does not add a provider image" in documentation
