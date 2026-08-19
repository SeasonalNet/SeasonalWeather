"""API application boundary for manual alert origination."""

from __future__ import annotations

import contextlib
import datetime as dt
from typing import Any

from seasonalweather.api.models import OriginateAudioRequest, OriginateTextRequest, VoiceMode
from seasonalweather.application.errors import ControlError, NotFoundError
from seasonalweather.artifacts.audio_assets import AudioAssetService
from seasonalweather.same.locations import normalize_same_location, same_location_matches_service_area


class ManualOriginationService:
    """Translate API requests into the existing manual runtime authority."""

    def __init__(self, orchestrator: Any, assets: AudioAssetService) -> None:
        self.orch = orchestrator
        self.assets = assets

    def _now_local(self) -> dt.datetime:
        return dt.datetime.now(tz=getattr(self.orch, "_tz", dt.UTC))

    def _same_codes_in_service_area(self, same_codes: list[str]) -> list[str]:
        allow = getattr(self.orch, "_same_fips_allow_set", set())
        normalized: list[str] = []
        disallowed: list[str] = []
        for raw in same_codes:
            code = normalize_same_location(raw)
            if not code or not same_location_matches_service_area(code, allow, allow_statewide_input=False):
                disallowed.append(code if code else str(raw))
                continue
            normalized.append(code)
        if disallowed:
            raise ControlError(
                "same_code_out_of_service_area",
                "One or more SAME codes are outside the configured service area.",
                details={"same_codes": disallowed},
            )
        return normalized

    async def originate_text(self, req: OriginateTextRequest, *, actor: str) -> dict[str, Any]:
        same_codes = list(req.same_codes)
        if req.voice_mode == VoiceMode.FULL_EAS.value:
            same_codes = self._same_codes_in_service_area(same_codes)
        try:
            result = await self.orch.manual_runtime.originate_text(
                event_code=req.event_code,
                headline=req.headline,
                script_text=req.text,
                voice_mode=req.voice_mode,
                same_locations=same_codes,
                sender=req.sender,
                actor=actor,
                interrupt_policy=req.interrupt_policy,
                expires_in_minutes=req.expires_in_minutes,
                heightened_override=req.heightened,
            )
        except NotImplementedError as exc:
            raise ControlError("manual_origination_not_supported", str(exc)) from exc
        except FileNotFoundError as exc:
            raise NotFoundError(
                "manual_audio_missing", "Manual origination audio source is missing.", details={"path": str(exc)}
            ) from exc
        except ValueError as exc:
            raise ControlError("invalid_manual_origination", str(exc)) from exc
        self._record_text_action(req, actor)
        return result

    async def originate_audio(self, req: OriginateAudioRequest, *, actor: str) -> dict[str, Any]:
        same_codes = list(req.same_codes)
        if req.voice_mode == VoiceMode.FULL_EAS.value:
            same_codes = self._same_codes_in_service_area(same_codes)
        self.assets.load(req.audio_asset_id)
        _work_dir, audio_dir, _cache_dir, _logs_dir = self.orch._paths()
        timestamp = self._now_local().strftime("%Y%m%d-%H%M%S")
        output = audio_dir / f"api_audio_{timestamp}_{req.audio_asset_id}.wav"
        self.assets.copy_to(asset_id=req.audio_asset_id, destination=output)
        try:
            result = await self.orch.manual_runtime.originate_audio(
                event_code=req.event_code,
                headline=req.headline,
                wav_path=output,
                voice_mode=req.voice_mode,
                same_locations=same_codes,
                sender=req.sender,
                actor=actor,
                interrupt_policy=req.interrupt_policy,
                expires_in_minutes=req.expires_in_minutes,
                heightened_override=req.heightened,
            )
        except FileNotFoundError as exc:
            output.unlink(missing_ok=True)
            raise NotFoundError(
                "manual_audio_missing", "Manual origination audio source is missing.", details={"path": str(exc)}
            ) from exc
        except ValueError as exc:
            output.unlink(missing_ok=True)
            raise ControlError("invalid_manual_origination", str(exc)) from exc
        result["audio_asset_id"] = req.audio_asset_id
        result["audio_path"] = str(output)
        with contextlib.suppress(Exception):
            self.orch.discord.api_action(
                method="POST",
                endpoint="/v1/originate/audio",
                actor=actor,
                status="succeeded",
                headline=req.headline,
                details={"event_code": req.event_code, "voice_mode": req.voice_mode, "asset_id": req.audio_asset_id},
            )
        self._record_eas_action(req, actor)
        return result

    def _record_text_action(self, req: OriginateTextRequest, actor: str) -> None:
        with contextlib.suppress(Exception):
            self.orch.discord.api_action(
                method="POST",
                endpoint="/v1/originate/text",
                actor=actor,
                status="succeeded",
                headline=req.headline,
                details={"event_code": req.event_code, "voice_mode": req.voice_mode},
            )
        if req.voice_mode == VoiceMode.FULL_EAS.value:
            with contextlib.suppress(Exception):
                self.orch.discord.alert_aired(
                    code=req.event_code,
                    event=req.headline,
                    source="SeasonalWeather (local API)",
                    mode="full",
                    area=", ".join(req.same_codes) if req.same_codes else "",
                )

    def _record_eas_action(self, req: OriginateAudioRequest, actor: str) -> None:
        if req.voice_mode != VoiceMode.FULL_EAS.value:
            return
        with contextlib.suppress(Exception):
            self.orch.discord.alert_aired(
                code=req.event_code,
                event=req.headline,
                source="SeasonalWeather (local API)",
                mode="full",
                area=", ".join(req.same_codes) if req.same_codes else "",
            )
