"""Reject quality suppressions added after an explicit Git base revision."""

from __future__ import annotations

import argparse
import io
import os
import re
import subprocess
import tokenize
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path

from tools.quality.governance import ROOT

SuppressionKey = tuple[str, str]

_SKIP_DIRECTORIES = frozenset({".git", ".venv", ".venv-ci", "build", "__pycache__"})
_SUPPRESSION = re.compile(
    r"#\s*(?P<directive>"
    r"noqa(?:\s*:[^#]*)?|"
    r"type:\s*ignore(?:\[[^#]*\])?|"
    r"pyright:\s*(?:ignore|strict)(?:\[[^#]*\])?|"
    r"mypy:\s*ignore(?:\[[^#]*\])?|"
    r"nosec(?:\s+[^#]*)?|"
    r"pragma:\s*no\s*cover|"
    r"pylint:\s*(?:disable|skip-file)(?:\s*=[^#]*)?|"
    r"fmt:\s*(?:off|skip)|"
    r"isort:\s*skip"
    r")",
    re.IGNORECASE,
)


def _source_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*.py")):
        if not _SKIP_DIRECTORIES.isdisjoint(path.relative_to(root).parts):
            continue
        yield path


def _collect_text(relative: str, text: str) -> Counter[SuppressionKey]:
    findings: Counter[SuppressionKey] = Counter()
    lines = text.splitlines()
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        for token in tokens:
            if token.type != tokenize.COMMENT:
                continue
            match = _SUPPRESSION.search(token.string)
            if match is None:
                continue
            line = lines[token.start[0] - 1].strip() if token.start[0] <= len(lines) else token.string.strip()
            directive = " ".join(match.group("directive").split())
            findings[(relative, f"{line} [{directive}]")] += 1
    except (IndentationError, SyntaxError, tokenize.TokenError) as exc:
        raise ValueError(f"could not tokenize {relative}: {exc}") from exc
    return findings


def collect_suppressions(root: Path) -> Counter[SuppressionKey]:
    """Collect inline suppression directives from the current source tree."""

    findings: Counter[SuppressionKey] = Counter()
    for path in _source_files(root):
        relative = path.relative_to(root).as_posix()
        findings.update(_collect_text(relative, path.read_text(encoding="utf-8")))
    return findings


def _git_text(root: Path, base_ref: str) -> Iterable[tuple[str, str]]:
    listed = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", base_ref],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if listed.returncode != 0:
        detail = listed.stderr.strip() or "unknown git error"
        raise ValueError(f"base revision {base_ref!r} is unavailable: {detail}")
    for relative in (line.strip() for line in listed.stdout.splitlines()):
        if not relative.endswith(".py"):
            continue
        shown = subprocess.run(
            ["git", "show", f"{base_ref}:{relative}"],
            cwd=root,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        if shown.returncode != 0:
            detail = shown.stderr.strip() or "unknown git error"
            raise ValueError(f"could not read {base_ref}:{relative}: {detail}")
        yield relative, shown.stdout


def collect_base_suppressions(root: Path, base_ref: str) -> Counter[SuppressionKey]:
    """Collect suppressions from a committed Git base, failing closed if unavailable."""

    findings: Counter[SuppressionKey] = Counter()
    for relative, text in _git_text(root, base_ref):
        findings.update(_collect_text(relative, text))
    return findings


def new_suppressions(
    current: Counter[SuppressionKey],
    base: Counter[SuppressionKey],
) -> Counter[SuppressionKey]:
    """Return only positive additions; removing old debt never fails the check."""

    return current - base


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reject net-new inline quality suppressions.")
    parser.add_argument(
        "--base-ref",
        default=os.environ.get("QUALITY_SUPPRESSIONS_BASE", "HEAD"),
        help="Git revision whose existing suppressions are allowed (default: QUALITY_SUPPRESSIONS_BASE or HEAD)",
    )
    args = parser.parse_args(argv)
    try:
        current = collect_suppressions(ROOT)
        base = collect_base_suppressions(ROOT, args.base_ref)
    except (OSError, ValueError) as exc:
        print(f"quality-suppressions: {exc}")
        return 1

    additions = new_suppressions(current, base)
    if additions:
        print(f"quality-suppressions: {sum(additions.values())} net-new suppression(s) relative to {args.base_ref!r};")
        for (relative, source), count in sorted(additions.items()):
            suffix = f" (x{count})" if count > 1 else ""
            print(f"  {relative}: {source}{suffix}")
        return 1
    print(
        f"quality-suppressions: {sum(current.values())} existing suppression(s); "
        f"no net-new suppressions relative to {args.base_ref!r}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
