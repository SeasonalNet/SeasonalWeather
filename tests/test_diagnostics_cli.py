from __future__ import annotations

import json
from pathlib import Path

import pytest

from seasonalweather.diagnostics.cli import main
from seasonalweather.diagnostics.loader import load_catalog


def test_diagnostics_list_human_and_json_are_deterministic(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["list"]) == 0
    human = capsys.readouterr()
    assert "SWCFG1001" in human.out
    assert human.err == ""

    assert main(["list", "--format", "json"]) == 0
    machine = capsys.readouterr()
    parsed = json.loads(machine.out)
    assert parsed["diagnostics"][0]["code"] == "SWCFG1001"
    assert machine.out.count("\n") == 1
    assert machine.err == ""


def test_diagnostics_namespace_listing_includes_reserved_entries(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["list", "--namespaces", "--format", "json"]) == 0
    captured = capsys.readouterr()
    entries = {item["namespace"]: item for item in json.loads(captured.out)["namespaces"]}
    assert entries["SWERN"]["state"] == "active"
    assert entries["SWCACHE"]["state"] == "reserved"
    assert entries["SWREDIS"]["state"] == "reserved"


def test_every_active_code_explains_in_human_and_json(capsys: pytest.CaptureFixture[str]) -> None:
    for definition in load_catalog().definitions:
        code = str(definition.code)
        assert main(["explain", code]) == 0
        human = capsys.readouterr()
        assert f"Code: {code}" in human.out
        assert human.err == ""

        assert main(["explain", code, "--format", "json"]) == 0
        machine = capsys.readouterr()
        assert json.loads(machine.out)["diagnostic"]["code"] == code
        assert machine.err == ""


@pytest.mark.parametrize(
    ("code", "kind"),
    [
        ("not-a-code", "unknown_namespace"),
        ("SWCFG1999", "unknown"),
        ("SWCACHE1001", "reserved_namespace"),
    ],
)
def test_explain_errors_are_bounded_and_never_leak_tracebacks(
    code: str,
    kind: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["explain", code]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert kind in captured.err
    assert "Traceback" not in captured.err


def test_diagnostics_export_cli_uses_clean_temporary_tree(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    destination = tmp_path / "usr/share/seasonalweather/diagnostics"
    assert main(["export", "--output", str(destination)]) == 0
    captured = capsys.readouterr()
    assert destination.joinpath("catalog.json").is_file()
    assert captured.err == ""


def test_diagnostics_argparse_usage_error_is_exit_two() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["explain"])
    assert exc_info.value.code == 2
