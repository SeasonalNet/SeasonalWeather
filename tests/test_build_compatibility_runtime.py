from __future__ import annotations

from dataclasses import replace

import pytest

from seasonalweather.api import server as api_server
from seasonalweather.build_metadata import current_build_info
from seasonalweather.build_metadata.compatibility import (
    BuildCompatibilityError,
    check_runtime_compatibility,
    ensure_runtime_compatibility,
)
from seasonalweather.diagnostics.bindings import FOUNDATION_CODES


def test_build_compatibility_rejects_unusable_protocol_identity() -> None:
    info = replace(current_build_info(), swwp_protocol_versions=(2,))

    result = check_runtime_compatibility(info, role="controller")

    assert not result.compatible
    assert any(finding.field == "swwp_protocol_versions" for finding in result.incompatible_findings)
    with pytest.raises(BuildCompatibilityError):
        ensure_runtime_compatibility(info, role="controller")


def test_controller_startup_emits_build_compatibility_diagnostic(monkeypatch, caplog) -> None:
    incompatible = replace(current_build_info(), image_profile="routine-worker")
    monkeypatch.setattr(api_server, "current_build_info", lambda: incompatible)

    assert api_server.main(["--config", "unused"]) == 1

    record = next(item for item in caplog.records if item.getMessage().startswith("Build identity is incompatible"))
    assert record.code == FOUNDATION_CODES["build.compatibility_rejected"]
    assert record.event == "build_compatibility_rejected"
