from tools.quality.architecture_check import scan
from tools.quality.governance import ROOT, load_toml

FIXTURES = ROOT / "tests/architecture/fixtures"
CONFIG = load_toml(ROOT / "quality/architecture.toml")


def test_valid_architecture_fixture_passes():
    assert scan(FIXTURES / "valid", CONFIG) == []


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
    }
    assert any("filesystem mutation" in finding.message for finding in findings)


def test_control_module_has_no_duplicate_job_repository_or_scheduler_authority():
    source = (ROOT / "seasonalweather/control.py").read_text(encoding="utf-8")

    assert "seasonalweather.job_store" not in source
    assert "JobRepository(" not in source
    assert "JobScheduler(" not in source
    assert "sqlite3" not in source


def test_control_and_api_have_no_swwp_or_simulation_authority():
    control = (ROOT / "seasonalweather/control.py").read_text(encoding="utf-8")
    api = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "seasonalweather/api").glob("*.py"))

    for source in (control, api):
        assert "seasonalweather.swwp" not in source
        assert "swwp_simulation" not in source
        assert "SimulatedPeers" not in source


def test_control_and_api_have_no_capability_scheduler_authority():
    control = (ROOT / "seasonalweather/control.py").read_text(encoding="utf-8")
    api = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "seasonalweather/api").glob("*.py"))

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
        "seasonalweather.artifacts.integration",
        "seasonalweather.artifacts.promotion",
        "seasonalweather.artifacts.service",
        "seasonalweather.artifacts.staging",
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
        assert "/v1/diagnostics" not in source


def test_control_and_api_routes_have_no_mutable_occurrence_authority():
    control = (ROOT / "seasonalweather/control.py").read_text(encoding="utf-8")
    route_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "seasonalweather/api").glob("*.py")
        if path.name != "server.py"
    )

    for source in (control, route_sources):
        assert "runtime_diagnostics" not in source
        assert "diagnostic_occurrences" not in source
        assert "OccurrenceRepository" not in source
