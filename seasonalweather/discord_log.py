"""
seasonalweather/discord_log.py

Centralised, non-blocking, rate-limited Discord embed poster.

All public methods are synchronous fire-and-forget: they enqueue a payload
and return immediately so the alert path is never delayed.

A single asyncio drain task (started by Orchestrator.run()) pulls from the
queue, applies per-URL token-bucket rate limiting, and posts to Discord.
On HTTP 429 the payload is re-enqueued after Retry-After expires.
The queue never drops; during severe outbreaks it drains at the rate Discord
allows (~20/min per webhook).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from typing import Any

import httpx2

log = logging.getLogger("seasonalweather.discord_log")

# ---------------------------------------------------------------------------
# NWS hazard-map sidebar colors (6-char hex, no #)
# Source: https://www.weather.gov/help-map
# ---------------------------------------------------------------------------
_NWS_COLORS: dict[str, str] = {
    # Tornado
    "TOR": "FF0000",
    "TOA": "FFFF00",
    # Severe Thunderstorm
    "SVR": "FF8C00",
    "SVA": "DB7093",
    # Flash Flood / Flood
    "FFW": "8B0000",
    "FFA": "2E8B57",
    "FLW": "00FF00",
    "FLA": "2E8B57",
    "FLS": "00FF00",
    "FFS": "8B0000",
    # Marine
    "SMW": "FF8C00",
    "CFW": "6495ED",
    "CFA": "6495ED",
    # Winter
    "WSW": "FF69B4",
    "WSA": "4682B4",
    "BZW": "FF4500",
    "FZW": "483D8B",
    "FZA": "00FFFF",
    "FSW": "483D8B",
    "SQW": "C0C0C0",
    "WW": "7B68EE",
    "ISW": "8B008B",
    # Wind
    "HWW": "DAA520",
    "HWA": "DAA520",
    "EWW": "FF8C00",
    "DSW": "FFE4B5",
    # Hurricane / Tropical / Surge
    "HUW": "DC143C",
    "HUA": "FF8C00",
    "TRW": "B22222",
    "TRA": "F4A460",
    "HLS": "FFE4B5",
    "SSW": "9400D3",
    "SSA": "DB7093",
    # Fire
    "FRW": "FF1493",
    "FWW": "FF0000",
    "FWA": "FF6347",
    # Civil / Emergency
    "CAE": "00FFFF",
    "CDW": "FFE4B5",
    "CEM": "FFD700",
    "LAE": "C0C0C0",
    "EAN": "000000",
    "EVI": "FF6347",
    "TOE": "C0C0C0",
    "SPW": "C0C0C0",
    "LEW": "C0C0C0",
    "BLU": "1E90FF",
    "ADR": "C0C0C0",
    "NMN": "C0C0C0",
    "MEP": "FF69B4",
    # Hazmat / Nuke / Radiological
    "HMW": "4B0082",
    "NUW": "4B0082",
    "RHW": "4B0082",
    # Earthquake / Volcano
    "EQW": "8B4513",
    "VOW": "8B4513",
    "VOA": "A0522D",
    # Tsunami / Avalanche
    "TSW": "1C00FF",
    "TSA": "00CED1",
    "AVW": "1E90FF",
    "AVA": "87CEEB",
    # Tests
    "RWT": "C0C0C0",
    "RMT": "A9A9A9",
    "DMO": "A9A9A9",
    # Statements / voice-only
    "SPS": "FFE4B5",
    "SVS": "00FF7F",
}

_DEFAULT_ALERT_COLOR = "4169E1"
_OPS_COLOR = "378ADD"
_API_SUCCESS_COLOR = "639922"
_API_FAIL_COLOR = "E24B4A"
_WARN_COLOR = "BA7517"
_ERROR_COLOR = "E24B4A"
_TERMINAL_COLOR = "888888"
_DETAIL_COLOR = "5D6FA3"
_HEALTH_OK_COLOR = "639922"

# ---------------------------------------------------------------------------
# EAS event code → Lucide icon name
# All icons confirmed present in lucide-static / lucide-react.
# ---------------------------------------------------------------------------
_ICON_MAP: dict[str, str] = {
    # Tornado / convective
    "TOR": "siren",
    "TOA": "siren",
    # Severe Thunderstorm
    "SVR": "cloud-lightning",
    "SVA": "cloud-lightning",
    # Flood
    "FFW": "waves",
    "FFA": "waves",
    "FLW": "waves",
    "FLA": "waves",
    "FLS": "waves",
    "FFS": "waves",
    # Marine
    "SMW": "anchor",
    "CFW": "anchor",
    "CFA": "anchor",
    # Winter / ice / snow / squall
    "WSW": "snowflake",
    "WSA": "snowflake",
    "BZW": "snowflake",
    "FZW": "snowflake",
    "FZA": "snowflake",
    "FSW": "snowflake",
    "SQW": "snowflake",
    "WW": "snowflake",
    "ISW": "snowflake",
    # Wind
    "HWW": "wind",
    "HWA": "wind",
    "EWW": "wind",
    "DSW": "wind",
    # Hurricane / tropical / surge
    "HUW": "wind",
    "HUA": "wind",
    "TRW": "wind",
    "TRA": "wind",
    "HLS": "wind",
    "SSW": "waves",
    "SSA": "waves",
    # Fire
    "FRW": "flame",
    "FWW": "flame",
    "FWA": "flame",
    # Civil / emergency
    "CAE": "shield-alert",
    "CDW": "shield-alert",
    "CEM": "shield-alert",
    "LAE": "shield-alert",
    "EAN": "shield-alert",
    "EVI": "shield-alert",
    "TOE": "phone-missed",
    "SPW": "shield",
    "LEW": "shield",
    "BLU": "shield",
    "MEP": "shield-alert",
    # Hazmat / nuke / rad
    "HMW": "triangle-alert",
    "NUW": "triangle-alert",
    "RHW": "triangle-alert",
    # Earthquake / volcano
    "EQW": "mountain",
    "VOW": "mountain",
    "VOA": "mountain",
    # Tsunami / avalanche
    "TSW": "waves",
    "TSA": "waves",
    "AVW": "mountain-snow",
    "AVA": "mountain-snow",
    # Tests
    "RWT": "radio",
    "RMT": "radio",
    "DMO": "radio",
    # Statements / voice-only
    "SPS": "bell",
    "SVS": "bell",
    # Internal sentinels
    "_voice": "bell",
    "_expired": "bell-off",
    "_test": "radio",
    "_ops": "activity",
    "_api": "terminal",
    "_error": "circle-alert",
    "_startup": "power",
    "_stall": "wifi-off",
    "_decision": "list-checks",
    "_audio": "audio-lines",
    "_feed": "rss",
    "_default": "bell-ring",
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _color_int(hex_str: str) -> int:
    try:
        return int(hex_str.lstrip("#"), 16)
    except (ValueError, AttributeError):
        return 0x4169E1


def _alert_color_int(code: str) -> int:
    return _color_int(_NWS_COLORS.get((code or "").strip().upper(), _DEFAULT_ALERT_COLOR))


def _alert_color_hex(code: str) -> str:
    return _NWS_COLORS.get((code or "").strip().upper(), _DEFAULT_ALERT_COLOR)


def _icon_name(code: str, *, sentinel: str | None = None) -> str:
    if sentinel:
        return _ICON_MAP.get(sentinel, _ICON_MAP["_default"])
    return _ICON_MAP.get((code or "").strip().upper(), _ICON_MAP["_default"])


def _icon_url(cdn_base: str, icon: str, hex_color: str) -> str | None:
    if not cdn_base:
        return None
    return f"{cdn_base.rstrip('/')}/icon?icon={icon}&hex={hex_color.lstrip('#').upper()}"


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _area_display(area: str) -> str:
    """Format a semicolon-separated area string as ' · ' joined inline code."""
    parts = [p.strip() for p in (area or "").split(";") if p.strip()]
    joined = " · ".join(parts) if parts else (area or "").strip()
    return f"`{joined}`" if joined else "—"


def _same_codes_display(same_codes: list[str] | None) -> str:
    codes = [str(c).strip() for c in (same_codes or []) if str(c).strip()]
    if not codes:
        return "—"
    shown = " · ".join(f"`{c}`" for c in codes[:8])
    extra = len(codes) - 8
    if extra > 0:
        shown += f" · +{extra} more"
    return shown


def _items_display(items: list[str] | tuple[str, ...] | set[str] | None, *, limit: int = 8) -> str:
    vals = [str(v).strip() for v in (items or []) if str(v).strip()]
    if not vals:
        return "—"
    shown = " · ".join(f"`{v}`" for v in vals[:limit])
    extra = len(vals) - limit
    if extra > 0:
        shown += f" · +{extra} more"
    return shown


def _detail_value(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple, set)):
        return _items_display([str(v) for v in value])
    return str(value).strip() or "—"


# ---------------------------------------------------------------------------
# Token bucket — per webhook URL
# ---------------------------------------------------------------------------


class _TokenBucket:
    """
    Leaky token bucket: `capacity` = max burst, refills at `rate_per_minute / 60`
    tokens per second. consume() returns 0.0 if a token is available immediately,
    or the fractional seconds to wait until one is.
    """

    def __init__(self, rate_per_minute: int) -> None:
        self._capacity = float(max(1, rate_per_minute))
        self._tokens = self._capacity
        self._rate = self._capacity / 60.0  # tokens / second
        self._last = time.monotonic()

    def consume(self) -> float:
        now = time.monotonic()
        self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
        self._last = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return 0.0
        return (1.0 - self._tokens) / self._rate


# ---------------------------------------------------------------------------
# DiscordLogger
# ---------------------------------------------------------------------------


class DiscordLogger:
    """
    Fire-and-forget Discord embed poster for SeasonalWeather.

    Usage:
        # In Orchestrator.__init__:
        self.discord = DiscordLogger.from_config(cfg.logs.discord)

        # SeasonalWeatherServiceRuntime registers self.discord.start() with
        # the controller task supervisor.

        # Anywhere in the codebase:
        self.discord.alert_aired(code="TOR", event="Tornado Warning", ...)
    """

    def __init__(
        self,
        *,
        alerts_url: str = "",
        alerts_enabled: bool = True,
        ops_url: str = "",
        ops_enabled: bool = True,
        api_url: str = "",
        api_enabled: bool = True,
        errors_url: str = "",
        errors_enabled: bool = True,
        rate_limit_per_minute: int = 20,
        post_tests: bool = True,
        post_voice_only: bool = True,
        cycle_rebuild_log: bool = True,
        alerttracker_lifecycle_log: bool = False,
        ops_detail_log: bool = False,
        source_health_log: bool = True,
        icon_cdn_url: str = "",
    ) -> None:
        self._alerts_url = alerts_url.strip()
        self._alerts_enabled = alerts_enabled and bool(self._alerts_url)
        self._ops_url = ops_url.strip()
        self._ops_enabled = ops_enabled and bool(self._ops_url)
        self._api_url = api_url.strip()
        self._api_enabled = api_enabled and bool(self._api_url)
        self._errors_url = errors_url.strip()
        self._errors_enabled = errors_enabled and bool(self._errors_url)

        self._rate_limit = max(1, rate_limit_per_minute)
        self._post_tests = post_tests
        self._post_voice = post_voice_only
        self._cycle_log = cycle_rebuild_log
        self._tracker_log = alerttracker_lifecycle_log
        self._ops_detail_log = ops_detail_log
        self._source_health_log = source_health_log
        self._icon_cdn = icon_cdn_url.strip()

        self._queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
        self._buckets: dict[str, _TokenBucket] = {}
        self._client: httpx2.AsyncClient | None = None

    @classmethod
    def from_config(cls, cfg: Any) -> "DiscordLogger":
        """Construct from a LogsDiscordConfig dataclass."""
        if not cfg.enabled:
            return cls()  # All URLs empty → nothing fires
        return cls(
            alerts_url=cfg.alerts_url,
            alerts_enabled=cfg.alerts_enabled,
            ops_url=cfg.ops_url,
            ops_enabled=cfg.ops_enabled,
            api_url=cfg.api_url,
            api_enabled=cfg.api_enabled,
            errors_url=cfg.errors_url,
            errors_enabled=cfg.errors_enabled,
            rate_limit_per_minute=cfg.rate_limit_per_minute,
            post_tests=cfg.post_tests,
            post_voice_only=cfg.post_voice_only,
            cycle_rebuild_log=cfg.cycle_rebuild_log,
            alerttracker_lifecycle_log=cfg.alerttracker_lifecycle_log,
            ops_detail_log=getattr(cfg, "ops_detail_log", False),
            source_health_log=getattr(cfg, "source_health_log", True),
            icon_cdn_url=cfg.icon_cdn_url,
        )

    def _any_enabled(self) -> bool:
        return any([self._alerts_enabled, self._ops_enabled, self._api_enabled, self._errors_enabled])

    def _bucket(self, url: str) -> _TokenBucket:
        if url not in self._buckets:
            self._buckets[url] = _TokenBucket(self._rate_limit)
        return self._buckets[url]

    async def _ensure_client(self) -> httpx2.AsyncClient:
        if self._client is None:
            self._client = httpx2.AsyncClient(
                timeout=httpx2.Timeout(10.0, connect=5.0),
                headers={"User-Agent": "SeasonalWeather/1.0"},
            )
        return self._client

    def _enqueue(self, url: str, payload: dict) -> None:
        if not url:
            return
        self._queue.put_nowait((url, payload))

    # ------------------------------------------------------------------
    # Background drain task
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """
        Long-running drain coroutine registered by the controller supervisor.
        """
        if not self._any_enabled():
            log.info("Discord webhook logging: disabled (no URLs configured or all disabled)")
            return

        log.info(
            "Discord webhook logging started (alerts=%s ops=%s api=%s errors=%s rate=%d/min icon_cdn=%s)",
            self._alerts_enabled,
            self._ops_enabled,
            self._api_enabled,
            self._errors_enabled,
            self._rate_limit,
            self._icon_cdn or "(none)",
        )

        while True:
            try:
                url, payload = await self._queue.get()
                bucket = self._bucket(url)
                wait = bucket.consume()
                if wait > 0:
                    await asyncio.sleep(wait)

                try:
                    client = await self._ensure_client()
                    r = await client.post(url, json=payload)
                    if r.status_code == 429:
                        try:
                            retry_after = float(r.headers.get("Retry-After", "5"))
                        except (ValueError, TypeError):
                            retry_after = 5.0
                        log.warning(
                            "Discord webhook 429; re-enqueuing after %.1fs (url=%.50s...)",
                            retry_after,
                            url,
                        )
                        await asyncio.sleep(retry_after)
                        self._enqueue(url, payload)
                    elif r.status_code not in (200, 204):
                        log.warning(
                            "Discord webhook unexpected status %d (url=%.50s...)",
                            r.status_code,
                            url,
                        )
                except Exception:
                    log.exception("Discord webhook post failed (url=%.50s...)", url)
            except Exception:
                log.exception("Discord log drain loop error")

    async def aclose(self) -> None:
        """Close the current HTTP client during bounded controller shutdown."""
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    # ------------------------------------------------------------------
    # Internal embed / payload builders
    # ------------------------------------------------------------------

    def _icon_url(self, icon: str, hex_color: str) -> str | None:
        return _icon_url(self._icon_cdn, icon, hex_color)

    @staticmethod
    def _field(name: str, value: str, *, inline: bool = True) -> dict:
        return {"name": name, "value": (value or "—")[:1024], "inline": inline}

    @staticmethod
    def _embed(
        *,
        color: int,
        title: str,
        description: str = "",
        fields: list[dict] | None = None,
        footer_text: str = "",
        thumbnail_url: str | None = None,
        author_name: str = "",
        author_icon_url: str | None = None,
        timestamp: str | None = None,
    ) -> dict:
        e: dict[str, Any] = {"color": color, "title": title[:256]}
        if description:
            e["description"] = description[:4096]
        if fields:
            e["fields"] = fields[:25]
        if author_name:
            a: dict[str, Any] = {"name": author_name[:256]}
            if author_icon_url:
                a["icon_url"] = author_icon_url
            e["author"] = a
        if thumbnail_url:
            e["thumbnail"] = {"url": thumbnail_url}
        if footer_text:
            e["footer"] = {"text": footer_text[:2048]}
        e["timestamp"] = timestamp or _now_iso()
        return e

    @staticmethod
    def _payload(embeds: list[dict]) -> dict:
        return {"username": "SeasonalWeather", "embeds": embeds[:10]}

    def _detail_fields(
        self,
        details: dict | None,
        *,
        limit: int = 8,
        inline: bool = True,
    ) -> list[dict]:
        fields: list[dict] = []
        for key, value in list((details or {}).items())[:limit]:
            name = str(key).replace("_", " ").strip().title() or "Detail"
            fields.append(self._field(name, _detail_value(value), inline=inline))
        return fields

    # ------------------------------------------------------------------
    # ── ALERTS CHANNEL ────────────────────────────────────────────────
    # ------------------------------------------------------------------

    def alert_aired(
        self,
        *,
        code: str,
        event: str,
        source: str,
        mode: str,
        area: str = "",
        same_codes: list[str] | None = None,
        vtec: list[str] | None = None,
        expires: str = "",
        is_test: bool = False,
        is_ern: bool = False,
    ) -> None:
        """
        A full or voice-only alert has been aired.

        mode:  "full" | "voice" | "voice_only"
        source: human-readable label, e.g. "CAP + NWWS-OI", "NWWS-OI", "ERN/GWES"
        """
        if not self._alerts_enabled:
            return
        if is_test and not self._post_tests:
            return
        mode_l = (mode or "").strip().lower()
        is_voice = mode_l in {"voice", "voice_only"}
        if is_voice and not self._post_voice:
            return

        code_u = (code or "SPS").strip().upper()
        hex_c = _alert_color_hex(code_u)
        color = _color_int(hex_c)

        if is_test:
            icon = _icon_name(code_u, sentinel="_test")
        else:
            # Voice-only alert presentations still use the underlying hazard icon.
            icon = _icon_name(code_u)

        thumb = self._icon_url(icon, hex_c)

        event_str = (event or code_u).strip()
        mode_display = "Originated (scheduled test)" if is_test else ("FULL + SAME" if not is_voice else "Voice-only")
        if is_ern:
            mode_display += " (ERN relay)"

        title = f"{event_str} — {'originated' if is_test else 'aired'}"
        description = ""
        if is_test:
            description = "Routine local test of the SeasonalWeather alert stream."
        elif is_ern:
            description = "Relayed from the configured ERN/GWES monitor."

        fields: list[dict] = [
            self._field("Source", source, inline=True),
            self._field("Mode", mode_display, inline=True),
            self._field("Code", code_u, inline=True),
        ]
        if area:
            fields.append(self._field("Coverage" if (is_test or is_ern) else "Area", _area_display(area), inline=False))
        if same_codes:
            fields.append(self._field("SAME", _same_codes_display(same_codes), inline=False))
        if expires:
            fields.append(self._field("Expires", expires, inline=True))
        for v in (vtec or [])[:2]:
            fields.append(self._field("VTEC", f"`{v}`", inline=False))

        src_slug = "local" if is_test else "ern" if is_ern else "cap" if "cap" in source.lower() else "nwws"
        embed = self._embed(
            color=color,
            title=title,
            description=description,
            fields=fields,
            footer_text=f"SeasonalWeather · {src_slug} · alerts",
            thumbnail_url=thumb,
        )
        self._enqueue(self._alerts_url, self._payload([embed]))

    def alert_updated(
        self,
        *,
        code: str,
        event: str,
        vtec_action: str,
        source: str = "CAP",
        area: str = "",
        vtec: list[str] | None = None,
    ) -> None:
        """CON/EXT/EXA/EXB — voice-only continuation/extension, no retone."""
        if not self._alerts_enabled or not self._post_voice:
            return

        code_u = (code or "SPS").strip().upper()
        hex_c = _alert_color_hex(code_u)
        color = _color_int(hex_c)
        thumb = self._icon_url(_icon_name(code_u), hex_c)

        action_labels = {
            "CON": "continuing",
            "EXT": "extended",
            "EXA": "expanded",
            "EXB": "expanded",
        }
        action_word = action_labels.get(vtec_action.upper(), vtec_action.lower())
        title = f"{(event or code_u).strip()} — {action_word}"

        fields: list[dict] = [
            self._field("VTEC action", vtec_action.upper(), inline=True),
            self._field("Source", source, inline=True),
            self._field("Mode", "Voice-only (no retone)", inline=True),
        ]
        if area:
            fields.append(self._field("Area", _area_display(area), inline=False))
        for v in (vtec or [])[:1]:
            fields.append(self._field("VTEC", f"`{v}`", inline=False))

        embed = self._embed(
            color=color,
            title=title,
            fields=fields,
            footer_text=f"SeasonalWeather · {source.lower()} update · alerts",
            thumbnail_url=thumb,
        )
        self._enqueue(self._alerts_url, self._payload([embed]))

    def alert_expired(
        self,
        *,
        code: str,
        event: str,
        vtec_action: str,
        source: str = "CAP",
        area: str = "",
        vtec: list[str] | None = None,
    ) -> None:
        """CAN/EXP where all in-scope tracks are terminal: gray, event icon."""
        if not self._alerts_enabled:
            return

        code_u = (code or "SPS").strip().upper()
        color = _color_int(_TERMINAL_COLOR)
        thumb = self._icon_url(_icon_name(code_u), _TERMINAL_COLOR)

        action_labels = {"CAN": "cancelled", "EXP": "expired"}
        action_word = action_labels.get(vtec_action.upper(), vtec_action.lower())
        title = f"{(event or code_u).strip()} — {action_word}"

        fields: list[dict] = [
            self._field("VTEC action", vtec_action.upper(), inline=True),
            self._field("Source", source, inline=True),
            self._field("Mode", "Voice-only (no retone)", inline=True),
        ]
        if area:
            fields.append(self._field("Area", _area_display(area), inline=False))
        for v in (vtec or [])[:1]:
            fields.append(self._field("VTEC", f"`{v}`", inline=False))

        embed = self._embed(
            color=color,
            title=title,
            fields=fields,
            footer_text=f"SeasonalWeather · {source.lower()} · alerts",
            thumbnail_url=thumb,
        )
        self._enqueue(self._alerts_url, self._payload([embed]))

    def alert_partial_terminal(
        self,
        *,
        code: str,
        event: str,
        vtec_action: str,
        source: str = "CAP",
        area: str = "",
        vtec: list[str] | None = None,
        ended_tracks: list[str] | None = None,
        continuing_tracks: list[str] | None = None,
        mode: str = "voice",
    ) -> None:
        """CAN/EXP mixed with non-terminal tracks: event color/icon, partial title."""
        if not self._alerts_enabled or not self._post_voice:
            return

        code_u = (code or "SPS").strip().upper()
        hex_c = _alert_color_hex(code_u)
        color = _color_int(hex_c)
        thumb = self._icon_url(_icon_name(code_u), hex_c)

        action_labels = {"CAN": "partially cancelled", "EXP": "partially expired"}
        action_word = action_labels.get(vtec_action.upper(), f"partial {vtec_action.lower()}")
        title = f"{(event or code_u).strip()} — {action_word}"

        ended = [str(t).strip() for t in (ended_tracks or []) if str(t).strip()]
        continuing = [str(t).strip() for t in (continuing_tracks or []) if str(t).strip()]

        mode_l = (mode or "").strip().lower()
        mode_display = "FULL + SAME mixed lifecycle" if mode_l == "full" else "Voice-only partial lifecycle (no retone)"

        fields: list[dict] = [
            self._field("VTEC action", vtec_action.upper(), inline=True),
            self._field("Source", source, inline=True),
            self._field("Mode", mode_display, inline=True),
        ]
        if area:
            fields.append(self._field("Area", _area_display(area), inline=False))
        if ended:
            shown = " · ".join(f"`{t}`" for t in ended[:6])
            if len(ended) > 6:
                shown += f" · +{len(ended) - 6} more"
            fields.append(self._field("Ended tracks", shown, inline=False))
        if continuing:
            shown = " · ".join(f"`{t}`" for t in continuing[:6])
            if len(continuing) > 6:
                shown += f" · +{len(continuing) - 6} more"
            fields.append(self._field("Continuing tracks", shown, inline=False))
        for v in (vtec or [])[:2]:
            fields.append(self._field("VTEC", f"`{v}`", inline=False))

        embed = self._embed(
            color=color,
            title=title,
            fields=fields,
            footer_text=f"SeasonalWeather · {source.lower()} partial lifecycle · alerts",
            thumbnail_url=thumb,
        )
        self._enqueue(self._alerts_url, self._payload([embed]))

    # ------------------------------------------------------------------
    # ── OPS CHANNEL ───────────────────────────────────────────────────
    # ------------------------------------------------------------------

    def service_started(
        self,
        *,
        cap_enabled: bool = False,
        ern_enabled: bool = False,
        tests_enabled: bool = False,
        mode: str = "normal",
    ) -> None:
        if not self._ops_enabled:
            return
        thumb = self._icon_url(_ICON_MAP["_startup"], _OPS_COLOR)
        fields = [
            self._field("CAP", "Enabled" if cap_enabled else "Disabled", inline=True),
            self._field("ERN", "Enabled" if ern_enabled else "Disabled", inline=True),
            self._field("RWT/RMT", "Enabled" if tests_enabled else "Disabled", inline=True),
            self._field("Mode", mode.capitalize(), inline=True),
        ]
        embed = self._embed(
            color=_color_int(_OPS_COLOR),
            title="Service started",
            description="Orchestrator online. Liquidsoap reachable.",
            fields=fields,
            footer_text="SeasonalWeather · startup · ops",
            thumbnail_url=thumb,
        )
        self._enqueue(self._ops_url, self._payload([embed]))

    def cycle_rebuilt(
        self,
        *,
        reason: str = "scheduled",
        mode: str = "normal",
        interval: int = 300,
        seq_dur: float = 0.0,
        segments: int = 0,
        active_alerts: int = 0,
        old_mode: str = "",
        new_mode: str = "",
        added: list[str] | None = None,
        removed: list[str] | None = None,
        order_preview: list[str] | None = None,
    ) -> None:
        if not self._ops_enabled or not self._cycle_log:
            return
        thumb = self._icon_url(_ICON_MAP["_ops"], _OPS_COLOR)
        dur_str = f"{int(seq_dur // 60)}m {int(seq_dur % 60)}s" if seq_dur > 0 else "—"
        fields: list[dict] = [
            self._field("Reason", reason, inline=True),
            self._field("Mode", mode.capitalize(), inline=True),
            self._field("Interval", f"{interval}s", inline=True),
            self._field("Seq duration", dur_str, inline=True),
            self._field("Segments", str(segments), inline=True),
        ]
        if active_alerts:
            fields.append(self._field("Active alerts", str(active_alerts), inline=True))
        if old_mode or new_mode:
            fields.append(self._field("Mode change", f"{old_mode or '—'} → {new_mode or mode}", inline=True))
        if added:
            fields.append(self._field("Added", _items_display(added, limit=8), inline=False))
        if removed:
            fields.append(self._field("Removed", _items_display(removed, limit=8), inline=False))
        if order_preview:
            fields.append(self._field("Order preview", _items_display(order_preview, limit=10), inline=False))
        embed = self._embed(
            color=_color_int(_OPS_COLOR),
            title="Cycle rebuilt",
            fields=fields,
            footer_text="SeasonalWeather · cycle · ops",
            thumbnail_url=thumb,
        )
        self._enqueue(self._ops_url, self._payload([embed]))

    def source_health(
        self,
        *,
        source: str,
        status: str,
        message: str = "",
        severity: str = "info",
        details: dict | None = None,
    ) -> None:
        """Post a source health transition or startup source-state summary."""
        if not self._ops_enabled or not self._source_health_log:
            return

        sev = (severity or "info").strip().lower()
        status_l = (status or "status").strip().lower()
        if sev in {"ok", "success", "recovered"} or status_l in {"ok", "online", "enabled", "connected", "recovered"}:
            hex_c = _HEALTH_OK_COLOR
            icon = _ICON_MAP["_ops"]
        elif sev in {"warning", "warn", "degraded"} or status_l in {"disabled", "degraded", "stalled", "reconnecting"}:
            hex_c = _WARN_COLOR
            icon = _ICON_MAP["_stall"] if status_l in {"stalled", "reconnecting"} else _ICON_MAP["_ops"]
        elif sev in {"error", "failed", "fail"} or status_l in {"failed", "error"}:
            hex_c = _ERROR_COLOR
            icon = _ICON_MAP["_error"]
        else:
            hex_c = _OPS_COLOR
            icon = _ICON_MAP["_ops"]

        fields = [self._field("Status", status or "—", inline=True)]
        fields.extend(self._detail_fields(details, limit=6, inline=True))
        embed = self._embed(
            color=_color_int(hex_c),
            title=f"{(source or 'Source').strip()} — {status or 'status'}",
            description=message[:300] if message else "",
            fields=fields,
            footer_text="SeasonalWeather · source health · ops",
            thumbnail_url=self._icon_url(icon, hex_c),
        )
        self._enqueue(self._ops_url, self._payload([embed]))

    def alert_decision(
        self,
        *,
        source: str,
        result: str,
        reason: str = "",
        event: str = "",
        code: str = "",
        product_type: str = "",
        mode: str = "",
        awips: str = "",
        wfo: str = "",
        same_targets: int | None = None,
        zones: int | None = None,
        vtec: list[str] | None = None,
        details: dict | None = None,
    ) -> None:
        """Structured alert routing/targeting decision for the ops channel."""
        if not self._ops_enabled or not self._ops_detail_log:
            return

        result_l = (result or "decision").strip().lower()
        if result_l in {"air", "aired", "admit", "accepted"}:
            hex_c = _HEALTH_OK_COLOR
        elif result_l in {"skip", "suppress", "dedupe", "drop", "reject"}:
            hex_c = _WARN_COLOR
        elif result_l in {"fail", "failed", "error"}:
            hex_c = _ERROR_COLOR
        else:
            hex_c = _DETAIL_COLOR

        title_bits = [(source or "Alert").strip(), "decision"]
        if result:
            title_bits.append(f"— {result}")
        fields: list[dict] = [self._field("Result", result or "—", inline=True)]
        if reason:
            fields.append(self._field("Reason", reason, inline=True))
        if mode:
            fields.append(self._field("Mode", mode.upper(), inline=True))
        if event:
            fields.append(self._field("Event", event, inline=False))
        if code:
            fields.append(self._field("Code", f"`{code}`", inline=True))
        if product_type and product_type != code:
            fields.append(self._field("Product", f"`{product_type}`", inline=True))
        if awips:
            fields.append(self._field("AWIPS", f"`{awips}`", inline=True))
        if wfo:
            fields.append(self._field("WFO", f"`{wfo}`", inline=True))
        if same_targets is not None:
            fields.append(self._field("SAME targets", str(same_targets), inline=True))
        if zones is not None:
            fields.append(self._field("UGC zones", str(zones), inline=True))
        for vv in (vtec or [])[:2]:
            fields.append(self._field("VTEC", f"`{vv}`", inline=False))
        fields.extend(self._detail_fields(details, limit=max(0, 25 - len(fields)), inline=True))

        embed = self._embed(
            color=_color_int(hex_c),
            title=" ".join(title_bits),
            fields=fields,
            footer_text="SeasonalWeather · alert decision · ops",
            thumbnail_url=self._icon_url(_ICON_MAP["_decision"], hex_c),
        )
        self._enqueue(self._ops_url, self._payload([embed]))

    def dedupe_event(
        self,
        *,
        source: str,
        result: str,
        key: str,
        event: str = "",
        code: str = "",
        mode: str = "",
        ttl_s: float | None = None,
    ) -> None:
        """Post a dedupe reservation/suppression event when detailed ops logging is enabled."""
        if not self._ops_enabled or not self._ops_detail_log:
            return
        result_l = (result or "").strip().lower()
        hex_c = _WARN_COLOR if result_l in {"hit", "skip", "suppressed"} else _DETAIL_COLOR
        fields = [
            self._field("Result", result or "—", inline=True),
            self._field("Key", f"`{key[:180]}`", inline=False),
        ]
        if event:
            fields.append(self._field("Event", event, inline=False))
        if code:
            fields.append(self._field("Code", f"`{code}`", inline=True))
        if mode:
            fields.append(self._field("Mode", mode.upper(), inline=True))
        if ttl_s is not None:
            fields.append(self._field("TTL", f"{ttl_s:.0f}s", inline=True))
        embed = self._embed(
            color=_color_int(hex_c),
            title=f"{(source or 'Alert').strip()} dedupe — {result or 'event'}",
            fields=fields,
            footer_text="SeasonalWeather · dedupe · ops",
            thumbnail_url=self._icon_url(_ICON_MAP["_decision"], hex_c),
        )
        self._enqueue(self._ops_url, self._payload([embed]))

    def audio_pipeline(
        self,
        *,
        source: str,
        status: str,
        event: str = "",
        code: str = "",
        mode: str = "",
        path: str = "",
        duration_s: float | None = None,
        backend: str = "",
        cache: str = "",
        same_locs: int | None = None,
        fallback: str = "",
    ) -> None:
        """Post alert audio render/push pipeline results."""
        status_l = (status or "").strip().lower()
        failed = status_l in {"failed", "error"}
        if failed:
            if not self._errors_enabled:
                return
            url = self._errors_url
            hex_c = _ERROR_COLOR
            footer = "SeasonalWeather · audio pipeline · errors"
        else:
            if not self._ops_enabled or not self._ops_detail_log:
                return
            url = self._ops_url
            hex_c = _DETAIL_COLOR
            footer = "SeasonalWeather · audio pipeline · ops"

        fields: list[dict] = [
            self._field("Source", source or "—", inline=True),
            self._field("Status", status or "—", inline=True),
        ]
        if mode:
            fields.append(self._field("Mode", mode.upper(), inline=True))
        if event:
            fields.append(self._field("Event", event, inline=False))
        if code:
            fields.append(self._field("Code", f"`{code}`", inline=True))
        if duration_s is not None:
            fields.append(self._field("Duration", f"{duration_s:.1f}s", inline=True))
        if same_locs is not None:
            fields.append(self._field("SAME locs", str(same_locs), inline=True))
        if backend:
            fields.append(self._field("Backend", backend, inline=True))
        if cache:
            fields.append(self._field("Cache", cache, inline=True))
        if fallback:
            fields.append(self._field("Fallback", fallback, inline=False))
        if path:
            fields.append(self._field("Output", f"`{path[-180:]}`", inline=False))

        embed = self._embed(
            color=_color_int(hex_c),
            title=f"Audio pipeline — {status or 'event'}",
            fields=fields,
            footer_text=footer,
            thumbnail_url=self._icon_url(_ICON_MAP["_audio"], hex_c),
        )
        self._enqueue(url, self._payload([embed]))

    def station_feed_update(
        self,
        *,
        action: str,
        source: str,
        event: str = "",
        code: str = "",
        alert_id: str = "",
        active_count: int | None = None,
        details: dict | None = None,
    ) -> None:
        """Post handled-alerts feed update summaries when detailed ops logging is enabled."""
        if not self._ops_enabled or not self._ops_detail_log:
            return
        fields: list[dict] = [
            self._field("Action", action or "—", inline=True),
            self._field("Source", source or "—", inline=True),
        ]
        if code:
            fields.append(self._field("Code", f"`{code}`", inline=True))
        if event:
            fields.append(self._field("Event", event, inline=False))
        if alert_id:
            fields.append(self._field("Alert ID", f"`{alert_id[:180]}`", inline=False))
        if active_count is not None:
            fields.append(self._field("Active count", str(active_count), inline=True))
        fields.extend(self._detail_fields(details, limit=max(0, 25 - len(fields)), inline=True))
        embed = self._embed(
            color=_color_int(_DETAIL_COLOR),
            title=f"Station feed — {action or 'update'}",
            fields=fields,
            footer_text="SeasonalWeather · station feed · ops",
            thumbnail_url=self._icon_url(_ICON_MAP["_feed"], _DETAIL_COLOR),
        )
        self._enqueue(self._ops_url, self._payload([embed]))

    def mode_changed(
        self,
        *,
        old_mode: str,
        new_mode: str,
        reason: str = "",
    ) -> None:
        if not self._ops_enabled:
            return
        hex_c = _WARN_COLOR if new_mode == "heightened" else _OPS_COLOR
        thumb = self._icon_url(_ICON_MAP["_ops"], hex_c)
        fields: list[dict] = [
            self._field("Old mode", old_mode.capitalize(), inline=True),
            self._field("New mode", new_mode.capitalize(), inline=True),
        ]
        if reason:
            fields.append(self._field("Reason", reason, inline=False))
        embed = self._embed(
            color=_color_int(hex_c),
            title=f"Mode changed → {new_mode}",
            fields=fields,
            footer_text="SeasonalWeather · mode · ops",
            thumbnail_url=thumb,
        )
        self._enqueue(self._ops_url, self._payload([embed]))

    def nwws_stall(self, *, backoff_attempt: int = 1) -> None:
        if not self._ops_enabled:
            return
        thumb = self._icon_url(_ICON_MAP["_stall"], _WARN_COLOR)
        embed = self._embed(
            color=_color_int(_WARN_COLOR),
            title="NWWS stall — reconnecting",
            description=(
                "No messages received from nwws-oi.weather.gov within the "
                "configured stall window. Initiating reconnect with backoff."
            ),
            fields=[self._field("Backoff attempt", f"#{backoff_attempt}", inline=True)],
            footer_text="SeasonalWeather · nwws · ops",
            thumbnail_url=thumb,
        )
        self._enqueue(self._ops_url, self._payload([embed]))

    def nwws_reconnected(self) -> None:
        if not self._ops_enabled:
            return
        embed = self._embed(
            color=_color_int(_OPS_COLOR),
            title="NWWS reconnected",
            footer_text="SeasonalWeather · nwws · ops",
            thumbnail_url=self._icon_url(_ICON_MAP["_ops"], _OPS_COLOR),
        )
        self._enqueue(self._ops_url, self._payload([embed]))

    def alerttracker_lifecycle(
        self,
        *,
        loaded: int,
        purged: int,
        active: int,
    ) -> None:
        """Startup AlertTracker restore summary. Gated by alerttracker_lifecycle_log knob."""
        if not self._ops_enabled or not self._tracker_log:
            return
        embed = self._embed(
            color=_color_int(_OPS_COLOR),
            title="AlertTracker restored",
            fields=[
                self._field("Loaded", str(loaded), inline=True),
                self._field("Purged", str(purged), inline=True),
                self._field("Active", str(active), inline=True),
            ],
            footer_text="SeasonalWeather · alerttracker · ops",
            thumbnail_url=self._icon_url(_ICON_MAP["_ops"], _OPS_COLOR),
        )
        self._enqueue(self._ops_url, self._payload([embed]))

    # ------------------------------------------------------------------
    # ── API CHANNEL ───────────────────────────────────────────────────
    # ------------------------------------------------------------------

    def api_action(
        self,
        *,
        method: str,
        endpoint: str,
        actor: str,
        status: str,
        command_id: str = "",
        headline: str = "",
        details: dict | None = None,
        success: bool = True,
    ) -> None:
        if not self._api_enabled:
            return
        hex_c = _API_SUCCESS_COLOR if success else _API_FAIL_COLOR
        color = _color_int(hex_c)
        thumb = self._icon_url(_ICON_MAP["_api"], hex_c)

        title = f"{method.upper()} {endpoint}"
        if not success:
            title += " — rejected"

        fields: list[dict] = [
            self._field("Actor", actor or "unknown", inline=True),
            self._field("Status", status, inline=True),
        ]
        if command_id:
            fields.append(self._field("Command ID", f"`{command_id}`", inline=True))
        if headline:
            fields.append(self._field("Headline", headline, inline=False))
        if details:
            for k, v in list(details.items())[:4]:
                fields.append(self._field(str(k), str(v)[:100], inline=True))

        embed = self._embed(
            color=color,
            title=title,
            fields=fields,
            footer_text="SeasonalWeather · api",
            thumbnail_url=thumb,
        )
        self._enqueue(self._api_url, self._payload([embed]))

    # ------------------------------------------------------------------
    # ── ERRORS CHANNEL ────────────────────────────────────────────────
    # ------------------------------------------------------------------

    def error(
        self,
        *,
        title: str,
        module: str = "",
        exception_type: str = "",
        message: str = "",
        context: dict | None = None,
        fallback: str = "",
    ) -> None:
        if not self._errors_enabled:
            return
        thumb = self._icon_url(_ICON_MAP["_error"], _ERROR_COLOR)
        fields: list[dict] = []
        if module:
            fields.append(self._field("Module", module, inline=True))
        if exception_type:
            fields.append(self._field("Exception", exception_type, inline=True))
        if fallback:
            fields.append(self._field("Fallback", fallback, inline=False))
        if context:
            for k, v in list(context.items())[:4]:
                fields.append(self._field(str(k), str(v)[:100], inline=True))
        embed = self._embed(
            color=_color_int(_ERROR_COLOR),
            title=title,
            description=(message[:300] if message else ""),
            fields=fields or None,
            footer_text="SeasonalWeather · errors",
            thumbnail_url=thumb,
        )
        self._enqueue(self._errors_url, self._payload([embed]))

    def warning(
        self,
        *,
        title: str,
        module: str = "",
        message: str = "",
        context: dict | None = None,
    ) -> None:
        if not self._errors_enabled:
            return
        thumb = self._icon_url(_ICON_MAP["_error"], _WARN_COLOR)
        fields: list[dict] = []
        if module:
            fields.append(self._field("Module", module, inline=True))
        if context:
            for k, v in list(context.items())[:4]:
                fields.append(self._field(str(k), str(v)[:100], inline=True))
        embed = self._embed(
            color=_color_int(_WARN_COLOR),
            title=title,
            description=(message[:300] if message else ""),
            fields=fields or None,
            footer_text="SeasonalWeather · errors",
            thumbnail_url=thumb,
        )
        self._enqueue(self._errors_url, self._payload([embed]))
