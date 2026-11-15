"""Bounded typed failures shared by synthesis execution boundaries."""

from __future__ import annotations


class ProcessFailure(RuntimeError):
    """A bounded, classified synthesis operation failure."""

    def __init__(
        self,
        classification: str,
        message: str,
        *,
        returncode: int | None = None,
        stderr: str = "",
    ) -> None:
        self.classification = classification
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(message)
