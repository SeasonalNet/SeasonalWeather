"""
broadcast/segment_store.py — Persistent cycle segment registry.

Each cacheable segment (hwo, fcst, obs, id, etc.) has:
  - A *stable* audio file path  (cycle_seg_{key}.wav)
  - Metadata (text, duration, freshness) persisted to JSON

Audio files use stable names so Liquidsoap's push queue can hold a path
reference that is atomically replaced on refresh — no timestamp-named files
accumulating in the audio directory for cycle content.

The ``render_segment_wav`` helper synthesises text to WAV with silence
padding using fully-temp intermediate files, then atomically replaces the
stable output path.  It is safe to call while Liquidsoap has the previous
version of the file queued, because Liquidsoap opens files at play time.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Dict, List, Optional, cast

from ..database.core import SeasonalDatabase
from ..database.segments import SegmentRepository
from ..jobs.worker_client import SynthesisClient
from ..tts.audio import concat_wavs, wav_duration_seconds, write_silence_wav
from ..tts.models import FinalizationCallbackError
from .segment_builders import SegmentProvenance
from .segment_registry import DEFAULT_SEGMENT_REGISTRY

log = logging.getLogger("seasonalweather.segment_store")

_DEFAULT_SEG_GAP_S: float = 0.45


async def _synthesize(synthesizer: SynthesisClient, text: str, output_path: Path, *, purpose: str) -> None:
    await synthesizer.synthesize(text, output_path, purpose=purpose)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


# ---------------------------------------------------------------------------
#  WAV rendering helper (module-level, importable by the refresher)
# ---------------------------------------------------------------------------


def render_segment_wav(
    synthesizer: SynthesisClient,
    text: str,
    output_path: Path,
    *,
    sample_rate: int,
    seg_gap_s: float = _DEFAULT_SEG_GAP_S,
) -> float:
    """Synchronous compatibility wrapper around worker-owned synthesis."""
    return asyncio.run(
        render_segment_wav_async(
            synthesizer,
            text,
            output_path,
            sample_rate=sample_rate,
            seg_gap_s=seg_gap_s,
        )
    )


async def render_segment_wav_async(
    synthesizer: SynthesisClient,
    text: str,
    output_path: Path,
    *,
    sample_rate: int,
    seg_gap_s: float = _DEFAULT_SEG_GAP_S,
    publication_fence=None,
    publication_committed=None,
    publication_aborted=None,
) -> float:
    """Async production rendering with worker-owned synthesis semantics."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".segment-", dir=str(output_path.parent)) as staging_root:
        staging = Path(staging_root)
        raw = staging / "tts.wav"
        gap = staging / "segment-gap.wav"
        complete = staging / "segment-completed.wav"
        await _synthesize(synthesizer, text, raw, purpose="routine")
        write_silence_wav(gap, seg_gap_s, sample_rate)
        concat_wavs(complete, [gap, raw, gap])
        duration = wav_duration_seconds(complete)
        try:
            if publication_fence is not None:
                publication_fence()
        except BaseException as exc:
            raise FinalizationCallbackError() from exc
        os.replace(complete, output_path)
        try:
            if publication_committed is not None:
                publication_committed()
        except SegmentCommitAmbiguousError:
            raise
        except BaseException as exc:
            if publication_aborted is not None:
                publication_aborted()
            raise FinalizationCallbackError() from exc
        return duration


# ---------------------------------------------------------------------------
#  Data model
# ---------------------------------------------------------------------------


@dataclass
class SegmentEntry:
    """
    Metadata for one cycle segment.  Audio bytes live on disk at *audio_path*;
    this dataclass only carries index information that is persisted to JSON.
    """

    key: str
    title: str
    text: str  # last synthesised text (for change detection)
    audio_path: str  # stable path — atomically replaced on refresh
    duration_s: float
    last_updated_ts: float  # unix epoch
    refresh_interval_s: int  # 0 = never auto-stale (live / on-demand only)
    max_age_s: int = 0  # authoritative registry freshness ceiling
    is_placeholder: bool = False
    provenance: SegmentProvenance = field(default_factory=SegmentProvenance)

    def is_stale(self) -> bool:
        """True when content is old enough to warrant a refresh."""
        if self.is_placeholder and self.last_updated_ts <= 0:
            return True
        if self.refresh_interval_s <= 0:
            return False
        return (time.time() - self.last_updated_ts) >= self.refresh_interval_s

    def is_expired(self) -> bool:
        """True when content exceeds its maximum acceptable age."""
        max_age = self.max_age_s or self.refresh_interval_s
        if max_age <= 0:
            return False
        return (time.time() - self.last_updated_ts) >= max_age


@dataclass(frozen=True)
class SegmentCommitResult:
    """Typed evidence that the complete segment target state was committed."""

    key: str
    entry: SegmentEntry
    committed: bool = True


@dataclass(frozen=True)
class SegmentCommitReceipt:
    """Durable identity for a completed refresh publication."""

    key: str
    command_id: str
    target: str
    publication_committed: bool = True


@dataclass(frozen=True)
class _MalformedMetadataIdentity:
    """The safely decoded identity fragments of one malformed entry."""

    key: str | None
    audio_path: Path | None


@dataclass(frozen=True)
class _FileMetadataLoad:
    """Staged file-backed metadata and the limits of what it proves."""

    readable: bool
    missing: bool = False
    entries: dict[str, SegmentEntry] = field(default_factory=dict)
    malformed: tuple[_MalformedMetadataIdentity, ...] = ()
    duplicate_keys: frozenset[str] = frozenset()
    unknown_identity: bool = False

    def _candidate_target(self, key: str, target: Path | None) -> Path | None:
        if target is not None:
            return target.resolve()
        entry = self.entries.get(key)
        return None if entry is None else Path(entry.audio_path).resolve()

    def _valid_target_is_unambiguous(self, key: str, target: Path | None) -> bool:
        if target is None:
            return True
        referenced_keys = tuple(
            entry.key for entry in self.entries.values() if Path(entry.audio_path).resolve() == target
        )
        return len(referenced_keys) <= 1 and (not referenced_keys or referenced_keys[0] == key)

    def _malformed_evidence_is_disjoint(self, key: str, target: Path | None) -> bool:
        for item in self.malformed:
            if item.key == key:
                return False
            if item.key is None and (target is None or item.audio_path is None or item.audio_path == target):
                return False
            if target is not None and item.audio_path == target:
                return False
        return True

    def safely_decodes_exact_key(self, key: str, target: Path | None) -> bool:
        """Return whether this snapshot can safely answer one exact-key query."""
        if not self.readable or key in self.duplicate_keys:
            return False
        candidate_target = self._candidate_target(key, target)
        if not self._valid_target_is_unambiguous(key, candidate_target):
            return False
        return self._malformed_evidence_is_disjoint(key, candidate_target)


class RefreshEvidenceState(StrEnum):
    """Exact command/key classification for durable refresh evidence."""

    NONE = "none"
    COMMITTED = "committed"
    UNRESOLVED = "unresolved"


class RefreshReconciliationOutcome(StrEnum):
    """Exact outcome of reconciling one command/key publication identity."""

    PUBLICATION_PROVEN = "publication_proven"
    PUBLICATION_NOT_PROVEN = "publication_not_proven"
    STILL_UNRESOLVED = "still_unresolved"


class SegmentMetadataReplacePhase(StrEnum):
    """Fence the point at which a legacy metadata replacement is attempted."""

    NOT_ENTERED = "not_entered"
    ATTEMPTED = "attempted"
    REPLACED = "replaced"


class SegmentCommitAmbiguousError(FinalizationCallbackError):
    """A file-backed metadata commit may have replaced the authoritative index."""

    def __init__(self, *, key: str, command_id: str | None) -> None:
        super().__init__("segment metadata publication is ambiguous")
        self.key = key
        self.command_id = command_id


