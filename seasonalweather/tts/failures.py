"""Bounded typed failures shared by synthesis execution boundaries."""

from __future__ import annotations


class ProcessFailure(RuntimeError):
    """A bounded, classified synthesis operation failure."""

    def __init__(self, classification: str, message: str) -> None:
        self.classification = classification
        super().__init__(message)
