"""Compatibility exports; formatter implementation lives in .formatters."""

from .formatters import (
    build_now_script,
    extract_now_narrative,
)

__all__ = [
    "extract_now_narrative",
    "build_now_script",
]
