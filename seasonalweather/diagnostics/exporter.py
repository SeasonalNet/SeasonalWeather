"""Deterministic clean-tree operator export from packaged resources."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from .loader import load_catalog, packaged_catalog_bytes, packaged_explanation_bytes


class CatalogExportError(ValueError):
    pass


def export_catalog(destination: Path) -> Path:
    if ".." in destination.parts:
        raise CatalogExportError("Export destination cannot contain parent traversal.")
    selected = destination.absolute()
    _reject_symlink_chain(selected)
    parent = selected.parent
    parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".seasonalweather-diagnostics.", dir=parent))
    backup: Path | None = None
    try:
        _populate_stage(stage)
        backup = _move_existing_to_backup(selected, parent)
        os.replace(stage, selected)
        if backup is not None:
            shutil.rmtree(backup)
        return selected
    except OSError as exc:
        if backup is not None and backup.exists() and not selected.exists():
            os.replace(backup, selected)
        raise CatalogExportError("Diagnostic catalog export failed.") from exc
    finally:
        if stage.exists():
            shutil.rmtree(stage)
        if backup is not None and backup.exists():
            shutil.rmtree(backup)


def _populate_stage(stage: Path) -> None:
    (stage / "explanations").mkdir()
    (stage / "catalog.json").write_bytes(packaged_catalog_bytes())
    for definition in load_catalog().definitions:
        target = stage / definition.explanation_path
        target.write_bytes(packaged_explanation_bytes(definition.explanation_path))


def _move_existing_to_backup(selected: Path, parent: Path) -> Path | None:
    if not selected.exists():
        return None
    backup = Path(tempfile.mkdtemp(prefix=".seasonalweather-diagnostics-old.", dir=parent))
    backup.rmdir()
    os.replace(selected, backup)
    return backup


def _reject_symlink_chain(path: Path) -> None:
    for candidate in (path, *path.parents):
        if candidate.exists() and candidate.is_symlink():
            raise CatalogExportError("Export destination cannot traverse a symlink.")
