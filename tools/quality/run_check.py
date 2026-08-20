from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

from tools.quality.governance import ROOT, load_toml


@dataclass(frozen=True)
class Check:
    command: tuple[str, ...]
    count: Callable[[str], int]
    count_stream: str = "stdout"


def _line_count(pattern: str) -> Callable[[str], int]:
    regex = re.compile(pattern, re.MULTILINE)
    return lambda output: len(regex.findall(output))


def _bandit_count(output: str) -> int:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ValueError("Bandit did not produce valid JSON") from exc
    if not isinstance(payload.get("results"), list):
        raise ValueError("Bandit JSON does not contain a results list")
    return len(payload.get("results", []))


def _radon_count(output: str) -> int:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ValueError("Radon did not produce valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Radon JSON is not an object")
    return sum(
        1 for findings in payload.values() for finding in findings if finding.get("rank") in {"C", "D", "E", "F"}
    )


def checks() -> dict[str, Check]:
    python = sys.executable
    return {
        "format": Check(
            (python, "-m", "ruff", "format", "--check", "seasonalweather", "tests", "tools"),
            _line_count(r"^(?:Would reformat: .+|unformatted: File would be reformatted)$"),
        ),
        "lint": Check(
            (python, "-m", "ruff", "check", "--output-format=concise", "seasonalweather", "tests", "tools"),
            _line_count(r"^[^:\n]+:\d+:\d+: "),
        ),
        "typecheck": Check(
            (python, "-m", "mypy"),
            _line_count(r"^.+:\d+: error: "),
        ),
        "basedpyright": Check(
            (python, "-m", "basedpyright"),
            _line_count(r" - error: "),
        ),
        "dependency": Check(
            (python, "-m", "deptry", ".", "--config", "pyproject.toml", "--no-ansi"),
            _line_count(r"^.+: DEP\d{3} "),
            "stderr",
        ),
        "dead-code": Check(
            (python, "-m", "vulture", "seasonalweather", "tools", "--min-confidence", "80"),
            _line_count(r"^.+:\d+: "),
        ),
        "security": Check(
            (python, "-m", "bandit", "-r", "seasonalweather", "-c", "pyproject.toml", "-f", "json", "-q"),
            _bandit_count,
        ),
        "complexity": Check(
            (python, "-m", "radon", "cc", "seasonalweather", "-j", "-n", "C"),
            _radon_count,
        ),
    }


def run(name: str, *, update: bool = False) -> int:
    check = checks()[name]
    result = subprocess.run(check.command, cwd=ROOT, text=True, capture_output=True, check=False)
    output = "\n".join(part.rstrip() for part in (result.stdout, result.stderr) if part.strip())
    count_output = result.stderr if check.count_stream == "stderr" else result.stdout
    try:
        findings = check.count(count_output)
    except (TypeError, ValueError) as exc:
        if output:
            print(output)
        print(f"{name}: unable to parse tool output: {exc}")
        return 2

    baseline_path = ROOT / "quality/baselines.toml"
    baseline = load_toml(baseline_path)
    maximum = baseline["checks"][name]["maximum"]
    print(f"{name}: {findings} finding(s), governed maximum {maximum}")
    if result.returncode not in {0, 1}:
        if output:
            print(output)
        print(f"{name}: tool failed with unexpected exit code {result.returncode}")
        return result.returncode
    if result.returncode == 1 and findings == 0:
        if output:
            print(output)
        print(f"{name}: tool failed without a recognized finding")
        return 1
    if update:
        print(f"Set checks.{name}.maximum = {findings} in {baseline_path.relative_to(ROOT)}")
        return 0
    if findings > maximum:
        if output:
            print(output)
        print(f"{name}: baseline exceeded by {findings - maximum} finding(s)")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a governed quality ratchet.")
    parser.add_argument("name", choices=sorted(checks()))
    parser.add_argument("--report-baseline", action="store_true")
    args = parser.parse_args()
    return run(args.name, update=args.report_baseline)


if __name__ == "__main__":
    raise SystemExit(main())
