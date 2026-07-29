"""Offline configuration parse/schema lint command."""

from __future__ import annotations

import argparse
import sys

from ..configuration import compile_path, render_report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seasonalweather config",
        description=(
            "Offline YAML parse and strict-schema validation. Semantic, "
            "compatibility, environmental preflight, and reload applicability "
            "are not evaluated."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    lint = commands.add_parser(
        "lint",
        help="validate configuration parse and schema stages",
    )
    lint.add_argument("--config", default="/etc/seasonalweather/config.yaml")
    lint.add_argument(
        "--format",
        choices=("human", "json"),
        default="human",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    compiled = compile_path(args.config)
    if args.format == "json":
        sys.stdout.write(f"{compiled.report.to_json()}\n")
    elif compiled.valid:
        sys.stdout.write(
            f"configuration parse/schema validation succeeded (schema {compiled.report.resolved_config_schema})\n"
        )
    else:
        sources = (compiled.source,) if compiled.source else ()
        sys.stderr.write(f"{render_report(compiled.report, sources=sources)}\n")
    return 0 if compiled.valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
