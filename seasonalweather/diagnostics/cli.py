"""Argument parsing and exit policy for read-only diagnostic catalog commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .exporter import CatalogExportError, export_catalog
from .registry import CatalogLookupError, DiagnosticCatalogService
from .renderer import render_explanation, render_list, render_namespaces
from .representations import (
    explanation_representation,
    list_representation,
    namespace_list_representation,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seasonalweather diagnostics",
        description="Inspect the immutable packaged diagnostic catalog.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="list active diagnostic definitions")
    listing.add_argument("--format", choices=("human", "json"), default="human")
    listing.add_argument(
        "--namespaces",
        action="store_true",
        help="list the active and reserved namespace registry",
    )
    explain = commands.add_parser("explain", help="explain one exact diagnostic code")
    explain.add_argument("code")
    explain.add_argument("--format", choices=("human", "json"), default="human")
    export = commands.add_parser("export", help="export a clean operator-readable resource tree")
    export.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    service = DiagnosticCatalogService()
    if args.command == "list":
        if args.format == "json":
            payload = (
                namespace_list_representation(service.catalog)
                if args.namespaces
                else list_representation(service.catalog)
            )
            sys.stdout.write(f"{_json(payload)}\n")
        else:
            rendered = render_namespaces(service.catalog) if args.namespaces else render_list(service.catalog)
            sys.stdout.write(f"{rendered}\n")
        return 0
    if args.command == "explain":
        try:
            result = service.explain(args.code)
        except CatalogLookupError as exc:
            sys.stderr.write(f"diagnostics explain: {exc.kind}: {exc}\n")
            return 1
        if args.format == "json":
            sys.stdout.write(
                f"{_json(explanation_representation(service.catalog, result.definition, result.markdown))}\n"
            )
        else:
            sys.stdout.write(f"{render_explanation(service.catalog, result)}\n")
        return 0
    try:
        exported = export_catalog(args.output)
    except CatalogExportError as exc:
        sys.stderr.write(f"diagnostics export: {exc}\n")
        return 1
    sys.stdout.write(f"exported diagnostic catalog to {exported}\n")
    return 0


def _json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
