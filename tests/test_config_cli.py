from __future__ import annotations

import json
from pathlib import Path

from seasonalweather.cli.config import main

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "config/config.yaml"


def test_config_lint_valid_human_mode(capsys) -> None:
    assert main(["lint", "--config", str(EXAMPLE)]) == 0
    captured = capsys.readouterr()
    assert "succeeded" in captured.out
    assert captured.err == ""


def test_config_lint_invalid_human_mode(tmp_path: Path, capsys) -> None:
    candidate = tmp_path / "invalid.yaml"
    candidate.write_text("a: [\n", encoding="utf-8")

    assert main(["lint", "--config", str(candidate)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "yaml.syntax" in captured.err


def test_config_lint_json_is_one_clean_document(capsys) -> None:
    assert main(["lint", "--config", str(EXAMPLE), "--format", "json"]) == 0
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)

    assert parsed["valid"] is True
    assert captured.out.count("\n") == 1
    assert captured.err == ""


def test_config_lint_source_read_failure_is_structured(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "missing.yaml"
    assert main(["lint", "--config", str(missing), "--format", "json"]) == 1
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)

    assert parsed["issues"][0]["rule_id"] == "source.read"
    assert captured.err == ""


def test_config_lint_never_prints_secret_sentinel(tmp_path: Path, capsys) -> None:
    sentinel = "SENTINEL-CLI-SECRET"
    candidate = tmp_path / "secret.yaml"
    candidate.write_text(
        f"station:\n  access_token: {sentinel}\n",
        encoding="utf-8",
    )

    assert main(["lint", "--config", str(candidate)]) == 1
    captured = capsys.readouterr()
    assert sentinel not in captured.out
    assert sentinel not in captured.err
