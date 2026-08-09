"""Authoritative source-mapped configuration compiler."""

from .compiler import CompiledConfiguration, compile_path, compile_source
from .loader import ConfigurationCompileError, build_runtime_config, load_runtime_config
from .renderer import render_report
from .report import CompileReport
from .source import CompilerLimits, SourceDocument

__all__ = [
    "CompileReport",
    "CompiledConfiguration",
    "CompilerLimits",
    "ConfigurationCompileError",
    "SourceDocument",
    "compile_path",
    "compile_source",
    "build_runtime_config",
    "load_runtime_config",
    "render_report",
]
