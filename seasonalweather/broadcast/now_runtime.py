"""Runtime handling for NWWS Short-Term Forecast (NOW) products."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..alerts.product import ParsedProduct, parse_product_text
from .formatters import parse_nws_header_issued_dt
from .segment_store import render_segment_wav as _legacy_render_segment_wav
from .segment_store import render_segment_wav_async

# Retain the historical module-level seam for deterministic runtime tests and
# downstream callers. Production execution uses the joined async bridge below.
render_segment_wav = _legacy_render_segment_wav


async def _render_now_segment(tts, text: str, output_path: Path, *, sample_rate: int) -> float:
    if render_segment_wav is not _legacy_render_segment_wav:
        return float(render_segment_wav(tts, text, output_path, sample_rate=sample_rate))
    return float(
        await render_segment_wav_async(
            tts,
            text,
            output_path,
            sample_rate=sample_rate,
        )
    )


log = logging.getLogger("seasonalweather.broadcast.now_runtime")


@dataclass(frozen=True)
class _NowWorkItem:
    parsed: ParsedProduct
    source: str
    product_id: str | None = None


class NowRuntime:
    """Publish accepted NOW products as expiring routine cycle inserts.

    NOW products intentionally bypass AlertTracker and the interrupt audio
    planes.  The existing cycle-insert repository provides persistence,
    repetition, expiry, and support for more than one simultaneous local NOW.
    """

    def __init__(self, host: Any) -> None:
        self.host = host
        self._warned_no_repository = False
        self._queue: asyncio.Queue[_NowWorkItem] = asyncio.Queue(maxsize=50)
        self._queued_api_product_ids: set[str] = set()
        self._seen_api_product_ids: dict[str, dt.datetime] = {}

    def submit(
        self,
        parsed: ParsedProduct,
        *,
        source: str = "nwws",
        product_id: str | None = None,
    ) -> bool:
        """Queue a NOW product without delaying the primary NWWS consumer.

        API product IDs are tracked in memory so a two-minute recovery poll does
        not repeatedly fetch or enqueue the same product during one process
        lifetime. Products that raise during processing are deliberately left
        retryable on the next poll.
        """
        normalized_product_id = str(product_id or "").strip() or None
        if normalized_product_id and (
            normalized_product_id in self._queued_api_product_ids or normalized_product_id in self._seen_api_product_ids
        ):
            return False

        item = _NowWorkItem(
            parsed=parsed,
            source=str(source or "nwws").strip().lower() or "nwws",
            product_id=normalized_product_id,
        )
        try:
            self._queue.put_nowait(item)
            if normalized_product_id:
                self._queued_api_product_ids.add(normalized_product_id)
            return True
        except asyncio.QueueFull:
            log.error(
                "NOW worker queue full; dropping source=%s wfo=%s awips=%s product_id=%s",
                item.source,
                parsed.wfo,
                parsed.awips_id or "",
                normalized_product_id or "",
            )
            return False

    async def run(self) -> None:
        """Render submitted NOW products on a dedicated routine-content worker."""
        while True:
            item = await self._queue.get()
            try:
                await self.handle(
                    item.parsed,
                    source=item.source,
                    product_id=item.product_id,
                )
                if item.product_id:
                    self._seen_api_product_ids[item.product_id] = dt.datetime.now(dt.timezone.utc)
            except Exception:
                log.exception(
                    "NOW worker failed source=%s wfo=%s awips=%s product_id=%s",
                    item.source,
                    item.parsed.wfo,
                    item.parsed.awips_id or "",
                    item.product_id or "",
                )
            finally:
                if item.product_id:
                    self._queued_api_product_ids.discard(item.product_id)
                self._queue.task_done()

    def backfill_wfos(self) -> list[str]:
        """Return configured three-letter offices for NOW API recovery."""
        offices: set[str] = set()
        for raw in getattr(self.host, "_nwws_allowed_wfos", set()):
            office = str(raw or "").strip().upper()
            if len(office) == 4 and office.startswith("K"):
                offices.add(office[1:])
            elif len(office) == 3:
                offices.add(office)
        return sorted(offices)

    def _prune_seen_api_product_ids(self, *, now_utc: dt.datetime) -> None:
        """Bound in-memory API dedupe state for long-running services."""
        retention_minutes = max(
            360,
            int(self.host.cfg.now.api_backfill.lookback_minutes) * 2,
        )
        cutoff = now_utc - dt.timedelta(minutes=retention_minutes)
        self._seen_api_product_ids = {
            product_id: seen_at for product_id, seen_at in self._seen_api_product_ids.items() if seen_at >= cutoff
        }

    async def backfill_recent_once(self) -> int:
        """Queue recent api.weather.gov NOW products for normal processing.

        More than one NOW may be active for non-overlapping UGC scopes, so the
        recovery path walks a bounded recent index rather than fetching only the
        single latest product. References are processed newest first; the normal
        stale-order and overlap checks then reject older superseded siblings.
        """
        cfg = self.host.cfg.now
        backfill = cfg.api_backfill
        if not cfg.enabled or not backfill.enabled:
            return 0

        offices = self.backfill_wfos()
        if not offices:
            log.debug("NOW API backfill skipped; no nwws.allowed_wfos configured")
            return 0

        now_utc = dt.datetime.now(dt.timezone.utc)
        self._prune_seen_api_product_ids(now_utc=now_utc)
        cutoff = now_utc - dt.timedelta(minutes=backfill.lookback_minutes)
        submitted = 0

        for office in offices:
            try:
                refs = await self.host.api.list_product_references(
                    "NOW",
                    office,
                    limit=backfill.max_products_per_office,
                )
            except Exception:
                log.exception("NOW API product-index backfill failed for office=%s", office)
                continue

            for ref in refs:
                product_id = str(ref.product_id or "").strip()
                if not product_id:
                    continue
                if product_id in self._seen_api_product_ids or product_id in self._queued_api_product_ids:
                    continue

                reference_issued = self._parse_utc_iso(ref.issuance_time)
                if reference_issued is not None and reference_issued < cutoff:
                    continue

                try:
                    product = await self.host.api.get_product(product_id)
                    if product is None or not product.product_text:
                        continue
                    parsed = parse_product_text(product.product_text)
                    if parsed is None or (parsed.product_type or "").strip().upper() != "NOW":
                        log.warning(
                            "NOW API backfill rejected mismatched product: office=%s product_id=%s parsed_type=%s",
                            office,
                            product_id,
                            getattr(parsed, "product_type", "") or "",
                        )
                        self._seen_api_product_ids[product_id] = now_utc
                        continue

                    expected_wfo = f"K{office}"
                    if (parsed.wfo or "").strip().upper() != expected_wfo:
                        log.warning(
                            "NOW API backfill rejected mismatched office: expected=%s parsed=%s product_id=%s",
                            expected_wfo,
                            parsed.wfo,
                            product_id,
                        )
                        self._seen_api_product_ids[product_id] = now_utc
                        continue

                    if self.submit(
                        parsed,
                        source="api-backfill",
                        product_id=product_id,
                    ):
                        submitted += 1
                except Exception:
                    log.exception(
                        "NOW API product backfill failed for office=%s product_id=%s",
                        office,
                        product_id,
                    )

        return submitted

    async def run_backfill_loop(self) -> None:
        """Recover recent NOW products that NWWS-OI did not deliver."""
        backfill = self.host.cfg.now.api_backfill
        await asyncio.sleep(backfill.initial_delay_seconds)
        while True:
            submitted = await self.backfill_recent_once()
            if submitted:
                log.info("NOW API backfill queued %d product(s)", submitted)
            await asyncio.sleep(backfill.interval_seconds)

    @staticmethod
    def _utc_iso(value: dt.datetime) -> str:
        return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()

    @staticmethod
    def _parse_utc_iso(value: Any) -> dt.datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = dt.datetime.fromisoformat(raw)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)

    @staticmethod
    def _scope_insert_id(*, wfo: str, awips_id: str, zones: Iterable[str]) -> str:
        scope = ",".join(sorted({str(z).strip().upper() for z in zones if str(z).strip()}))
        digest = hashlib.sha1(f"{wfo.strip().upper()}|{awips_id.strip().upper()}|{scope}".encode("utf-8")).hexdigest()[
            :16
        ]
        return f"nwws_now_{digest}"

    def _cancel_overlapping(self, *, wfo: str, zones: Iterable[str], keep_id: str | None = None) -> int:
        repo = getattr(self.host, "cycle_insert_repo", None)
        if repo is None:
            return 0

        wanted = {str(z).strip().upper() for z in zones if str(z).strip()}
        if not wanted:
            return 0

        cancelled = 0
        now_iso = self._utc_iso(dt.datetime.now(dt.timezone.utc))
        for item in repo.list_inserts(include_inactive=False, limit=500):
            insert_id = str(item.get("insert_id") or "")
            if keep_id and insert_id == keep_id:
                continue
            meta = item.get("meta") or {}
            if str(meta.get("source_type") or "") not in {"nws_now", "nwws_now"}:
                continue
            if str(meta.get("wfo") or "").strip().upper() != wfo.strip().upper():
                continue
            prior_zones = {str(z).strip().upper() for z in (meta.get("ugc_zones") or []) if str(z).strip()}
            if not (wanted & prior_zones):
                continue
            repo.cancel_insert(insert_id=insert_id, updated_at=now_iso)
            cancelled += 1
        return cancelled

    def _newer_overlap_exists(
        self,
        *,
        wfo: str,
        zones: Iterable[str],
        issued_utc: dt.datetime,
    ) -> bool:
        """Protect a current cycle insert from delayed/backfilled older NOW text."""
        repo = getattr(self.host, "cycle_insert_repo", None)
        if repo is None:
            return False

        wanted = {str(z).strip().upper() for z in zones if str(z).strip()}
        if not wanted:
            return False

        for item in repo.list_inserts(include_inactive=False, limit=500):
            meta = item.get("meta") or {}
            if str(meta.get("source_type") or "") not in {"nws_now", "nwws_now"}:
                continue
            if str(meta.get("wfo") or "").strip().upper() != wfo.strip().upper():
                continue
            prior_zones = {str(z).strip().upper() for z in (meta.get("ugc_zones") or []) if str(z).strip()}
            if not (wanted & prior_zones):
                continue
            prior_issued = self._parse_utc_iso(meta.get("issued_at"))
            if prior_issued is not None and prior_issued > issued_utc + dt.timedelta(seconds=30):
                return True
        return False

    def _notify_changed(self) -> None:
        try:
            conductor = getattr(self.host, "conductor", None)
            if conductor is not None and hasattr(conductor, "notify_inserts_changed"):
                conductor.notify_inserts_changed()
        except Exception:
            log.debug("NOW cycle-insert notification failed", exc_info=True)

    async def handle(
        self,
        parsed: ParsedProduct,
        *,
        source: str = "nwws",
        product_id: str | None = None,
    ) -> bool:
        """Target, render, and queue one NOW product.  Returns True if active."""
        cfg = self.host.cfg.now
        if not cfg.enabled:
            log.debug("NOW disabled; ignoring wfo=%s awips=%s", parsed.wfo, parsed.awips_id or "")
            return False

        repo = getattr(self.host, "cycle_insert_repo", None)
        if repo is None:
            if not self._warned_no_repository:
                log.warning("NOW cycle audio unavailable because the SQLite cycle-insert repository is disabled")
                self._warned_no_repository = True
            return False

        raw_text = parsed.raw_text or ""
        now_utc = dt.datetime.now(dt.timezone.utc)
        issued_utc = parse_nws_header_issued_dt(raw_text) or now_utc
        (
            zones,
            in_area_same,
            ugc_source,
            mapped_ok,
            ugc_expires_utc,
        ) = await self.host.target_resolver._nwws_same_targets_from_texts(raw_text, raw_text)

        if not zones:
            log.warning(
                "NOW suppressed: no UGC block wfo=%s awips=%s",
                parsed.wfo,
                parsed.awips_id or "",
            )
            return False
        if not mapped_ok:
            log.warning(
                "NOW suppressed: UGC mapping failed wfo=%s awips=%s zones=%s",
                parsed.wfo,
                parsed.awips_id or "",
                ",".join(zones[:20]),
            )
            return False
        if self._newer_overlap_exists(
            wfo=parsed.wfo,
            zones=zones,
            issued_utc=issued_utc,
        ):
            log.info(
                "NOW superseded before processing: wfo=%s awips=%s issued=%s zones=%s",
                parsed.wfo,
                parsed.awips_id or "",
                issued_utc.isoformat(),
                ",".join(zones[:20]),
            )
            return False
        if not in_area_same:
            cancelled = self._cancel_overlapping(wfo=parsed.wfo, zones=zones)
            if cancelled:
                self._notify_changed()
            log.info(
                "NOW out-of-area: wfo=%s awips=%s zones=%s source=%s cancelled_prior=%d",
                parsed.wfo,
                parsed.awips_id or "",
                ",".join(zones[:20]),
                ugc_source,
                cancelled,
            )
            return False

        script = self.host.formatters.now_script(raw_text, intro=cfg.intro)
        if not script:
            log.warning(
                "NOW suppressed: no safe narrative after .NOW marker wfo=%s awips=%s",
                parsed.wfo,
                parsed.awips_id or "",
            )
            return False

        expires_utc = ugc_expires_utc or (issued_utc + dt.timedelta(minutes=cfg.default_expire_minutes))
        expires_utc = expires_utc.astimezone(dt.timezone.utc).replace(microsecond=0)
        if expires_utc <= now_utc:
            cancelled = self._cancel_overlapping(wfo=parsed.wfo, zones=zones)
            if cancelled:
                self._notify_changed()
            log.info(
                "NOW stale on receipt: wfo=%s awips=%s expires=%s cancelled_prior=%d",
                parsed.wfo,
                parsed.awips_id or "",
                expires_utc.isoformat(),
                cancelled,
            )
            return False

        insert_id = self._scope_insert_id(
            wfo=parsed.wfo,
            awips_id=parsed.awips_id or "NOW",
            zones=zones,
        )
        content_hash = hashlib.sha256(script.encode("utf-8")).hexdigest()
        existing = repo.get_insert(insert_id)
        existing_meta = (existing or {}).get("meta") or {}
        existing_audio_raw = str((existing or {}).get("audio_path") or "").strip()
        existing_audio = Path(existing_audio_raw) if existing_audio_raw else None
        same_content = (
            existing is not None
            and existing_meta.get("content_sha256") == content_hash
            and existing_audio is not None
            and existing_audio.exists()
        )

        cancelled = self._cancel_overlapping(
            wfo=parsed.wfo,
            zones=zones,
            keep_id=insert_id,
        )

        if same_content:
            assert existing_audio is not None
            audio_path = existing_audio
            duration = float(existing.get("duration_seconds") or 0.0)
        else:
            audio_path = Path(self.host.cfg.paths.audio_dir) / f"insert_{insert_id}.wav"
            duration = await _render_now_segment(
                getattr(self.host, "synthesizer", getattr(self.host, "tts", None)),
                script,
                audio_path,
                sample_rate=int(self.host.cfg.audio.sample_rate),
            )

        now_iso = self._utc_iso(now_utc)
        issued_iso = self._utc_iso(issued_utc)
        expires_iso = self._utc_iso(expires_utc)
        incoming_source = "api.weather.gov" if source == "api-backfill" else "NWWS-OI"
        retained_source = str(existing_meta.get("source") or "")
        record_source = retained_source if same_content and retained_source == "NWWS-OI" else incoming_source
        record_product_id = str(product_id or existing_meta.get("api_product_id") or "")
        record_actor = (
            str((existing or {}).get("actor") or f"nwws:{parsed.wfo}")
            if same_content and retained_source == "NWWS-OI"
            else f"{source}:{parsed.wfo}"
        )
        record = {
            "insert_id": insert_id,
            # cycle_inserts.kind is intentionally constrained to the public API
            # values "text" and "audio".  NOW provenance lives in meta.
            "kind": "text",
            "title": "Short-Term Forecast.",
            "text": script,
            "audio_path": str(audio_path),
            "audio_asset_id": None,
            "placement": "after_status",
            "start_after": now_iso,
            "expires_at": expires_iso,
            "repeat_mode": "every_n_rotations",
            "repeat_every_rotations": 1,
            "max_airings": 1000000,
            "defer_during_active_alerts": False,
            "status": "active",
            "actor": record_actor,
            "created_at": str((existing or {}).get("created_at") or now_iso),
            "updated_at": now_iso,
            "last_aired_at": (existing or {}).get("last_aired_at") if same_content else None,
            "airing_count": int((existing or {}).get("airing_count") or 0) if same_content else 0,
            "last_aired_rotation": (existing or {}).get("last_aired_rotation") if same_content else None,
            "duration_seconds": duration,
            "meta": {
                "source": record_source,
                "source_type": "nws_now",
                "product_type": "NOW",
                "wfo": parsed.wfo,
                "awips_id": parsed.awips_id or "",
                "api_product_id": record_product_id,
                "ugc_source": ugc_source,
                "ugc_zones": list(zones),
                "same_locations": list(in_area_same),
                "issued_at": issued_iso,
                "content_sha256": content_hash,
            },
        }
        repo.upsert_insert(record)
        self._notify_changed()

        log.info(
            "NOW queued for routine cycle: source=%s product_id=%s id=%s "
            "wfo=%s awips=%s zones=%s same=%s expires=%s rendered=%s "
            "cancelled_prior=%d",
            source,
            product_id or "",
            insert_id,
            parsed.wfo,
            parsed.awips_id or "",
            ",".join(zones[:20]),
            ",".join(in_area_same[:20]),
            expires_iso,
            not same_content,
            cancelled,
        )
        return True
