from __future__ import annotations

import asyncio
import datetime as dt
import logging
from types import SimpleNamespace
from typing import Any

from ..alerts.active import ActiveAlert
from .formatters import FormatterSubsystem

log = logging.getLogger("seasonalweather")


class IpawsRuntime:
    """Runtime consumer and airing path for IPAWS CAP civil alerts.

    The host object is the orchestrator for now. This extraction keeps IPAWS
    dry-run, full/voice routing, dedupe, audio, active-alert, and Discord
    behavior unchanged while taking the source-specific runtime wall out of
    main.py.
    """

    def __init__(self, host: Any, formatters: FormatterSubsystem) -> None:
        self.host = host
        self.formatters = formatters
        self._full_last_by_key: dict[tuple[str, str], dt.datetime] = {}

    async def run(self) -> None:
        """
        Dispatch loop for IPAWS CAP events.

        Routing:
          event_code in ipaws.full_events  -> _air_full  (SAME tones)
          event_code in ipaws.voice_events -> _air_voice (no tones)
          anything else                    -> silently logged and dropped

        A dryrun=true config suppresses airing but logs what would have aired,
        which is the right mode for initial deployment.
        """
        host = self.host
        while True:
            ev = await host.ipaws_queue.get()

            event_code = (ev.event_code or "").strip().upper()
            event_label = (ev.event or event_code or "").strip()
            message_type = str(getattr(ev, "message_type", None) or "").strip().lower()

            log.info(
                "IPAWS event: code=%s event=%s id=%s sender=%s fips=%s",
                event_code,
                event_label,
                ev.identifier,
                ev.sender_name_clean or ev.sender_name_raw or "",
                ",".join(ev.same_fips[:6]) + ("..." if len(ev.same_fips) > 6 else ""),
            )

            if message_type == "cancel":
                same_locs = host.target_resolver._filter_same_locations_to_service_area(
                    list(getattr(ev, "same_fips", None) or [])
                )
                removed_direct = host.alert_tracker.remove(f"IPAWS:{(ev.identifier or '').strip()}")
                removed_matching = host._remove_matching_ipaws_state(
                    code=event_code,
                    same_locs=same_locs,
                    reason=f"ipaws-cancel:{ev.identifier}",
                )
                log.info(
                    "IPAWS cancel: removed active state direct=%s matching=%d code=%s id=%s",
                    removed_direct,
                    removed_matching,
                    event_code,
                    ev.identifier,
                )
                host._schedule_cycle_refill("post-ipaws-cancel")
                continue

            full_events = set(host.cfg.ipaws.full_events)
            voice_events = set(host.cfg.ipaws.voice_events)

            should_full = event_code in full_events
            should_voice = (not should_full) and (event_code in voice_events)

            if not should_full and not should_voice:
                log.debug(
                    "IPAWS: code=%s not in full_events or voice_events; dropping",
                    event_code,
                )
                continue

            if host.cfg.ipaws.dryrun:
                log.info(
                    "IPAWS DRYRUN: would have aired %s code=%s id=%s",
                    "FULL" if should_full else "VOICE",
                    event_code,
                    ev.identifier,
                )
                continue

            try:
                if should_full:
                    await self.air_full(ev)
                else:
                    await self.air_voice(ev)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "IPAWS: unhandled error airing code=%s id=%s",
                    event_code,
                    ev.identifier,
                )

    async def air_full(self, ev: Any) -> None:
        """
        Air an IPAWS civil alert with full SAME tones.

        Dedupe strategy (two keys):
          1. IPAWSFULL:{identifier}|{sent} -- source-unique, prevents double-air
             on repeated polls before the ledger clears.
          2. _dedupe_func_full_key(event_code, same_locs) -- cross-source
             functional key shared with ERN airing path, prevents WJON/CAP +
             SeasonalWeather IPAWS double-air of the same civil alert.
        """
        host = self.host
        now = dt.datetime.now(tz=host._tz)

        key = (str(ev.identifier or "").strip(), str(ev.sent or "").strip())
        last = self._full_last_by_key.get(key)
        if last and (now - last).total_seconds() < 180:
            log.info(
                "IPAWS full: cooldown active; skipping id=%s code=%s",
                ev.identifier,
                ev.event_code,
            )
            return

        event_code = (ev.event_code or "").strip().upper()
        event_label = (ev.event or event_code or "Alert").strip()

        script = self.formatters.ipaws_script(ev)
        if not script.strip():
            return

        same_locs_raw = list(ev.same_fips) if ev.same_fips else []
        same_locs = host.target_resolver._filter_same_locations_to_service_area(same_locs_raw)

        keys: list[str] = []

        # Source-unique key.
        id_part = host._sha1_12((ev.identifier or "") + "|" + (ev.sent or ""))
        keys.append(f"IPAWSFULL:{id_part}")

        # Cross-source functional key (shared with ERN path for this event+FIPS).
        fkey = host._dedupe_func_full_key(event_code, same_locs or same_locs_raw)
        if fkey:
            keys.append(fkey)

        ok, hit = await host._dedupe_reserve(keys)
        if not ok:
            log.info(
                "IPAWS full skipped (dedupe hit=%s) id=%s code=%s",
                hit,
                ev.identifier,
                event_code,
            )
            return

        try:
            dummy = SimpleNamespace(
                product_type=event_code, awips_id=None, wfo="IPAWS", raw_text=""
            )

            title = host._np_alert_title("cap_full", event=event_label)
            meta = host._np_meta(
                title=title,
                kind="alert",
                extra={
                    "sw_alert_source": "ipaws",
                    "sw_alert_mode": "full",
                    "sw_event": event_label,
                    "sw_event_code": event_code,
                    "sw_alert_id": str(ev.identifier or "").strip(),
                },
            )
            async def _render_ipaws_full():
                return await host.audio_originator.render_alert_audio(
                    dummy,
                    script,
                    same_locations=same_locs if same_locs else None,
                )

            out_wav = await host._render_and_push_interrupt_audio(
                source="ipaws-full",
                full=True,
                render=_render_ipaws_full,
                meta=meta,
            )

            self._full_last_by_key[key] = now
            host.last_product_desc = f"IPAWS {event_label}".strip()

            # Heightened mode if this code is a toneout type.
            try:
                if event_code and event_code in host.cfg.policy.toneout_product_types:
                    host.last_toneout_at = now
                    host.last_heightened_at = now
                    host.heightened_until = now + dt.timedelta(
                        seconds=host.cfg.cycle.min_heightened_seconds
                    )
                    host._update_mode()
            except Exception:
                pass

            host._schedule_cycle_refill("post-ipaws-full")

            # AlertTracker -- expire based on CAP expires field (no VTEC on civil alerts).
            try:
                tracker_id = f"IPAWS:{(ev.identifier or '').strip()}"
                expires_iso = (ev.expires or "").strip() or (
                    dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)
                ).isoformat()
                alert_entry = ActiveAlert(
                    id=tracker_id,
                    source="IPAWS",
                    event=event_label,
                    code=event_code,
                    vtec=[],
                    headline=str(ev.headline or event_label),
                    script_text=script,
                    audio_path=str(out_wav),
                    expires=expires_iso,
                    issued=str(ev.sent or dt.datetime.now(dt.timezone.utc).isoformat()),
                    same_locs=same_locs or same_locs_raw,
                    cycle_only=False,
                    watch_number=None,
                )
                host.alert_tracker.add_or_update(alert_entry)
                host.alert_tracker.mark_aired(tracker_id)
                host._remove_shadowed_ern_state(
                    code=event_code,
                    same_locs=same_locs or same_locs_raw,
                    reason=f"ipaws-full:{tracker_id}",
                )
                log.info(
                    "AlertTracker: registered IPAWS FULL id=%s code=%s expires=%s",
                    tracker_id,
                    event_code,
                    expires_iso,
                )
            except Exception:
                log.exception(
                    "AlertTracker: failed to register IPAWS FULL code=%s", event_code
                )

            log.info(
                "IPAWS ACTION: aired FULL code=%s event=%s id=%s sender=%s audio=%s",
                event_code,
                event_label,
                ev.identifier,
                ev.sender_name_clean or ev.sender_name_raw or "",
                out_wav,
            )
            host.discord.alert_aired(
                code=event_code,
                event=event_label,
                source="IPAWS",
                mode="full",
                area=",".join(same_locs or same_locs_raw)[:160],
                vtec=[],
                expires=self.formatters.cap_local_expiry(ev.expires or ""),
            )

        except Exception:
            await host._dedupe_release(keys)
            raise

    async def air_voice(self, ev: Any) -> None:
        """
        Air an IPAWS civil alert voice-only (no SAME tones).
        Used for event codes in ipaws.voice_events.
        """
        host = self.host
        event_code = (ev.event_code or "").strip().upper()
        event_label = (ev.event or event_code or "Alert").strip()

        script = self.formatters.ipaws_script(ev)
        if not script.strip():
            return

        same_locs_raw = list(ev.same_fips) if ev.same_fips else []
        same_locs = host.target_resolver._filter_same_locations_to_service_area(same_locs_raw)

        id_part = host._sha1_12((ev.identifier or "") + "|" + (ev.sent or ""))
        key_str = f"IPAWSVOICE:{id_part}"
        fkey = host._dedupe_func_full_key(event_code, same_locs or same_locs_raw)
        keys = [key_str] + ([fkey] if fkey else [])

        ok, hit = await host._dedupe_reserve(keys)
        if not ok:
            log.info(
                "IPAWS voice skipped (dedupe hit=%s) id=%s code=%s",
                hit,
                ev.identifier,
                event_code,
            )
            return

        try:
            title = host._np_alert_title("cap_full", event=event_label)
            meta = host._np_meta(
                title=title,
                kind="alert",
                extra={
                    "sw_alert_source": "ipaws",
                    "sw_alert_mode": "voice",
                    "sw_event": event_label,
                    "sw_event_code": event_code,
                    "sw_alert_id": str(ev.identifier or "").strip(),
                },
            )
            async def _render_ipaws_voice():
                return await host.audio_originator.render_voice_only_audio(script, prefix="ipawsvoice")

            out_wav = await host._render_and_push_interrupt_audio(
                source="ipaws-voice",
                full=False,
                render=_render_ipaws_voice,
                meta=meta,
            )

            host.last_product_desc = f"IPAWS {event_label}".strip()
            host._schedule_cycle_refill("post-ipaws-voice")

            try:
                tracker_id = f"IPAWS:{(ev.identifier or '').strip()}"
                expires_iso = (ev.expires or "").strip() or (
                    dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)
                ).isoformat()
                alert_entry = ActiveAlert(
                    id=tracker_id,
                    source="IPAWS",
                    event=event_label,
                    code=event_code,
                    vtec=[],
                    headline=str(ev.headline or event_label),
                    script_text=script,
                    audio_path=str(out_wav),
                    expires=expires_iso,
                    issued=str(ev.sent or dt.datetime.now(dt.timezone.utc).isoformat()),
                    same_locs=same_locs or same_locs_raw,
                    cycle_only=True,
                    watch_number=None,
                )
                host.alert_tracker.add_or_update(alert_entry)
                host.alert_tracker.mark_aired(tracker_id)
                host._remove_shadowed_ern_state(
                    code=event_code,
                    same_locs=same_locs or same_locs_raw,
                    reason=f"ipaws-voice:{tracker_id}",
                )
                log.info(
                    "AlertTracker: registered IPAWS VOICE id=%s code=%s expires=%s",
                    tracker_id,
                    event_code,
                    expires_iso,
                )
            except Exception:
                log.exception("AlertTracker: failed to register IPAWS VOICE code=%s", event_code)

            log.info(
                "IPAWS ACTION: aired VOICE code=%s event=%s id=%s audio=%s",
                event_code,
                event_label,
                ev.identifier,
                out_wav,
            )
            host.discord.alert_aired(
                code=event_code,
                event=event_label,
                source="IPAWS",
                mode="voice",
                area=",".join(same_locs or same_locs_raw)[:160],
                vtec=[],
                expires=self.formatters.cap_local_expiry(ev.expires or ""),
            )

        except Exception:
            await host._dedupe_release(keys)
            raise
