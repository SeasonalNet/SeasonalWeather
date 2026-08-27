from tools.quality.architecture_check import scan
from tools.quality.governance import ROOT, load_toml

FIXTURES = ROOT / "tests/architecture/fixtures"
CONFIG = load_toml(ROOT / "quality/architecture.toml")


def test_valid_architecture_fixture_passes():
    assert scan(FIXTURES / "valid", CONFIG) == []


def test_project_metadata_dependency_profiles_have_independent_boundaries(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
dependencies = ["pydantic==2.13.4"]

[dependency-groups]
controller = ["fastapi==0.139.0"]
worker-runtime = ["websockets==15.0.1"]
piper = ["piper-tts"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    assert scan(tmp_path, CONFIG) == []


def test_project_metadata_dependency_profiles_reject_controller_leakage(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
dependencies = ["fastapi==0.139.0"]

[dependency-groups]
controller = ["fastapi==0.139.0", "piper-tts"]
piper = ["fastapi==0.139.0"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    findings = scan(tmp_path, CONFIG)
    assert [finding.rule for finding in findings] == ["SWARCH052", "SWARCH053", "SWARCH053"]


def test_invalid_architecture_fixture_proves_rules_fail_closed():
    findings = scan(FIXTURES / "invalid", CONFIG)

    assert {finding.rule for finding in findings} >= {
        "SWARCH001",
        "SWARCH002",
        "SWARCH003",
        "SWARCH006",
        "SWARCH009",
        "SWARCH010",
        "SWARCH011",
        "SWARCH012",
        "SWARCH013",
        "SWARCH014",
        "SWARCH015",
        "SWARCH016",
        "SWARCH017",
        "SWARCH018",
        "SWARCH019",
        "SWARCH020",
        "SWARCH021",
        "SWARCH022",
        "SWARCH023",
        "SWARCH024",
        "SWARCH025",
        "SWARCH026",
        "SWARCH027",
        "SWARCH028",
        "SWARCH029",
        "SWARCH030",
        "SWARCH031",
        "SWARCH032",
        "SWARCH033",
        "SWARCH034",
        "SWARCH035",
        "SWARCH036",
        "SWARCH037",
        "SWARCH038",
        "SWARCH039",
        "SWARCH040",
        "SWARCH041",
        "SWARCH042",
        "SWARCH043",
        "SWARCH044",
        "SWARCH045",
        "SWARCH050",
        "SWARCH051",
    }
    assert any("filesystem mutation" in finding.message for finding in findings)


def test_import_cycle_rule_has_an_independent_negative_fixture() -> None:
    assert not any(finding.rule == "SWARCH050" for finding in scan(FIXTURES / "valid", CONFIG))
    findings = scan(FIXTURES / "invalid", CONFIG)
    cycle_findings = [finding for finding in findings if finding.rule == "SWARCH050"]
    assert [finding.path for finding in cycle_findings] == [
        "seasonalweather/build_metadata/cycle_a.py",
        "seasonalweather/build_metadata/cycle_b.py",
    ]


def test_observability_boundary_has_an_independent_negative_fixture() -> None:
    findings = scan(FIXTURES / "invalid", CONFIG)
    boundary_findings = [finding for finding in findings if finding.rule == "SWARCH051"]
    assert [finding.path for finding in boundary_findings] == [
        "seasonalweather/observability/authority.py",
    ]


def test_nwws_architecture_rules_have_independent_matching_negative_fixtures():
    findings = scan(FIXTURES / "invalid", CONFIG)
    by_rule = {
        rule: [finding.path for finding in findings if finding.rule == rule]
        for rule in ("SWARCH040", "SWARCH041", "SWARCH042", "SWARCH043")
    }
    assert by_rule["SWARCH040"] == ["seasonalweather/nwws/bad_boundary.py", "seasonalweather/nwws/job_authority.py"]
    assert by_rule["SWARCH041"] == ["seasonalweather/nwws/slixmpp_adapter.py"]
    assert by_rule["SWARCH042"] == ["seasonalweather/broadcast/nwws_consumer.py"]
    assert by_rule["SWARCH043"] == [
        "seasonalweather/broadcast/nwws_consumer.py",
        "seasonalweather/broadcast/slixmpp_import.py",
    ]


def test_tts_architecture_rules_have_independent_matching_negative_fixtures():
    findings = scan(FIXTURES / "invalid", CONFIG)
    by_rule = {
        rule: [finding.path for finding in findings if finding.rule == rule]
        for rule in (
            "SWARCH034",
            "SWARCH035",
            "SWARCH036",
            "SWARCH037",
            "SWARCH038",
            "SWARCH039",
        )
    }

    assert by_rule["SWARCH034"] == ["seasonalweather/tts/models.py"]
    assert by_rule["SWARCH035"] == ["seasonalweather/tts/local.py"]
    assert by_rule["SWARCH036"] == ["seasonalweather/tts/policy.py"]
    assert by_rule["SWARCH037"] == ["seasonalweather/tts/adapters/provider.py"]
    assert by_rule["SWARCH038"] == ["seasonalweather/tts/transport.py"]
    assert by_rule["SWARCH039"] == ["seasonalweather/broadcast/remote.py"]


def test_segment_registry_architecture_rules_have_independent_matching_negative_fixtures():
    valid_findings = scan(FIXTURES / "valid", CONFIG)
    assert [finding for finding in valid_findings if finding.rule in {"SWARCH044", "SWARCH045"}] == []

    findings = scan(FIXTURES / "invalid", CONFIG)
    by_rule = {
        rule: [finding.path for finding in findings if finding.rule == rule] for rule in ("SWARCH044", "SWARCH045")
    }
    assert by_rule["SWARCH044"] == [
        "seasonalweather/broadcast/segment_registry_authority.py",
        "seasonalweather/broadcast/segment_registry_frozenset.py",
        "seasonalweather/broadcast/segment_registry_parallel_authority.py",
    ]
    assert by_rule["SWARCH045"] == ["seasonalweather/broadcast/segment_registry.py"]


def test_p1_20_boundaries_have_positive_and_independent_negative_fixtures():
    assert scan(FIXTURES / "valid", CONFIG) == []
    findings = scan(FIXTURES / "invalid", CONFIG)
    by_rule = {
        rule: [finding.path for finding in findings if finding.rule == rule]
        for rule in ("SWARCH046", "SWARCH047", "SWARCH048", "SWARCH049")
    }
    assert by_rule["SWARCH046"] == ["seasonalweather/api/api.py"]
    assert by_rule["SWARCH047"] == [
        "seasonalweather/broadcast/cycle.py",
        "seasonalweather/broadcast/cycle.py",
    ]
    assert by_rule["SWARCH048"] == ["seasonalweather/broadcast/segment_service.py"]
    assert by_rule["SWARCH049"] == ["seasonalweather/control.py"]


def test_control_module_has_no_duplicate_job_repository_or_scheduler_authority():
    source = (ROOT / "seasonalweather/control.py").read_text(encoding="utf-8")

    assert "seasonalweather.job_store" not in source
    assert "JobRepository(" not in source
    assert "JobScheduler(" not in source
    assert "sqlite3" not in source


def test_control_and_api_have_no_swwp_or_simulation_authority():
    control = (ROOT / "seasonalweather/control.py").read_text(encoding="utf-8")
    # server.py is the approved live-SWWP composition owner; this assertion is
    # for the route layer, while architecture_check scans the complete tree.
    api = (ROOT / "seasonalweather/api/api.py").read_text(encoding="utf-8")

    for source in (control, api):
        assert "seasonalweather.swwp" not in source
        assert "swwp_simulation" not in source
        assert "SimulatedPeers" not in source


def test_control_and_api_have_no_capability_scheduler_authority():
    control = (ROOT / "seasonalweather/control.py").read_text(encoding="utf-8")
    # server.py is the approved live-SWWP composition owner; this assertion is
    # for the route layer, while architecture_check scans the complete tree.
    api = (ROOT / "seasonalweather/api/api.py").read_text(encoding="utf-8")

    for source in (control, api):
        assert "CapabilityRegistry" not in source
        assert "CapabilitySchedulerService" not in source
        assert "QualificationReason" not in source
        assert "reserve(" not in source


def test_control_and_api_have_no_artifact_publication_authority():
    control = (ROOT / "seasonalweather/control.py").read_text(encoding="utf-8")
    api = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "seasonalweather/api").glob("*.py"))

    for source in (control, api):
        assert "seasonalweather.artifacts" not in source
        assert "ArtifactService(" not in source
        assert "PromotionService(" not in source


def test_worker_boundary_declares_artifact_promotion_as_controller_authority():
    authorities = set(CONFIG["controller_authority_imports"])
    assert {
        "seasonalweather.api",
        "seasonalweather.artifacts.integration",
        "seasonalweather.artifacts.promotion",
        "seasonalweather.artifacts.service",
        "seasonalweather.artifacts.staging",
        "seasonalweather.commands",
        "seasonalweather.control",
        "seasonalweather.database",
        "seasonalweather.discord_log",
        "seasonalweather.health_service",
        "seasonalweather.lifecycle",
        "seasonalweather.nwws",
        "seasonalweather.same",
    } <= authorities


def test_configuration_parser_and_schema_authority_stays_out_of_control_and_api():
    control = (ROOT / "seasonalweather/control.py").read_text(encoding="utf-8")
    api = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "seasonalweather/api").glob("*.py"))

    for source in (control, api):
        assert "yaml.safe_load" not in source
        assert "validate_schema(" not in source
        assert "parse_document(" not in source


