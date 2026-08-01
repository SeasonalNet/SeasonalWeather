from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from seasonalweather.config import load_config

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutate,
):
    raw = yaml.safe_load((REPO_ROOT / "config/config.yaml").read_text(encoding="utf-8"))
    mutate(raw)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(raw, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("ICECAST_SOURCE_PASSWORD", "synthetic-source-password")
    return load_config(str(config_path))


def test_job_repository_config_is_typed_separate_and_explicit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    jobs_path = tmp_path / "jobs.sqlite3"
    operations_path = tmp_path / "operations.sqlite3"

    def mutate(raw):
        raw["database"]["path"] = str(operations_path)
        raw["jobs"].update(
            {
                "enabled": True,
                "required": True,
                "path": str(jobs_path),
                "busy_timeout_ms": 1234,
            }
        )

    cfg = _load(monkeypatch, tmp_path, mutate)

    assert cfg.jobs.enabled is True
    assert cfg.jobs.required is True
    assert cfg.jobs.path == str(jobs_path)
    assert cfg.jobs.busy_timeout_ms == 1234
    assert cfg.jobs.path != cfg.database.path


@pytest.mark.parametrize(
    ("jobs_update", "message"),
    [
        ({"enabled": True, "path": ""}, "explicitly configured"),
        ({"enabled": False, "required": True}, "cannot be true"),
        (
            {
                "enabled": True,
                "assignment_ack_seconds": 60,
                "lease_seconds": 60,
            },
            "lease timing",
        ),
    ],
)
def test_invalid_job_repository_config_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    jobs_update: dict[str, object],
    message: str,
) -> None:
    def mutate(raw):
        raw["jobs"].update(jobs_update)

    with pytest.raises(ValueError, match=message):
        _load(monkeypatch, tmp_path, mutate)


def test_job_and_operational_databases_cannot_share_a_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    shared = tmp_path / "shared.sqlite3"

    def mutate(raw):
        raw["database"]["path"] = str(shared)
        raw["jobs"].update({"enabled": True, "path": str(shared)})

    with pytest.raises(ValueError, match="separate"):
        _load(monkeypatch, tmp_path, mutate)


def test_runtime_job_database_separation_rejects_symlink_alias(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    operations_path = tmp_path / "operations.sqlite3"
    jobs_alias = tmp_path / "jobs-alias.sqlite3"
    operations_path.write_text("existing database", encoding="utf-8")
    jobs_alias.symlink_to(operations_path)

    def mutate(raw):
        raw["database"]["path"] = str(operations_path)
        raw["jobs"].update({"enabled": True, "path": str(jobs_alias)})

    with pytest.raises(ValueError, match="separate"):
        _load(monkeypatch, tmp_path, mutate)
