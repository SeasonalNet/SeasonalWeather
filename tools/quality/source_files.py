"""Discover repository source without traversing generated dependency artifacts."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

_GENERATED_DIRECTORIES = frozenset(
    {".git", "build", "dist", ".cache", ".mypy_cache", ".pytest_cache", ".ruff_cache", "__pycache__"}
)


def _generated_directory(name: str) -> bool:
    return name in _GENERATED_DIRECTORIES or name == ".venv" or name.startswith(".venv-")


def _raise_walk_error(error: OSError) -> None:
    raise error


def source_files(root: Path) -> Iterator[Path]:
    """Visit source files, including untracked files, and prune generated trees."""

    for directory, children, files in os.walk(root, onerror=_raise_walk_error):
        children[:] = sorted(name for name in children if not _generated_directory(name))
        for name in sorted(files):
            yield Path(directory) / name


def python_sources(root: Path) -> Iterator[Path]:
    return (path for path in source_files(root) if path.suffix == ".py")
