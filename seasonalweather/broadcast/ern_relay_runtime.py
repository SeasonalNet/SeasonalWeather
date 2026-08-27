from __future__ import annotations

import asyncio
import datetime as dt
import logging
from types import SimpleNamespace
from typing import Any

from ..alerts.active import ActiveAlert
from ..same.events import label_or_code as _same_label_or_code
from .formatters import FormatterSubsystem
from .station_feed_runtime import note_ern as _station_feed_note_ern

log = logging.getLogger("seasonalweather")

class ErnRelayRuntime:
    """Runtime consumer for ERN/GWES SAME relay events.

    The host object is the orchestrator for now; pass 5 moves the source
    consumer out of main.py without changing ERN relay policy, dedupe, audio,
    active-alert, station-feed, or Discord behavior.
    """

    def __init__(self, host: Any, formatters: FormatterSubsystem) -> None:
        self.host = host
        self.formatters = formatters

    async def run(self) -> None:
        host = self.host
        while True:
            ev = await host.ern_queue.get()

            if ev.kind == "header":
                log.info(
                    "ERN SAME header: org=%s event=%s sender=%s conf=%.3f same=%s text=%s",
                    ev.org,
                    ev.event,
                    (ev.sender or "").strip(),
                    ev.confidence,
                    ",".join(ev.locations[:12]) + ("..." if len(ev.locations) > 12 else ""),
                    ev.text,
                )

                try:
                    if ev.event and ev.event in host.cfg.policy.toneout_product_types and ev.event not in {"RWT", "RMT"}:
                        host.ern_last_tone_at = dt.datetime.now(tz=host._tz)
                except Exception:
                    pass
            else:
                log.info("ERN SAME EOM: conf=%.3f text=%s", ev.confidence, ev.text)

            if host.cfg.ern.dryrun:
                continue
            if not host.cfg.ern.relay.enabled:
                continue
            if ev.kind != "header":
                continue

            code = (ev.event or "").strip().upper()
            if code not in {e.strip().upper() for e in host.cfg.ern.relay.events if e.strip()}:
                continue

            conf = float(getattr(ev, "confidence", 0.0) or 0.0)
            if conf < host.cfg.ern.relay.min_confidence:
                log.info(
                    "ERN relay: confidence too low (%.3f < %.3f) event=%s sender=%s",
                    conf,
                    host.cfg.ern.relay.min_confidence,
                    code,
                    ev.sender,
                )
                continue

            senders = {s.strip().upper() for s in host.cfg.ern.relay.senders if s.strip()}
            sender_u = (ev.sender or "").strip().upper()
            if senders and sender_u not in senders:
                log.info("ERN relay: sender not allowed (sender=%s allowed=%s)", ev.sender, ",".join(sorted(senders)))
                continue

            now = dt.datetime.now(tz=host._tz)
            if host._ern_relay_last_any_at and (now - host._ern_relay_last_any_at).total_seconds() < host.cfg.ern.relay.cooldown_seconds:
                log.info("ERN relay: cooldown active; skipping event=%s sender=%s", code, ev.sender)
                continue

            in_area_locs = host.target_resolver._filter_same_locations_to_service_area(getattr(ev, "locations", None))
            if not in_area_locs:
                log.info(
                    "ERN relay: no in-area SAME locations after filtering; skipping event=%s sender=%s decoded=%s",
                    code,
                    ev.sender,
                    ",".join(getattr(ev, "locations", [])[:12]) + ("..." if len(getattr(ev, "locations", [])) > 12 else ""),
                )
                continue

            # Cross-source dedupe: reserve keys BEFORE rendering/airing

            keys: list[str] = []

            fkey3 = host._dedupe_func_full_key(code, in_area_locs)

            if fkey3:

                keys.append(fkey3)

            # ERN-specific fallback (suppresses identical repeats even if functional key is absent)

            sender_u2 = (ev.sender or "").strip().upper()

            loc_sig = ",".join(sorted(set(in_area_locs)))[:1200]

            keys.append(f"ERNRELAY:{code}:{host._sha1_12(sender_u2 + '|' + loc_sig)}")

            ok, hit = await host._dedupe_reserve(keys)

            if not ok:

                log.info("ERN relay skipped (dedupe hit=%s) event=%s sender=%s same=%s", hit, code, ev.sender, loc_sig[:160])

                continue

            sf_ev = ev
            area_text = ""
            try:
                area_text = await host.target_resolver._sf_area_text_from_same_codes(in_area_locs)
                if area_text:
                    try:
                        setattr(sf_ev, "area", area_text)
                    except Exception:
                        try:
                            sf_ev = SimpleNamespace(**getattr(ev, "__dict__", {}))
                            setattr(sf_ev, "area", area_text)
                        except Exception:
                            sf_ev = ev
            except Exception:
                area_text = ""

            script = self.formatters.ern_relay_script(
                ev,
                same_locations=in_area_locs,
                area_text=area_text,
                tz=host._tz,
            )
            dummy = SimpleNamespace(product_type=code, awips_id=None, wfo="ERN", raw_text="")

            event_label = _same_label_or_code(code)
            title = host._np_alert_title("ern", event=event_label)
            meta = host._np_meta(
                title=title,
                kind="alert",
                extra={
                    "sw_alert_source": "ern",
                    "sw_alert_mode": "full",
                    "sw_event_code": code,
                    "sw_event": event_label,
                    "sw_sender": (ev.sender or "").strip(),
                },
            )
            async def _render_ern_full():
                return await host.audio_originator.render_alert_audio(
                    dummy,
                    script,
                    same_locations=in_area_locs,
                )

            out_wav = await host._render_and_push_interrupt_audio(
                source="ern-full",
                full=True,
                render=_render_ern_full,
                meta=meta,
            )

            host._ern_relay_last_any_at = now
            host.last_product_desc = f"ERN {code}".strip()

            try:
                event_label = _same_label_or_code(code)
                relay_id = "ERN:%s:%s" % (
                    code,
                    host._sha1_12(
                        "|".join([
                            (ev.sender or "").strip().upper(),
                            ",".join(sorted(set(in_area_locs))),
                            str(getattr(ev, "jjjhhmm", "") or ""),
                            str(getattr(ev, "tttt", "") or ""),
                        ])
                    ),
                )
                alert_entry = ActiveAlert(
                    id=relay_id,
                    source="ERN",
                    event=event_label,
                    code=code,
                    vtec=[],
                    headline=f"ERN relay: {event_label}",
                    script_text=script,
                    audio_path=str(out_wav),
                    expires=host._alert_expires_from_ern(ev),
                    issued=dt.datetime.now(dt.timezone.utc).isoformat(),
                    same_locs=in_area_locs,
                    cycle_only=True,
                    watch_number=None,
                )
                host.alert_tracker.add_or_update(alert_entry)
                host.alert_tracker.mark_aired(relay_id)
                log.info("AlertTracker: registered ERN relay id=%s code=%s", relay_id, code)
            except Exception:
                log.exception("AlertTracker: failed to register ERN relay event=%s sender=%s", code, ev.sender)

            host._schedule_cycle_refill("post-ern-relay")
            log.info(
                "ERN ACTION: aired relay event=%s sender=%s same_locs=%d audio=%s",
                code,
                ev.sender,
                len(in_area_locs),
                out_wav,
            )
            # _ERN_DL_
            host.discord.alert_aired(
                code=code,
                event=_same_label_or_code(code),
                source=f"ERN/GWES ({(ev.sender or '').strip() or 'unknown'})",
                mode="full",
                area=area_text,
                same_codes=in_area_locs,
                is_ern=True,
            )
            _station_feed_note_ern(sf_ev, same_locations=in_area_locs, out_wav=str(out_wav))