def segment_entry_eligible_to_air(entry: SegmentEntry | None) -> bool:
    """Return the conductor's pure cached-segment airability decision."""
    if entry is None or entry.is_placeholder:
        return False
    path = Path(entry.audio_path)
    return path.is_file() and not path.is_symlink()


def _entry_from_payload(item: dict) -> SegmentEntry:
    payload = dict(item)
    payload["max_age_s"] = int(item.get("max_age_s", item.get("refresh_interval_s", 0)))
    raw_provenance = payload.pop("provenance", None)
    if isinstance(raw_provenance, dict):
        payload.pop("placeholder", None)
        payload["provenance"] = SegmentProvenance(**raw_provenance)
    else:
        payload.pop("placeholder", None)
        payload.pop("stale", None)
        payload["provenance"] = SegmentProvenance(
            placeholder=bool(payload.get("is_placeholder", False)),
            current_content_hash=payload.pop("content_hash", None),
            source_name=payload.pop("source_name", None),
            product_identifier=payload.pop("product_identifier", None),
            product_type=payload.pop("product_type", None),
            issuing_office=payload.pop("issuing_office", None),
            issuance_time=payload.pop("issuance_time", None),
            fetch_time=payload.pop("fetch_time", None),
            last_successful_synthesis=payload.pop("last_successful_synthesis", None),
            source_reference=payload.pop("source_reference", None),
            last_error=payload.pop("last_error", None),
            consecutive_failures=int(payload.pop("consecutive_failures", 0) or 0),
            last_aired=payload.pop("last_aired", None),
            next_eligible_airtime=payload.pop("next_eligible_airtime", None),
        )
    return SegmentEntry(**payload)


def _successful_provenance(
    observed: SegmentProvenance,
    existing: SegmentEntry | None,
    *,
    placeholder: bool,
) -> SegmentProvenance:
    if existing is None:
        return observed
    return SegmentProvenance(
        source_name=observed.source_name,
        product_identifier=observed.product_identifier,
        product_type=observed.product_type,
        issuing_office=observed.issuing_office,
        issuance_time=observed.issuance_time,
        fetch_time=observed.fetch_time,
        last_successful_synthesis=observed.last_successful_synthesis,
        current_content_hash=observed.current_content_hash,
        source_reference=observed.source_reference,
        last_error=None,
        consecutive_failures=0,
        stale=False,
        placeholder=placeholder,
        last_aired=existing.provenance.last_aired,
        next_eligible_airtime=existing.provenance.next_eligible_airtime,
    )


# ---------------------------------------------------------------------------
#  Store
# ---------------------------------------------------------------------------


