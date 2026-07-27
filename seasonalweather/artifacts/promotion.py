"""Controller-only active-path publication.  No worker path is accepted here."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .hashing import ContentIdentity, hash_file


@dataclass(frozen=True)
class PromotionReceipt:
    target_key: str
    prior_digest: str | None
    promoted_digest: str
    changed: bool


class PromotionService:
    def __init__(self, active_root: Path, *, maximum_bytes: int) -> None:
        self.active_root = Path(active_root)
        self.maximum_bytes = maximum_bytes
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, target_key: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(target_key, threading.Lock())

    def active_identity(self, target_key: str) -> ContentIdentity | None:
        if not target_key or "/" in target_key or "\\" in target_key or ".." in target_key:
            raise ValueError("active target key is not permitted")
        target = self.active_root / target_key
        if not target.exists():
            return None
        metadata = target.stat(follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ValueError("active target must be a regular non-symlink file")
        return hash_file(target, maximum_bytes=self.maximum_bytes)

    def storage_available(self) -> bool:
        return (
            self.active_root.is_dir()
            and not self.active_root.is_symlink()
            and os.access(self.active_root, os.W_OK | os.X_OK)
        )

    def target_present(self, target_key: str) -> bool:
        if not target_key or "/" in target_key or "\\" in target_key or ".." in target_key:
            raise ValueError("active target key is not permitted")
        try:
            metadata = (self.active_root / target_key).stat(follow_symlinks=False)
        except FileNotFoundError:
            return False
        return stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def promote(
        self,
        blob: Path,
        identity: ContentIdentity,
        *,
        target_key: str,
        authorize: Callable[[], None] = lambda: None,
    ) -> PromotionReceipt:
        if not target_key or "/" in target_key or "\\" in target_key or ".." in target_key:
            raise ValueError("active target key is not permitted")
        with self._lock_for(target_key):
            authorize()
            return self._promote_locked(blob, identity, target_key)

    def _promote_locked(self, blob: Path, identity: ContentIdentity, target_key: str) -> PromotionReceipt:
        self.active_root.mkdir(mode=0o750, parents=True, exist_ok=True)
        root_stat = self.active_root.stat(follow_symlinks=False)
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            raise ValueError("active root must be a real directory")
        target = self.active_root / target_key
        prior: ContentIdentity | None = None
        if target.exists():
            metadata = target.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise ValueError("active target must be a regular non-symlink file")
            prior = hash_file(target, maximum_bytes=self.maximum_bytes)
            if prior == identity:
                return PromotionReceipt(target_key, prior.sha256, identity.sha256, False)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{target_key}.", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as output, blob.open("rb") as source:
                shutil.copyfileobj(source, output, length=65_536)
                output.flush()
                os.fsync(output.fileno())
            if hash_file(temporary, maximum_bytes=self.maximum_bytes) != identity:
                raise RuntimeError("controller blob changed before promotion")
            os.chmod(temporary, 0o640)
            os.replace(temporary, target)
            self._fsync_directory(target.parent)
            if hash_file(target, maximum_bytes=self.maximum_bytes) != identity:
                raise RuntimeError("active target identity is uncertain after replace")
            return PromotionReceipt(target_key, prior.sha256 if prior else None, identity.sha256, True)
        finally:
            temporary.unlink(missing_ok=True)
