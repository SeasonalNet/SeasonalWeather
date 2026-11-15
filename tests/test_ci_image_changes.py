from tools.ci.image_changes import classify_paths, gate_for_paths, requires_image_gate


def test_image_gate_runs_for_image_inputs() -> None:
    assert requires_image_gate(["Dockerfile.worker"])
    assert requires_image_gate(["seasonalweather/worker/cli.py"])
    assert requires_image_gate(["tools/samegen/src/main.rs"])
    assert requires_image_gate(["tools/quality/phase2_exit_gate.py"])
    assert requires_image_gate(["scripts/wrappers/dectalk-text2wav"])


def test_image_gate_skips_non_image_changes() -> None:
    assert not requires_image_gate(["docs/operator-guide.md"])
    assert not requires_image_gate(["tests/test_alerts.py"])
    assert not requires_image_gate(["CONTRIBUTING.md"])


def test_change_categories_select_the_smallest_gate() -> None:
    assert classify_paths(["README.md", "docs/ci-performance.md"]) == {"docs"}
    assert gate_for_paths(["README.md", "docs/ci-performance.md"]) == (False, False)
    assert classify_paths(["tests/test_alerts.py", "quality/exceptions.toml"]) == {"tests", "quality"}
    assert gate_for_paths(["tests/test_alerts.py", "quality/exceptions.toml"]) == (True, False)
    assert gate_for_paths(["seasonalweather/main.py", "docs/notes.md"]) == (True, True)
    assert gate_for_paths([".github/workflows/ci.yml"]) == (True, False)


def test_unknown_changes_fail_closed_to_the_full_gate() -> None:
    assert classify_paths(["new-runtime-input.bin"]) == {"unknown"}
    assert gate_for_paths(["new-runtime-input.bin"]) == (True, True)
    assert gate_for_paths([], force=True) == (True, True)