class SegmentStore:
    """
    In-memory registry of cycle segment metadata, backed by a JSON index.

    Thread/task safety model
    ------------------------
    *Reads* (``get``, ``is_stale``, ``is_ready``) are safe to call from any
    async task without acquiring the lock — Python dict reads are atomic at
    the CPython level and the conductor only reads, never writes.

    *Writes* (``update``, ``mark_placeholder``) take ``_lock`` and then call
    ``_persist_unlocked`` before releasing.  Only the SegmentRefresher writes,
    so there is at most one concurrent writer in practice.
    """

    INDEX_FILENAME = "segment_store.json"
    _MAX_UNREFERENCED_GENERATIONS = 2
    _MAX_PRIVATE_CANDIDATES_PER_START = 256
    _JOURNAL_KEY_RE = re.compile(r"^_alert_[A-Za-z0-9_.:-]{1,96}$")
    _COMMAND_ID_RE = re.compile(r"^cmd_[A-Za-z0-9_-]{1,80}$")
    _KEY_TOKEN_RE = re.compile(r"^[A-Za-z0-9_]+$")
    _OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,192}$")
    _VERSIONED_TARGET_RE = re.compile(r"^cycle_seg_(?P<safe>[A-Za-z0-9_-]+)\.(?P<generation>[0-9a-f]{32})\.wav$")
    _PRIVATE_CANDIDATE_RE = re.compile(r"^\.segment-candidate-[0-9a-f]{32}\.wav$")

    def __init__(
        self,
        work_dir: Path,
        audio_dir: Path,
        database: SeasonalDatabase | None = None,
        *,
        static_key_predicate: Callable[[str], bool] | None = None,
    ) -> None:
        self._work_dir = Path(work_dir)
        self._audio_dir = Path(audio_dir)
        self._index_path = self._work_dir / self.INDEX_FILENAME
        self._entries: Dict[str, SegmentEntry] = {}
        self._lock = asyncio.Lock()
        self._state_lock = threading.RLock()
        self._repo = SegmentRepository(database) if database is not None else None
        self._committed_receipts: list[SegmentCommitReceipt] = []
        self._static_key_predicate = static_key_predicate or DEFAULT_SEGMENT_REGISTRY.is_managed
        self._metadata_replace_succeeded = False
        self._metadata_replace_phase = SegmentMetadataReplacePhase.NOT_ENTERED
        self._last_file_metadata_load: _FileMetadataLoad | None = None
        self._file_snapshot_authoritative = False

    # ------------------------------------------------------------------
    #  Stable path derivation
    # ------------------------------------------------------------------

    def audio_path_for(self, key: str) -> Path:
        """Return the canonical stable WAV path for *key*."""
        return self._audio_dir / f"cycle_seg_{self._key_token(key)}.wav"

    # ------------------------------------------------------------------
    #  Persistence
    # ------------------------------------------------------------------

    def load(self) -> int:
        """
        Load index at startup.  Missing audio files are flagged as placeholders
        so the refresher re-synthesises them.
        Returns the number of entries restored.
        """
        if self._repo is not None:
            loaded = self._load_repository_entries()
            if loaded is not None:
                return loaded
        return self._load_legacy_entries()

    def committed_refresh_receipts(self) -> tuple[SegmentCommitReceipt, ...]:
        """Return startup receipts without inferring them from segment contents."""
        with self._state_lock:
            return tuple(self._committed_receipts)

    def refresh_evidence_state(self, key: str, command_id: str) -> RefreshEvidenceState:
        """Classify only evidence attributable to this exact command and key."""
        with self._state_lock:
            if any(item.key == key and item.command_id == command_id for item in self._committed_receipts):
                return RefreshEvidenceState.COMMITTED
            receipt_state = self._receipt_evidence_state(key, command_id)
            return (
                receipt_state
                if receipt_state is not RefreshEvidenceState.NONE
                else self._journal_evidence_state(key, command_id)
            )

    def _receipt_evidence_state(self, key: str, command_id: str) -> RefreshEvidenceState:
        receipt_path = self._receipt_path(key, command_id)
        if not receipt_path.exists():
            return RefreshEvidenceState.NONE
        try:
            receipt = self._validated_receipt_file(receipt_path)
        except Exception:
            return RefreshEvidenceState.UNRESOLVED
        self._record_receipt(receipt)
        return RefreshEvidenceState.COMMITTED

    def _journal_evidence_state(self, key: str, command_id: str) -> RefreshEvidenceState:
        token = self._key_token(key)
        command_prefix = f".segment-commit-{token}-{command_id}-"
        legacy_name = f".segment-commit-{token}.json"
        for journal_path in self._journal_paths_for_key(key):
            state = self._one_journal_evidence_state(
                journal_path,
                key=key,
                command_id=command_id,
                command_prefix=command_prefix,
                legacy_name=legacy_name,
            )
            if state is not RefreshEvidenceState.NONE:
                return state
        return RefreshEvidenceState.NONE

    def _one_journal_evidence_state(
        self,
        journal_path: Path,
        *,
        key: str,
        command_id: str,
        command_prefix: str,
        legacy_name: str,
    ) -> RefreshEvidenceState:
        try:
            journal_key, _target, _previous, journal_command_id, committed, publication_won, _previous_entry = (
                self._validated_journal(journal_path)
            )
        except Exception:
            return (
                RefreshEvidenceState.UNRESOLVED
                if journal_path.name == legacy_name or journal_path.name.startswith(command_prefix)
                else RefreshEvidenceState.NONE
            )
        if journal_key != key:
            return RefreshEvidenceState.NONE
        if journal_command_id == command_id:
            return RefreshEvidenceState.COMMITTED if (committed or publication_won) else RefreshEvidenceState.UNRESOLVED
        return (
            RefreshEvidenceState.UNRESOLVED
            if journal_command_id is None and journal_path.name == legacy_name
            else RefreshEvidenceState.NONE
        )

    def acknowledge_refresh_receipt(self, receipt: SegmentCommitReceipt) -> None:
        """Remove one receipt only after its exact command was reconciled."""
        with self._state_lock:
            try:
                self._receipt_path(receipt.key, receipt.command_id).unlink(missing_ok=True)
            except OSError:
                log.warning("segment_store: failed to clear refresh receipt key=%s", receipt.key)
                return
            self._committed_receipts = [item for item in self._committed_receipts if item != receipt]

    def acknowledge_refresh_command(self, command_id: str, key: str) -> bool:
        """Acknowledge a command receipt after durable command success."""
        with self._state_lock:
            receipt = next(
                (item for item in self._committed_receipts if item.command_id == command_id and item.key == key), None
            )
            if receipt is not None:
                return self._acknowledge_refresh_receipt(receipt)
            return self._acknowledge_refresh_journal(command_id, key)

    def _acknowledge_refresh_receipt(self, receipt: SegmentCommitReceipt) -> bool:
        self.acknowledge_refresh_receipt(receipt)
        if receipt in self._committed_receipts:
            return False
        journal_path = self._journal_path(
            receipt.key,
            self._operation_id(target=Path(receipt.target), command_id=receipt.command_id),
        )
        try:
            journal_path.unlink(missing_ok=True)
        except OSError:
            log.warning("segment_store: failed to clear refresh journal key=%s", receipt.key)
            return False
        return True

    def _acknowledge_refresh_journal(self, command_id: str, key: str) -> bool:
        for journal_path in self._journal_paths_for_key(key):
            try:
                journal_key, target, _previous, journal_command_id, committed, publication_won, _previous_entry = (
                    self._validated_journal(journal_path)
                )
                if journal_key != key or journal_command_id != command_id:
                    continue
                if not (committed or publication_won) and not self._accepted_target(key, target):
                    continue
                receipt = self._write_commit_receipt(key=key, target=target, command_id=command_id)
                self._record_receipt(receipt)
                self.acknowledge_refresh_receipt(receipt)
                journal_path.unlink(missing_ok=True)
                return receipt not in self._committed_receipts and not journal_path.exists()
            except OSError:
                log.warning("segment_store: refresh receipt cleanup remains retryable key=%s", key)
                return False
            except Exception:
                log.exception("segment_store: failed to reconcile refresh journal key=%s", key)
        return False

    async def reconcile_committed_refresh_commands(self, command_store: object) -> int:
        """Repair exact durable refresh identities through the CommandStore."""
        repair = getattr(command_store, "reconcile_committed_segment_refresh", None)
        if not callable(repair):
            return 0
        # A receipt write can fail after the segment metadata and the
        # per-operation commit journal are durable.  Reconcile journals here
        # too, so the in-process repair path has the same exact identity
        # coverage as startup loading.
        await asyncio.to_thread(self._reconcile_pending_commits)
        with self._state_lock:
            receipts = tuple(self._committed_receipts)
        repaired = 0
        for receipt in receipts:
            if await repair(receipt.command_id, receipt.key, publication_won=True):
                self.acknowledge_refresh_receipt(receipt)
                repaired += 1
        return repaired

    async def reconcile_committed_refresh_command(
        self, command_store: object, command_id: str, key: str
    ) -> RefreshReconciliationOutcome:
        """Reconcile one exact refresh without collapsing disk outcomes."""
        repair = getattr(command_store, "reconcile_committed_segment_refresh", None)
        if not callable(repair):
            return RefreshReconciliationOutcome.STILL_UNRESOLVED
        outcome, receipt = await asyncio.to_thread(self._reconcile_one_pending_command, command_id, key)
        if outcome is not RefreshReconciliationOutcome.PUBLICATION_PROVEN or receipt is None:
            return outcome
        if not await repair(receipt.command_id, receipt.key, publication_won=True):
            return RefreshReconciliationOutcome.STILL_UNRESOLVED
        self.acknowledge_refresh_receipt(receipt)
        return RefreshReconciliationOutcome.PUBLICATION_PROVEN

    async def reconcile_commandless_refresh(self, key: str) -> RefreshReconciliationOutcome:
        """Reconcile one background publication without creating command state."""
        return await asyncio.to_thread(self._reconcile_commandless_refresh_sync, key)

    def _commandless_journals(self, key: str) -> list[tuple[Path, Path]]:
        journals: list[tuple[Path, Path]] = []
        for journal_path in self._journal_paths_for_key(key):
            try:
                journal_key, target, _previous, command_id, _committed, _publication_won, _previous_entry = (
                    self._validated_journal(journal_path)
                )
            except (KeyError, OSError, TypeError, ValueError):
                continue
            if journal_key == key and command_id is None:
                journals.append((journal_path, target))
        return journals

    def _commandless_metadata_safe(
        self, key: str, metadata: _FileMetadataLoad, journals: list[tuple[Path, Path]]
    ) -> bool:
        safe = all(metadata.safely_decodes_exact_key(key, target) for _path, target in journals)
        if not safe:
            log.warning("segment_store: deferred commandless ambiguity key=%s", key)
        return safe

    def _commandless_publication_proven(
        self, key: str, metadata: _FileMetadataLoad, journals: list[tuple[Path, Path]]
    ) -> bool:
        return any(
            target.is_file() and self._metadata_references_target(key, target, entries=metadata.entries)
            for _path, target in journals
        )

    def _reconcile_commandless_refresh_sync(self, key: str) -> RefreshReconciliationOutcome:
        with self._state_lock:
            metadata = self._reload_file_backed_entries()
            journals = self._commandless_journals(key)
            if not journals:
                return (
                    RefreshReconciliationOutcome.PUBLICATION_NOT_PROVEN
                    if metadata.safely_decodes_exact_key(key, None)
                    else RefreshReconciliationOutcome.STILL_UNRESOLVED
                )
            if not self._commandless_metadata_safe(key, metadata, journals):
                return RefreshReconciliationOutcome.STILL_UNRESOLVED
            if key in metadata.entries:
                self._entries[key] = metadata.entries[key]
            for journal_path, _target in journals:
                self._reconcile_one_journal(journal_path, entries=metadata.entries)
            if any(path.exists() for path, _target in journals):
                return RefreshReconciliationOutcome.STILL_UNRESOLVED
            if self._commandless_publication_proven(key, metadata, journals):
                return RefreshReconciliationOutcome.PUBLICATION_PROVEN
            return RefreshReconciliationOutcome.PUBLICATION_NOT_PROVEN

    def _load_repository_entries(self) -> int | None:
        repo = cast(SegmentRepository, self._repo)
        try:
            raw_entries = repo.load_entries()
        except Exception:
            log.exception("segment_store: failed to load entries from SQLite")
            return None
        if not raw_entries:
            return None
        loaded = self._load_entries_from_payload(raw_entries)
        self._reconcile_pending_commits()
        log.info("segment_store: loaded %d entries from SQLite", loaded)
        return loaded

    def _load_legacy_entries(self) -> int:
        metadata = self._reload_file_backed_entries(bootstrap=True)
        if not metadata.readable:
            log.error("segment_store: authoritative file-backed metadata is unavailable")
            self._reconcile_pending_commits()
            return 0
        self._reconcile_pending_commits()
        loaded = len(self._entries)
        log.info("segment_store: loaded %d legacy entries from %s", loaded, self._index_path)
        if loaded and self._repo is not None:
            self._import_legacy_entries(loaded)
        return loaded

    @staticmethod
    def _raw_metadata_path(value: object) -> Path | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            return Path(value).resolve(strict=False)
        except (OSError, RuntimeError):
            return None

    def _read_file_metadata_entries(self) -> list[object] | None:
        try:
            raw = json.loads(self._index_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or not isinstance(raw.get("entries"), list):
                raise ValueError("segment index entries are not a list")
        except Exception:
            log.exception("segment_store: failed to reload authoritative file-backed metadata")
            return None
        return cast(list[object], raw["entries"])

    def _stage_file_metadata_item(self, item: object) -> tuple[SegmentEntry | None, _MalformedMetadataIdentity | None]:
        if not isinstance(item, dict):
            return None, _MalformedMetadataIdentity(None, None)
        raw_key = item.get("key")
        identity_key = raw_key if isinstance(raw_key, str) and raw_key else None
        try:
            entry = _entry_from_payload(item)
            if not isinstance(entry.key, str) or not entry.key:
                raise ValueError("segment metadata key is malformed")
            if not Path(entry.audio_path).exists():
                entry.is_placeholder = True
                entry.provenance = SegmentProvenance(**{**asdict(entry.provenance), "placeholder": True})
            return entry, None
        except Exception:
            log.warning("segment_store: staged malformed index entry: %s", item)
            return None, _MalformedMetadataIdentity(identity_key, self._raw_metadata_path(item.get("audio_path")))

    def _stage_file_metadata(self) -> _FileMetadataLoad:
        if not self._index_path.exists():
            return _FileMetadataLoad(readable=False, missing=True)
        raw_entries = self._read_file_metadata_entries()
        if raw_entries is None:
            return _FileMetadataLoad(readable=False)

        entries: dict[str, SegmentEntry] = {}
        malformed: list[_MalformedMetadataIdentity] = []
        duplicate_keys: set[str] = set()
        unknown_identity = False
        for item in raw_entries:
            entry, malformed_item = self._stage_file_metadata_item(item)
            if malformed_item is not None:
                malformed.append(malformed_item)
                unknown_identity = unknown_identity or malformed_item.key is None
            elif entry is not None and entry.key in entries:
                duplicate_keys.add(entry.key)
            elif entry is not None:
                entries[entry.key] = entry
        return _FileMetadataLoad(
            readable=True,
            entries=entries,
            malformed=tuple(malformed),
            duplicate_keys=frozenset(duplicate_keys),
            unknown_identity=unknown_identity,
        )

    def _install_bootstrap_entries(self, metadata: _FileMetadataLoad) -> None:
        installable = {
            key: entry
            for key, entry in metadata.entries.items()
            if metadata.safely_decodes_exact_key(key, Path(entry.audio_path))
        }
        self._entries.clear()
        self._entries.update(installable)
        self._file_snapshot_authoritative = True

    def _reload_file_backed_entries(self, *, bootstrap: bool = False) -> _FileMetadataLoad:
        if self._repo is not None:
            result = _FileMetadataLoad(readable=True, entries=dict(self._entries))
        else:
            result = self._stage_file_metadata()
            # A partially decoded file is evidence for an exact staged query,
            # never a replacement authoritative in-memory snapshot.
            if bootstrap and result.readable and not self._file_snapshot_authoritative:
                self._install_bootstrap_entries(result)
            elif result.readable and not result.malformed and not result.duplicate_keys and not result.unknown_identity:
                self._entries.clear()
                self._entries.update(result.entries)
                self._file_snapshot_authoritative = True
        self._last_file_metadata_load = result
        return result

    def _import_legacy_entries(self, loaded: int) -> None:
        repo = cast(SegmentRepository, self._repo)
        try:
            repo.replace_entries(self._entry_record(e) for e in self._entries.values())
            log.info("segment_store: imported %d legacy entries into SQLite", loaded)
        except Exception:
            log.exception("segment_store: failed to import legacy segment index into SQLite")

    @classmethod
    def _key_token(cls, key: str) -> str:
        # ``b64_`` is reserved for encoded tokens so the two representations
        # remain disjoint (for example, a literal ``b64_...`` key cannot
        # collide with a punctuation-bearing key's encoded form).
        if cls._KEY_TOKEN_RE.fullmatch(key) and not key.startswith("b64_"):
            return key
        encoded = base64.urlsafe_b64encode(key.encode("utf-8")).decode("ascii").rstrip("=")
        return f"b64_{encoded}"

    def _journal_path(self, key: str, operation_id: str | None = None) -> Path:
        token = self._key_token(key)
        if operation_id is not None:
            if not self._OPERATION_ID_RE.fullmatch(operation_id):
                raise ValueError("commit operation identity is malformed")
            return self._work_dir / f".segment-commit-{token}-{operation_id}.json"
        paths = self._journal_paths_for_key(key)
        return paths[0] if paths else self._work_dir / f".segment-commit-{token}.json"

    def _journal_paths_for_key(self, key: str) -> list[Path]:
        token = self._key_token(key)
        paths = list(self._work_dir.glob(f".segment-commit-{token}-*.json"))
        legacy = self._work_dir / f".segment-commit-{token}.json"
        if legacy.exists():
            paths.append(legacy)
        return sorted(set(paths), key=lambda path: path.name)

    def _receipt_path(self, key: str, command_id: str) -> Path:
        safe_key = self._key_token(key)
        safe_command = "".join(ch for ch in command_id if ch.isalnum() or ch in "_-")
        return self._work_dir / f".segment-commit-receipt-{safe_key}-{safe_command}.json"

    def _versioned_audio_path(self, key: str) -> Path:
        return self._audio_dir / f"cycle_seg_{self._key_token(key)}.{uuid.uuid4().hex}.wav"

    def _operation_id(self, *, target: Path, command_id: str | None) -> str:
        match = self._VERSIONED_TARGET_RE.fullmatch(target.name)
        if match is None:
            raise ValueError("commit target does not contain a governed generation")
        return f"{command_id or 'background'}-{match.group('generation')}"

    @staticmethod
    def _durable_write(path: Path, payload: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(str(temporary), str(path))
        _fsync_directory(path.parent)

    def _write_commit_journal(
        self,
        *,
        key: str,
        target: Path,
        previous: Path | None,
        command_id: str | None = None,
        previous_entry: dict | None = None,
    ) -> None:
        operation_id = self._operation_id(target=target, command_id=command_id)
        self._durable_write(
            self._journal_path(key, operation_id),
            json.dumps(
                {
                    "key": key,
                    "target": str(target),
                    "previous": str(previous) if previous is not None else None,
                    "command_id": command_id,
                    "operation_id": operation_id,
                    "committed": False,
                    "publication_won": False,
                    "previous_entry": previous_entry,
                },
                sort_keys=True,
            ),
        )

    def _mark_publication_won(self, *, key: str, target: Path, command_id: str | None) -> None:
        """Durably witness metadata publication before auxiliary evidence."""
        operation_id = self._operation_id(target=target, command_id=command_id)
        journal_path = self._journal_path(key, operation_id)
        raw = json.loads(journal_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("commit journal is not an object")
        raw["publication_won"] = True
        self._durable_write(journal_path, json.dumps(raw, sort_keys=True))

    def _mark_commit_committed(self, *, key: str, target: Path, command_id: str | None) -> None:
        operation_id = self._operation_id(target=target, command_id=command_id)
        journal_path = self._journal_path(key, operation_id)
        raw = json.loads(journal_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("commit journal is not an object")
        raw["committed"] = True
        self._durable_write(journal_path, json.dumps(raw, sort_keys=True))

    def _write_commit_receipt(self, *, key: str, target: Path, command_id: str) -> SegmentCommitReceipt:
        receipt = SegmentCommitReceipt(key=key, command_id=command_id, target=str(target))
        self._durable_write(self._receipt_path(key, command_id), json.dumps(asdict(receipt), sort_keys=True))
        return receipt

    def _audio_root(self) -> Path:
        return self._audio_dir.resolve()

    def _journal_key_is_governed(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        try:
            return bool(self._static_key_predicate(key)) or bool(self._JOURNAL_KEY_RE.fullmatch(key))
        except Exception:
            return False

    def _resolved_audio_path(self, value: object) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("commit path is missing")
        path = Path(value)
        if path.is_symlink():
            raise ValueError("commit path is a symlink")
        if path.exists() and not path.is_file():
            raise ValueError("commit path is not a regular file")
        root = self._audio_root()
        resolved = path.resolve(strict=False)
        try:
            relative = resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError("commit path is outside audio directory") from exc
        if len(relative.parts) != 1:
            raise ValueError("commit path is not a direct audio artifact")
        return resolved

    def _validate_audio_name(self, path: Path, *, key: str, allow_stable: bool) -> None:
        safe = self._key_token(key)
        versioned = self._VERSIONED_TARGET_RE.fullmatch(path.name)
        if versioned is not None:
            if versioned.group("safe") != safe:
                raise ValueError("versioned target key does not match journal key")
        elif not (allow_stable and path.name == f"cycle_seg_{safe}.wav"):
            raise ValueError("commit path name is not governed")

    def _validated_audio_path(self, value: object, *, key: str, allow_stable: bool) -> Path:
        path = self._resolved_audio_path(value)
        self._validate_audio_name(path, key=key, allow_stable=allow_stable)
        return path

    def _journal_operation_matches(
        self, journal_path: Path, *, key: str, target: Path, command_id: str | None, operation_id: object
    ) -> bool:
        expected = self._operation_id(target=target, command_id=command_id)
        if operation_id is not None:
            return (
                isinstance(operation_id, str)
                and bool(self._OPERATION_ID_RE.fullmatch(operation_id))
                and operation_id == expected
                and journal_path.name == self._journal_path(key, operation_id).name
            )
        token = self._key_token(key)
        return journal_path.name in {
            f".segment-commit-{token}.json",
            self._journal_path(key, expected).name,
        }

    @staticmethod
    def _validated_journal_bool(journal: dict, field: str) -> bool:
        value = journal.get(field, False)
        if not isinstance(value, bool):
            raise ValueError(f"commit journal {field} marker is malformed")
        return value

    def _validated_journal(
        self, journal_path: Path
    ) -> tuple[str, Path, Path | None, str | None, bool, bool, dict | None]:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        if not isinstance(journal, dict):
            raise ValueError("commit journal is not an object")
        key = journal.get("key")
        if not isinstance(key, str) or not self._journal_key_is_governed(key):
            raise ValueError("commit journal key is not governed")
        command_id = journal.get("command_id")
        if command_id is not None and (
            not isinstance(command_id, str) or not self._COMMAND_ID_RE.fullmatch(command_id)
        ):
            raise ValueError("commit journal command identity is malformed")
        target = self._validated_audio_path(journal.get("target"), key=key, allow_stable=False)
        previous_value = journal.get("previous")
        previous = (
            None if previous_value is None else self._validated_audio_path(previous_value, key=key, allow_stable=True)
        )
        operation_id = journal.get("operation_id")
        if not self._journal_operation_matches(
            journal_path, key=key, target=target, command_id=command_id, operation_id=operation_id
        ):
            raise ValueError("commit journal operation identity does not match filename")
        committed = self._validated_journal_bool(journal, "committed")
        publication_won = self._validated_journal_bool(journal, "publication_won")
        previous_entry = self._validated_previous_entry(journal.get("previous_entry"), key=key, previous=previous)
        return key, target, previous, command_id, committed, publication_won, previous_entry

    def _validated_previous_entry(self, raw: object, *, key: str, previous: Path | None) -> dict | None:
        if raw is None:
            return None
        if not isinstance(raw, dict) or previous is None:
            raise ValueError("commit journal previous metadata is malformed")
        restored = _entry_from_payload(dict(raw))
        if restored.key != key or Path(restored.audio_path).resolve() != previous:
            raise ValueError("commit journal previous metadata identity is malformed")
        return self._entry_record(restored)

    def _record_receipt(self, receipt: SegmentCommitReceipt) -> None:
        if receipt not in self._committed_receipts:
            self._committed_receipts.append(receipt)

    def _reconcile_receipt_files(self) -> None:
        for receipt_path in sorted(self._work_dir.glob(".segment-commit-receipt-*.json")):
            try:
                self._record_receipt(self._validated_receipt_file(receipt_path))
            except Exception:
                log.exception("segment_store: failed to validate refresh receipt %s", receipt_path)

    def _validated_receipt_file(self, receipt_path: Path) -> SegmentCommitReceipt:
        raw = json.loads(receipt_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or set(raw) not in (
            {"key", "command_id", "target"},
            {"key", "command_id", "target", "publication_committed"},
        ):
            raise ValueError("refresh receipt schema is not bounded")
        key = raw.get("key")
        command_id = raw.get("command_id")
        if not isinstance(key, str) or not self._journal_key_is_governed(key) or not isinstance(command_id, str):
            raise ValueError("refresh receipt identity is malformed")
        if not self._COMMAND_ID_RE.fullmatch(command_id):
            raise ValueError("refresh receipt command identity is malformed")
        if raw.get("publication_committed", True) is not True:
            raise ValueError("refresh receipt does not prove committed publication")
        target = self._validated_audio_path(raw.get("target"), key=key, allow_stable=False)
        if receipt_path.name != self._receipt_path(key, command_id).name:
            raise ValueError("refresh receipt filename does not match identity")
        return SegmentCommitReceipt(key, command_id, str(target))

    def _reconcile_pending_commits(self) -> None:
        """Resolve prepared commits only after validating filesystem authority."""
        with self._state_lock:
            self._work_dir.mkdir(parents=True, exist_ok=True)
            self._reconcile_receipt_files()
            metadata = self._reload_file_backed_entries()
            if not metadata.readable:
                log.warning("segment_store: deferred commit reconciliation without authoritative metadata")
                return
            for journal_path in sorted(self._work_dir.glob(".segment-commit-*.json")):
                if journal_path.name.startswith(".segment-commit-receipt-"):
                    continue
                try:
                    key, target, _previous, _command_id, _committed, _publication_won, _previous_entry = (
                        self._validated_journal(journal_path)
                    )
                    if not metadata.safely_decodes_exact_key(key, target):
                        log.warning("segment_store: deferred ambiguous metadata reconciliation key=%s", key)
                        continue
                    if key in metadata.entries:
                        self._entries[key] = metadata.entries[key]
                    self._reconcile_one_journal(journal_path, entries=metadata.entries)
                except Exception:
                    log.exception("segment_store: refused unsafe commit journal %s", journal_path)
            self._cleanup_private_candidates()

    def _reconcile_one_pending_command(
        self, command_id: str, key: str
    ) -> tuple[RefreshReconciliationOutcome, SegmentCommitReceipt | None]:
        with self._state_lock:
            self._work_dir.mkdir(parents=True, exist_ok=True)
            metadata = self._reload_file_backed_entries()
            target = self._exact_journal_target(key, command_id)
            if not metadata.readable or not metadata.safely_decodes_exact_key(key, target):
                return RefreshReconciliationOutcome.STILL_UNRESOLVED, None
            return self._reconcile_safe_pending_command(command_id, key, metadata)

    def _reconcile_safe_pending_command(
        self, command_id: str, key: str, metadata: _FileMetadataLoad
    ) -> tuple[RefreshReconciliationOutcome, SegmentCommitReceipt | None]:
        self._load_exact_receipt(key, command_id)
        if key in metadata.entries:
            self._entries[key] = metadata.entries[key]
        self._reconcile_exact_journals(key, command_id, entries=metadata.entries)
        receipt = next(
            (item for item in self._committed_receipts if item.command_id == command_id and item.key == key), None
        )
        if receipt is not None:
            return RefreshReconciliationOutcome.PUBLICATION_PROVEN, receipt
        state = self.refresh_evidence_state(key, command_id)
        if state in {RefreshEvidenceState.UNRESOLVED, RefreshEvidenceState.COMMITTED}:
            return RefreshReconciliationOutcome.STILL_UNRESOLVED, None
        return RefreshReconciliationOutcome.PUBLICATION_NOT_PROVEN, None

    def _exact_journal_target(self, key: str, command_id: str | None) -> Path | None:
        for journal_path in self._journal_paths_for_key(key):
            try:
                journal_key, target, _previous, journal_command_id, _committed, _publication_won, _previous_entry = (
                    self._validated_journal(journal_path)
                )
            except (KeyError, OSError, TypeError, ValueError):
                continue
            if journal_key == key and journal_command_id == command_id:
                return target
        return None

    def _load_exact_receipt(self, key: str, command_id: str) -> None:
        receipt_path = self._receipt_path(key, command_id)
        if not receipt_path.exists():
            return
        try:
            self._record_receipt(self._validated_receipt_file(receipt_path))
        except Exception:
            log.exception("segment_store: failed to validate refresh receipt %s", receipt_path)

    def _reconcile_exact_journals(
        self, key: str, command_id: str, *, entries: dict[str, SegmentEntry] | None = None
    ) -> None:
        for journal_path in self._journal_paths_for_key(key):
            try:
                journal_key, _target, _previous, journal_command_id, _committed, _publication_won, _previous_entry = (
                    self._validated_journal(journal_path)
                )
                if journal_key == key and journal_command_id == command_id:
                    self._reconcile_one_journal(journal_path, entries=entries)
            except Exception:
                log.exception("segment_store: refused unsafe refresh journal %s", journal_path)

    def _reconcile_one_journal(self, journal_path: Path, *, entries: dict[str, SegmentEntry] | None = None) -> None:
        key, target, previous, command_id, committed, publication_won, previous_entry = self._validated_journal(
            journal_path
        )
        references_target = self._metadata_references_target(key, target, entries=entries)
        accepted = references_target and target.is_file()
        if (committed or publication_won) and target.is_file():
            self._complete_reconciled_journal(journal_path, key, target, command_id)
            return
        if committed or publication_won:
            log.warning("segment_store: retained contradictory committed journal key=%s", key)
            return
        if accepted:
            self._complete_reconciled_journal(journal_path, key, target, command_id)
            return
        if references_target:
            self._restore_reconciled_journal(journal_path, key, previous, previous_entry)
            return
        if target.exists():
            self._remove_reconciled_journal(journal_path, key, target)
            return
        journal_path.unlink(missing_ok=True)

    def _complete_reconciled_journal(self, journal_path: Path, key: str, target: Path, command_id: str | None) -> None:
        if command_id is not None:
            self._record_receipt(self._write_commit_receipt(key=key, target=target, command_id=command_id))
        journal_path.unlink(missing_ok=True)
        log.info("segment_store: reconciled committed segment key=%s", key)

    def _restore_reconciled_journal(
        self, journal_path: Path, key: str, previous: Path | None, previous_entry: dict | None
    ) -> None:
        if self._restore_previous_entry(key, previous, previous_entry):
            journal_path.unlink(missing_ok=True)
            log.info("segment_store: restored previous segment metadata key=%s", key)
        else:
            log.warning("segment_store: retained unresolved segment journal key=%s", key)

    def _remove_reconciled_journal(self, journal_path: Path, key: str, target: Path) -> None:
        if self._remove_unreferenced_target(key, target):
            journal_path.unlink(missing_ok=True)
            log.info("segment_store: removed interrupted segment target key=%s", key)

    def _accepted_target(self, key: str, target: Path) -> bool:
        return self._metadata_references_target(key, target) and target.is_file()

    def _metadata_references_target(
        self, key: str, target: Path, *, entries: dict[str, SegmentEntry] | None = None
    ) -> bool:
        selected_entries = self._entries if entries is None else entries
        referenced_by = [item.key for item in selected_entries.values() if Path(item.audio_path).resolve() == target]
        if len(referenced_by) > 1 or (referenced_by and referenced_by[0] != key):
            raise ValueError("commit target is referenced by another segment")
        entry = selected_entries.get(key)
        return entry is not None and Path(entry.audio_path).resolve() == target

    def _remove_unreferenced_target(self, key: str, target: Path) -> bool:
        if self._metadata_references_target(key, target):
            return False
        try:
            target.unlink()
        except FileNotFoundError:
            return True
        except OSError:
            log.warning("segment_store: failed to clean unreferenced target key=%s", key)
            return False
        return True

    def _restore_previous_entry(self, key: str, previous: Path | None, previous_entry: dict | None) -> bool:
        if previous is None or previous_entry is None or not previous.is_file():
            return False
        restored = _entry_from_payload(dict(previous_entry))
        if restored.key != key or Path(restored.audio_path).resolve() != previous:
            return False
        current = self._entries.get(key)
        self._entries[key] = restored
        try:
            self._persist_unlocked()
        except Exception:
            if current is None:
                self._entries.pop(key, None)
            else:
                self._entries[key] = current
            return False
        return True

    def _cleanup_private_candidates(self) -> None:
        """Bound startup cleanup to regular files in the exact private namespace."""
        try:
            candidates = sorted(
                (path for path in self._audio_dir.iterdir() if self._PRIVATE_CANDIDATE_RE.fullmatch(path.name)),
                key=lambda path: path.name,
            )[: self._MAX_PRIVATE_CANDIDATES_PER_START]
        except OSError:
            log.warning("segment_store: could not inspect private candidate namespace")
            return
        for path in candidates:
            try:
                if path.is_symlink() or not path.is_file() or path.parent.resolve() != self._audio_root():
                    continue
                path.unlink()
            except OSError:
                log.warning("segment_store: bounded private candidate cleanup failed path=%s", path)

    def _cleanup_unreferenced_generations(self, key: str) -> None:
        for path in self._unreferenced_generations(key)[self._MAX_UNREFERENCED_GENERATIONS :]:
            self._remove_unreferenced_generation(path)

    def _guard_unresolved_same_key_publication(self, key: str) -> None:
        """Do not supersede a commit whose publication boundary is unresolved."""
        for journal_path in self._journal_paths_for_key(key):
            try:
                journal_key, target, _previous, command_id, committed, publication_won, _previous_entry = (
                    self._validated_journal(journal_path)
                )
            except Exception:
                log.debug("segment_store: ignored unsafe same-key journal %s", journal_path, exc_info=True)
            else:
                if journal_key == key and not committed and not publication_won:
                    references_target = self._metadata_references_target(key, target)
                    if not target.exists() and not references_target:
                        journal_path.unlink(missing_ok=True)
                    else:
                        raise SegmentCommitAmbiguousError(key=key, command_id=command_id)

    def _pending_recovery_targets(self, key: str) -> set[Path]:
        targets: set[Path] = set()
        for journal_path in self._journal_paths_for_key(key):
            target = self._pending_journal_target(journal_path, key)
            if target is not None:
                targets.add(target)
        for receipt_path in self._work_dir.glob(".segment-commit-receipt-*.json"):
            target = self._pending_receipt_target(receipt_path, key)
            if target is not None:
                targets.add(target)
        return targets

    def _pending_journal_target(self, journal_path: Path, key: str) -> Path | None:
        try:
            journal_key, target, _previous, _command_id, _committed, _publication_won, _previous_entry = (
                self._validated_journal(journal_path)
            )
        except Exception:
            return None
        return target.resolve() if journal_key == key else None

    def _pending_receipt_target(self, receipt_path: Path, key: str) -> Path | None:
        try:
            receipt = self._validated_receipt_file(receipt_path)
        except Exception:
            return None
        return Path(receipt.target).resolve() if receipt.key == key else None

    def _unreferenced_generations(self, key: str) -> list[Path]:
        safe = self._key_token(key)
        pattern = re.compile(rf"^cycle_seg_{re.escape(safe)}\.[0-9a-f]+\.wav$")
        referenced = {Path(entry.audio_path).resolve() for entry in self._entries.values()}
        referenced.update(self._pending_recovery_targets(key))
        return sorted(
            (
                path
                for path in self._audio_dir.glob(f"cycle_seg_{safe}.*.wav")
                if pattern.match(path.name) and path.resolve() not in referenced
            ),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )

    def _remove_unreferenced_generation(self, path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("segment_store: bounded cleanup failed path=%s", path)

    def _load_entries_from_payload(self, items: List[dict]) -> int:
        loaded = 0
        for item in items:
            try:
                e = _entry_from_payload(item)
                if not Path(e.audio_path).exists():
                    e.is_placeholder = True
                    e.provenance = SegmentProvenance(**{**asdict(e.provenance), "placeholder": True})
                self._entries[e.key] = e
                loaded += 1
            except Exception:
                log.warning("segment_store: skipped malformed index entry: %s", item)
        return loaded

    def _persist_unlocked(self) -> None:
        """
        Persist the store.  SQLite is the source of truth when enabled; the
        JSON index is only written in legacy file-backed mode.
        """
        self._metadata_replace_succeeded = False
        self._metadata_replace_phase = SegmentMetadataReplacePhase.NOT_ENTERED
        if self._repo is not None:
            self._repo.replace_entries(self._entry_record(entry) for entry in self._entries.values())
            return

        self._work_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._index_path.with_suffix(".tmp")
        payload = {"entries": [self._entry_record(e) for e in self._entries.values()]}
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        self._metadata_replace_phase = SegmentMetadataReplacePhase.ATTEMPTED
        os.replace(str(tmp), str(self._index_path))
        self._metadata_replace_succeeded = True
        self._metadata_replace_phase = SegmentMetadataReplacePhase.REPLACED
        self._file_snapshot_authoritative = True
        _fsync_directory(self._work_dir)

    @staticmethod
    def _entry_record(entry: SegmentEntry) -> dict:
        record = asdict(entry)
        provenance = record.pop("provenance", {})
        if "current_content_hash" in provenance:
            provenance["content_hash"] = provenance.pop("current_content_hash")
        record.update(provenance)
        return record

    def _candidate_entry(
        self,
        *,
        key: str,
        title: str,
        text: str,
        audio_path: Path,
        duration_s: float,
        refresh_interval_s: int,
        max_age_s: int | None,
        provenance: SegmentProvenance | None,
        existing: SegmentEntry | None,
    ) -> SegmentEntry:
        now_iso = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()
        observed = (provenance or SegmentProvenance()).after_success(
            text=text,
            fetch_time=provenance.fetch_time if provenance else None,
            synthesis_time=now_iso,
            source=provenance,
        )
        observed = _successful_provenance(observed, existing, placeholder=False)
        return SegmentEntry(
            key=key,
            title=title,
            text=text,
            audio_path=str(audio_path),
            duration_s=duration_s,
            last_updated_ts=time.time(),
            refresh_interval_s=refresh_interval_s,
            max_age_s=max_age_s if max_age_s is not None else refresh_interval_s,
            is_placeholder=False,
            provenance=observed,
        )

    def _finish_committed_candidate(self, *, key: str, target: Path, command_id: str | None) -> None:
        journal_path = self._journal_path(key, self._operation_id(target=target, command_id=command_id))
        if command_id is not None:
            try:
                self._record_receipt(self._write_commit_receipt(key=key, target=target, command_id=command_id))
            except OSError:
                # The committed journal remains the durable correlation record.
                # Receipt materialization is auxiliary and must not rewrite the
                # already-committed segment truth.
                log.warning("segment_store: failed to write durable refresh receipt key=%s", key)
                return
        try:
            journal_path.unlink(missing_ok=True)
        except OSError:
            log.warning("segment_store: failed to clear completed commit journal key=%s", key)

    def commit_candidate(
        self,
        *,
        key: str,
        title: str,
        text: str,
        candidate_path: Path,
        duration_s: float,
        refresh_interval_s: int,
        max_age_s: int | None = None,
        provenance: SegmentProvenance | None = None,
        command_id: str | None = None,
    ) -> SegmentCommitResult:
        """Publish an immutable artifact and metadata as one recoverable commit."""
        candidate = Path(candidate_path)
        target = self._versioned_audio_path(key)
        with self._state_lock:
            self._guard_unresolved_same_key_publication(key)
            if not candidate.is_file() or candidate.is_symlink():
                raise RuntimeError("staged segment artifact is missing or unsafe")
            existing = self._entries.get(key)
            previous = Path(existing.audio_path) if existing is not None else None
            previous_entry = self._entry_record(existing) if existing is not None else None
            self._metadata_replace_succeeded = False
            self._metadata_replace_phase = SegmentMetadataReplacePhase.NOT_ENTERED
            self._write_commit_journal(
                key=key,
                target=target,
                previous=previous,
                command_id=command_id,
                previous_entry=previous_entry,
            )
            new_entry: SegmentEntry | None = None
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(str(candidate), str(target))
                with target.open("rb") as artifact:
                    os.fsync(artifact.fileno())
                _fsync_directory(target.parent)
                new_entry = self._candidate_entry(
                    key=key,
                    title=title,
                    text=text,
                    audio_path=target,
                    duration_s=duration_s,
                    refresh_interval_s=refresh_interval_s,
                    max_age_s=max_age_s,
                    provenance=provenance,
                    existing=existing,
                )
                self._entries[key] = new_entry
                self._persist_unlocked()
            except BaseException as exc:
                self._handle_candidate_failure(
                    key=key,
                    target=target,
                    existing=existing,
                    new_entry=new_entry,
                    command_id=command_id,
                    cause=exc,
                )
                raise
            try:
                self._mark_publication_won(key=key, target=target, command_id=command_id)
            except BaseException as exc:
                # Metadata is authoritative, but the durable witness needed
                # to reconcile it after a later same-key publication is not.
                # Retain the target and journal and force exact recovery.
                self._entries[key] = new_entry
                raise SegmentCommitAmbiguousError(key=key, command_id=command_id) from exc
            try:
                self._mark_commit_committed(key=key, target=target, command_id=command_id)
            except OSError:
                log.warning("segment_store: failed to mark committed refresh journal key=%s", key)
            except Exception:
                log.exception("segment_store: failed to mark committed refresh journal key=%s", key)
            self._finish_committed_candidate(key=key, target=target, command_id=command_id)
            try:
                self._cleanup_unreferenced_generations(key)
            except OSError:
                log.warning("segment_store: bounded cleanup could not complete key=%s", key)
            return SegmentCommitResult(key=key, entry=new_entry)

    def _handle_candidate_failure(
        self,
        *,
        key: str,
        target: Path,
        existing: SegmentEntry | None,
        new_entry: SegmentEntry | None,
        command_id: str | None,
        cause: BaseException,
    ) -> None:
        if self._metadata_replace_phase is not SegmentMetadataReplacePhase.NOT_ENTERED:
            if new_entry is not None:
                self._entries[key] = new_entry
            raise SegmentCommitAmbiguousError(key=key, command_id=command_id) from cause
        if existing is None:
            self._entries.pop(key, None)
        else:
            self._entries[key] = existing
        self._remove_unreferenced_target(key, target)

    # ------------------------------------------------------------------
    #  Read API  (no lock required)
    # ------------------------------------------------------------------

    def get(self, key: str) -> Optional[SegmentEntry]:
        return self._entries.get(key)

    def all_keys(self) -> List[str]:
        return list(self._entries.keys())

    def is_stale(self, key: str) -> bool:
        """True if the entry is missing or past its refresh interval."""
        e = self._entries.get(key)
        return e is None or e.is_stale()

    def is_ready(self, key: str) -> bool:
        """True if entry exists, has audio on disk, and is not a placeholder."""
        return segment_entry_eligible_to_air(self._entries.get(key))

    def health_snapshot(self) -> Dict[str, int | float]:
        """Return bounded freshness counts without exposing text or paths."""
        entries = tuple(self._entries.values())
        now = time.time()
        ages = [max(0.0, now - entry.last_updated_ts) for entry in entries if entry.last_updated_ts > 0]
        return {
            "count": len(entries),
            "ready_count": sum(self.is_ready(entry.key) for entry in entries),
            "stale_count": sum(entry.is_stale() for entry in entries),
            "placeholder_count": sum(entry.is_placeholder for entry in entries),
            "oldest_age_seconds": max(ages, default=0.0),
        }

    # ------------------------------------------------------------------
    #  Write API  (async, takes lock)
    # ------------------------------------------------------------------

    async def update(
        self,
        key: str,
        title: str,
        text: str,
        audio_path: Path,
        duration_s: float,
        refresh_interval_s: int,
        max_age_s: int | None = None,
        *,
        is_placeholder: bool = False,
        provenance: SegmentProvenance | None = None,
    ) -> None:
        """Register or replace a segment entry and persist the index."""
        async with self._lock:
            with self._state_lock:
                existing = self._entries.get(key)
                now_iso = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()
                observed = (provenance or SegmentProvenance()).after_success(
                    text=text,
                    fetch_time=(provenance.fetch_time if provenance else None),
                    synthesis_time=now_iso,
                    source=provenance,
                )
                observed = _successful_provenance(observed, existing, placeholder=is_placeholder)
                self._entries[key] = SegmentEntry(
                    key=key,
                    title=title,
                    text=text,
                    audio_path=str(audio_path),
                    duration_s=duration_s,
                    last_updated_ts=time.time(),
                    refresh_interval_s=refresh_interval_s,
                    max_age_s=max_age_s if max_age_s is not None else refresh_interval_s,
                    is_placeholder=is_placeholder,
                    provenance=observed,
                )
                self._persist_unlocked()
        log.debug(
            "segment_store: updated key=%s dur=%.1fs placeholder=%s",
            key,
            duration_s,
            is_placeholder,
        )

    async def mark_placeholder(
        self,
        key: str,
        title: str,
        refresh_interval_s: int,
        max_age_s: int | None = None,
        error: str | None = None,
    ) -> None:
        """
        Register *key* as known-unavailable.  Sets ``last_updated_ts=0`` so
        the entry is immediately stale and the refresher will retry.
        """
        async with self._lock:
            with self._state_lock:
                existing = self._entries.get(key)
                if existing is not None and not existing.is_placeholder and existing.last_updated_ts > 0:
                    if error:
                        existing.provenance = existing.provenance.after_failure(error)
                    self._persist_unlocked()
                    return
                provenance = (existing.provenance if existing is not None else SegmentProvenance()).after_failure(
                    error or "content unavailable"
                )
                provenance = SegmentProvenance(**{**asdict(provenance), "placeholder": True})
                self._entries[key] = SegmentEntry(
                    key=key,
                    title=title,
                    text="",
                    audio_path=str(self.audio_path_for(key)),
                    duration_s=0.0,
                    last_updated_ts=0.0,  # immediately stale → refresher retries
                    refresh_interval_s=refresh_interval_s,
                    max_age_s=max_age_s if max_age_s is not None else refresh_interval_s,
                    is_placeholder=True,
                    provenance=provenance,
                )
                self._persist_unlocked()
        log.debug("segment_store: marked placeholder key=%s", key)

    async def record_failure(
        self,
        key: str,
        error: str,
        *,
        title: str | None = None,
        refresh_interval_s: int = 0,
        max_age_s: int = 0,
    ) -> None:
        """Retain last-known-good content while recording bounded failure evidence."""
        async with self._lock:
            with self._state_lock:
                existing = self._entries.get(key)
                if existing is None:
                    self._entries[key] = SegmentEntry(
                        key=key,
                        title=title or key,
                        text="",
                        audio_path=str(self.audio_path_for(key)),
                        duration_s=0.0,
                        last_updated_ts=0.0,
                        refresh_interval_s=refresh_interval_s,
                        max_age_s=max_age_s or refresh_interval_s,
                        is_placeholder=True,
                        provenance=SegmentProvenance(placeholder=True).after_failure(error),
                    )
                else:
                    existing.provenance = existing.provenance.after_failure(error)
                self._persist_unlocked()

    # ------------------------------------------------------------------
    #  Async synthesis helper
    # ------------------------------------------------------------------

    async def synth_and_update(
        self,
        synthesizer: SynthesisClient,
        key: str,
        title: str,
        text: str,
        refresh_interval_s: int,
        max_age_s: int | None = None,
        *,
        sample_rate: int,
        seg_gap_s: float = _DEFAULT_SEG_GAP_S,
        provenance: SegmentProvenance | None = None,
        publication_fence=None,
        publication_committed=None,
        publication_aborted=None,
        command_id: str | None = None,
    ) -> float:
        """
        Request worker-owned synthesis for *text* and atomically update the
        store entry. Returns duration in seconds. The worker result is then
        padded and promoted through the controller-owned segment commit path.
        """
        candidate_path = self._audio_dir / f".segment-candidate-{uuid.uuid4().hex}.wav"

        def complete_commit() -> None:
            result = self.commit_candidate(
                key=key,
                title=title,
                text=text,
                candidate_path=candidate_path,
                duration_s=wav_duration_seconds(candidate_path),
                refresh_interval_s=refresh_interval_s,
                max_age_s=max_age_s,
                provenance=provenance,
                command_id=command_id,
            )
            if publication_committed is not None:
                publication_committed(result)

        try:
            return await render_segment_wav_async(
                synthesizer,
                text,
                candidate_path,
                sample_rate=sample_rate,
                seg_gap_s=seg_gap_s,
                publication_fence=publication_fence,
                publication_committed=complete_commit,
                publication_aborted=publication_aborted,
            )
        finally:
            candidate_path.unlink(missing_ok=True)

    def mark_aired(self, key: str, aired_at: str, next_eligible_airtime: str | None) -> None:
        """Persist airing evidence after the conductor accepted a cycle push."""
        with self._state_lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            entry.provenance = SegmentProvenance(
                **{
                    **asdict(entry.provenance),
                    "last_aired": aired_at,
                    "next_eligible_airtime": next_eligible_airtime,
                }
            )
            self._persist_unlocked()
