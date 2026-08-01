"""Shared hard limits for the complete configuration-validation envelope."""

from __future__ import annotations

# Sixty-four admitted probes at the four-worker ceiling and thirty-second
# per-probe maximum require approximately 480 seconds. The remaining time is
# reserved for bounded worker-group cleanup and the deterministic typed stages.
VALIDATION_ENVELOPE_SECONDS = 600
