"""Authoritative source-mapped configuration compiler."""

from .compiler import CompiledConfiguration, compile_path, compile_source
from .renderer import render_report
from .report import CompileReport
from .source import CompilerLimits, SourceDocument

__all__ = [
    "CompileReport",
    "CompiledConfiguration",
    "CompilerLimits",
    "SourceDocument",
    "compile_path",
    "compile_source",
    "render_report",
]
