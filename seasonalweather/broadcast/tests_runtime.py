from __future__ import annotations

import datetime as dt
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from ..same.events import label_or_code as _same_label_or_code
from .rwt_rmt import RwtRmtSchedule, RwtRmtScheduler
from .station_feed_runtime import note_required_test as _station_feed_note_required_test
from .tests import default_test_script_lines, format_test_presentation_template

if TYPE_CHECKING:
    from ..main import Orchestrator


log = logging.getLogger("seasonalweather")


class RequiredTestRuntime:
    """Own local RWT/RMT scheduling, gating, and origination."""

    def __init__(self, orch: "Orchestrator") -> None:
        self.orch = orch

    def start_scheduler(
        self,
        *,
        supervisor: Any,
    ) -> None:
        orch = self.orch
        if not orch.cfg.tests.enabled:
            log.info("RWT/RMT scheduler disabled (set tests.enabled: true in config.yaml to enable)")
            return

        try:
            sched = RwtRmtSchedule(
                enabled=True,
                tz_name=orch.cfg.station.timezone,
                rwt_enabled=True,
                rwt_weekday=orch.cfg.tests.rwt.weekday,
                rwt_hour=orch.cfg.tests.rwt.hour,
                rwt_minute=orch.cfg.tests.rwt.minute,
                rmt_enabled=True,
                rmt_nth=orch.cfg.tests.rmt.nth,
                rmt_weekday=orch.cfg.tests.rmt.weekday,
                rmt_hour=orch.cfg.tests.rmt.hour,
                rmt_minute=orch.cfg.tests.rmt.minute,
                jitter_seconds=orch.cfg.tests.jitter_seconds,
                postpone_minutes=orch.cfg.tests.postpone_minutes,
                max_postpone_hours=orch.cfg.tests.max_postpone_hours,
                state_path="",
                state_key="rwt_rmt",
                rwt_postpone_policy=orch.cfg.tests.rwt.postpone_policy,
                rwt_postpone_minutes=orch.cfg.tests.rwt.postpone_minutes,
                rwt_max_postpone_hours=orch.cfg.tests.rwt.max_postpone_hours,
                rwt_max_postpone_days=orch.cfg.tests.rwt.max_postpone_days,
                rmt_postpone_policy=orch.cfg.tests.rmt.postpone_policy,
                rmt_postpone_minutes=orch.cfg.tests.rmt.postpone_minutes,
                rmt_max_postpone_hours=orch.cfg.tests.rmt.max_postpone_hours,
                rmt_max_postpone_days=orch.cfg.tests.rmt.max_postpone_days,
            )

            def _rlog(s: str) -> None:
                log.info("%s", s)

            rsch = RwtRmtScheduler(
                schedule=sched,
                gate_fn=self.gate,
                fire_fn=self.originate_required_test,
                log_fn=_rlog,
                database=orch.database,
            )
            supervisor.create_task(
                rsch.run_forever(),
                name="rwt_rmt_scheduler",
                required=False,
                stop=rsch.stop,
            )
            log.info("RWT/RMT scheduler enabled (state=sqlite)")
        except Exception:
            log.exception("Failed to start RWT/RMT scheduler")

    async def local_test_presentation(self, code: str, same_codes: list[str] | None = None) -> tuple[str, str, str]:
        orch = self.orch
        event_text = _same_label_or_code(code)
        station_name = str(orch.cfg.station.name or "SeasonalWeather").strip() or "SeasonalWeather"
        service_area_name = str(orch.cfg.station.service_area_name or "service area").strip() or "service area"
        codes = [str(x).strip() for x in (same_codes or []) if str(x).strip()]

        auto_area_text = ""
        if codes:
            try:
                auto_area_text = await orch.target_resolver._sf_area_text_from_same_codes(codes)
            except Exception:
                auto_area_text = ""
        auto_area_text = auto_area_text or service_area_name

        pres = orch.cfg.tests.presentation
        fmt_ctx = {
            "code": str(code or "").strip().upper(),
            "event": event_text,
            "station_name": station_name,
            "service_area_name": service_area_name,
            "auto_area_text": auto_area_text,
        }
        headline = (
            format_test_presentation_template(
                pres.headline_template,
                **fmt_ctx,
            )
            or f"{event_text} for the {service_area_name}"
        )
        area_text = (
            format_test_presentation_template(
                pres.area_text,
                **fmt_ctx,
            )
            or auto_area_text
        )
        discord_area_text = (
            format_test_presentation_template(
                pres.discord_area_text,
                **{**fmt_ctx, "area_text": area_text},
            )
            or area_text
        )
        return headline, area_text, discord_area_text

    def same_codes_for_presentation(self) -> list[str]:
        try:
            codes = sorted(getattr(self.orch, "_same_fips_allow_set", None) or [])
        except Exception:
            codes = []
        return [str(x).strip() for x in codes if str(x).strip()]

    def gate(self, event_code: str = "") -> tuple[bool, str]:
        orch = self.orch
        now = dt.datetime.now(tz=orch._tz)
        code = str(event_code or "").strip().upper()
        test_cfg = orch.cfg.tests.rwt if code == "RWT" else orch.cfg.tests.rmt
        gate = test_cfg.gate

        if gate.block_heightened and orch.heightened_until and now < orch.heightened_until:
            return (False, "heightened mode active")
        if gate.block_recent_toneout and orch.last_toneout_at:
            if (now - orch.last_toneout_at).total_seconds() < orch.cfg.tests.toneout_cooldown_seconds:
                return (False, "recent tone-out cooldown")

        if gate.block_recent_severe_cap and orch.cap_last_severe_at:
            if (now - orch.cap_last_severe_at).total_seconds() < orch.cfg.tests.cap_block_seconds:
                return (False, "recent severe CAP match")

        if gate.block_recent_ern and orch.ern_last_tone_at:
            if (now - orch.ern_last_tone_at).total_seconds() < orch.cfg.tests.ern_block_seconds:
                return (False, "recent ERN SAME activity")

        return (True, "ok")

    async def originate_required_test(self, event_code: str) -> None:
        """
        Originates a local RWT/RMT using the existing SAME+audio pipeline.
        Does NOT trigger heightened mode.
        """
        orch = self.orch
        code = (event_code or "").strip().upper()
        if code not in {"RWT", "RMT"}:
            return

        # Script: use config override when provided, else fall back to built-in default.
        _cfg_lines: tuple[str, ...] = (
            orch.cfg.tests.rwt.script_lines if code == "RWT" else orch.cfg.tests.rmt.script_lines
        )
        if _cfg_lines:
            lines = list(_cfg_lines)
        else:
            lines = default_test_script_lines(code)

        spoken = "\n".join(lines).strip()

        dummy = SimpleNamespace(product_type=code, awips_id=None, wfo="KLWX", raw_text="")

        tkey = "rwt" if code == "RWT" else "rmt"
        title = orch._np_alert_title(tkey, event="")
        meta = orch._np_meta(title=title, kind="test", extra={"sw_alert_source": "local", "sw_event_code": code})

        async def _render_required_test():
            return await orch.audio_originator.render_alert_audio(dummy, spoken)

        out_wav = await orch._render_and_push_interrupt_audio(
            source=f"local-{code.lower()}",
            full=True,
            render=_render_required_test,
            meta=meta,
        )

        # --- Station feed note (radio UI/API handled-alerts feed) ---
        local_test_same_codes = self.same_codes_for_presentation()
        test_headline = f"Required {'Weekly' if code == 'RWT' else 'Monthly'} Test"
        test_area_text = str(orch.cfg.station.service_area_name or "SeasonalWeather").strip() or "SeasonalWeather"
        discord_test_area_text = test_area_text
        try:
            test_headline, test_area_text, discord_test_area_text = await self.local_test_presentation(
                code,
                local_test_same_codes,
            )
        except Exception:
            pass

        _station_feed_note_required_test(
            code=code,
            headline=test_headline,
            area_text=test_area_text,
            same_codes=local_test_same_codes,
            out_wav=str(out_wav),
        )

        orch._schedule_cycle_refill("post-test")
        log.info("Originated %s test (audio=%s)", code, out_wav)
        # _TEST_DL_v3_
        orch.discord.alert_aired(
            code=code,
            event=f"Required {'Weekly' if code == 'RWT' else 'Monthly'} Test",
            source="SeasonalWeather (local)",
            mode="full",
            area=discord_test_area_text,
            same_codes=local_test_same_codes,
            is_test=True,
        )
