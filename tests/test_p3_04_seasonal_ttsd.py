from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_seasonal_ttsd_is_an_explicit_external_overlay() -> None:
    base = _yaml(ROOT / "compose.yaml")
    overlay = _yaml(ROOT / "compose.seasonal-ttsd.yaml")

    assert "seasonal-ttsd" not in base["services"]
    assert set(overlay["services"]) == {"controller"}
    assert overlay["services"]["controller"]["secrets"] == [
        {
            "source": "SEASONAL_TTSD_CLIENT_CREDENTIAL",
            "target": "SEASONAL_TTSD_CLIENT_CREDENTIAL",
            "uid": "10001",
            "gid": "10001",
            "mode": "0400",
        }
    ]
    assert overlay["secrets"]["SEASONAL_TTSD_CLIENT_CREDENTIAL"]["file"] == (
        "${SEASONALWEATHER_SECRET_DIR:-./secrets}/SEASONAL_TTSD_CLIENT_CREDENTIAL"
    )


def test_seasonal_ttsd_overlay_preserves_controller_only_secret_authority() -> None:
    overlay = _yaml(ROOT / "compose.seasonal-ttsd.yaml")
    service = overlay["services"]["controller"]

    assert "volumes" not in service
    assert "environment" not in service
    assert "profiles" not in service
    assert "SEASONAL_TTSD_CLIENT_CREDENTIAL" not in str(overlay["services"].get("routine-worker", {}))
