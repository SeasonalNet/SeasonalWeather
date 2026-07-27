"""Root-confined staged-file claims and immutable content-addressed storage."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .hashing import ContentIdentity, hash_file
from .models import ArtifactReference


@dataclass(frozen=True)
class ClaimedArtifact:
    path: Path
    identity: ContentIdentity


class StagingService:
    """Claim worker bytes beneath one configured root; workers never choose destinations."""

    def __init__(self, staging_root: Path, blob_root: Path, *, maximum_bytes: int) -> None:
        self.staging_root = Path(staging_root)
        self.blob_root = Path(blob_root)
        self.maximum_bytes = maximum_bytes
        self.pending_root = self.staging_root / ".controller-pending"

    def _source_path(self, reference: ArtifactReference) -> Path:
        root = self.staging_root / reference.staging_namespace
        candidate = root.joinpath(*reference.staging_token.split("/"))
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError("staging reference escapes configured root") from exc
        current = self.staging_root
        try:
            staging_metadata = current.stat(follow_symlinks=False)
        except FileNotFoundError as exc:
            raise ValueError("staging root is missing") from exc
        if stat.S_ISLNK(staging_metadata.st_mode) or not stat.S_ISDIR(staging_metadata.st_mode):
            raise ValueError("staging root must be a real directory")
        for component in (reference.staging_namespace, *reference.staging_token.split("/")[:-1]):
            current = current / component
            try:
                metadata = current.stat(follow_symlinks=False)
            except FileNotFoundError as exc:
                raise ValueError("staging namespace is missing") from exc
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("staging path contains an unsafe component")
        return candidate

    def claim(self, reference: ArtifactReference) -> ClaimedArtifact:
        """Atomically take worker output before inspecting its bytes.

        Pending storage is controller-owned but on the staging filesystem; a
        cross-filesystem claim fails closed instead of copying mutable bytes.
        """
        self._source_path(reference)
        self.pending_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        pending_metadata = self.pending_root.stat(follow_symlinks=False)
        if stat.S_ISLNK(pending_metadata.st_mode) or not stat.S_ISDIR(pending_metadata.st_mode):
            raise ValueError("controller pending root must be a real directory")
        source_fd, parent_fd, source_identity = self._open_source(reference)
        try:
            pending_fd = os.open(
                self.pending_root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError as exc:
            os.close(source_fd)
            os.close(parent_fd)
            raise ValueError("controller pending root cannot be opened safely") from exc
        if os.fstat(pending_fd).st_dev != os.fstat(parent_fd).st_dev:
            os.close(source_fd)
            os.close(parent_fd)
            os.close(pending_fd)
            raise ValueError("staging and controller pending storage must share a filesystem")
        pending, identity = self._claim_copy(
            reference.staging_token.split("/")[-1],
            source_fd,
            parent_fd,
            pending_fd,
            source_identity,
        )
        if identity.sha256 != reference.claimed_sha256 or identity.size_bytes != reference.claimed_size_bytes:
            pending.unlink(missing_ok=True)
            raise ValueError("claimed artifact identity does not match controller computation")
        return ClaimedArtifact(path=pending, identity=identity)

    def _open_source(self, reference: ArtifactReference) -> tuple[int, int, tuple[int, int, int, int]]:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        directory_fd = os.open(self.staging_root, directory_flags)
        try:
            for component in (reference.staging_namespace, *reference.staging_token.split("/")[:-1]):
                next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            source_fd = os.open(
                reference.staging_token.split("/")[-1],
                os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
        except OSError as exc:
            os.close(directory_fd)
            raise ValueError("staged artifact cannot be opened safely") from exc
        try:
            source_identity = self._descriptor_identity(source_fd)
        except BaseException:
            os.close(source_fd)
            os.close(directory_fd)
            raise
        return source_fd, directory_fd, source_identity

    def _claim_copy(
        self,
        leaf_name: str,
        source_fd: int,
        parent_fd: int,
        pending_fd: int,
        source_identity: tuple[int, int, int, int],
    ) -> tuple[Path, ContentIdentity]:
        quarantine_fd, quarantine_name = tempfile.mkstemp(prefix=".source-", dir=self.pending_root)
        os.close(quarantine_fd)
        quarantine = Path(quarantine_name)
        try:
            os.rename(
                leaf_name,
                quarantine.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=pending_fd,
            )
            if source_identity != self._safe_file_identity(quarantine):
                raise ValueError("staged artifact changed during exclusive claim")
            pending, identity = self._copy_descriptor(source_fd)
            if source_identity != self._descriptor_identity(source_fd):
                pending.unlink(missing_ok=True)
                raise ValueError("staged artifact changed while being copied")
        except BaseException:
            raise
        finally:
            os.close(source_fd)
            os.close(parent_fd)
            os.close(pending_fd)
            quarantine.unlink(missing_ok=True)
        return pending, identity

    def _descriptor_identity(self, descriptor: int) -> tuple[int, int, int, int]:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("staged artifact must be an unlinked regular file")
        if metadata.st_size < 1 or metadata.st_size > self.maximum_bytes:
            raise ValueError("staged artifact has unsafe file metadata")
        allocated = getattr(metadata, "st_blocks", 0) * 512
        if allocated and allocated + 4096 < metadata.st_size:
            raise ValueError("sparse staged artifacts are not accepted")
        return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)

    def _copy_descriptor(self, source_fd: int) -> tuple[Path, ContentIdentity]:
        target_fd, name = tempfile.mkstemp(prefix=".claim-", dir=self.pending_root)
        target = Path(name)
        digest = hashlib.sha256()
        size = 0
        try:
            os.lseek(source_fd, 0, os.SEEK_SET)
            while chunk := os.read(source_fd, 65_536):
                size += len(chunk)
                if size > self.maximum_bytes:
                    raise ValueError("artifact exceeds configured size limit")
                view = memoryview(chunk)
                while view:
                    written = os.write(target_fd, view)
                    if written < 1:
                        raise OSError("short write while claiming staged artifact")
                    view = view[written:]
                digest.update(chunk)
            os.fsync(target_fd)
            os.chmod(target, 0o600)
            return target, ContentIdentity(f"sha256:{digest.hexdigest()}", size)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        finally:
            os.close(target_fd)

    def cleanup_pending(self, *, maximum_files: int = 64) -> int:
        """Remove bounded orphaned controller copies left before durable prepare."""
        if maximum_files < 1:
            return 0
        try:
            entries = os.scandir(self.pending_root)
        except FileNotFoundError:
            return 0
        removed = 0
        with entries:
            for raw_entry in entries:
                if removed >= maximum_files:
                    break
                if not raw_entry.name.startswith((".claim-", ".source-")):
                    continue
                try:
                    metadata = raw_entry.stat(follow_symlinks=False)
                    if stat.S_ISREG(metadata.st_mode):
                        os.unlink(raw_entry.path)
                        removed += 1
                except FileNotFoundError:
                    continue
        if removed:
            self._fsync_directory(self.pending_root)
        return removed

    def pending_count(self, *, limit: int = 257) -> int:
        """Return a bounded pending-file count suitable for health reporting."""
        try:
            entries = os.scandir(self.pending_root)
        except FileNotFoundError:
            return 0
        count = 0
        with entries:
            for entry in entries:
                if entry.name.startswith((".claim-", ".source-")):
                    count += 1
                    if count >= limit:
                        break
        return count

    def recover_claim(self, identity: ContentIdentity, *, maximum_files: int = 256) -> ClaimedArtifact | None:
        """Find a bounded service-owned pending copy by recomputed identity."""
        try:
            entries = os.scandir(self.pending_root)
        except FileNotFoundError:
            return None
        inspected = 0
        with entries:
            for raw_entry in entries:
                if inspected >= maximum_files:
                    break
                if not raw_entry.name.startswith(".claim-"):
                    continue
                inspected += 1
                entry = self.pending_root / raw_entry.name
                try:
                    metadata = raw_entry.stat(follow_symlinks=False)
                    if (
                        stat.S_ISREG(metadata.st_mode)
                        and metadata.st_nlink == 1
                        and hash_file(entry, maximum_bytes=self.maximum_bytes) == identity
                    ):
                        return ClaimedArtifact(entry, identity)
                except (FileNotFoundError, ValueError):
                    continue
        return None

    def _safe_file_identity(self, path: Path) -> tuple[int, int, int, int]:
        try:
            root_stat = self.staging_root.stat(follow_symlinks=False)
            file_stat = path.stat(follow_symlinks=False)
        except FileNotFoundError as exc:
            raise ValueError("staged artifact is missing") from exc
        if stat.S_ISLNK(root_stat.st_mode) or stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("staged artifact must be a regular non-symlink file")
        if file_stat.st_nlink != 1 or file_stat.st_size < 1 or file_stat.st_size > self.maximum_bytes:
            raise ValueError("staged artifact has unsafe file metadata")
        return (file_stat.st_dev, file_stat.st_ino, file_stat.st_size, file_stat.st_mtime_ns)

    def import_claimed(self, claim: ClaimedArtifact) -> Path:
        destination = self.blob_path(claim.identity.sha256)
        self._ensure_blob_directory(destination.parent)
        if destination.exists():
            try:
                metadata = destination.stat(follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                    raise RuntimeError("content-addressed destination is not a regular file")
                existing = hash_file(destination, maximum_bytes=self.maximum_bytes)
                if existing != claim.identity:
                    raise RuntimeError("content-addressed digest collision or corruption")
                return destination
            finally:
                claim.path.unlink(missing_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=".artifact-", dir=destination.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as target, claim.path.open("rb") as source:
                shutil.copyfileobj(source, target, length=65_536)
                target.flush()
                os.fsync(target.fileno())
            os.chmod(temporary, 0o440)
            if hash_file(temporary, maximum_bytes=self.maximum_bytes) != claim.identity:
                raise RuntimeError("copied blob identity differs from claimed bytes")
            try:
                os.link(temporary, destination)
            except FileExistsError as exc:
                existing = hash_file(destination, maximum_bytes=self.maximum_bytes)
                if existing != claim.identity:
                    raise RuntimeError("content-addressed digest collision or corruption") from exc
            self._fsync_directory(destination.parent)
            return destination
        finally:
            temporary.unlink(missing_ok=True)
            claim.path.unlink(missing_ok=True)

    def _ensure_blob_directory(self, leaf: Path) -> None:
        self.blob_root.mkdir(mode=0o750, parents=True, exist_ok=True)
        current = self.blob_root
        components = leaf.relative_to(self.blob_root).parts
        for component in ("", *components):
            if component:
                current = current / component
                current.mkdir(mode=0o750, exist_ok=True)
            metadata = current.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("blob path contains an unsafe component")

    def blob_path(self, canonical_digest: str) -> Path:
        digest = canonical_digest.removeprefix("sha256:")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("blob digest is not canonical SHA-256")
        return self.blob_root / "sha256" / digest[:2] / digest[2:4] / digest

    def storage_available(self) -> bool:
        return all(
            path.is_dir() and not path.is_symlink() and os.access(path, os.W_OK | os.X_OK)
            for path in (self.staging_root, self.blob_root)
        )

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
