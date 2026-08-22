"""Artifact-byte transport boundaries used by controller composition."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ArtifactStoragePaths:
    """Controller-owned paths for one artifact transport implementation."""

    staging: Path
    blobs: Path
    active: Path


class ArtifactTransport(Protocol):
    """Locate artifact bytes without putting them on the control protocol."""

    @property
    def paths(self) -> ArtifactStoragePaths: ...


@dataclass(frozen=True)
class SharedVolumeArtifactTransport:
    """Initial same-host transport backed by a shared artifact volume.

    Workers receive a read-write mount only for ``paths.staging``. The
    controller keeps ownership of ``paths.blobs`` and ``paths.active``;
    SWWP carries references and result metadata, never large artifact bytes.
    """

    artifact_root: Path

    @property
    def paths(self) -> ArtifactStoragePaths:
        worker_root = Path(self.artifact_root) / "worker-artifacts"
        return ArtifactStoragePaths(
            staging=worker_root / "staging",
            blobs=worker_root / "blobs",
            active=worker_root / "active",
        )
