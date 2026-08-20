import pytest

from tools.quality.governance import validate_governed_record
from tools.quality.run_check import _compact_failure_output, checks


def test_ruff_format_output_is_counted_for_current_and_legacy_formats():
    count = checks()["format"].count

    assert count("unformatted: File would be reformatted\nunformatted: File would be reformatted\n") == 2
    assert count("Would reformat: seasonalweather/main.py\n") == 1


def test_json_quality_tools_fail_closed_on_invalid_output():
    with pytest.raises(ValueError, match="Bandit"):
        checks()["security"].count("not json")
    with pytest.raises(ValueError, match="Radon"):
        checks()["complexity"].count("not json")


def test_dependency_findings_are_counted_from_stderr():
    check = checks()["dependency"]

    assert check.count_stream == "stderr"
    assert check.count("requirements.txt: DEP002 'unused' defined as a dependency\n") == 1


def test_basedpyright_errors_are_counted_from_stdout():
    check = checks()["basedpyright"]

    assert check.count_stream == "stdout"
    assert check.count("file.py:1:1 - error: type mismatch\n") == 1


def test_quality_failure_output_is_bounded():
    output = "\n".join(f"diagnostic {index}" for index in range(100))

    compact = _compact_failure_output(output)

    assert len(compact.splitlines()) == 24
    assert "diagnostic lines suppressed" in compact


def test_governed_record_rejects_expired_review_date():
    record = {
        "owner": "maintainer",
        "rationale": "test",
        "scope": "test",
        "review_date": "2000-01-01",
        "removal_condition": "remove after test",
    }

    assert any("expired" in error for error in validate_governed_record(record, context="test"))
