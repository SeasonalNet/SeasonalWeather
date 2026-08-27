from __future__ import annotations

import datetime as dt
import logging

from ..alerts.active import ActiveAlert, _vtec_track_id
from ..alerts.product import ParsedProduct, parse_product_text
from ..alerts.vtec import same_codes_for_vtec
from ..alerts.vtec import toneout_policy as _vtec_toneout_policy
from ..nwws.source import NwwsProductEnvelope
from .audio_origination import safe_event_code as _safe_event_code
from .cap_policy import best_expiry_from_vtec, vtec_matches_configured_toneout_code
from .formatters import NwwsScriptRenderResult, parse_nws_header_issued_dt, pns_text_same_issuance
from .formatters import FormatterSubsystem
from .station_feed_runtime import nwws_area_from_text as _sf_nwws_area_from_text
from .station_feed_runtime import nwws_best_issued_dt as _sf_nwws_best_issued_dt
from .station_feed_runtime import nwws_event_label as _sf_nwws_event_label
from .station_feed_runtime import nwws_extract_issuer as _sf_nwws_extract_issuer
from .station_feed_runtime import nwws_make_headline as _sf_nwws_make_headline
from .station_feed_runtime import note_nwws as _station_feed_note_nwws

log = logging.getLogger("seasonalweather")


def _first_vtec_action(actions: set[str], priority: tuple[str, ...]) -> str:
    """Return a deterministic VTEC action from a set using caller priority."""
    for action in priority:
        if action in actions:
            return action
    return ""


def _vtec_action(raw: str) -> str:
    """Return the action token from one normalized VTEC string."""
    parts = str(raw or "").strip("/").split(".")
    return parts[1].upper() if len(parts) > 1 and parts[0] == "O" else ""


def _nwws_discord_event_code(product_type: str, same_code: str | None, vtec: list[str]) -> str:
    """Return the event code Discord should present for NWWS carrier products."""
    policy_code = (same_code or "").strip().upper()
    if policy_code:
        return policy_code
    for code in same_codes_for_vtec(vtec):
        code_u = (code or "").strip().upper()
        if code_u:
            return code_u
    return _safe_event_code(product_type)


def _discord_audit(discord, method: str, **kwargs) -> None:
    """Best-effort optional Discord audit call; never affects alert handling."""
    try:
        fn = getattr(discord, method, None)
        if fn is not None:
            fn(**kwargs)
    except Exception:
        log.debug("Discord audit hook failed method=%s", method, exc_info=True)


