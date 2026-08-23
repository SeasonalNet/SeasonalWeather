from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

from ..diagnostics.bindings import FOUNDATION_CODES
from .assets import AudioAssetRepository
from .commands import CommandRepository
from .core import SeasonalDatabase
from .segments import SegmentRepository

log = logging.getLogger("seasonalweather.database.housekeeping")

# Generated audio prefixes used by SeasonalWeather's render paths.  Files that
# do not match one of these patterns are left alone unless they live in tmp/.
_GENERATED_AUDIO_GLOBS = (
    "alert_*.wav",
    "api_audio_alert_*.wav",
    "api_text_*.wav",
    "capupdate_*.wav",
    "capvoice_*.wav",
    "cycle_*.wav",
    "cycle_seg*.wav",
    "cycle_time*.wav",
    "ipawsvoice_*.wav",
    "insert_*.wav",
    "nwwsvoice_*.wav",
    "rebcast_*.wav",
)


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _to_utc_iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()


def _safe_unlink(path: Path) -> bool:
    try:
        path.unlink(missing_ok=True)
        return True
    except Exception:
        log.exception("database housekeeping: failed to delete %s", path)
        return False


class DatabaseHousekeeper:
    def __init__(self, cfg: Any, db: SeasonalDatabase, diagnostic_sink: object | None = None) -> None:
        self.cfg = cfg
        self.db = db
        self._diagnostic_sink = diagnostic_sink
        self._assets = AudioAssetRepository(db)
        self._commands = CommandRepository(db)
        self._segments = SegmentRepository(db)
        self._stop = asyncio.Event()
        artifact_root = str(getattr(cfg.paths, "artifact_dir", "") or "").strip() or cfg.paths.work_dir
        temporary_root = str(getattr(cfg.paths, "temporary_dir", "") or "").strip()
        self._uploads_dir = Path(artifact_root) / "api" / "uploads"
        self._audio_dir = Path(cfg.paths.audio_dir)
        self._tmp_dir = Path(temporary_root) / "seasonalweather" if temporary_root else Path(cfg.paths.work_dir) / "tmp"

    def stop(self) -> None:
        self._stop.set()

    def _enabled(self) -> bool:
        return bool(getattr(self.cfg.database.housekeeping, "enabled", True))

    def _interval_seconds(self) -> int:
        return max(60, int(getattr(self.cfg.database.housekeeping, "interval_seconds", 900) or 900))

    def _startup_delay_seconds(self) -> int:
        return max(0, int(getattr(self.cfg.database.housekeeping, "startup_delay_seconds", 45) or 45))

    def _audio_asset_grace_seconds(self) -> int:
        return max(60, int(getattr(self.cfg.database.housekeeping, "audio_asset_grace_seconds", 900) or 900))

    def _generated_audio_retention_seconds(self) -> int:
        return max(
            3600,
            int(getattr(self.cfg.database.housekeeping, "generated_audio_retention_seconds", 10800) or 86400),
        )

    def _generated_audio_max_bytes(self) -> int:
        return max(0, int(getattr(self.cfg.database.housekeeping, "generated_audio_max_bytes", 1073741824) or 0))

    def _tmp_file_grace_seconds(self) -> int:
        return max(60, int(getattr(self.cfg.database.housekeeping, "tmp_file_grace_seconds", 900) or 900))

    def _api_command_retention_days(self) -> int:
        return max(1, int(getattr(self.cfg.database.housekeeping, "api_command_retention_days", 14) or 14))

    def _wal_checkpoint_enabled(self) -> bool:
        return bool(getattr(self.cfg.database.housekeeping, "wal_checkpoint", True))

    async def run_forever(self) -> None:
        if not self._enabled():
            log.info("Database housekeeping disabled")
            return

        startup_delay = self._startup_delay_seconds()
        if startup_delay > 0:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=float(startup_delay))
                return
            except asyncio.TimeoutError:
                pass

        log.info(
            "Database housekeeping enabled (interval=%ss command_retention=%sd asset_grace=%ss generated_audio_retention=%ss generated_audio_max=%s tmp_grace=%ss)",
            self._interval_seconds(),
            self._api_command_retention_days(),
            self._audio_asset_grace_seconds(),
            self._generated_audio_retention_seconds(),
            self._generated_audio_max_bytes(),
            self._tmp_file_grace_seconds(),
        )

        while not self._stop.is_set():
            try:
                stats = self.run_once()
                if any(v for v in stats.values()):
                    log.info("Database housekeeping: %s", ", ".join(f"{k}={v}" for k, v in sorted(stats.items()) if v))
                if stats.get("segment_placeholders_marked", 0):
                    self._diagnose(
                        FOUNDATION_CODES["database.reconciliation_required"],
                        "Database reconciliation marked missing segment artifacts as placeholders.",
                    )
            except Exception as exc:
                log.exception("Database housekeeping: pass failed")
                self._diagnose(
                    FOUNDATION_CODES["database.operation_failed"],
                    "Database housekeeping failed within the bounded maintenance pass.",
                    exc,
                )

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=float(self._interval_seconds()))
                break
            except asyncio.TimeoutError:
                continue

    def _diagnose(self, code: str, message: str, exception: BaseException | None = None) -> None:
        sink = self._diagnostic_sink
        emit = getattr(sink, "emit", None)
        if not callable(emit):
            return
        emit(
            code,
            component="database-housekeeping",
            message=message,
            operational_effect="Database maintenance is degraded; canonical state remains controller-owned.",
            recovery_action="Inspect the bounded database failure and allow the next maintenance pass to retry.",
            exception=exception,
            source_id="sqlite",
        )

    def run_once(self) -> dict[str, int]:
        now = _utc_now()
        stats: dict[str, int] = {}
        stats["api_commands_pruned"] = self._prune_api_commands(now)
        asset_stats = self._prune_audio_assets(now)
        stats.update(asset_stats)
        generated_stats = self._cleanup_generated_audio(now)
        stats.update(generated_stats)
        stats["tmp_files_deleted"] = self._cleanup_tmp_directory(now)
        stats["segment_placeholders_marked"] = self._reconcile_segments()
        if self._wal_checkpoint_enabled():
            stats["wal_checkpointed"] = self._wal_checkpoint()
        return stats

    def _prune_api_commands(self, now: dt.datetime) -> int:
        cutoff = _to_utc_iso(now - dt.timedelta(days=self._api_command_retention_days()))
        return self._commands.prune_terminal_before(cutoff)

    def _prune_audio_assets(self, now: dt.datetime) -> dict[str, int]:
        expired = self._assets.list_expired_assets(_to_utc_iso(now))
        removed_rows = 0
        removed_files = 0
        expired_ids: list[str] = []
        for item in expired:
            asset_id = str(item.get("asset_id") or "").strip()
            if asset_id:
                expired_ids.append(asset_id)
            wav_path = str(item.get("wav_path") or "").strip()
            if wav_path:
                removed_files += 1 if _safe_unlink(Path(wav_path)) else 0
            meta_path = self._uploads_dir / f"{asset_id}.json"
            if asset_id:
                removed_files += 1 if _safe_unlink(meta_path) else 0
        if expired_ids:
            removed_rows = self._assets.delete_assets(expired_ids)

        dir_stats = self._cleanup_upload_directory(now)
        return {
            "audio_assets_pruned": removed_rows,
            "upload_files_deleted": removed_files + dir_stats,
        }

    def _cleanup_upload_directory(self, now: dt.datetime) -> int:
        if not self._uploads_dir.exists():
            return 0
        grace_cutoff = now - dt.timedelta(seconds=self._audio_asset_grace_seconds())
        grace_ts = grace_cutoff.timestamp()
        live_assets = self._assets.list_live_assets(_to_utc_iso(now))
        live_asset_ids = {str(item.get("asset_id") or "") for item in live_assets}
        keep_wavs = {str(item.get("wav_path") or "") for item in live_assets if str(item.get("wav_path") or "").strip()}

        legacy_keep_wavs: set[str] = set()
        deleted = 0

        for meta_path in sorted(self._uploads_dir.glob("aud_*.json")):
            asset_id = meta_path.stem
            if asset_id in live_asset_ids:
                deleted += 1 if _safe_unlink(meta_path) else 0
                continue
            try:
                payload = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                payload = None
            if isinstance(payload, dict):
                wav_path = str(payload.get("path") or "").strip()
                expires_raw = str(payload.get("expires_at") or "").strip()
                keep_legacy = False
                if wav_path:
                    keep_legacy = True
                    legacy_keep_wavs.add(wav_path)
                if expires_raw:
                    try:
                        exp = dt.datetime.fromisoformat(expires_raw)
                        if exp.tzinfo is None:
                            exp = exp.replace(tzinfo=dt.timezone.utc)
                        else:
                            exp = exp.astimezone(dt.timezone.utc)
                        if exp <= now:
                            keep_legacy = False
                    except Exception:
                        keep_legacy = False
                if not keep_legacy and meta_path.stat().st_mtime <= grace_ts:
                    deleted += 1 if _safe_unlink(meta_path) else 0
            elif meta_path.stat().st_mtime <= grace_ts:
                deleted += 1 if _safe_unlink(meta_path) else 0

        keep_wavs.update(legacy_keep_wavs)
        for wav_path in sorted(self._uploads_dir.glob("aud_*.wav")):
            if str(wav_path) in keep_wavs:
                continue
            if wav_path.stat().st_mtime <= grace_ts:
                deleted += 1 if _safe_unlink(wav_path) else 0

        for tmp_path in sorted(self._uploads_dir.glob("aud_*.upload.wav")):
            if tmp_path.stat().st_mtime <= grace_ts:
                deleted += 1 if _safe_unlink(tmp_path) else 0
        return deleted

    def _cleanup_generated_audio(self, now: dt.datetime) -> dict[str, int]:
        if not self._audio_dir.exists():
            return {"generated_audio_deleted": 0, "generated_audio_bytes_deleted": 0}

        keep_paths = self._protected_audio_paths(now)
        cutoff_ts = (now - dt.timedelta(seconds=self._generated_audio_retention_seconds())).timestamp()
        candidates = self._generated_audio_candidates()

        deleted = 0
        bytes_deleted = 0
        survivors: list[tuple[float, Path, int]] = []

        for wav_path in candidates:
            keep_key = self._path_key(wav_path)
            if keep_key in keep_paths:
                continue
            try:
                st = wav_path.stat()
            except FileNotFoundError:
                continue
            except Exception:
                log.exception("database housekeeping: failed to stat generated audio %s", wav_path)
                continue
            size = int(st.st_size)
            if st.st_mtime <= cutoff_ts:
                if _safe_unlink(wav_path):
                    deleted += 1
                    bytes_deleted += size
                continue
            survivors.append((float(st.st_mtime), wav_path, size))

        max_bytes = self._generated_audio_max_bytes()
        if max_bytes > 0:
            current_bytes = sum(size for _mtime, _path, size in survivors)
            for _mtime, wav_path, size in sorted(survivors, key=lambda item: item[0]):
                if current_bytes <= max_bytes:
                    break
                keep_key = self._path_key(wav_path)
                if keep_key in keep_paths:
                    continue
                if _safe_unlink(wav_path):
                    deleted += 1
                    bytes_deleted += size
                    current_bytes -= size

        return {
            "generated_audio_deleted": deleted,
            "generated_audio_bytes_deleted": bytes_deleted,
        }

    def _cleanup_tmp_directory(self, now: dt.datetime) -> int:
        if not self._tmp_dir.exists():
            return 0
        cutoff_ts = (now - dt.timedelta(seconds=self._tmp_file_grace_seconds())).timestamp()
        deleted = 0
        for path in sorted(self._tmp_dir.rglob("*"), reverse=True):
            try:
                if path.is_dir():
                    # Only remove empty directories after their contents have been handled.
                    try:
                        path.rmdir()
                        deleted += 1
                    except OSError:
                        pass
                    continue
                if path.stat().st_mtime <= cutoff_ts:
                    deleted += 1 if _safe_unlink(path) else 0
            except FileNotFoundError:
                continue
            except Exception:
                log.exception("database housekeeping: failed to clean tmp path %s", path)
        return deleted

    def _generated_audio_candidates(self) -> list[Path]:
        candidates: dict[str, Path] = {}
        for pattern in _GENERATED_AUDIO_GLOBS:
            for path in self._audio_dir.glob(pattern):
                if path.is_file():
                    candidates[self._path_key(path)] = path
        return sorted(candidates.values(), key=lambda p: str(p))

    def _protected_audio_paths(self, now: dt.datetime) -> set[str]:
        keep: set[str] = set()
        now_iso = _to_utc_iso(now)

        for item in self._segments.load_entries():
            self._add_audio_path(keep, item.get("audio_path"))

        try:
            with self.db.connect() as conn:
                rows = conn.execute(
                    "SELECT audio_path FROM active_alerts WHERE audio_path IS NOT NULL AND expires_at >= ?",
                    (now_iso,),
                ).fetchall()
                for row in rows:
                    self._add_audio_path(keep, row["audio_path"])

                feed_rows = conn.execute(
                    "SELECT payload_json FROM station_feed_alerts WHERE expires_at >= ?",
                    (now_iso,),
                ).fetchall()
                for row in feed_rows:
                    self._add_station_feed_audio_paths(keep, str(row["payload_json"] or "{}"))

                insert_rows = conn.execute(
                    """
                    SELECT audio_path FROM cycle_inserts
                    WHERE audio_path IS NOT NULL
                      AND status = 'active'
                      AND expires_at >= ?
                    """,
                    (now_iso,),
                ).fetchall()
                for row in insert_rows:
                    self._add_audio_path(keep, row["audio_path"])
        except Exception:
            log.exception("database housekeeping: failed to load protected audio paths")

        for item in self._assets.list_live_assets(now_iso):
            self._add_audio_path(keep, item.get("wav_path"))

        return keep

    def _add_station_feed_audio_paths(self, keep: set[str], payload_json: str) -> None:
        try:
            payload = json.loads(payload_json)
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        links = payload.get("links")
        if isinstance(links, dict):
            self._add_audio_path(keep, links.get("wav"))
            self._add_audio_path(keep, links.get("audio"))
        self._add_audio_path(keep, payload.get("audio_path"))

    def _add_audio_path(self, keep: set[str], raw_path: Any) -> None:
        raw = str(raw_path or "").strip()
        if not raw:
            return
        path = Path(raw)
        if not path.is_absolute():
            path = self._audio_dir / path
        keep.add(self._path_key(path))

    @staticmethod
    def _path_key(path: Path) -> str:
        return str(path.expanduser().absolute())

    def _reconcile_segments(self) -> int:
        entries = self._segments.load_entries()
        changed = 0
        for entry in entries:
            audio_path = str(entry.get("audio_path") or "").strip()
            if not audio_path:
                if not bool(entry.get("is_placeholder", False)):
                    entry["is_placeholder"] = True
                    entry["duration_s"] = 0.0
                    entry["last_updated_ts"] = 0.0
                    changed += 1
                continue
            if not Path(audio_path).exists() and not bool(entry.get("is_placeholder", False)):
                entry["is_placeholder"] = True
                entry["duration_s"] = 0.0
                entry["last_updated_ts"] = 0.0
                changed += 1
        if changed:
            self._segments.replace_entries(entries)
        return changed

    def _wal_checkpoint(self) -> int:
        try:
            with self.db.connect() as conn:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            return 1
        except Exception:
            log.exception("Database housekeeping: WAL checkpoint failed")
            return 0
