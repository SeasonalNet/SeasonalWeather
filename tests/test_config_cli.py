from __future__ import annotations

import json
from pathlib import Path

import pytest

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
    assert "error[SWCFG1003]" in captured.err
    assert "diagnostics explain SWCFG1003" in captured.err


def test_config_lint_json_is_one_clean_document(capsys) -> None:
    assert main(["lint", "--config", str(EXAMPLE), "--format", "json"]) == 0
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)

    assert parsed["valid"] is True
    assert [stage["stage"] for stage in parsed["stages"]] == [
        "parse",
        "schema",
        "semantic",
        "compatibility",
        "deprecation",
        "advisory",
        "preflight",
    ]
    assert parsed["stages"][-1]["state"] == "skipped"
    assert parsed["preflight_ready"] is False
    assert parsed["summary"]["acceptable_for_reload_decision"] is False
    assert captured.out.count("\n") == 1
    assert captured.err == ""


def test_config_lint_rejects_selected_remote_semantic_invalidity(tmp_path: Path, capsys) -> None:
    candidate = tmp_path / "selected-remote.yaml"
    text = EXAMPLE.read_text(encoding="utf-8")
    text = text.replace('  backend: "local"', '  backend: "seasonal_ttsd"', 1)
    text = text.replace(
        '  seasonal_ttsd:\n    base_url: ""',
        '  seasonal_ttsd:\n    base_url: "https://tts.example.test/?token=secret"',
        1,
    )
    text = text.replace('    client_credential_file: ""', '    client_credential_file: "/run/credentials/client"', 1)
    candidate.write_text(text, encoding="utf-8")

    assert main(["lint", "--config", str(candidate), "--format", "json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is False
    assert any(issue["path"]["pointer"] == "/tts/seasonal_ttsd/base_url" for issue in payload["issues"])


@pytest.mark.parametrize(
    "base_url",
    [
        "https://[bad",
        "https://tts.example.test:abc",
        "https://tts.example.test:99999",
        "https://tts.example.test:0",
    ],
)
def test_config_lint_rejects_malformed_remote_urls_without_traceback(tmp_path: Path, capsys, base_url: str) -> None:
    text = EXAMPLE.read_text(encoding="utf-8")
    text = text.replace('  backend: "local"', '  backend: "seasonal_ttsd"', 1)
    text = text.replace('  seasonal_ttsd:\n    base_url: ""', f'  seasonal_ttsd:\n    base_url: "{base_url}"', 1)
    text = text.replace('    client_credential_file: ""', '    client_credential_file: "/run/credentials/client"', 1)
    candidate = tmp_path / "malformed-url.yaml"
    candidate.write_text(text, encoding="utf-8")

    assert main(["lint", "--config", str(candidate), "--format", "json"]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["valid"] is False
    assert any(issue["path"]["pointer"] == "/tts/seasonal_ttsd/base_url" for issue in payload["issues"])
    assert "Traceback" not in captured.out
    assert captured.err == ""


def test_config_lint_source_read_failure_is_structured(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "missing.yaml"
    assert main(["lint", "--config", str(missing), "--format", "json"]) == 1
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)

    assert parsed["issues"][0]["rule_id"] == "compiler.parse"
    assert parsed["issues"][0]["diagnostic_rule_id"] == "source.read"
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


def test_config_lint_preflight_is_explicit_read_only_and_does_not_create_database(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    executable = tmp_path / "espeak-ng"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    text = EXAMPLE.read_text(encoding="utf-8")
    database = tmp_path / "must-not-be-created.sqlite3"
    text = text.replace(
        'path: "/var/lib/seasonalweather/state/seasonalweather.sqlite3"',
        f'path: "{database}"',
    )
    for configured in (
        "/var/lib/seasonalweather/artifacts/audio",
        "/var/lib/seasonalweather/state/cache",
        "/var/lib/seasonalweather/state",
        "/var/lib/seasonalweather/jobs",
        "/var/lib/seasonalweather/artifacts",
        "/var/lib/seasonalweather/audio",
        "/var/lib/seasonalweather/cache",
        "/etc/seasonalweather",
        "/var/log/seasonalweather",
        "/var/lib/seasonalweather",
    ):
        text = text.replace(f'"{configured}"', f'"{runtime}"')
    candidate = tmp_path / "candidate.yaml"
    candidate.write_text(text, encoding="utf-8")

    assert main(["lint", "--config", str(candidate), "--format", "json", "--preflight"]) == 0
    parsed = json.loads(capsys.readouterr().out)

    assert parsed["preflight_ready"] is True
    assert parsed["stages"][-1]["state"] == "completed"
    assert not database.exists()
