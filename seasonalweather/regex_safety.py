"""Architecture-safe facade for the single bounded TTS regex authority."""

from .tts.regex_safety import (
    MAX_CONFIGURED_REGEX_PATTERN,
    MAX_CONFIGURED_REGEX_REPLACEMENT,
    MAX_CONFIGURED_REGEX_REPLACEMENTS,
    MAX_CONFIGURED_REGEX_RULES,
    compile_safe_regex,
    validate_replacement,
    validate_safe_regex,
)

__all__ = [
    "MAX_CONFIGURED_REGEX_PATTERN",
    "MAX_CONFIGURED_REGEX_REPLACEMENT",
    "MAX_CONFIGURED_REGEX_REPLACEMENTS",
    "MAX_CONFIGURED_REGEX_RULES",
    "compile_safe_regex",
    "validate_replacement",
    "validate_safe_regex",
]
