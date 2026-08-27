from __future__ import annotations

import datetime as dt
import logging
from types import SimpleNamespace

from ..alerts.active import ActiveAlert, _vtec_track_id
from ..alerts.vtec import VTEC_PARSE_RE as _VTEC_PARSE_RE, toneout_policy as _vtec_toneout_policy
from .cap_policy import (
    alert_expires_from_cap,
    best_expiry_from_vtec,
    alert_tracker_id_for_cap,
    cap_event_to_same_code,
    cap_should_full,
    cap_should_update,
    cap_should_voice,
    cap_vtec_list,
)
from .formatters import FormatterSubsystem
from .station_feed_runtime import (
    cap_reference_ids as _sf_cap_reference_ids,
    note_cap as _station_feed_note_cap,
    remove_ids as _sf_remove_ids,
)

log = logging.getLogger("seasonalweather")


def _discord_audit(discord, method: str, **kwargs) -> None:
    """Best-effort optional Discord audit call; never affects CAP handling."""
    try:
        fn = getattr(discord, method, None)
        if fn is not None:
            fn(**kwargs)
    except Exception:
        log.debug("Discord audit hook failed method=%s", method, exc_info=True)


class CapRuntime:
    """Consumes NWS CAP alerts and handles CAP full/voice/update airing."""

    def __init__(self, orchestrator, formatters: FormatterSubsystem) -> None:
        self.orchestrator = orchestrator
        self.formatters = formatters

    async def run(self) -> None:
        o = self.orchestrator
        while True:
            ev = await o.cap_queue.get()

            vtec = cap_vtec_list(ev)
            tracks = o._vtec_tracks(vtec)
            cap_mt = str(getattr(ev, "message_type", None) or "").strip().lower()
            cap_ref_ids = _sf_cap_reference_ids(ev)

            if cap_mt == "cancel" and not tracks:
                try:
                    same_code = cap_event_to_same_code((ev.event or "").strip())
                    same_locs = o.target_resolver._filter_same_locations_to_service_area(
                        list(getattr(ev, "same_fips", None) or [])
                    )
                    o.alert_tracker.remove(alert_tracker_id_for_cap(ev, same_code))
                    o._remove_matching_ipaws_state(
                        code=same_code,
                        same_locs=same_locs,
                        reason=f"cap-cancel:{getattr(ev, 'alert_id', '')}",
                    )
                    o._remove_shadowed_ern_state(
                        code=same_code,
                        same_locs=same_locs,
                        reason=f"cap-cancel:{getattr(ev, 'alert_id', '')}",
                    )
                except Exception:
                    log.exception("AlertTracker: failed handling CAP cancel without VTEC id=%s", getattr(ev, "alert_id", None))
                _sf_remove_ids(cap_ref_ids + [getattr(ev, "alert_id", None)])
                log.info("CAP cancel: evicted state without airing id=%s refs=%s", getattr(ev, "alert_id", None), ",".join(cap_ref_ids[:4]))
                _discord_audit(
                    o.discord,
                    "station_feed_update",
                    action="remove",
                    source="CAP",
                    event=(ev.event or "").strip(),
                    code=cap_event_to_same_code((ev.event or "").strip()),
                    alert_id=str(getattr(ev, "alert_id", "") or ""),
                    details={"refs": cap_ref_ids[:4], "reason": "cap_cancel_no_vtec"},
                )
                continue

            log.info(
                "CAP match: event=%s severity=%s urgency=%s certainty=%s status=%s msgType=%s sent=%s same=%s headline=%s id=%s vtec=%s tracks=%s",
                ev.event,
                ev.severity,
                ev.urgency,
                ev.certainty,
                ev.status,
                ev.message_type,
                ev.sent,
                ",".join(ev.same_fips[:12]) + ("..." if len(ev.same_fips) > 12 else ""),
                ev.headline,
                ev.alert_id,
                ",".join(vtec[:2]) if vtec else "",
                ",".join(t for (t, _a) in tracks[:2]) if tracks else "",
            )

            try:
                sev = str(ev.severity or "").strip().lower()
                if sev in {"severe", "extreme"}:
                    o.cap_last_severe_at = dt.datetime.now(tz=o._tz)
            except Exception:
                pass

            if o.cfg.cap.dryrun:
                _discord_audit(
                    o.discord,
                    "alert_decision",
                    source="CAP",
                    result="skip",
                    reason="dryrun",
                    event=(ev.event or "").strip(),
                    code=cap_event_to_same_code((ev.event or "").strip()),
                    same_targets=len(getattr(ev, "same_fips", None) or []),
                    vtec=vtec[:2],
                    details={"alert_id": ev.alert_id},
                )
                continue

            if cap_should_full(o.cfg, ev):
                await self.air_full(ev)
                continue

            # CON/EXT/CAN/EXP for watched/warned events → voice-only update narration
            if cap_should_update(o.cfg, ev, o._vtec_tracks):
                await self.air_update(ev)
                continue

            if cap_should_voice(o.cfg, ev):
                await self.air_voice(ev)

    async def air_full(self, ev: "CapAlertEvent") -> None:  # type: ignore[name-defined]
        o = self.orchestrator
        now = dt.datetime.now(tz=o._tz)

        key = (str(ev.alert_id or "").strip(), str(ev.sent or "").strip())
        last = o._cap_full_last_by_key.get(key)
        if last and (now - last).total_seconds() < o.cfg.cap.full.cooldown_seconds:
            log.info("CAP full: cooldown active; skipping id=%s sent=%s event=%s", ev.alert_id, ev.sent, ev.event)
            _discord_audit(
                o.discord,
                "dedupe_event",
                source="CAP",
                result="cooldown",
                key=f"CAPFULL:{key[0]}:{key[1]}",
                event=(ev.event or "").strip(),
                code=cap_event_to_same_code((ev.event or "").strip()),
                mode="full",
                ttl_s=max(0.0, o.cfg.cap.full.cooldown_seconds - (now - last).total_seconds()),
            )
            return

        vtec = cap_vtec_list(ev)
        tracks = o._vtec_tracks(vtec)
        vtec_actions = {act for (_t, act) in tracks} if tracks else set()

        ev_event = (ev.event or "").strip()
        _WATCH_EVENTS = {"Tornado Watch", "Severe Thunderstorm Watch"}
        is_watch = ev_event in _WATCH_EVENTS

        # ---- Determine watch number and kind for TOA/SVA ----
        watch_number: int | None = None
        watch_kind = "tornado"
        if is_watch:
            for v in vtec:
                m = _VTEC_PARSE_RE.search(v)
                if not m:
                    continue
                phen = (m.group("phen") or "").upper()
                sig = (m.group("sig") or "").upper()
                if sig != "A":
                    continue
                if phen == "TO":
                    watch_kind = "tornado"
                elif phen == "SV":
                    watch_kind = "severe"
                else:
                    continue
                try:
                    watch_number = int(m.group("etn"))
                except Exception:
                    pass
                break

        # ---- Route to appropriate script builder ----
        if is_watch:
            if vtec_actions & {"EXA", "EXB"}:
                # Watch expansion: full announcement with SAME for added counties
                script = self.formatters.cap_watch_expansion(ev)
            else:
                # NEW or UPG watch
                script = self.formatters.cap_watch(ev)
            if not script.strip():
                script = self.formatters.cap_full(ev)
        else:
            script = self.formatters.cap_full(ev)

        if not script.strip():
            return

        same_code = _vtec_toneout_policy(vtec).same_code or cap_event_to_same_code(ev_event)
        same_locs_raw = list(ev.same_fips) if getattr(ev, "same_fips", None) else []
        same_locs = o.target_resolver._filter_same_locations_to_service_area(same_locs_raw)

        keys: list[str] = []

        # Track/action-level dedupe prevents CAP vs NWWS double-air for the
        # same lifecycle action while allowing later EXA/EXB updates on the
        # same VTEC track to air when policy says they are FULL-worthy.
        for track_id, act in tracks:
            keys.append(f"TRACKFULL:{track_id}:{act or 'UNK'}")

        # Also keep raw VTEC strings (fine-grain)
        for v in vtec:
            keys.append(f"VTEC:{v}")

        # Functional FULL dedupe is only safe when we do NOT have a concrete
        # VTEC track.  Otherwise, two distinct warnings for the same counties can
        # collide (for example TO.W.0004 vs TO.W.0005).
        if not tracks:
            fkey = o._dedupe_func_full_key(same_code, same_locs)
            if fkey:
                keys.append(fkey)

        fips_part = ",".join(sorted(set(str(x).strip() for x in (same_locs or []) if str(x).strip())))[:800]
        keys.append(f"CAPFULL:{(ev.event or '').strip()}:{(ev.sent or '').strip()}:{o._sha1_12((ev.alert_id or '') + '|' + fips_part)}")

        ok, hit = await o._dedupe_reserve(keys)
        if not ok:
            log.info(
                "CAP full skipped (dedupe hit=%s) id=%s sent=%s event=%s vtec=%s",
                hit,
                ev.alert_id,
                ev.sent,
                ev.event,
                ",".join(vtec[:2]) if vtec else "",
            )
            return

        try:
            dummy = SimpleNamespace(product_type=same_code, awips_id=None, wfo="CAP", raw_text="")

            event_label = (ev.event or "").strip() or "Weather alert"
            title = o._np_alert_title("cap_full", event=event_label)
            meta = o._np_meta(
                title=title,
                kind="alert",
                extra={
                    "sw_alert_source": "cap",
                    "sw_alert_mode": "full",
                    "sw_event": event_label,
                    "sw_event_code": (same_code or "").strip().upper(),
                    "sw_alert_id": str(ev.alert_id or "").strip(),
                },
            )
            async def _render_cap_full():
                return await o.audio_originator.render_alert_audio(
                    dummy,
                    script,
                    same_locations=same_locs if same_locs else None,
                )

            out_wav = await o._render_and_push_interrupt_audio(
                source="cap-full",
                full=True,
                render=_render_cap_full,
                meta=meta,
            )

            o._cap_full_last_by_key[key] = now
            o.last_product_desc = f"CAP {ev.event}".strip()

            try:
                code_u = (same_code or "").strip().upper()
                if code_u and code_u in o.cfg.policy.toneout_product_types:
                    o.last_toneout_at = now
                    o.last_heightened_at = now
                    o.heightened_until = now + dt.timedelta(seconds=o.cfg.cycle.min_heightened_seconds)
                    o._update_mode()
            except Exception:
                pass

            o._schedule_cycle_refill("post-cap-full")

            log.info("CAP ACTION: aired FULL event=%s code=%s id=%s sent=%s vtec=%s audio=%s", ev.event, same_code, ev.alert_id, ev.sent, ",".join(vtec[:2]) if vtec else "", out_wav)
            _discord_audit(
                o.discord,
                "alert_decision",
                source="CAP",
                result="air",
                reason="cap_full",
                event=(ev.event or "").strip(),
                code=same_code,
                mode="full",
                same_targets=len(same_locs),
                vtec=vtec[:2],
                details={
                    "alert_id": ev.alert_id,
                    "same_raw": len(same_locs_raw),
                    "tracks": [t for (t, _a) in tracks[:4]],
                },
            )
            # _CAP_FULL_DL_
            o.discord.alert_aired(
                code=same_code,
                event=(ev.event or "").strip(),
                source="CAP",
                mode="full",
                area=getattr(ev, "area_desc", "") or "",
                vtec=vtec[:2],
                expires=self.formatters.cap_local_expiry(
                    str(getattr(ev, "expires", "") or "")
                ),
            )
            _station_feed_note_cap(ev, mode="FULL", same_locations=(same_locs if same_locs else same_locs_raw), out_wav=str(out_wav), same_code=same_code, vtec=vtec)
            _discord_audit(
                o.discord,
                "station_feed_update",
                action="upsert",
                source="CAP",
                event=(ev.event or "").strip(),
                code=same_code,
                alert_id=str(ev.alert_id or ""),
                details={"mode": "FULL", "same_targets": len(same_locs if same_locs else same_locs_raw)},
            )

            # ---- Register to AlertTracker for active cycle rotation ----
            try:
                tracker_id = alert_tracker_id_for_cap(ev, same_code)
                expires_iso = alert_expires_from_cap(ev, vtec, best_expiry_from_vtec=best_expiry_from_vtec)
                _is_watch = (ev.event or "").strip() in {"Tornado Watch", "Severe Thunderstorm Watch"}
                _watch_num: int | None = None
                if _is_watch:
                    for _v in vtec:
                        _m = _VTEC_PARSE_RE.search(_v)
                        if _m and (_m.group("sig") or "").upper() == "A":
                            try:
                                _watch_num = int(_m.group("etn"))
                            except Exception:
                                pass
                            break
                alert_entry = ActiveAlert(
                    id=tracker_id,
                    source="CAP",
                    event=str(ev.event or ""),
                    code=same_code,
                    vtec=vtec,
                    headline=str(ev.headline or ""),
                    script_text=script,
                    audio_path=str(out_wav),
                    expires=expires_iso,
                    issued=str(ev.sent or dt.datetime.now(dt.timezone.utc).isoformat()),
                    same_locs=same_locs,
                    cycle_only=False,
                    watch_number=_watch_num,
                )
                o.alert_tracker.add_or_update(alert_entry)
                o.alert_tracker.mark_aired(tracker_id)
                o._remove_shadowed_ern_state(
                    code=same_code,
                    same_locs=same_locs,
                    reason=f"cap-full:{tracker_id}",
                )
                log.info("AlertTracker: registered CAP FULL id=%s event=%s expires=%s", tracker_id, ev.event, expires_iso)
            except Exception:
                log.exception("AlertTracker: failed to register CAP FULL event=%s", ev.event)
        except Exception:
            await o._dedupe_release(keys)
            raise

    async def air_voice(self, ev: "CapAlertEvent") -> None:  # type: ignore[name-defined]
        o = self.orchestrator
        now = dt.datetime.now(tz=o._tz)

        key = (str(ev.alert_id or "").strip(), str(ev.sent or "").strip())
        last = o._cap_voice_last_by_key.get(key)
        if last and (now - last).total_seconds() < o.cfg.cap.voice.cooldown_seconds:
            log.info("CAP voice: cooldown active; skipping id=%s sent=%s event=%s", ev.alert_id, ev.sent, ev.event)
            _discord_audit(
                o.discord,
                "dedupe_event",
                source="CAP",
                result="cooldown",
                key=f"CAPVOICE:{key[0]}:{key[1]}",
                event=(ev.event or "").strip(),
                code=cap_event_to_same_code((ev.event or "").strip()),
                mode="voice",
                ttl_s=max(0.0, o.cfg.cap.voice.cooldown_seconds - (now - last).total_seconds()),
            )
            return

        script = self.formatters.cap_voice(ev)
        if not script.strip():
            return

        vtec = cap_vtec_list(ev)
        tracks = o._vtec_tracks(vtec)

        same_code = _vtec_toneout_policy(vtec).same_code or cap_event_to_same_code((ev.event or "").strip())
        same_locs_raw = list(ev.same_fips) if getattr(ev, "same_fips", None) else []
        same_locs = o.target_resolver._filter_same_locations_to_service_area(same_locs_raw)

        keys: list[str] = []
        for track_id, _act in tracks:
            keys.append(f"TRACKVOICE:{track_id}")

        fips_part = ",".join(sorted(set(str(x).strip() for x in (ev.same_fips or []) if str(x).strip())))[:800]
        keys.append(f"CAPVOICE:{(ev.event or '').strip()}:{(ev.sent or '').strip()}:{o._sha1_12((ev.alert_id or '') + '|' + fips_part)}")

        ok, hit = await o._dedupe_reserve(keys)
        if not ok:
            log.info(
                "CAP voice skipped (dedupe hit=%s) id=%s sent=%s event=%s vtec=%s",
                hit,
                ev.alert_id,
                ev.sent,
                ev.event,
                ",".join(vtec[:2]) if vtec else "",
            )
            _discord_audit(
                o.discord,
                "dedupe_event",
                source="CAP",
                result="hit",
                key=hit,
                event=(ev.event or "").strip(),
                code=same_code,
                mode="voice",
            )
            return

        try:
            event_label = (ev.event or "").strip() or "Weather alert"
            title = o._np_alert_title("cap_update", event=event_label)
            meta = o._np_meta(
                title=title,
                kind="alert",
                extra={
                    "sw_alert_source": "cap",
                    "sw_alert_mode": "voice",
                    "sw_event": event_label,
                    "sw_event_code": (same_code or "").strip().upper(),
                    "sw_alert_id": str(ev.alert_id or "").strip(),
                },
            )
            async def _render_cap_voice():
                return await o.audio_originator.render_voice_only_audio(script, prefix="capvoice")

            out_wav = await o._render_and_push_interrupt_audio(
                source="cap-voice",
                full=False,
                render=_render_cap_voice,
                meta=meta,
            )

            o._cap_voice_last_by_key[key] = now
            o.last_product_desc = f"CAP {ev.event}".strip()

            o._schedule_cycle_refill("post-cap-voice")

            log.info("CAP ACTION: aired voice-only event=%s id=%s sent=%s audio=%s", ev.event, ev.alert_id, ev.sent, out_wav)
            _discord_audit(
                o.discord,
                "alert_decision",
                source="CAP",
                result="air",
                reason="cap_voice",
                event=(ev.event or "").strip(),
                code=same_code,
                mode="voice",
                same_targets=len(same_locs),
                vtec=vtec[:2],
                details={"alert_id": ev.alert_id, "same_raw": len(same_locs_raw)},
            )
            # _CAP_VOICE_DL_
            o.discord.alert_aired(
                code=same_code,
                event=(ev.event or "").strip(),
                source="CAP",
                mode="voice",
                area=getattr(ev, "area_desc", "") or "",
                vtec=vtec[:2],
            )

            # Register to AlertTracker (cycle_only → no SAME retone on cycle replay)
            try:
                vtec_v = cap_vtec_list(ev)
                tracker_id_v = alert_tracker_id_for_cap(ev, same_code)
                expires_iso_v = alert_expires_from_cap(ev, vtec_v, best_expiry_from_vtec=best_expiry_from_vtec)
                _ae = ActiveAlert(
                    id=tracker_id_v,
                    source="CAP",
                    event=str(ev.event or ""),
                    code=same_code,
                    vtec=vtec_v,
                    headline=str(ev.headline or ""),
                    script_text=script,
                    audio_path=str(out_wav),
                    expires=expires_iso_v,
                    issued=str(ev.sent or dt.datetime.now(dt.timezone.utc).isoformat()),
                    same_locs=same_locs,
                    cycle_only=True,
                )
                o.alert_tracker.add_or_update(_ae)
                o.alert_tracker.mark_aired(tracker_id_v)
                o._remove_shadowed_ern_state(
                    code=same_code,
                    same_locs=same_locs,
                    reason=f"cap-voice:{tracker_id_v}",
                )
            except Exception:
                log.exception("AlertTracker: failed to register CAP VOICE event=%s", ev.event)
            _station_feed_note_cap(
                ev,
                mode="VOICE",
                same_locations=(same_locs if same_locs else same_locs_raw),
                out_wav=str(out_wav),
                same_code=same_code,
                vtec=vtec,
            )
            _discord_audit(
                o.discord,
                "station_feed_update",
                action="upsert",
                source="CAP",
                event=(ev.event or "").strip(),
                code=same_code,
                alert_id=str(ev.alert_id or ""),
                details={"mode": "VOICE", "same_targets": len(same_locs if same_locs else same_locs_raw)},
            )
        except Exception:
            await o._dedupe_release(keys)
            raise


    async def air_update(self, ev: "CapAlertEvent") -> None:  # type: ignore[name-defined]
        o = self.orchestrator
        """
        Voice-only narration for VTEC CON/EXT/CAN/EXP on already-aired events.
        No SAME tones.  Removes entry from AlertTracker on CAN/EXP.
        """
        now = dt.datetime.now(tz=o._tz)
        vtec = cap_vtec_list(ev)
        tracks = o._vtec_tracks(vtec)
        vtec_actions = {act for (_t, act) in tracks} if tracks else set()

        ev_event = (ev.event or "").strip()
        _WATCH_EVENTS = {"Tornado Watch", "Severe Thunderstorm Watch"}
        is_watch = ev_event in _WATCH_EVENTS

        # Determine watch number/kind for watches
        watch_number: int | None = None
        watch_kind = "tornado"
        if is_watch:
            for v in vtec:
                m = _VTEC_PARSE_RE.search(v)
                if not m:
                    continue
                phen = (m.group("phen") or "").upper()
                sig = (m.group("sig") or "").upper()
                if sig != "A":
                    continue
                watch_kind = "tornado" if phen == "TO" else "severe"
                try:
                    watch_number = int(m.group("etn"))
                except Exception:
                    pass
                break

        if is_watch:
            script = self.formatters.cap_watch_action(ev, vtec_actions, tracks, watch_number, watch_kind)
        elif self.formatters.cap_prefers_statement_update(ev_event, vtec_actions):
            script = self.formatters.cap_statement_action(ev, vtec_actions, tracks)
        else:
            script = self.formatters.cap_warning_action(ev, vtec_actions, tracks)

        if not script.strip():
            log.info("CAP update: empty script, skipping event=%s vtec_actions=%s", ev_event, vtec_actions)
            _discord_audit(
                o.discord,
                "alert_decision",
                source="CAP",
                result="skip",
                reason="empty_update_script",
                event=ev_event,
                code=cap_event_to_same_code(ev_event),
                mode="voice",
                vtec=vtec[:2],
                details={"actions": sorted(vtec_actions), "alert_id": ev.alert_id},
            )
            return

        same_code = cap_event_to_same_code(ev_event)
        same_locs_raw = list(ev.same_fips) if getattr(ev, "same_fips", None) else []
        same_locs = o.target_resolver._filter_same_locations_to_service_area(same_locs_raw)

        key_str = f"CAPUPDATE:{(ev.alert_id or '').strip()}:{(ev.sent or '').strip()}"
        keys = [key_str]
        for track_id, act in tracks:
            keys.append(f"TRACKVOICE:{track_id}:{act or 'UNK'}")

        ok, hit = await o._dedupe_reserve(keys)
        if not ok:
            log.info("CAP update skipped (dedupe hit=%s) event=%s vtec_actions=%s", hit, ev_event, vtec_actions)
            _discord_audit(
                o.discord,
                "dedupe_event",
                source="CAP",
                result="hit",
                key=hit,
                event=ev_event,
                code=same_code,
                mode="voice",
            )
            return

        try:
            event_label = ev_event or "Weather alert"
            title = o._np_alert_title("cap_update", event=event_label)
            meta = o._np_meta(
                title=title,
                kind="alert",
                extra={
                    "sw_alert_source": "cap",
                    "sw_alert_mode": "update",
                    "sw_event": event_label,
                    "sw_event_code": (same_code or "").strip().upper(),
                    "sw_alert_id": str(ev.alert_id or "").strip(),
                },
            )
            async def _render_cap_update():
                return await o.audio_originator.render_voice_only_audio(script, prefix="capupdate")

            out_wav = await o._render_and_push_interrupt_audio(
                source="cap-update",
                full=False,
                render=_render_cap_update,
                meta=meta,
            )

            o.last_product_desc = f"CAP {ev_event}".strip()
            o._schedule_cycle_refill("post-cap-update")

            # Update or remove from AlertTracker
            try:
                tracker_id = alert_tracker_id_for_cap(ev, same_code)
                if vtec_actions & {"CAN", "EXP"}:
                    removed = o.alert_tracker.remove(tracker_id)
                    if not removed:
                        # Try by VTEC track
                        track_ids = {_vtec_track_id(v) for v in vtec if _vtec_track_id(v)}
                        o.alert_tracker.remove_by_vtec_tracks(
                            track_ids,  # type: ignore[arg-type]
                            reason=f"cap-update:{','.join(sorted(vtec_actions))}",
                        )
                    o._remove_matching_ipaws_state(
                        code=same_code,
                        same_locs=same_locs,
                        reason=f"cap-update:{','.join(sorted(vtec_actions))}",
                    )
                    o._remove_shadowed_ern_state(
                        code=same_code,
                        same_locs=same_locs,
                        reason=f"cap-update:{','.join(sorted(vtec_actions))}",
                    )
                    log.info("AlertTracker: removed id=%s event=%s action=%s", tracker_id, ev_event, vtec_actions)
                else:
                    # CON/EXT/EXA: update the stored script to latest narration
                    existing = o.alert_tracker.find_by_vtec_track(tracker_id.replace("CAP:", "")) or o.alert_tracker._alerts.get(tracker_id)
                    if existing:
                        o.alert_tracker.update_script(existing.id, script)
                        o.alert_tracker.mark_aired(existing.id)
                    log.info("AlertTracker: updated id=%s event=%s action=%s", tracker_id, ev_event, vtec_actions)
            except Exception:
                log.exception("AlertTracker: failed to update/remove on CAP update event=%s", ev_event)

            log.info("CAP ACTION: aired UPDATE event=%s code=%s id=%s vtec_actions=%s audio=%s",
                     ev_event, same_code, ev.alert_id, vtec_actions, out_wav)
            _discord_audit(
                o.discord,
                "alert_decision",
                source="CAP",
                result="air",
                reason="cap_update",
                event=ev_event,
                code=same_code,
                mode="voice",
                same_targets=len(same_locs),
                vtec=vtec[:2],
                details={"actions": sorted(vtec_actions), "alert_id": ev.alert_id},
            )
            # _CAP_UPDATE_DL_
            if vtec_actions & {"CAN", "EXP"}:
                o.discord.alert_expired(
                    code=same_code,
                    event=ev_event,
                    vtec_action=next(iter(vtec_actions & {"CAN", "EXP"})),
                    source="CAP",
                    area=getattr(ev, "area_desc", "") or "",
                    vtec=vtec[:2],
                )
            else:
                o.discord.alert_updated(
                    code=same_code,
                    event=ev_event,
                    vtec_action=next(iter(vtec_actions), "CON"),
                    source="CAP",
                    area=getattr(ev, "area_desc", "") or "",
                    vtec=vtec[:2],
                )
            _station_feed_note_cap(ev, mode="VOICE", same_locations=(same_locs if same_locs else same_locs_raw),
                                   out_wav=str(out_wav), same_code=same_code, vtec=vtec)
            _discord_audit(
                o.discord,
                "station_feed_update",
                action="upsert",
                source="CAP",
                event=ev_event,
                code=same_code,
                alert_id=str(ev.alert_id or ""),
                details={"mode": "VOICE", "actions": sorted(vtec_actions)},
            )
        except Exception:
            await o._dedupe_release(keys)
            raise
