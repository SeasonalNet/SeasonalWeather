from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from seasonalweather.config import _build_app_config, load_config
from seasonalweather.configuration import (
    CompilerLimits,
    SourceDocument,
    compile_path,
    compile_source,
    render_report,
)
from seasonalweather.configuration.environment import EnvironmentValues
from seasonalweather.configuration.loader import ConfigurationCompileError
from seasonalweather.configuration.origins import OriginKind
from seasonalweather.configuration.paths import ConfigPath
from seasonalweather.configuration.yaml_parser import parse_document

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "config/config.yaml"
SECRET = "SENTINEL-CONFIGURATION-SECRET-DO-NOT-LEAK"


def _source(text: str, name: str = "candidate.yaml") -> SourceDocument:
    return SourceDocument.from_bytes(text.encode(), source_id=name)


def _compile(text: str):
    return compile_source(_source(text), environ={})


def _with_obsolete_live_fields(text: str) -> str:
    text = text.replace(
        "    api_command_retention_days: 14\n    audio_asset_grace_seconds: 900\n",
        "    api_command_retention_days: 14\n"
        "    audio_asset_grace_seconds: 900\n"
        "    command_retention_days: 14\n"
        "    asset_grace_seconds: 900\n",
        1,
    )
    text = text.replace(
        "  ttl_seconds: 7200\n",
        "  ttl_seconds: 7200\n  min_write_seconds: 0.5\n",
        1,
    )
    text = text.replace(
        "    grace_sec: 5\n",
        "    grace_sec: 5\n    keep_unparseable: true\n",
        1,
    )
    return text.replace(
        "\napi:\n",
        "\nrebroadcast:\n"
        "  enabled: true\n"
        "  interval_seconds: 300\n"
        "  min_gap_seconds: 300\n"
        "  ttl_seconds: 3600\n"
        "  max_items: 6\n"
        "  include_voice: false\n"
        "\nlive_time:\n"
        "  enabled: true\n"
        "  interval_seconds: 32\n"
        "\napi:\n",
        1,
    )


def test_example_is_valid_strict_schema_and_deterministic() -> None:
    first = compile_path(EXAMPLE, environ={})
    second = compile_path(EXAMPLE, environ={})

    assert first.valid
    assert first.report.parse_valid
    assert first.report.schema_valid
    assert first.report.explicit_config_schema == 1
    assert first.report.resolved_config_schema == 1
    assert first.report.to_json() == second.report.to_json()
    assert json.loads(first.report.to_json())["valid"] is True


def test_supported_database_housekeeping_names_preserve_runtime_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    text = EXAMPLE.read_text(encoding="utf-8")
    text = text.replace("api_command_retention_days: 14", "api_command_retention_days: 37", 1)
    text = text.replace("audio_asset_grace_seconds: 900", "audio_asset_grace_seconds: 1234", 1)
    candidate = tmp_path / "supported-housekeeping.yaml"
    candidate.write_text(text, encoding="utf-8")
    monkeypatch.setenv("ICECAST_SOURCE_PASSWORD", "synthetic-source")
    monkeypatch.setenv("SEASONAL_API_TOKEN", "synthetic-token")

    compiled = compile_path(candidate, environ={})
    runtime = load_config(str(candidate))

    assert compiled.valid
    assert compiled.value is not None
    database = compiled.value["database"]
    assert isinstance(database, dict)
    housekeeping = database["housekeeping"]
    assert isinstance(housekeeping, dict)
    assert housekeeping["api_command_retention_days"] == 37
    assert housekeeping["audio_asset_grace_seconds"] == 1234
    assert runtime.database.housekeeping.api_command_retention_days == 37
    assert runtime.database.housekeeping.audio_asset_grace_seconds == 1234


