"""Service-owned immutable configuration candidate and report artifacts."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from seasonalweather.configuration.compiler import CompiledConfiguration, compile_source
from seasonalweather.configuration.origins import ENVIRONMENT_BINDINGS
from seasonalweather.configuration.source import DEFAULT_LIMITS, SourceDocument
from seasonalweather.diagnostics.bindings import RELOAD_CODES, code_for_rule
from seasonalweather.validation.candidate_identity import canonical_report_sha256
from seasonalweather.validation.pipeline import CandidateIdentity, EnvironmentInputIdentity

from .models import CandidateRecord


class CandidateStoreError(RuntimeError):
    diagnostic_code = RELOAD_CODES["candidate_or_preparation_failed"]


class CandidateIntegrityError(CandidateStoreError):
    diagnostic_code = code_for_rule("validation.report_rejected")


class CandidateStore:
    """Captures exact bounded bytes without trusting caller-selected artifact paths."""

    def __init__(
        self,
        root: str | Path,
        *,
        environ: Mapping[str, str] | None = None,
        clock: Any = lambda: dt.datetime.now(dt.UTC),
        identity_key: bytes | None = None,
    ) -> None:
        self.root = Path(root)
        self._environ = os.environ if environ is None else environ
        self._clock = clock
        self._identity_key = identity_key
        self._initialize_root()

    def _initialize_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink() or not self.root.is_dir():
            raise CandidateStoreError("candidate store root is not a service-owned directory")
        os.chmod(self.root, 0o700)

    def _key(self) -> bytes:
        if self._identity_key is not None:
            if len(self._identity_key) < 32:
                raise CandidateStoreError("candidate identity key is too short")
            return self._identity_key
        path = self.root / ".identity-key"
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            value = secrets.token_bytes(32)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                return self._key()
            try:
                os.write(fd, value)
                os.fsync(fd)
            finally:
                os.close(fd)
            return value
        try:
            value = os.read(fd, 64)
        finally:
            os.close(fd)
        if len(value) != 32:
            raise CandidateStoreError("candidate identity key is malformed")
        return value

    def environment_identities(self) -> tuple[EnvironmentInputIdentity, ...]:
        key = self._key()
        output: list[EnvironmentInputIdentity] = []
        for _path, variable, _default in sorted(ENVIRONMENT_BINDINGS, key=lambda item: item[1]):
            raw = self._environ.get(variable, "")
            present = bool(raw)
            opaque = None
            if present:
                digest = hmac.new(key, f"{variable}\0{raw}".encode(), hashlib.sha256).hexdigest()
                opaque = f"hmac-sha256:{digest}"
            output.append(EnvironmentInputIdentity(variable, present, opaque))
        return tuple(output)

    def capture(self, source_path: str | Path) -> tuple[CandidateRecord, CompiledConfiguration]:
        source_name = str(source_path)
        data = self._read_exact(source_path)
        source = SourceDocument.from_bytes(data, source_id=source_name)
        compiled = compile_source(source, environ=self._environ)
        identity = CandidateIdentity.from_compiled(
            compiled,
            environment_inputs=self.environment_identities(),
        )
        reference = f"candidate_{identity.identity_sha256[:40]}"
        captured_at = self._clock().astimezone(dt.UTC)
        record = CandidateRecord(
            reference=reference,
            source_name=source_name,
            source_sha256=source.digest,
            candidate_sha256=str(identity.sha256),
            byte_length=len(data),
            candidate_identity_sha256=identity.identity_sha256,
            config_schema_version=identity.config_schema_version,
            source_manifest=tuple(item.to_dict() for item in identity.source_manifest),
            origin_manifest=tuple(cast(dict[str, object], item.to_dict()) for item in identity.origin_manifest),
            environment_inputs=tuple(item.to_dict() for item in identity.environment_inputs),
            captured_at=captured_at,
        )
        if (self.root / reference).exists():
            existing = self.load(reference)
            if self._stable_metadata(existing) != self._stable_metadata(record) or self.read_bytes(existing) != data:
                raise CandidateIntegrityError("candidate identity collision has conflicting content") from None
            self.verify(existing)
            return existing, compiled
        self._persist(record, data)
        self.verify(record)
        return record, compiled

    @staticmethod
    def _read_exact(source_path: str | Path) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(source_path, flags)
        except OSError as exc:
            raise CandidateStoreError("configuration candidate could not be captured") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise CandidateStoreError("configuration candidate is not a regular file")
            data = os.read(fd, DEFAULT_LIMITS.max_source_bytes + 1)
            if len(data) > DEFAULT_LIMITS.max_source_bytes:
                raise CandidateStoreError("configuration candidate exceeds the byte limit")
            if os.fstat(fd).st_size != len(data):
                raise CandidateStoreError("configuration candidate changed during capture")
            return data
        finally:
            os.close(fd)

    def _persist(self, record: CandidateRecord, data: bytes) -> None:
        directory = self.root / record.reference
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            existing = self.load(record.reference)
            if self._stable_metadata(existing) != self._stable_metadata(record) or self.read_bytes(existing) != data:
                raise CandidateIntegrityError("candidate identity collision has conflicting content")
            return
        try:
            self._exclusive_write(directory / "source.bin", data, 0o600)
            metadata = json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            self._exclusive_write(directory / "metadata.json", metadata, 0o600)
        except BaseException:
            for child in (directory / "source.bin", directory / "metadata.json"):
                child.unlink(missing_ok=True)
            directory.rmdir()
            raise

    @staticmethod
    def _stable_metadata(record: CandidateRecord) -> dict[str, object]:
        payload = record.to_dict()
        payload.pop("captured_at", None)
        return payload

    @staticmethod
    def _exclusive_write(path: Path, data: bytes, mode: int) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
        try:
            written = 0
            while written < len(data):
                written += os.write(fd, data[written:])
            os.fsync(fd)
        finally:
            os.close(fd)

    def load(self, reference: str) -> CandidateRecord:
        directory = self._candidate_directory(reference)
        try:
            payload = json.loads(self._safe_read(directory / "metadata.json", 262_144).decode())
            return CandidateRecord(
                reference=str(payload["reference"]),
                source_name=str(payload["source_name"]),
                source_sha256=str(payload["source_sha256"]),
                candidate_sha256=str(payload["candidate_sha256"]),
                byte_length=int(payload["byte_length"]),
                candidate_identity_sha256=str(payload["candidate_identity_sha256"]),
                config_schema_version=(
                    int(payload["config_schema_version"]) if payload["config_schema_version"] is not None else None
                ),
                source_manifest=tuple(payload["source_manifest"]),
                origin_manifest=tuple(payload["origin_manifest"]),
                environment_inputs=tuple(payload["environment_inputs"]),
                captured_at=dt.datetime.fromisoformat(str(payload["captured_at"])),
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CandidateIntegrityError("candidate metadata could not be admitted") from exc

    def read_bytes(self, record: CandidateRecord) -> bytes:
        return self._safe_read(
            self._candidate_directory(record.reference) / "source.bin", DEFAULT_LIMITS.max_source_bytes
        )

    def verify(self, record: CandidateRecord) -> bytes:
        stored = self.load(record.reference)
        if stored != record:
            raise CandidateIntegrityError("candidate metadata changed after capture")
        data = self.read_bytes(record)
        if len(data) != record.byte_length or hashlib.sha256(data).hexdigest() != record.source_sha256:
            raise CandidateIntegrityError("candidate bytes changed after capture")
        return data

    def verify_commit_artifacts(self, candidate: CandidateRecord, report_ref: str, report_sha256: str) -> None:
        """Verify only the admitted bytes needed at the final commit fence."""
        self.verify(candidate)
        if report_ref != f"report_{report_sha256[:40]}":
            raise CandidateIntegrityError("report reference does not match its digest")
        raw = self._safe_read(self._candidate_directory(candidate.reference) / f"{report_ref}.json", 2_097_152)
        try:
            payload = json.loads(raw.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CandidateIntegrityError("validation report is malformed") from exc
        if not isinstance(payload, dict) or canonical_report_sha256(payload) != report_sha256:
            raise CandidateIntegrityError("validation report digest does not match")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        if raw != canonical:
            raise CandidateIntegrityError("validation report bytes changed after capture")

    def compile(self, record: CandidateRecord) -> CompiledConfiguration:
        data = self.verify(record)
        compiled = compile_source(SourceDocument.from_bytes(data, source_id=record.source_name), environ=self._environ)
        identity = CandidateIdentity.from_compiled(
            compiled,
            environment_inputs=self.environment_identities(),
        )
        if identity.identity_sha256 != record.candidate_identity_sha256:
            raise CandidateIntegrityError("complete candidate inputs changed after capture")
        return compiled

    def store_report(self, candidate: CandidateRecord, report: Mapping[str, object]) -> tuple[str, str]:
        self.verify(candidate)
        digest = canonical_report_sha256(report)
        reference = f"report_{digest[:40]}"
        path = self._candidate_directory(candidate.reference) / f"{reference}.json"
        data = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        try:
            self._exclusive_write(path, data, 0o600)
        except FileExistsError:
            if self._safe_read(path, 2_097_152) != data:
                raise CandidateIntegrityError("report digest collision has conflicting content") from None
        return reference, digest

    def load_report(self, candidate: CandidateRecord, reference: str, digest: str) -> dict[str, object]:
        if reference != f"report_{digest[:40]}":
            raise CandidateIntegrityError("report reference does not match its digest")
        raw = self._safe_read(self._candidate_directory(candidate.reference) / f"{reference}.json", 2_097_152)
        try:
            payload = json.loads(raw.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CandidateIntegrityError("validation report is malformed") from exc
        if not isinstance(payload, dict) or canonical_report_sha256(payload) != digest:
            raise CandidateIntegrityError("validation report digest does not match")
        return payload

    def cleanup(self, *, retain_after: dt.datetime, protected_references: frozenset[str]) -> tuple[str, ...]:
        if retain_after.tzinfo is None or retain_after.utcoffset() is None:
            raise ValueError("candidate retention boundary must be timezone-aware")
        removed: list[str] = []
        for directory in sorted(self.root.glob("candidate_*")):
            if self._cleanup_candidate(directory, retain_after, protected_references):
                removed.append(directory.name)
        return tuple(removed)

    def _cleanup_candidate(
        self,
        directory: Path,
        retain_after: dt.datetime,
        protected_references: frozenset[str],
    ) -> bool:
        reference = directory.name
        if reference in protected_references or directory.is_symlink() or not directory.is_dir():
            return False
        try:
            record = self.load(reference)
        except CandidateIntegrityError:
            return False
        if record.captured_at >= retain_after:
            return False
        children = tuple(directory.iterdir())
        if any(child.is_symlink() or not child.is_file() for child in children):
            return False
        for child in children:
            child.unlink()
        directory.rmdir()
        return True

    def _candidate_directory(self, reference: str) -> Path:
        if not reference.startswith("candidate_") or not reference[10:].isalnum():
            raise CandidateIntegrityError("candidate reference is malformed")
        path = self.root / reference
        if path.is_symlink() or not path.is_dir():
            raise CandidateIntegrityError("candidate directory is unavailable")
        return path

    @staticmethod
    def _safe_read(path: Path, limit: int) -> bytes:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise CandidateIntegrityError("candidate artifact is not a regular file")
            data = os.read(fd, limit + 1)
            if len(data) > limit or info.st_size != len(data):
                raise CandidateIntegrityError("candidate artifact is oversized or changed")
            return data
        finally:
            os.close(fd)
