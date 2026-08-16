from __future__ import annotations

import copy
import logging
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import AppConfig, LogsRuntimeConfig


_DEFAULT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_VALID_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
_VALID_COLOR_MODES = {"never", "auto", "always"}
_RESET = "\x1b[0m"
_DIM = "\x1b[2m"
_LEVEL_COLORS = {
    "DEBUG": "\x1b[2m",
    "INFO": "\x1b[32m",
    "WARNING": "\x1b[33m",
    "ERROR": "\x1b[31m",
    "CRITICAL": "\x1b[1;31m",
}
_LOGGER_COLOR = "\x1b[36m"


class _RuntimeMessageFilter(logging.Filter):
    """Filter routine steady-state log chatter based on config.yaml toggles."""

    def __init__(self, runtime_cfg: "LogsRuntimeConfig") -> None:
        super().__init__()
        self._runtime_cfg = runtime_cfg

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()

        if not self._runtime_cfg.cap_poll_summary and message.startswith("CAP poll: emitted "):
            return False
        if not self._runtime_cfg.ipaws_poll_summary and message.startswith("IPAWS poll: emitted="):
            return False

        if not self._runtime_cfg.conductor_alert_push and message.startswith("CycleConductor: → alert_"):
            return False
        if not self._runtime_cfg.conductor_live_time_push and message.startswith("CycleConductor: → time "):
            return False
        if not self._runtime_cfg.conductor_cycle_push and message.startswith("CycleConductor: → "):
            return False

        if not self._runtime_cfg.segment_refresher_synth and (
            message.startswith("SegmentRefresher: synthesising alert segment id=")
            or message.startswith("SegmentRefresher: alert script changed, re-synthesising id=")
            or message.startswith("SegmentRefresher: synthesised key=")
        ):
            return False
        if not self._runtime_cfg.segment_refresher_alert_lifecycle and (
            message.startswith("SegmentRefresher: alert segment expired/cancelled id=")
        ):
            return False

        return True


class _SlixmppOutputContainmentFilter(logging.Filter):
    """Keep dependency wire records out of configured application outputs."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (record.name == "slixmpp" or record.name.startswith("slixmpp."))


class _AnsiFormatter(logging.Formatter):
    """ANSI presentation formatter; never mutates the original LogRecord."""

    def format(self, record: logging.LogRecord) -> str:
        colored = copy.copy(record)
        level_color = _LEVEL_COLORS.get(str(record.levelname).upper(), "")
        if level_color:
            colored.levelname = f"{level_color}{record.levelname}{_RESET}"
        colored.name = f"{_LOGGER_COLOR}{record.name}{_RESET}"
        # Keep the timestamp dim but leave the application message itself clean.
        rendered = super().format(colored)
        if rendered:
            parts = rendered.split(" ", 2)
            if len(parts) >= 2:
                parts[0] = f"{_DIM}{parts[0]}"
                parts[1] = f"{parts[1]}{_RESET}"
                rendered = " ".join(parts)
        return rendered


def _normalize_level(value: str | None, *, default: str) -> str:
    level = str(value or default).strip().upper()
    return level if level in _VALID_LEVELS else default


def _normalize_color_mode(value: str | None, *, default: str = "never") -> str:
    mode = str(value or default).strip().lower()
    return mode if mode in _VALID_COLOR_MODES else default


def _should_use_color(mode: str) -> bool:
    if mode == "always":
        return True
    if mode == "auto":
        return bool(getattr(sys.stdout, "isatty", lambda: False)())
    return False


def _apply_level(logger_name: str, level_name: str) -> None:
    logging.getLogger(logger_name).setLevel(getattr(logging, level_name, logging.INFO))


def setup_logging(cfg: "AppConfig | None" = None) -> None:
    runtime = getattr(getattr(cfg, "logs", None), "runtime", None)
    root_level = _normalize_level(getattr(runtime, "level", None), default="INFO")
    color_mode = _normalize_color_mode(getattr(runtime, "color", None), default="never")

    logging.basicConfig(
        level=getattr(logging, root_level, logging.INFO),
        format=_DEFAULT_FORMAT,
        stream=sys.stdout,
        force=True,
    )

    root_logger = logging.getLogger()
    formatter: logging.Formatter
    if _should_use_color(color_mode):
        formatter = _AnsiFormatter(_DEFAULT_FORMAT)
        for handler in root_logger.handlers:
            handler.setFormatter(formatter)

    for handler in root_logger.handlers:
        handler.addFilter(_SlixmppOutputContainmentFilter())

    if runtime is None:
        return

    runtime_filter = _RuntimeMessageFilter(runtime)
    for handler in root_logger.handlers:
        handler.addFilter(runtime_filter)

    _apply_level("httpx", _normalize_level(runtime.httpx_level, default="WARNING"))
    _apply_level("httpcore", _normalize_level(runtime.httpcore_level, default="WARNING"))
    _apply_level("uvicorn.access", _normalize_level(runtime.uvicorn_access_level, default="WARNING"))
    _apply_level("uvicorn.error", _normalize_level(runtime.uvicorn_error_level, default="INFO"))
    _apply_level("asyncio", _normalize_level(runtime.asyncio_level, default="WARNING"))
    _apply_level("slixmpp", _normalize_level(runtime.slixmpp_level, default="WARNING"))
    _apply_level("slixmpp.xmlstream", _normalize_level(runtime.slixmpp_xmlstream_level, default="WARNING"))

    for logger_name, level_name in (runtime.logger_levels or {}).items():
        name = str(logger_name).strip()
        if not name:
            continue
        _apply_level(name, _normalize_level(level_name, default="INFO"))
