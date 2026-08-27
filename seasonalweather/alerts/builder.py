"""Compatibility exports; formatter implementation lives in ..broadcast.formatters."""

from ..broadcast.formatters import (
    SpokenAlert,
    _clean_line,
    _collapse_blank_lines,
    _find_body_start,
    _looks_like_all_caps_prose,
    _sentence_case_all_caps_prose,
    _unwrap_soft_wrap,
    build_spoken_alert,
    build_spoken_alert_full,
    strip_nws_product_headers,
)

__all__ = [
    "_looks_like_all_caps_prose",
    "_sentence_case_all_caps_prose",
    "SpokenAlert",
    "strip_nws_product_headers",
    "_unwrap_soft_wrap",
    "_collapse_blank_lines",
    "_find_body_start",
    "_clean_line",
    "build_spoken_alert_full",
    "build_spoken_alert",
]
