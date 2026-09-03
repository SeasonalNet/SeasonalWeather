"""Lazy compiler API; shared source/path models do not load runtime owners."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .compiler import CompiledConfiguration, compile_path, compile_source
    from .loader import ConfigurationCompileError, build_runtime_config, load_runtime_config
    from .renderer import render_report
    from .report import CompileReport
    from .source import CompilerLimits, SourceDocument

_EXPORTS = {
    "CompileReport": "report",
    "CompiledConfiguration": "compiler",
    "CompilerLimits": "source",
    "ConfigurationCompileError": "loader",
    "SourceDocument": "source",
    "compile_path": "compiler",
    "compile_source": "compiler",
    "build_runtime_config": "loader",
    "load_runtime_config": "loader",
    "render_report": "renderer",
}

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


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value
    return value
