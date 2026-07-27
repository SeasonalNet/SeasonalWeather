"""Streaming content identity helpers with explicit byte bounds."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ContentIdentity:
    sha256: str
    size_bytes: int


def hash_file(path: Path, *, maximum_bytes: int, chunk_size: int = 65_536) -> ContentIdentity:
    if maximum_bytes < 1 or chunk_size < 1:
        raise ValueError("hash bounds must be positive")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(chunk_size):
            size += len(chunk)
            if size > maximum_bytes:
                raise ValueError("artifact exceeds configured size limit")
            digest.update(chunk)
    if size < 1:
        raise ValueError("artifact must not be empty")
    return ContentIdentity(f"sha256:{digest.hexdigest()}", size)
