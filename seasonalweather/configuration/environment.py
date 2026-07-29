"""Centralized bounded environment resolution for configuration values."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class EnvironmentValues:
    values: Mapping[str, str] = field(repr=False)

    def optional(self, name: str, default: str = "") -> str:
        value = self.values.get(name, "").strip()
        return value if value else default

    def raw_optional(self, name: str) -> str:
        return self.values.get(name, "")

    def required(self, name: str) -> str:
        value = self.values.get(name, "").strip()
        if not value:
            raise RuntimeError(
                f"Required environment variable {name!r} is not set. Check /etc/seasonalweather/seasonalweather.env."
            )
        return value

    def integer(self, name: str, default: int) -> int:
        value = self.values.get(name, "").strip()
        if not value:
            return default
        try:
            return int(value)
        except ValueError:
            return default
