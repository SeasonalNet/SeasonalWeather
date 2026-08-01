"""Offline staged configuration validation and opt-in preflight."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from ..configuration import compile_path
from ..configuration.compiler import CompiledConfiguration
from ..configuration.origins import ENVIRONMENT_BINDINGS
from ..validation import (
    EnvironmentInputIdentity,
    ValidationContext,
    ValidationPolicy,
    ValidationReport,
    configured_preflight_probes,
    render_validation_report,
    validate_compiled,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seasonalweather config",
        description=(
            "Offline parse, schema, semantic, compatibility, deprecation, and "
            "advisory validation. Environmental preflight is explicit."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    lint = commands.add_parser(
        "lint",
        help="validate all deterministic configuration stages",
    )
    lint.add_argument("--config", default="/etc/seasonalweather/config.yaml")
    lint.add_argument(
        "--format",
        choices=("human", "json"),
        default="human",
    )
    lint.add_argument(
        "--preflight",
        action="store_true",
        help="run bounded read-only checks for explicitly configured local paths",
    )
    lint.add_argument(
        "--warnings-as-errors",
        action="store_true",
        help="make warning policy blocking for this invocation",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    compiled = compile_path(args.config)
    context = ValidationContext(
        preflight_enabled=args.preflight,
        preflight_probes=configured_preflight_probes(compiled) if args.preflight else (),
        environment_inputs=_environment_identities(),
        policy=ValidationPolicy(warning_blocks=args.warnings_as_errors),
    )
    report = asyncio.run(validate_compiled(compiled, context=context))
    succeeded = report.decision.valid and (not args.preflight or report.decision.preflight_ready)
    _emit_report(compiled, report, output_format=args.format, succeeded=succeeded)
    return 0 if succeeded else 1


def _emit_report(
    compiled: CompiledConfiguration,
    report: ValidationReport,
    *,
    output_format: str,
    succeeded: bool,
) -> None:
    if output_format == "json":
        sys.stdout.write(f"{report.to_json()}\n")
    elif succeeded:
        nonblocking = tuple(issue for issue in report.issues if not issue.blocking)
        if nonblocking:
            source = (compiled.source,) if compiled.source else ()
            sys.stdout.write(f"{render_validation_report(report, sources=source)}\n\n")
        sys.stdout.write(f"configuration validation succeeded (schema {compiled.report.resolved_config_schema})\n")
    else:
        source = (compiled.source,) if compiled.source else ()
        sys.stderr.write(f"{render_validation_report(report, sources=source)}\n")


def _environment_identities() -> tuple[EnvironmentInputIdentity, ...]:
    names = sorted({variable for _, variable, _ in ENVIRONMENT_BINDINGS})
    return tuple(EnvironmentInputIdentity(variable=name, present=bool(os.environ.get(name, ""))) for name in names)


if __name__ == "__main__":
    raise SystemExit(main())