def test_six_obsolete_live_paths_are_rejected_and_were_runtime_noops() -> None:
    stale_text = _with_obsolete_live_fields(EXAMPLE.read_text(encoding="utf-8"))
    compiled = compile_source(_source(stale_text), environ={})
    unknown_paths = tuple(
        issue.path.to_pointer()
        for issue in compiled.report.issues
        if issue.rule_id == "schema.unknown_field" and issue.path is not None
    )

    assert set(unknown_paths) == {
        "/database/housekeeping/command_retention_days",
        "/database/housekeeping/asset_grace_seconds",
        "/station_feed/min_write_seconds",
        "/station_feed/housekeeping/keep_unparseable",
        "/rebroadcast",
        "/live_time",
    }
    assert {issue.code for issue in compiled.report.issues if issue.rule_id == "schema.unknown_field"} == {"SWCFG1020"}
    repeated = compile_source(_source(stale_text), environ={})
    assert unknown_paths == tuple(
        issue.path.to_pointer()
        for issue in repeated.report.issues
        if issue.rule_id == "schema.unknown_field" and issue.path is not None
    )
    assert compiled.value is None

    parsed = parse_document(_source(stale_text)).parsed
    assert parsed is not None
    stale_raw = parsed.value
    cleaned_raw = copy.deepcopy(stale_raw)
    cleaned_raw["database"]["housekeeping"].pop("command_retention_days")
    cleaned_raw["database"]["housekeeping"].pop("asset_grace_seconds")
    cleaned_raw["station_feed"].pop("min_write_seconds")
    cleaned_raw["station_feed"]["housekeeping"].pop("keep_unparseable")
    cleaned_raw.pop("rebroadcast")
    cleaned_raw.pop("live_time")
    environment = EnvironmentValues(
        {
            "ICECAST_SOURCE_PASSWORD": "synthetic-source",
            "SEASONAL_API_TOKEN": "synthetic-token",
        }
    )

    assert _build_app_config(stale_raw, environment=environment) == _build_app_config(
        cleaned_raw,
        environment=environment,
    )


def test_legacy_absent_schema_version_is_explicitly_resolved() -> None:
    text = EXAMPLE.read_text(encoding="utf-8").replace("config_schema: 1\n\n", "", 1)
    compiled = compile_source(_source(text), environ={})

    assert compiled.valid
    assert compiled.report.explicit_config_schema is None
    assert compiled.report.resolved_config_schema == 1
    assert compiled.origins[ConfigPath(("config_schema",))].kind is OriginKind.GENERATED


@pytest.mark.parametrize("value", ["true", '"1"', "0", "-1", "2", "null"])
def test_invalid_or_unsupported_schema_versions_fail_closed(value: str) -> None:
    text = EXAMPLE.read_text(encoding="utf-8").replace("config_schema: 1", f"config_schema: {value}", 1)
    compiled = compile_source(_source(text), environ={})

    assert compiled.report.parse_valid
    assert not compiled.report.schema_valid
    assert compiled.value is None
    assert compiled.report.issues[0].rule_id.startswith("schema.config_schema_")


@pytest.mark.parametrize(
    "text,rule",
    [
        ("a: [1,\n", "yaml.syntax"),
        ("---\na: 1\n---\nb: 2\n", "yaml.multiple_documents"),
        ("value: !python/object:builtins.object {}\n", "yaml.tag"),
        ("1: value\n", "yaml.non_string_key"),
        ("base: &base\n  value: 1\n", "yaml.anchor"),
        ("base: &base {value: 1}\ncopy: *base\n", "yaml.anchor"),
    ],
)
def test_unsafe_or_malformed_yaml_is_source_addressed(text: str, rule: str) -> None:
    compiled = _compile(text)

    assert not compiled.report.parse_valid
    assert compiled.value is None
    assert compiled.report.issues[0].rule_id == rule
    assert compiled.report.issues[0].primary is not None


def test_empty_and_comment_only_sources_fail_as_bounded_parse_issues() -> None:
    for text in ("", " \n# only a comment\n"):
        compiled = _compile(text)
        assert compiled.report.issues[0].rule_id == "yaml.empty"
        assert compiled.value is None


def test_duplicate_key_has_later_primary_and_first_related() -> None:
    compiled = _compile("station:\n  name: first\n  name: second\n")
    issue = next(issue for issue in compiled.report.issues if issue.rule_id == "yaml.duplicate_key")

    assert issue.path == ConfigPath(("station", "name"))
    assert issue.primary is not None
    assert issue.primary.span.start.line == 2
    assert issue.related[0].location.span.start.line == 1
    assert compiled.value is None


def test_duplicate_keys_inside_sequences_and_quoted_equivalents_fail() -> None:
    compiled = _compile('items:\n  - "token": first\n    token: second\n')
    issue = next(issue for issue in compiled.report.issues if issue.rule_id == "yaml.duplicate_key")
    assert issue.path == ConfigPath(("items", 0, "token"))
    assert issue.redacted


