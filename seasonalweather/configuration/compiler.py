"""Pure parse/schema compiler orchestration."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from .issues import CompileIssue, IssuePhase
from .origins import ENVIRONMENT_BINDINGS, OriginKind, ValueOrigin
from .paths import ConfigPath
from .redaction import is_secret_path
from .report import CompileReport, SourceSummary
from .schema import (
    CURRENT_CONFIG_SCHEMA,
    SUPPORTED_CONFIG_SCHEMAS,
    validate_schema,
)
from .source import (
    DEFAULT_LIMITS,
    CompilerLimits,
    ParsedSource,
    SourceDocument,
    SourceLocation,
    SourceReadError,
)
from .yaml_parser import parse_document


@dataclass(frozen=True)
class CompiledConfiguration:
    """A successful or failed immutable parse/schema compilation."""

    report: CompileReport
    source: SourceDocument | None = field(default=None, repr=False)
    parsed: ParsedSource | None = field(default=None, repr=False)
    value: Mapping[str, object] | None = field(default=None, repr=False)
    origins: Mapping[ConfigPath, ValueOrigin] = field(
        default_factory=lambda: MappingProxyType({}),
        repr=False,
    )

    @property
    def valid(self) -> bool:
        return self.report.valid


def compile_path(
    path: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
    limits: CompilerLimits = DEFAULT_LIMITS,
) -> CompiledConfiguration:
    try:
        source = SourceDocument.read(path, limits=limits)
    except SourceReadError as exc:
        issue = CompileIssue(
            rule_id=exc.rule_id,
            phase=IssuePhase.PARSE,
            message=exc.safe_message,
        )
        report = CompileReport(
            parse_valid=False,
            schema_valid=False,
            explicit_config_schema=None,
            resolved_config_schema=None,
            sources=(SourceSummary(exc.source_id, exc.sha256, exc.byte_length),),
            issues=(issue,),
        )
        return CompiledConfiguration(report=report)
    return compile_source(source, environ=environ, limits=limits)


def compile_source(
    source: SourceDocument,
    *,
    environ: Mapping[str, str] | None = None,
    limits: CompilerLimits = DEFAULT_LIMITS,
) -> CompiledConfiguration:
    parse = parse_document(source, limits=limits)
    summary = (SourceSummary(source.source_id, source.digest, source.byte_length),)
    if parse.parsed is None:
        report = CompileReport(
            parse_valid=False,
            schema_valid=False,
            explicit_config_schema=None,
            resolved_config_schema=None,
            sources=summary,
            issues=parse.issues,
        )
        return CompiledConfiguration(report=report, source=source)

    parsed = parse.parsed
    explicit, resolved, version_issue, version_origin = _resolve_schema(parsed)
    if version_issue is not None:
        report = CompileReport(
            parse_valid=True,
            schema_valid=False,
            explicit_config_schema=explicit,
            resolved_config_schema=resolved,
            sources=summary,
            issues=(version_issue,),
            origins=(version_origin,) if version_origin else (),
        )
        return CompiledConfiguration(report=report, source=source, parsed=parsed)

    schema = validate_schema(parsed, limits=limits)
    origins = list(schema.origins)
    if version_origin:
        origins.append(version_origin)
    origins.extend(_environment_origins(environ if environ is not None else os.environ))
    origins.extend(
        (
            ValueOrigin(
                path=ConfigPath(("service_area", "same_fips_all")),
                kind=OriginKind.GENERATED,
                declaration_id="service-area-same-fips-union",
            ),
            ValueOrigin(
                path=ConfigPath(("nwws", "credentials_defaulted")),
                kind=OriginKind.GENERATED,
                declaration_id="nwws-credential-default-detection",
            ),
        )
    )
    origins_by_path = {
        origin.path: origin
        for origin in sorted(
            origins,
            key=lambda item: (
                item.path,
                item.kind.value,
                item.environment_variable or "",
            ),
        )
    }
    ordered_origins = tuple(origins_by_path[path] for path in sorted(origins_by_path))
    report = CompileReport(
        parse_valid=True,
        schema_valid=not schema.issues,
        explicit_config_schema=explicit,
        resolved_config_schema=resolved,
        sources=summary,
        issues=schema.issues,
        origins=ordered_origins,
    )
    if schema.issues:
        return CompiledConfiguration(
            report=report,
            source=source,
            parsed=parsed,
            origins=MappingProxyType(origins_by_path),
        )
    return CompiledConfiguration(
        report=report,
        source=source,
        parsed=parsed,
        value=MappingProxyType(parsed.value),
        origins=MappingProxyType(origins_by_path),
    )


def _resolve_schema(
    parsed: ParsedSource,
) -> tuple[int | None, int | None, CompileIssue | None, ValueOrigin | None]:
    path = ConfigPath(("config_schema",))
    if "config_schema" not in parsed.value:
        origin = ValueOrigin(
            path=path,
            kind=OriginKind.GENERATED,
            declaration_id="legacy-config-schema-v1",
        )
        return None, CURRENT_CONFIG_SCHEMA, None, origin
    value = parsed.value["config_schema"]
    node = parsed.locations.get(path)
    location = node.value if node else parsed.document_location
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return (
            value if isinstance(value, int) and not isinstance(value, bool) else None,
            None,
            _version_issue(
                "schema.config_schema_type",
                "config_schema must be a positive integer.",
                path,
                location,
            ),
            ValueOrigin(path=path, kind=OriginKind.FILE, location=location),
        )
    origin = ValueOrigin(path=path, kind=OriginKind.FILE, location=location)
    if value not in SUPPORTED_CONFIG_SCHEMAS:
        return (
            value,
            None,
            _version_issue(
                "schema.config_schema_unsupported",
                "Configuration schema version is not supported.",
                path,
                location,
            ),
            origin,
        )
    return value, value, None, origin


def _version_issue(
    rule_id: str,
    message: str,
    path: ConfigPath,
    location: SourceLocation,
) -> CompileIssue:
    return CompileIssue(
        rule_id=rule_id,
        phase=IssuePhase.SCHEMA,
        message=message,
        path=path,
        primary=location,
        redacted=is_secret_path(path),
        help=f"Use config_schema: {CURRENT_CONFIG_SCHEMA}.",
    )


def _environment_origins(environ: Mapping[str, str]) -> list[ValueOrigin]:
    origins: list[ValueOrigin] = []
    for path, variable, default in ENVIRONMENT_BINDINGS:
        raw = environ.get(variable, "")
        if raw:
            origins.append(
                ValueOrigin(
                    path=path,
                    kind=OriginKind.ENVIRONMENT,
                    environment_variable=variable,
                )
            )
        elif default is not None:
            origins.append(
                ValueOrigin(
                    path=path,
                    kind=OriginKind.DEFAULT,
                    declaration_id=f"environment-default:{variable}",
                )
            )
    return origins
