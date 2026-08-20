from __future__ import annotations

from pathlib import Path
from typing import Any

from .core import SeasonalDatabase


def bootstrap_database_from_config(cfg: Any) -> SeasonalDatabase:
    raw_path = str(getattr(cfg.database, "path", "") or "").strip()
    if raw_path:
        db_path = raw_path
    else:
        db_path = str(Path(cfg.paths.operational_state_dir) / "seasonalweather.sqlite3")
    db = SeasonalDatabase(
        path=db_path,
        busy_timeout_ms=int(getattr(cfg.database, "busy_timeout_ms", 5000) or 5000),
        journal_mode=str(getattr(cfg.database, "journal_mode", "WAL") or "WAL"),
    )
    db.bootstrap()
    return db