def test_yaml_12_boolean_words_are_strings_not_coerced() -> None:
    text = EXAMPLE.read_text(encoding="utf-8").replace("allow_remote: false", "allow_remote: yes", 1)
    compiled = compile_source(_source(text), environ={})
    issue = next(issue for issue in compiled.report.issues if issue.path == ConfigPath(("api", "allow_remote")))

    assert issue.rule_id == "schema.type"
    assert issue.primary is not None


def test_unknown_fields_and_strict_scalar_types_are_source_addressed() -> None:
    text = EXAMPLE.read_text(encoding="utf-8")
    text = text.replace("  icecast_port: 8000", '  icecast_port: "8000"', 1)
    text = text.replace("station:\n", "station:\n  unrecognized: value\n", 1)
    compiled = compile_source(_source(text), environ={})
    by_rule = {issue.rule_id: issue for issue in compiled.report.issues}

    assert by_rule["schema.unknown_field"].primary is not None
    assert by_rule["schema.unknown_field"].primary.label == "key"
    assert by_rule["schema.type"].primary is not None
    assert by_rule["schema.type"].primary.label == "value"
    assert compiled.value is None


def test_missing_required_field_points_to_containing_mapping() -> None:
    text = EXAMPLE.read_text(encoding="utf-8").replace('  name: "SeasonalWeather"\n', "", 1)
    compiled = compile_source(_source(text), environ={})
    issue = next(issue for issue in compiled.report.issues if issue.path == ConfigPath(("station", "name")))

    assert issue.rule_id == "schema.required"
    assert issue.primary is not None
    assert issue.primary.label == "containing mapping"


def test_file_environment_default_and_generated_origins_are_secret_free() -> None:
    text = EXAMPLE.read_text(encoding="utf-8").replace("config_schema: 1\n\n", "", 1)
    environment = {
        "NWWS_PASSWORD": SECRET,
        "LIQUIDSOAP_TELNET_HOST": "synthetic-host",
    }
    compiled = compile_source(_source(text), environ=environment)

    assert compiled.valid
    assert compiled.origins[ConfigPath(("station", "name"))].kind is OriginKind.FILE
    assert compiled.origins[ConfigPath(("station", "deployment_type"))].kind is OriginKind.DEFAULT
    assert compiled.origins[ConfigPath(("config_schema",))].kind is OriginKind.GENERATED
    password_origin = compiled.origins[ConfigPath(("secrets", "nwws_password"))]
    assert password_origin.kind is OriginKind.ENVIRONMENT
    assert password_origin.environment_variable == "NWWS_PASSWORD"
    output = compiled.report.to_json()
    assert SECRET not in output
    assert "synthetic-host" not in output


def test_secret_like_unknown_field_is_redacted_in_every_output() -> None:
    text = EXAMPLE.read_text(encoding="utf-8").replace("station:\n", f"station:\n  database_password: {SECRET}\n", 1)
    compiled = compile_source(_source(text), environ={})
    issue = next(
        issue for issue in compiled.report.issues if issue.path == ConfigPath(("station", "database_password"))
    )
    rendered = render_report(compiled.report, sources=(compiled.source,))

    assert issue.redacted
    assert SECRET not in rendered
    assert SECRET not in compiled.report.to_json()
    assert SECRET not in repr(compiled)
    assert "<redacted>" in rendered


def test_malformed_multiline_secret_is_conservatively_redacted() -> None:
    text = f"api_token: |\n  {SECRET}\nbroken: [\n"
    compiled = _compile(text)
    rendered = render_report(compiled.report, sources=(compiled.source,))

    assert SECRET not in rendered
    assert SECRET not in compiled.report.to_json()
    assert SECRET not in repr(compiled)


def test_multiline_spans_and_unicode_positions_are_preserved() -> None:
    compiled = _compile("message: |\n  café\n  warning\n")
    parsed = compiled.parsed
    assert parsed is not None
    location = parsed.locations[ConfigPath(("message",))].value

    assert location.span.start.line == 0
    assert location.span.end.line == 3
    assert location.span.end >= location.span.start