def test_control_and_api_have_no_diagnostic_catalog_file_authority():
    control = (ROOT / "seasonalweather/control.py").read_text(encoding="utf-8")
    api = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "seasonalweather/api").glob("*.py"))

    for source in (control, api):
        assert "catalog.json" not in source
        assert "diagnostics.loader" not in source
        assert "importlib.resources" not in source


def test_control_and_api_routes_have_no_mutable_occurrence_authority():
    control = (ROOT / "seasonalweather/control.py").read_text(encoding="utf-8")
    route_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "seasonalweather/api").glob("*.py")
        if path.name != "server.py"
    )

    for source in (control, route_sources):
        assert "diagnostic_occurrences" not in source
        assert "OccurrenceRepository" not in source
        assert ".repository.record(" not in source
        assert ".repository.resolve(" not in source
        assert ".repository.prune(" not in source


def test_control_and_api_have_no_staged_validation_or_preflight_authority():
    control = (ROOT / "seasonalweather/control.py").read_text(encoding="utf-8")
    api = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "seasonalweather/api").glob("*.py"))

    for source in (control, api):
        assert "validate_compiled(" not in source
        assert "run_preflight(" not in source
        assert "ValidationReport(" not in source
        assert "verify_report(" not in source


def test_control_has_no_duplicate_configuration_reload_authority():
    source = (ROOT / "seasonalweather/control.py").read_text(encoding="utf-8")

    assert "configuration_reload" not in source
    assert "reload_config" not in source
    assert "ReloadRepository" not in source
    assert "SafePointCoordinator" not in source
