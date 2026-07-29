from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _run(code: str, *, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
    )


def test_fatal_boundary_exits_nonzero_preserves_chain_and_redacts() -> None:
    result = _run(
        """
from seasonalweather.runtime_diagnostics.fatal import FatalBoundary
from seasonalweather.runtime_diagnostics.models import CorrelationContext, DiagnosticRole
boundary = FatalBoundary(None, CorrelationContext(
    role=DiagnosticRole.WORKER,
    instance_id="worker_00000001",
    component="simulated-worker",
))
def fail():
    try:
        raise ValueError("password=synthetic-secret")
    except ValueError as cause:
        raise RuntimeError("outer failure") from cause
raise SystemExit(boundary.run_process(fail))
"""
    )
    assert result.returncode != 0
    assert "fatal[SWRUN5001]" in result.stderr
    assert "synthetic-secret" not in result.stderr
    assert "RuntimeError" in result.stderr


def test_fatal_boundary_exception_group_does_not_kill_parent() -> None:
    result = _run(
        """
from seasonalweather.runtime_diagnostics.fatal import FatalBoundary
from seasonalweather.runtime_diagnostics.models import CorrelationContext, DiagnosticRole
boundary = FatalBoundary(None, CorrelationContext(
    role=DiagnosticRole.WORKER,
    instance_id="worker_00000001",
    component="simulated-worker",
))
raise SystemExit(boundary.run_process(
    lambda: (_ for _ in ()).throw(ExceptionGroup("group", [ValueError("one"), TypeError("two")]))
))
"""
    )
    assert result.returncode != 0
    assert "ExceptionGroup" in result.stderr


def test_fatal_boundary_survives_persistence_logging_and_flush_failures() -> None:
    result = _run(
        """
import logging, time
import seasonalweather.runtime_diagnostics.fatal as fatal
from seasonalweather.runtime_diagnostics.models import CorrelationContext, DiagnosticRole
class Instance:
    def to_dict(self):
        return {
            "code": "SWRUN5001",
            "message": "bounded fatal",
            "context": {"role": "worker", "instance_id": "worker_00000001", "component": "simulated-worker"},
            "exception_evidence": {"type": "builtins.RuntimeError", "message": "primary failure", "frames": []},
        }
class Service:
    def build(self, **values):
        return Instance()
    def promote(self, instance):
        raise OSError("password=synthetic-secret")
class BrokenHandler(logging.Handler):
    def emit(self, record):
        raise RuntimeError("logging unavailable")
    def flush(self):
        time.sleep(1)
logging.getLogger().handlers[:] = [BrokenHandler()]
boundary = fatal.FatalBoundary(Service(), CorrelationContext(
    role=DiagnosticRole.WORKER,
    instance_id="worker_00000001",
    component="simulated-worker",
))
raise SystemExit(boundary.run_process(lambda: (_ for _ in ()).throw(RuntimeError("primary failure"))))
"""
    )
    assert result.returncode != 0
    assert "fatal[SWRUN5001]" in result.stderr
    assert "primary failure" in result.stderr
    assert "synthetic-secret" not in result.stderr


def test_fatal_flush_exception_is_sanitized_and_bounded() -> None:
    result = _run(
        """
import logging
from seasonalweather.runtime_diagnostics.fatal import FatalBoundary
from seasonalweather.runtime_diagnostics.models import CorrelationContext, DiagnosticRole
class BrokenFlush(logging.Handler):
    def emit(self, record):
        pass
    def flush(self):
        raise RuntimeError("password=synthetic-secret")
logging.getLogger().handlers[:] = [BrokenFlush()]
boundary = FatalBoundary(None, CorrelationContext(
    role=DiagnosticRole.WORKER,
    instance_id="worker_00000001",
    component="simulated-worker",
))
raise SystemExit(boundary.run_process(lambda: (_ for _ in ()).throw(RuntimeError("primary failure"))))
"""
    )
    assert result.returncode == 1
    assert "secondary_failure=log_flush_failed" in result.stderr
    assert "synthetic-secret" not in result.stderr


def test_fatal_boundary_survives_direct_renderer_failure() -> None:
    result = _run(
        """
import seasonalweather.runtime_diagnostics.fatal as fatal
from seasonalweather.runtime_diagnostics.models import CorrelationContext, DiagnosticRole
fatal.emergency_bytes = lambda payload: (_ for _ in ()).throw(RuntimeError("renderer failed"))
boundary = fatal.FatalBoundary(None, CorrelationContext(
    role=DiagnosticRole.WORKER,
    instance_id="worker_00000001",
    component="simulated-worker",
))
raise SystemExit(boundary.run_process(lambda: (_ for _ in ()).throw(RuntimeError("primary failure"))))
"""
    )
    assert result.returncode != 0
    assert "fatal[unavailable]" in result.stderr