def test_paths_render_machine_and_human_escaping() -> None:
    path = ConfigPath(("a/b", "til~de", 2, "plain"))
    assert path.to_pointer() == "/a~1b/til~0de/2/plain"
    assert path.to_human() == '"a/b"["til~de"][2].plain'
    assert ConfigPath().to_pointer() == ""
    assert ConfigPath().to_human() == "<root>"


def test_source_limits_and_invalid_utf8_fail_without_payload_leak() -> None:
    limits = CompilerLimits(max_source_bytes=8)
    compiled = compile_source(
        SourceDocument.from_bytes(b"a: 1\n", source_id="small.yaml"),
        limits=limits,
        environ={},
    )
    assert compiled.report.parse_valid

    with pytest.raises(Exception) as size_error:
        SourceDocument.from_bytes(b"x" * 9, source_id="large.yaml", limits=limits)
    assert "xxxxxxxxx" not in str(size_error.value)

    with pytest.raises(Exception) as encoding_error:
        SourceDocument.from_bytes(b"\xff" + SECRET.encode(), source_id="bad.yaml")
    assert SECRET not in str(encoding_error.value)


@pytest.mark.parametrize(
    ("text", "limits", "rule"),
    [
        (
            "a:\n  b:\n    c: 1\n",
            CompilerLimits(max_depth=1),
            "source.limit.depth",
        ),
        ("a: 1\nb: 2\n", CompilerLimits(max_nodes=2), "source.limit.nodes"),
        (
            "items: [1, 2]\n",
            CompilerLimits(max_collection_items=1),
            "source.limit.collection",
        ),
        (
            "value: oversized\n",
            CompilerLimits(max_scalar_codepoints=4),
            "source.limit.scalar",
        ),
    ],
)
def test_parser_resource_limits_fail_closed(
    text: str,
    limits: CompilerLimits,
    rule: str,
) -> None:
    compiled = compile_source(_source(text), limits=limits, environ={})

    assert compiled.value is None
    assert any(issue.rule_id == rule for issue in compiled.report.issues)


def test_issue_count_is_bounded_with_a_deterministic_marker() -> None:
    compiled = compile_source(
        _source("a: 1\na: 2\nb: 1\nb: 2\nc: 1\nc: 2\n"),
        limits=CompilerLimits(max_issues=2),
        environ={},
    )

    assert len(compiled.report.issues) == 2
    assert any(issue.rule_id == "compiler.issue_limit" for issue in compiled.report.issues)


def test_oversized_numeric_constructor_failure_is_normalized() -> None:
    numeric = "9" * 5_000
    compiled = _compile(f"value: {numeric}\n")

    assert compiled.value is None
    assert compiled.report.issues[0].rule_id == "yaml.scalar_construction"
    assert numeric not in compiled.report.to_json()
    assert numeric not in render_report(compiled.report, sources=(compiled.source,))


def test_bom_crlf_and_source_identity_are_supported() -> None:
    data = b"\xef\xbb\xbfconfig_schema: 1\r\n"
    source = SourceDocument.from_bytes(data, source_id="display-name.yaml")
    compiled = compile_source(source, environ={})

    assert compiled.report.parse_valid
    assert compiled.report.sources[0].source_id == "display-name.yaml"
    assert compiled.report.sources[0].sha256 == source.digest


def test_report_contains_no_source_text_or_runtime_values() -> None:
    compiled = compile_path(EXAMPLE, environ={})
    output = compiled.report.to_json()

    assert "SeasonalWeather is not NOAA Weather Radio" not in output
    assert "effective_configuration" not in output
    assert "\u001b" not in output


def test_startup_has_no_permissive_duplicate_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "duplicate.yaml"
    candidate.write_text(
        "station: {}\n" + EXAMPLE.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setenv("ICECAST_SOURCE_PASSWORD", "synthetic-source")
    monkeypatch.setenv("SEASONAL_API_TOKEN", "synthetic-token")

    with pytest.raises(ConfigurationCompileError) as exc_info:
        load_config(str(candidate))
    assert "error[SWCFG1012]" in str(exc_info.value)


def test_valid_startup_keeps_runtime_dataclass_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ICECAST_SOURCE_PASSWORD", "synthetic-source")
    monkeypatch.setenv("SEASONAL_API_TOKEN", "synthetic-token")

    config = load_config(str(EXAMPLE))

    assert config.station.name == "SeasonalWeather"
    assert config.stream.icecast_port == 8000
    assert config.secrets.icecast_source_password == "synthetic-source"
