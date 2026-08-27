"""Runtime glue for Public Information Statement cycle injection.

The PNS classifier lives in :mod:`seasonalweather.broadcast.pns`; this module
owns the operational pieces that used to sit in main.py: dedupe reservation,
AlertTracker insertion, and the small API backfill recovery loop.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from typing import Any

from ..alerts.active import ActiveAlert
from ..alerts.product import parse_product_text
from .formatters import PnsDecision, PnsStateMachine

log = logging.getLogger("seasonalweather.broadcast.pns_runtime")


class PnsRuntime:
    """Queue accepted PNS decisions into the cycle-only active-alert state."""

    def __init__(self, host: Any) -> None:
        self.host = host
        self.state: PnsStateMachine = host.formatters.pns_state_machine(host.cfg.pns, tz=host._tz)
        self._recent_log_keys: dict[str, tuple[float, int]] = {}
        self._log_repeat_window_s = 1800.0

    def evaluate(self, *args: Any, **kwargs: Any) -> PnsDecision:
        return self.state.evaluate(*args, **kwargs)

    def _should_log_repeatable(self, key: str, *, now: float | None = None) -> tuple[bool, int]:
        """Return whether an INFO line should be emitted for a repeatable PNS result.

        API backfill polls the same latest PNS products repeatedly. Suppressed and
        deduped decisions are expected to repeat until the upstream latest-product
        pointer advances, so keep the first occurrence visible and collapse
        subsequent repeats into DEBUG noise until the repeat window expires.
        """
        ts = time.monotonic() if now is None else now
        last = self._recent_log_keys.get(key)
        if last is None:
            self._recent_log_keys[key] = (ts, 0)
            return True, 0

        last_ts, suppressed = last
        if ts - last_ts >= self._log_repeat_window_s:
            self._recent_log_keys[key] = (ts, 0)
            return True, suppressed

        self._recent_log_keys[key] = (last_ts, suppressed + 1)
        return False, suppressed + 1

    async def queue_decision(self, decision: PnsDecision, *, wfo: str, awips_id: str, context: str) -> bool:
        """Queue an accepted PNS decision as cycle-only active-alert state."""
        host = self.host
        if not decision.is_audio:
            repeat_key = ":".join(
                [
                    "suppressed",
                    context,
                    wfo,
                    awips_id or "",
                    decision.action,
                    decision.subtype,
                    decision.reason,
                    ",".join(decision.signals),
                ]
            )
            should_log, repeats = self._should_log_repeatable(repeat_key)
            log_method = log.info if should_log else log.debug
            log_method(
                "PNS audio suppressed; source=%s wfo=%s awips=%s action=%s subtype=%s reason=%s signals=%s%s",
                context,
                wfo,
                awips_id or "",
                decision.action,
                decision.subtype,
                decision.reason,
                ",".join(decision.signals) or "-",
                f" suppressed_repeats={repeats}" if should_log and repeats else "",
            )
            return False

        ok_pns, _ = await host._dedupe_reserve([decision.key])
        if not ok_pns:
            repeat_key = ":".join(["dedupe", context, decision.key, decision.subtype, wfo])
            should_log, repeats = self._should_log_repeatable(repeat_key)
            log_method = log.info if should_log else log.debug
            log_method(
                "PNS skipped (dedupe); source=%s id=%s subtype=%s wfo=%s%s",
                context,
                decision.key,
                decision.subtype,
                wfo,
                f" suppressed_repeats={repeats}" if should_log and repeats else "",
            )
            return False

        pns_exp_utc = decision.expires_utc or (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=4))
        pns_issued_utc = decision.issued_utc or dt.datetime.now(dt.timezone.utc)
        pns_ae = ActiveAlert(
            id=decision.key,
            source="PNS_CYCLE",
            event=decision.event,
            code=decision.code,
            vtec=[],
            headline=decision.headline or decision.event,
            script_text=decision.script_text,
            audio_path=None,
            expires=pns_exp_utc.isoformat(),
            issued=pns_issued_utc.isoformat(),
            same_locs=list(host.cfg.service_area.same_fips_all or []),
            cycle_only=True,
        )
        host.alert_tracker.add_or_update(pns_ae)
        host._schedule_cycle_refill(f"pns-{decision.subtype}")
        log.info(
            "PNS queued for cycle; source=%s id=%s subtype=%s event=%s wfo=%s awips=%s expires=%s signals=%s",
            context,
            decision.key,
            decision.subtype,
            decision.event,
            wfo,
            awips_id or "",
            pns_exp_utc.isoformat(),
            ",".join(decision.signals) or "-",
        )
        return True

    def backfill_wfos(self) -> list[str]:
        """Return 3-letter offices to poll for latest PNS backfill."""
        offices: set[str] = set()
        for raw in self.host._nwws_allowed_wfos:
            s = str(raw or "").strip().upper()
            if len(s) == 4 and s.startswith("K"):
                offices.add(s[1:])
            elif len(s) == 3:
                offices.add(s)
        return sorted(offices)

    async def backfill_latest_once(self) -> int:
        """Fetch latest API PNS products and queue any still-current audio-worthy PNS."""
        host = self.host
        if not getattr(host.cfg.pns, "enabled", True):
            return 0

        queued = 0
        offices = self.backfill_wfos()
        if not offices:
            log.debug("PNS API backfill skipped; no nwws.allowed_wfos configured")
            return 0

        for office in offices:
            try:
                pid = await host.api.latest_product_id("PNS", office)
                if not pid:
                    continue
                prod = await host.api.get_product(pid)
                if not prod or not prod.product_text:
                    continue

                parsed = parse_product_text(prod.product_text)
                wfo = (getattr(parsed, "wfo", None) or f"K{office}").strip().upper() if parsed else f"K{office}"
                awips_id = (getattr(parsed, "awips_id", None) or f"PNS{office}").strip().upper() if parsed else f"PNS{office}"

                decision = self.evaluate(
                    prod.product_text,
                    wfo=wfo,
                    awips_id=awips_id,
                    issued=getattr(prod, "issuance_time", None),
                )
                if await self.queue_decision(decision, wfo=wfo, awips_id=awips_id, context=f"api-backfill:{pid}"):
                    queued += 1
            except Exception:
                log.exception("PNS API backfill failed for office=%s", office)
        return queued

    async def run_backfill_loop(self) -> None:
        """Small recovery poller for missed NWWS-OI PNS products."""
        await asyncio.sleep(15)
        while True:
            queued = await self.backfill_latest_once()
            if queued:
                log.info("PNS API backfill queued %d product(s)", queued)
            await asyncio.sleep(120)