class NwwsRuntime:
    """NWWS-OI product consumer and toneout runtime.

    This is intentionally a thin state-forwarding extraction from Orchestrator.
    The runtime owns source handling, while existing orchestrator state remains
    authoritative for dedupe, mode, audio, tracker, Discord, and station feed
    side effects.
    """

    def __init__(self, orchestrator, formatters: FormatterSubsystem) -> None:
        object.__setattr__(self, "_orchestrator", orchestrator)
        object.__setattr__(self, "_formatters", formatters)

    def __getattr__(self, name: str):
        return getattr(self._orchestrator, name)

    def __setattr__(self, name: str, value) -> None:
        if name == "_orchestrator":
            object.__setattr__(self, name, value)
            return
        setattr(self._orchestrator, name, value)

    async def run(self) -> None:
        while True:
            envelope = await self.nwws_queue.get()
            self.health_state.mark_success("nwws_oi")

            # Flood-gate: allow the client logs for the first N messages, then silence them.
            self._nwws_raw_seen += 1
            if self._nwws_rx_log_first_n > 0 and self._nwws_raw_seen == self._nwws_rx_log_first_n:
                self._nwws_logger.setLevel(logging.WARNING)
                log.info(
                    "NWWS RX logging throttled after %d messages (seasonalweather.nwws -> WARNING)",
                    self._nwws_rx_log_first_n,
                )

            raw = envelope.raw_text if isinstance(envelope, NwwsProductEnvelope) else str(envelope)
            parsed = parse_product_text(raw)
            if not parsed:
                continue

            self._nwws_seen += 1

            allowed_wfo = (not self._nwws_allowed_wfos) or (parsed.wfo in self._nwws_allowed_wfos)
            toneout = parsed.product_type in self.cfg.policy.toneout_product_types
            raw_vtec = self._extract_vtec(parsed.raw_text or "")
            vtec_lifecycle = (not toneout) and vtec_matches_configured_toneout_code(self.cfg, raw_vtec)

            first_n = max(0, int(self._nwws_decision_log_first_n))
            every = max(0, int(self._nwws_decision_log_every))
            if (first_n and self._nwws_seen <= first_n) or (every and (self._nwws_seen % every) == 0):
                log.info(
                    "NWWS decision: #%d type=%s awips=%s wfo=%s allowed=%s toneout=%s vtec_lifecycle=%s",
                    self._nwws_seen,
                    parsed.product_type,
                    parsed.awips_id or "",
                    parsed.wfo,
                    allowed_wfo,
                    toneout,
                    vtec_lifecycle,
                )

            self.last_product_desc = f"{parsed.product_type} ({parsed.awips_id or ''})"

            # Trigger out-of-band segment refreshes for products whose content
            # feeds specific cycle segments so listeners hear fresh data sooner.
            try:
                _pt = (parsed.product_type or "").strip().upper()
                if _pt == "HWO":
                    self.refresher.trigger_immediate("hwo")
                elif _pt in {"RWS", "AFD", "SYN"}:
                    self.refresher.trigger_immediate("zfp")
                elif _pt == "RWR":
                    self.refresher.trigger_immediate("obs")
                    self.refresher.trigger_immediate("marine_obs")
                elif _pt in {"CWF", "CWA"}:
                    self.refresher.trigger_immediate("cwf")
                elif _pt in {"SWO", "SWS"}:
                    self.refresher.trigger_immediate("spc")
            except AttributeError:
                pass  # refresher not yet initialised

            if not allowed_wfo:
                continue

            if toneout or vtec_lifecycle:
                if vtec_lifecycle:
                    log.info(
                        "NWWS lifecycle carrier: type=%s awips=%s wfo=%s vtec=%s; routing to toneout handler",
                        parsed.product_type,
                        parsed.awips_id or "",
                        parsed.wfo,
                        ",".join(raw_vtec[:3]),
                    )
                await self._handle_toneout(parsed)

            # MWS (Marine Weather Statement) — Special Marine Warning lifecycle VTEC carrier.
            # SM.W (Special Marine Warning) is only issued ONCE by the NWS; all subsequent
            # lifecycle actions (CON, EXT, CAN, EXP) come through MWS products carrying
            # MA.W VTEC.  Routine MWSs without VTEC are silently ignored to avoid flooding
            # the schedule with routine marine text.
            elif (parsed.product_type or "").strip().upper() == "MWS":
                try:
                    _mws_vtec = self._extract_vtec(parsed.raw_text or "")
                    _mws_has_marine = any(".MA.W." in v or ".MA.A." in v for v in _mws_vtec)
                    if _mws_has_marine:
                        log.info(
                            "NWWS MWS: marine VTEC detected (%s), routing to toneout handler wfo=%s awips=%s",
                            ",".join(_mws_vtec[:3]),
                            parsed.wfo,
                            parsed.awips_id or "",
                        )
                        await self._handle_toneout(parsed)
                    else:
                        log.debug(
                            "NWWS MWS: no marine VTEC, ignoring wfo=%s awips=%s",
                            parsed.wfo,
                            parsed.awips_id or "",
                        )
                except Exception:
                    log.exception("NWWS MWS VTEC check failed wfo=%s awips=%s", parsed.wfo, parsed.awips_id or "")

            # PNS cycle injection — delegated to the configurable PNS state machine.
            # Only explicitly allowed, coherent PNS subtypes become cycle audio.
            elif (parsed.product_type or "").strip().upper() == "PNS":
                try:
                    official_pns = parsed.raw_text
                    try:
                        pid_pns = await self.api.latest_product_id(
                            "PNS", parsed.wfo[1:] if parsed.wfo.startswith("K") else parsed.wfo
                        )
                        if pid_pns:
                            prod_pns = await self.api.get_product(pid_pns)
                            if prod_pns and prod_pns.product_text:
                                same_product = self._nwws_api_product_matches_raw(parsed, prod_pns.product_text)
                                same_issuance = pns_text_same_issuance(
                                    parsed.raw_text or "",
                                    prod_pns.product_text,
                                    raw_fallback=getattr(parsed, "issued", None),
                                    candidate_fallback=getattr(prod_pns, "issuance_time", None),
                                )
                                if same_product and same_issuance:
                                    official_pns = prod_pns.product_text
                                else:
                                    raw_issued = parse_nws_header_issued_dt(
                                        parsed.raw_text or "", fallback=getattr(parsed, "issued", None)
                                    )
                                    api_issued = parse_nws_header_issued_dt(
                                        prod_pns.product_text, fallback=getattr(prod_pns, "issuance_time", None)
                                    )
                                    log.warning(
                                        "PNS API override rejected (stale/mismatched product): wfo=%s awips=%s pid=%s same_product=%s raw_issued=%s api_issued=%s",
                                        parsed.wfo,
                                        parsed.awips_id or "",
                                        pid_pns,
                                        same_product,
                                        raw_issued.isoformat() if raw_issued else "",
                                        api_issued.isoformat() if api_issued else "",
                                    )
                    except Exception:
                        log.exception("PNS official-text resolution failed; falling back to raw NWWS payload")

                    decision = self.pns_runtime.evaluate(
                        official_pns,
                        wfo=parsed.wfo or "",
                        awips_id=parsed.awips_id or "",
                        issued=getattr(parsed, "issued", None),
                    )
                    queued = await self.pns_runtime.queue_decision(
                        decision,
                        wfo=parsed.wfo or "",
                        awips_id=parsed.awips_id or "",
                        context="nwws",
                    )
                    if not queued:
                        continue
                except Exception:
                    log.exception("PNS handler error wfo=%s", parsed.wfo)

            # NOW (Short-Term Forecast) is routine, expiring cycle content.
            # It never enters the alert interrupt planes or AlertTracker.
            elif (parsed.product_type or "").strip().upper() == "NOW":
                self.now_runtime.submit(parsed)

    async def _handle_toneout(self, parsed: ParsedProduct) -> None:
        log.info(
            "NWWS toneout candidate: type=%s awips=%s wfo=%s", parsed.product_type, parsed.awips_id or "", parsed.wfo
        )

        # NWWS-OI already delivered the authoritative live product text.
        # Keep api.weather.gov out of the toneout hot path; API backfill remains
        # a separate recovery path when NWWS-OI misses products.
        official_text = parsed.raw_text or ""
        pid = None

        # --- NEW: derive SAME targeting from UGC zones (NWWS-only) ---
        zones, in_area_same, src, mapped_ok, ugc_expires_utc = await self.target_resolver._nwws_same_targets_from_texts(
            parsed.raw_text or "", official_text or ""
        )

        if zones:
            log.info(
                "NWWS targeting: ugc_zones=%d src=%s mapped_ok=%s in_area_same=%d",
                len(zones),
                src,
                mapped_ok,
                len(in_area_same),
            )

        vtec_preview = self._extract_vtec(official_text)
        _pre_policy = _vtec_toneout_policy(vtec_preview)
        _pre_is_wcn_watch = (parsed.product_type or "").strip().upper() == "WCN" and (
            _pre_policy.same_code or ""
        ).strip().upper() in {"SVA", "TOA"}
        if _pre_is_wcn_watch and not in_area_same:
            area_same = await self.target_resolver._nwws_wcn_watch_same_targets_from_area_desc(official_text or "")
            if area_same:
                in_area_same = area_same
                mapped_ok = True
                src = "wcn-area"
                log.info(
                    "NWWS WCN watch targeting recovered from county block: in_area_same=%d",
                    len(in_area_same),
                )

        # If we successfully mapped zones -> SOME SAME codes, and none are in-area => out-of-area, skip entirely.
        if zones and mapped_ok and not in_area_same:
            preview = ",".join(zones[:20]) + ("..." if len(zones) > 20 else "")
            log.info(
                "NWWS out-of-area: type=%s wfo=%s ugc_zones=%s (no intersection with service area); skipping",
                parsed.product_type,
                parsed.wfo,
                preview,
            )
            _discord_audit(
                self.discord,
                "alert_decision",
                source="NWWS-OI",
                result="skip",
                reason="out_of_area",
                product_type=parsed.product_type,
                awips=parsed.awips_id or "",
                wfo=parsed.wfo,
                same_targets=len(in_area_same or []),
                zones=len(zones or []),
                vtec=vtec_preview[:2],
                details={"target_source": src, "mapped_ok": mapped_ok},
            )
            return

        vtec = vtec_preview
        tracks = self._vtec_tracks(vtec)
        exp_utc = best_expiry_from_vtec(vtec)

        # VTEC toneout policy — vtec.py is authoritative for FULL vs VOICE.
        # This replaces the inline action-only check that ignored significance
        # (e.g. CF.Y.NEW was incorrectly treated as FULL before this fix).
        _nw_policy = _vtec_toneout_policy(vtec)
        vtec_actions = {act for (_t, act) in tracks} if tracks else set()
        should_full = _nw_policy.mode == "FULL"
        mixed_wcn = (
            (parsed.product_type or "").strip().upper() == "WCN"
            and should_full
            and bool(vtec_actions & {"NEW", "UPG", "EXA", "EXB"})
            and bool(vtec_actions & {"CAN", "EXP"})
        )
        discord_event_code = _nwws_discord_event_code(
            parsed.product_type,
            _nw_policy.same_code,
            vtec,
        )
        log.debug("NWWS vtec policy: %s", _nw_policy.reason)

        # Critical safety gate:
        # If we have UGC zones but could not map ANY of them to SAME, we do NOT air FULL.
        # This prevents blind tone-outs (especially marine zones) when mapping fails.
        if zones and (not mapped_ok) and should_full:
            log.warning(
                "NWWS SAME targeting failed (no zone->SAME mapping). Forcing voice-only type=%s wfo=%s zones=%s",
                parsed.product_type,
                parsed.wfo,
                ",".join(zones[:12]) + ("..." if len(zones) > 12 else ""),
            )
            should_full = False
            _discord_audit(
                self.discord,
                "alert_decision",
                source="NWWS-OI",
                result="downgrade",
                reason="zone_map_failed",
                code=discord_event_code,
                product_type=parsed.product_type,
                awips=parsed.awips_id or "",
                wfo=parsed.wfo,
                mode="voice",
                same_targets=len(in_area_same or []),
                zones=len(zones or []),
                vtec=vtec[:2],
                details={"target_source": src, "mapped_ok": mapped_ok},
            )

        # WCN watch county notifications can legitimately have clean watch text
        # and VTEC, but no UGC block in the NWWS carrier.  When that happens we
        # cannot build SAME headers from NWWS alone.  Do not air a SAME-less FULL
        # and reserve TRACKFULL, because the follow-up CAP alert usually carries
        # explicit SAME/FIPS codes and must remain eligible to tone out.
        _nw_is_wcn_watch_carrier = (
            (parsed.product_type or "").strip().upper() == "WCN"
            and should_full
            and (_nw_policy.same_code or "").strip().upper() in {"SVA", "TOA"}
        )
        if _nw_is_wcn_watch_carrier and not in_area_same:
            log.warning(
                "NWWS WCN watch has no SAME targets; forcing voice-only to preserve CAP full-tone path type=%s wfo=%s vtec=%s",
                parsed.product_type,
                parsed.wfo,
                ",".join(vtec[:2]) if vtec else "",
            )
            should_full = False
            _discord_audit(
                self.discord,
                "alert_decision",
                source="NWWS-OI",
                result="downgrade",
                reason="watch_no_same_targets",
                code=discord_event_code,
                product_type=parsed.product_type,
                awips=parsed.awips_id or "",
                wfo=parsed.wfo,
                mode="voice",
                same_targets=0,
                zones=len(zones or []),
                vtec=vtec[:2],
                details={"target_source": src, "policy": _nw_policy.reason},
            )

        keys: list[str] = []

        # Track/action-level dedupe prevents CAP+NWWS double-air for the same
        # lifecycle action while still allowing later CON -> EXP/CAN updates for
        # the same VTEC track.
        for track_id, act in tracks:
            action_mode = "FULL" if act in {"NEW", "UPG", "EXA", "EXB"} else "VOICE"
            keys.append(f"TRACK{action_mode}:{track_id}:{act or 'UNK'}")

        # Keep VTEC strings too (helps when track parse fails on weird edge cases)
        for v in vtec:
            keys.append(f"VTEC:{v}")

        # Functional FULL dedupe is only safe when we do NOT have a concrete
        # VTEC track.  Otherwise, distinct warnings for the same SAME footprint
        # can suppress each other.
        if should_full and not tracks:
            fkey2 = self._dedupe_func_full_key(parsed.product_type, in_area_same)
            if fkey2:
                keys.append(fkey2)

        # Message-level fallback key
        keys.append(
            f"NWWS:{parsed.product_type}:{parsed.wfo}:{self._sha1_12(official_text[:1200])}:{'FULL' if should_full else 'VOICE'}"
        )

        ok, hit = await self._dedupe_reserve(keys)
        if not ok:
            log.info(
                "NWWS %s skipped (dedupe hit=%s) type=%s awips=%s wfo=%s vtec=%s",
                "FULL" if should_full else "VOICE",
                hit,
                parsed.product_type,
                parsed.awips_id or "",
                parsed.wfo,
                ",".join(vtec[:2]) if vtec else "",
            )
            _discord_audit(
                self.discord,
                "dedupe_event",
                source="NWWS-OI",
                result="hit",
                key=hit,
                code=discord_event_code,
                mode="full" if should_full else "voice",
            )
            _discord_audit(
                self.discord,
                "alert_decision",
                source="NWWS-OI",
                result="skip",
                reason="dedupe",
                code=discord_event_code,
                product_type=parsed.product_type,
                awips=parsed.awips_id or "",
                wfo=parsed.wfo,
                mode="full" if should_full else "voice",
                same_targets=len(in_area_same or []),
                zones=len(zones or []),
                vtec=vtec[:2],
                details={"hit": hit},
            )
            return

        try:
            spoken = self._formatters.spoken_alert(parsed, official_text)

            sf_issued_dt = _sf_nwws_best_issued_dt(parsed, official_text)
            sf_event_label = _sf_nwws_event_label(parsed.product_type, vtec_list=vtec, text=official_text)
            sf_area_text = ""
            if in_area_same:
                try:
                    sf_area_text = await self.target_resolver._sf_area_text_from_same_codes(list(in_area_same))
                except Exception:
                    sf_area_text = ""
            if not sf_area_text:
                sf_area_text = _sf_nwws_area_from_text(official_text)
            sf_headline = _sf_nwws_make_headline(
                sf_event_label,
                issued_dt=sf_issued_dt,
                end_dt=exp_utc,
                issuer=_sf_nwws_extract_issuer(official_text, fallback_wfo=parsed.wfo),
            )

            rendered_terminal_script: NwwsScriptRenderResult | None = None
            try:
                full_actions = vtec_actions & {"NEW", "UPG", "EXA", "EXB"}
                terminal_actions = vtec_actions & {"CAN", "EXP"}
                full_action = _first_vtec_action(full_actions, ("NEW", "UPG", "EXA", "EXB"))
                terminal_action = _first_vtec_action(terminal_actions, ("CAN", "EXP"))
                rendered_script = self._formatters.nwws_product_script(
                    product_type=parsed.product_type,
                    base_script=spoken.script,
                    official_text=official_text,
                    vtec=[item for item in vtec if _vtec_action(item) in full_actions] if mixed_wcn else vtec,
                    vtec_actions=full_actions if mixed_wcn else vtec_actions,
                    has_tracks=bool(tracks),
                    should_full=should_full,
                    event_text=sf_event_label,
                    area_text=sf_area_text,
                    headline=sf_headline,
                    local_tz=self._tz,
                    watch_action=full_action if mixed_wcn else None,
                )
                if mixed_wcn:
                    rendered_terminal_script = self._formatters.nwws_product_script(
                        product_type=parsed.product_type,
                        base_script=spoken.script,
                        official_text=official_text,
                        vtec=[item for item in vtec if _vtec_action(item) in terminal_actions],
                        vtec_actions=terminal_actions,
                        has_tracks=True,
                        should_full=False,
                        event_text=sf_event_label,
                        area_text=sf_area_text,
                        headline=sf_headline,
                        local_tz=self._tz,
                        watch_action=terminal_action,
                    )
                spoken.script = rendered_script.script
                if rendered_script.changed:
                    log.info(
                        "NWWS script normalized by %s (act=%s type=%s awips=%s wfo=%s)",
                        rendered_script.renderer,
                        ",".join(sorted(vtec_actions))[:64],
                        parsed.product_type,
                        parsed.awips_id or "",
                        parsed.wfo,
                    )
                for note in rendered_script.notes:
                    if note.startswith("warning:"):
                        log.warning("NWWS script renderer note: %s", note[8:].strip())
                    else:
                        log.info("NWWS script renderer note: %s", note)
                if rendered_terminal_script is not None:
                    for note in rendered_terminal_script.notes:
                        if note.startswith("warning:"):
                            log.warning("NWWS terminal script renderer note: %s", note[8:].strip())
                        else:
                            log.info("NWWS terminal script renderer note: %s", note)
            except Exception:
                log.exception("NWWS script normalization failed; continuing with original script")

            same_for_render: list[str] = []
            render_parsed = parsed
            if should_full:
                # If we have in-area SAME targets, use them. Otherwise, AIR WITHOUT SAME (no 67 fallback).
                same_for_render = list(in_area_same) if in_area_same else []
                if zones and not mapped_ok:
                    log.warning(
                        "NWWS SAME targeting unavailable (zone map failed); airing without SAME headers type=%s wfo=%s",
                        parsed.product_type,
                        parsed.wfo,
                    )
                if _nw_policy.same_code and _nw_policy.same_code != (parsed.product_type or "").strip().upper():
                    render_parsed = ParsedProduct(
                        product_type=_nw_policy.same_code,
                        wfo=parsed.wfo,
                        awips_id=parsed.awips_id,
                        vtec=parsed.vtec,
                        raw_text=parsed.raw_text,
                    )

            event_label = sf_event_label
            if (not should_full) and (("EXP" in vtec_actions) or ("CAN" in vtec_actions)):
                tkey = "nwws_end"
            elif should_full:
                tkey = "nwws_full"
            else:
                tkey = "nwws_update"
            title = self._np_alert_title(tkey, event=event_label)
            meta = self._np_meta(
                title=title,
                kind="alert",
                extra={
                    "sw_alert_source": "nwws",
                    "sw_alert_mode": ("full" if should_full else "voice"),
                    "sw_event_code": (_nw_policy.same_code or parsed.product_type or "").strip().upper(),
                    "sw_event": event_label,
                    "sw_wfo": (parsed.wfo or "").strip(),
                    "sw_awips": (parsed.awips_id or "").strip(),
                },
            )
            if should_full:

                async def _render_nwws_full():
                    return await self.audio_originator.render_alert_audio(
                        render_parsed,
                        spoken.script,
                        same_locations=same_for_render,
                    )

                out_wav = await self._render_and_push_interrupt_audio(
                    source="nwws-full",
                    full=True,
                    render=_render_nwws_full,
                    meta=meta,
                )
                if mixed_wcn and rendered_terminal_script is not None:
                    voice_meta = dict(meta)
                    voice_meta["sw_alert_mode"] = "voice"

                    async def _render_nwws_mixed_voice():
                        return await self.audio_originator.render_voice_only_audio(
                            rendered_terminal_script.script,
                            prefix="nwwsvoice",
                        )

                    terminal_wav = await self._render_and_push_interrupt_audio(
                        source="nwws-voice-terminal",
                        full=False,
                        render=_render_nwws_mixed_voice,
                        meta=voice_meta,
                    )
                    log.info(
                        "NWWS mixed WCN: aired active FULL followed by terminal VOICE type=%s full_audio=%s terminal_audio=%s",
                        parsed.product_type,
                        out_wav,
                        terminal_wav,
                    )
            else:

                async def _render_nwws_voice():
                    return await self.audio_originator.render_voice_only_audio(
                        spoken.script,
                        prefix="nwwsvoice",
                    )

                out_wav = await self._render_and_push_interrupt_audio(
                    source="nwws-voice",
                    full=False,
                    render=_render_nwws_voice,
                    meta=meta,
                )

            now = dt.datetime.now(tz=self._tz)

            # Only FULL toneouts should push heightened mode + toneout timestamp.
            if should_full:
                self.last_toneout_at = now
                self.last_heightened_at = now
                self.heightened_until = now + dt.timedelta(seconds=self.cfg.cycle.min_heightened_seconds)
                self._update_mode()

            self._nwws_acted += 1
            log.info(
                "NWWS ACTION: aired %s #%d/%d type=%s code=%s awips=%s wfo=%s reason=%s same_targets=%d zones=%d vtec=%s tracks=%s audio=%s",
                "FULL" if should_full else "VOICE",
                self._nwws_acted,
                self._nwws_seen,
                parsed.product_type,
                discord_event_code,
                parsed.awips_id or "",
                parsed.wfo,
                _nw_policy.reason,
                len(in_area_same or []),
                len(zones or []),
                ",".join(vtec[:2]) if vtec else "",
                ",".join(t for (t, _a) in tracks[:2]) if tracks else "",
                out_wav,
            )
            _discord_audit(
                self.discord,
                "alert_decision",
                source="NWWS-OI",
                result="air",
                reason=_nw_policy.reason,
                event=sf_event_label,
                code=discord_event_code,
                product_type=parsed.product_type,
                mode="full" if should_full else "voice",
                awips=parsed.awips_id or "",
                wfo=parsed.wfo,
                same_targets=len(in_area_same or []),
                zones=len(zones or []),
                vtec=vtec[:2],
                details={
                    "tracks": [t for (t, _a) in tracks[:4]],
                    "target_source": src,
                    "mapped_ok": mapped_ok,
                },
            )
            # _NWWS_DL_
            _dl_vtec_acts = vtec_actions
            _dl_mode = "full" if should_full else "voice"
            _dl_terminal_action = _first_vtec_action(_dl_vtec_acts, ("CAN", "EXP"))
            _dl_update_action = _first_vtec_action(
                _dl_vtec_acts,
                ("CON", "EXT", "EXA", "EXB", "COR", "ROU", "NEW", "UPG"),
            )
            _dl_cancel_tracks = sorted(_nw_policy.cancel_tracks)
            _dl_continuation_tracks = sorted(_nw_policy.continuation_tracks)
            _dl_nonterminal_actions = _dl_vtec_acts - {"CAN", "EXP"}

            if _dl_terminal_action and (_dl_continuation_tracks or _dl_nonterminal_actions):
                _partial_logger = getattr(self.discord, "alert_partial_terminal", None)
                if _partial_logger is not None:
                    _partial_logger(
                        code=discord_event_code,
                        event=sf_event_label,
                        vtec_action=_dl_terminal_action,
                        source="NWWS-OI",
                        area=sf_area_text,
                        vtec=vtec[:2],
                        ended_tracks=_dl_cancel_tracks,
                        continuing_tracks=_dl_continuation_tracks
                        or [t for (t, action) in tracks if action in _dl_nonterminal_actions],
                        mode=_dl_mode,
                    )
                else:
                    self.discord.alert_updated(
                        code=discord_event_code,
                        event=sf_event_label,
                        vtec_action=_dl_terminal_action,
                        source="NWWS-OI",
                        area=sf_area_text,
                        vtec=vtec[:2],
                    )
                log.info(
                    "NWWS Discord: state=partial_terminal action=%s type=%s code=%s ended_tracks=%d continuing_tracks=%d active_actions=%s",
                    _dl_terminal_action,
                    parsed.product_type,
                    discord_event_code,
                    len(_dl_cancel_tracks),
                    len(_dl_continuation_tracks),
                    ",".join(sorted(_dl_nonterminal_actions)),
                )
            elif _dl_terminal_action:
                self.discord.alert_expired(
                    code=discord_event_code,
                    event=sf_event_label,
                    vtec_action=_dl_terminal_action,
                    source="NWWS-OI",
                    area=sf_area_text,
                    vtec=vtec[:2],
                )
                log.info(
                    "NWWS Discord: state=terminal action=%s type=%s code=%s",
                    _dl_terminal_action,
                    parsed.product_type,
                    discord_event_code,
                )
            elif not should_full and _dl_update_action:
                self.discord.alert_updated(
                    code=discord_event_code,
                    event=sf_event_label,
                    vtec_action=_dl_update_action,
                    source="NWWS-OI",
                    area=sf_area_text,
                    vtec=vtec[:2],
                )
                log.info(
                    "NWWS Discord: state=continuing action=%s type=%s code=%s",
                    _dl_update_action,
                    parsed.product_type,
                    discord_event_code,
                )
            else:
                self.discord.alert_aired(
                    code=discord_event_code,
                    event=sf_event_label,
                    source="NWWS-OI",
                    mode=_dl_mode,
                    area=sf_area_text,
                    vtec=vtec[:2],
                    is_test=(parsed.product_type in {"RWT", "RMT"}),
                )
                log.info(
                    "NWWS Discord: state=aired mode=%s type=%s code=%s",
                    _dl_mode,
                    parsed.product_type,
                    discord_event_code,
                )
            _sf_mode = getattr(spoken, "mode", ("FULL" if should_full else "VOICE"))
            _station_feed_note_nwws(
                parsed,
                mode=_sf_mode,
                same_locations=list(in_area_same or []),
                out_wav=str(out_wav),
                product_id=pid,
                expires_at=exp_utc,
                vtec=vtec,
                official_text=official_text,
                issued_at=sf_issued_dt,
                event_text=sf_event_label,
                headline=sf_headline,
                area_text=sf_area_text,
            )
            _discord_audit(
                self.discord,
                "station_feed_update",
                action="upsert",
                source="NWWS-OI",
                event=sf_event_label,
                code=discord_event_code,
                alert_id=(tracks[0][0] if tracks else (parsed.awips_id or "")),
                details={"mode": _sf_mode, "same_targets": len(in_area_same or [])},
            )

            self._schedule_cycle_refill("post-alert")

            # Register / update / remove from AlertTracker
            try:
                _nw_vtec = vtec
                _nw_tracks = tracks
                _nw_vtec_actions = vtec_actions
                _nw_exp_utc = best_expiry_from_vtec(_nw_vtec)
                _nw_expires_iso = (
                    _nw_exp_utc.isoformat()
                    if _nw_exp_utc
                    else (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=6)).isoformat()
                )
                _nw_same_code = _nw_policy.same_code or _safe_event_code(parsed.product_type)
                _nw_same_locs = list(in_area_same) if in_area_same else []
                _nw_track_ids = {_vtec_track_id(v) for v in _nw_vtec if _vtec_track_id(v)}
                _nw_event_label = sf_event_label
                _nw_headline = sf_headline
                if _nw_vtec_actions & {"CAN", "EXP"} and not should_full:
                    # Partial cancel awareness: use the cancel_tracks frozenset from
                    # toneout_policy() — only tracks with a CAN/EXP action are removed.
                    # Tracks still active (CON/EXT in the same product) are left in place.
                    cancel_track_ids: frozenset[str] = _nw_policy.cancel_tracks
                    continuation_track_ids: frozenset[str] = _nw_policy.continuation_tracks

                    # Fall back to all tracks if vtec.py didn't differentiate
                    # (e.g. a product with only CAN and no CON).
                    if not cancel_track_ids and not continuation_track_ids:
                        cancel_track_ids = frozenset(track_id for track_id in _nw_track_ids if track_id)

                    if cancel_track_ids:
                        removed_n = self.alert_tracker.remove_by_vtec_tracks(
                            cancel_track_ids,
                            reason=f"nwws:{parsed.product_type}:{','.join(sorted(_nw_vtec_actions & {'CAN', 'EXP'}))}",
                        )
                        self._remove_matching_ipaws_state(
                            code=_nw_same_code,
                            same_locs=_nw_same_locs,
                            reason=f"nwws:{parsed.product_type}:{','.join(sorted(_nw_vtec_actions & {'CAN', 'EXP'}))}",
                        )
                        self._remove_shadowed_ern_state(
                            code=_nw_same_code,
                            same_locs=_nw_same_locs,
                            reason=f"nwws:{parsed.product_type}:{','.join(sorted(_nw_vtec_actions & {'CAN', 'EXP'}))}",
                        )
                        log.info(
                            "AlertTracker: removed %d entries for NWWS CAN/EXP type=%s awips=%s tracks=%s",
                            removed_n,
                            parsed.product_type,
                            parsed.awips_id or "",
                            ",".join(sorted(cancel_track_ids)),
                        )

                    if continuation_track_ids:
                        # Some tracks are still active — keep them in the tracker
                        # with an updated script so the cycle reflects the partial state.
                        _nw_tid_str = next(iter(continuation_track_ids), None)
                        _nw_tracker_id = (
                            f"NWWS:{_nw_tid_str}"
                            if _nw_tid_str
                            else (f"NWWS:{parsed.product_type}:{parsed.wfo}:{(parsed.awips_id or '').strip()}")
                        )
                        _nw_issued = sf_issued_dt or dt.datetime.now(dt.timezone.utc)
                        if _nw_issued.tzinfo is None:
                            _nw_issued = _nw_issued.replace(tzinfo=dt.timezone.utc)
                        _ae_nw_con = ActiveAlert(
                            id=_nw_tracker_id,
                            source="NWWS",
                            event=_nw_event_label,
                            code=_nw_same_code,
                            vtec=_nw_vtec,
                            headline=_nw_headline,
                            script_text=spoken.script,
                            audio_path=str(out_wav),
                            expires=_nw_expires_iso,
                            issued=_nw_issued.isoformat(),
                            same_locs=_nw_same_locs,
                            cycle_only=True,
                        )
                        self.alert_tracker.add_or_update(_ae_nw_con)
                        self.alert_tracker.mark_aired(_nw_tracker_id)
                        self._remove_shadowed_ern_state(
                            code=_nw_same_code,
                            same_locs=_nw_same_locs,
                            reason=f"nwws-continuation:{_nw_tracker_id}",
                        )
                        log.info(
                            "AlertTracker: kept continuing NWWS id=%s type=%s (partial cancel) tracks=%s",
                            _nw_tracker_id,
                            parsed.product_type,
                            ",".join(sorted(continuation_track_ids)),
                        )
                else:
                    # New issuance, update, or continuation
                    _nw_tid_str = next(iter(_nw_track_ids), None)
                    _nw_tracker_id = (
                        f"NWWS:{_nw_tid_str}"
                        if _nw_tid_str
                        else (f"NWWS:{parsed.product_type}:{parsed.wfo}:{(parsed.awips_id or '').strip()}")
                    )
                    _nw_is_cycle_only = not should_full
                    _nw_issued = sf_issued_dt or dt.datetime.now(dt.timezone.utc)
                    if _nw_issued.tzinfo is None:
                        _nw_issued = _nw_issued.replace(tzinfo=dt.timezone.utc)
                    _ae_nw = ActiveAlert(
                        id=_nw_tracker_id,
                        source="NWWS",
                        event=_nw_event_label,
                        code=_nw_same_code,
                        vtec=_nw_vtec,
                        headline=_nw_headline,
                        script_text=spoken.script,
                        audio_path=str(out_wav),
                        expires=_nw_expires_iso,
                        issued=_nw_issued.isoformat(),
                        same_locs=_nw_same_locs,
                        cycle_only=_nw_is_cycle_only,
                    )
                    self.alert_tracker.add_or_update(_ae_nw)
                    self.alert_tracker.mark_aired(_nw_tracker_id)
                    self._remove_shadowed_ern_state(
                        code=_nw_same_code,
                        same_locs=_nw_same_locs,
                        reason=f"nwws:{_nw_tracker_id}",
                    )
                    log.info(
                        "AlertTracker: registered NWWS id=%s type=%s should_full=%s expires=%s",
                        _nw_tracker_id,
                        parsed.product_type,
                        should_full,
                        _nw_expires_iso,
                    )
            except Exception:
                log.exception("AlertTracker: failed to register NWWS type=%s", parsed.product_type)

        except Exception:
            await self._dedupe_release(keys)
            raise
