from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_phase3_gate_runs_repository_checks_and_compose_validation() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "phase3-gate: check compose-check" in makefile


def test_phase3_procedure_covers_the_operator_only_boundaries() -> None:
    document = (ROOT / "docs/p3-08-production-migration.md").read_text(encoding="utf-8")

    for section in (
        "## Required inputs and preflight",
        "## Cutover procedure",
        "## Configuration reload during and after migration",
        "## TTS mode and worker checks",
        "## Rollback",
        "## Observability and failure handling",
        "## Phase 3 exit gate",
    ):
        assert section in document
    for criterion in (
        "Compose ordering",
        "Host reboot and service recreation preserve state",
        "Liquidsoap reads newly generated audio",
        "Image rollback is tested",
        "No service exceeds its declared authority/failure domain",
    ):
        assert criterion in document