def test_fatal_boundary_survives_actual_direct_write_failure() -> None:
    result = _run(
        """
import seasonalweather.runtime_diagnostics.fatal as fatal
from seasonalweather.runtime_diagnostics.models import CorrelationContext, DiagnosticRole
fatal.os.write = lambda fd, data: (_ for _ in ()).throw(OSError("stderr unavailable"))
boundary = fatal.FatalBoundary(None, CorrelationContext(
    role=DiagnosticRole.WORKER,
    instance_id="worker_00000001",
    component="simulated-worker",
))
raise SystemExit(boundary.run_process(lambda: (_ for _ in ()).throw(RuntimeError("primary failure"))))
"""
    )
    assert result.returncode == 1
    assert result.stderr == ""


def test_fatal_import_is_transitively_isolated_from_database() -> None:
    result = _run(
        """
import sys
import seasonalweather.runtime_diagnostics.fatal
assert "seasonalweather.database" not in sys.modules
assert "seasonalweather.runtime_diagnostics.repository" not in sys.modules
assert "seasonalweather.runtime_diagnostics.service" not in sys.modules
"""
    )
    assert result.returncode == 0


def test_emergency_truncation_is_utf8_safe() -> None:
    from seasonalweather.runtime_diagnostics.fatal import (
        MAX_EMERGENCY_BYTES,
        emergency_bytes,
    )

    payload = {
        "code": "SWRUN5001",
        "message": "é" * MAX_EMERGENCY_BYTES,
        "context": {
            "role": "worker",
            "instance_id": "worker_00000001",
            "component": "simulated-worker",
        },
        "exception_evidence": {
            "type": "builtins.RuntimeError",
            "message": "é" * MAX_EMERGENCY_BYTES,
            "frames": [],
        },
    }
    encoded = emergency_bytes(payload)
    assert len(encoded) <= MAX_EMERGENCY_BYTES
    assert encoded.decode("utf-8").endswith("…\n")


def test_emergency_renderer_reports_nested_truncation_and_cycles() -> None:
    from seasonalweather.runtime_diagnostics.fatal import emergency_bytes

    payload = {
        "code": "SWRUN5001",
        "message": "bounded fatal",
        "context": {
            "role": "worker",
            "instance_id": "worker_00000001",
            "component": "simulated-worker",
        },
        "exception_evidence": {
            "type": "builtins.ExceptionGroup",
            "message": "root",
            "truncated": {"group_members": 2, "detail": "password=stderr-private"},
            "cause": {
                "type": "builtins.RuntimeError",
                "message": "cause",
                "truncated": {"cycle": True},
            },
            "context": {
                "type": "builtins.LookupError",
                "message": "context",
                "truncated": {"chain_depth": True},
            },
            "members": [
                {
                    "type": "builtins.ValueError",
                    "message": "member",
                    "truncated": {"frames": True},
                }
            ],
        },
    }
    rendered = emergency_bytes(payload).decode("utf-8")
    assert "truncated[group_members]=2" in rendered
    assert "truncated[cycle]=true" in rendered
    assert "truncated[chain_depth]=true" in rendered
    assert "truncated[frames]=true" in rendered
    assert "stderr-private" not in rendered
    assert "[REDACTED]" in rendered
    assert len(rendered.encode("utf-8")) <= 16_384


def test_clean_boundary_returns_zero() -> None:
    result = _run(
        """
from seasonalweather.runtime_diagnostics.fatal import FatalBoundary
from seasonalweather.runtime_diagnostics.models import CorrelationContext, DiagnosticRole
boundary = FatalBoundary(None, CorrelationContext(
    role=DiagnosticRole.WORKER,
    instance_id="worker_00000001",
    component="simulated-worker",
))
assert boundary.run(lambda: 7) == 7
"""
    )
    assert result.returncode == 0
    assert result.stderr == ""


def test_offline_cli_does_not_create_marker_or_occurrence_database(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    commands = (
        [sys.executable, "-m", "seasonalweather", "diagnostics", "list", "--format", "json"],
        [
            sys.executable,
            "-m",
            "seasonalweather",
            "config",
            "lint",
            "--config",
            str(repo / "config/config.yaml"),
        ],
        [sys.executable, "-m", "seasonalweather", "auth", "--help"],
    )
    for command in commands:
        result = subprocess.run(
            command,
            cwd=tmp_path,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "PYTHONPATH": str(repo)},
        )
        assert result.returncode == 0
    assert list(tmp_path.iterdir()) == []
