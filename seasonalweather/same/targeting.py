from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import re
import shutil
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx2

from ..config import AppConfig
from ..broadcast.product_text import (
    extract_nwws_wcn_area_desc as _extract_nwws_wcn_area_desc,
    match_nwws_wcn_area_same as _match_nwws_wcn_area_same,
)
from . import ugc as _ugc
from .locations import filter_same_locations_to_service_area as _same_filter_locations_to_service_area

log = logging.getLogger("seasonalweather")


class SameTargetResolver:
    def __init__(
        self,
        *,
        cfg: AppConfig,
        local_tz: ZoneInfo,
        same_fips_allow_set: set[str],
    ) -> None:
        self.cfg = cfg
        self._tz = local_tz
        self._same_fips_allow_set = same_fips_allow_set
        self._cache_dir = Path(cfg.paths.cache_dir)

        self._zone_client: httpx2.AsyncClient | None = None
        self._zone_cache_same: dict[str, list[str]] = {}
        self._zone_cache_fail: dict[str, dt.datetime] = {}
        self._zone_lock = asyncio.Lock()

        self._zonecounty_lock = asyncio.Lock()
        self._zonecounty_loaded = False
        self._zonecounty_map: dict[str, list[str]] = {}

        self._mareas_lock = asyncio.Lock()
        self._mareas_loaded = False
        self._mareas_map: dict[str, list[str]] = {}

        self._same_name_cache: dict[str, str] = {}
        self._same_name_fail: dict[str, dt.datetime] = {}
        self._same_name_lock = asyncio.Lock()

    def _filter_same_locations_to_service_area(
        self,
        locs: list[str] | tuple[str, ...] | None,
        *,
        allow_statewide_input: bool = True,
    ) -> list[str]:
        """
        Keep ONLY SAME FIPS locations that are within our configured service area.

        State-wide 0SS000 inputs match when this service area contains at least
        one concrete county/city SAME code in that state.  Matching state-wide
        codes are preserved as 0SS000 for relay/output; they are not expanded
        into county lists.
        """
        return _same_filter_locations_to_service_area(
            locs,
            self._same_fips_allow_set,
            allow_statewide_input=allow_statewide_input,
        )

    def _state_to_fips2(self, st: str) -> str | None:
        s = (st or "").strip().upper()
        if not s:
            return None
        return _ugc.STATE_ABBR_TO_FIPS.get(s)

    def _same_from_state_county(self, state_abbr: str, county3: str) -> str | None:
        # Delegates to ugc library; retains signature for existing callers.
        c3 = "".join(ch for ch in (county3 or "") if ch.isdigit()).zfill(3)
        if len(c3) != 3:
            return None
        return _ugc.same_from_county_zone(f"{(state_abbr or '').strip().upper()}C{c3}")

    def _same6_to_county_zone_id(self, same6: str) -> tuple[str | None, str | None]:
        """Convert SAME PSSCCC (6 digits) to NWS county-zone ID like 'MDC031'."""
        s = "".join(ch for ch in str(same6 or "").strip() if ch.isdigit())
        if len(s) != 6:
            return (None, None)
        st_fips2 = s[1:3]  # ignore partition
        cty3 = s[3:6]
        st = _ugc.FIPS_TO_STATE_ABBR.get(st_fips2)
        if not st:
            return (None, None)
        return (f"{st}C{cty3}", st)

    async def _same6_area_label(self, same6: str) -> str | None:
        """Best-effort county label for SAME via api.weather.gov/zones/county/<ST>C### (cached)."""
        code = "".join(ch for ch in str(same6 or "").strip() if ch.isdigit())
        if len(code) != 6:
            return None
        hit = self._same_name_cache.get(code)
        if hit:
            return hit
        now = dt.datetime.now(tz=self._tz)
        fail_at = self._same_name_fail.get(code)
        if fail_at and (now - fail_at).total_seconds() < 300:
            return None
        zone_id, st = self._same6_to_county_zone_id(code)
        if not zone_id:
            return None
        async with self._same_name_lock:
            hit2 = self._same_name_cache.get(code)
            if hit2:
                return hit2
            fail_at2 = self._same_name_fail.get(code)
            if fail_at2 and (now - fail_at2).total_seconds() < 300:
                return None
            data = await self._get_zone_json("county", zone_id)
            if not data:
                self._same_name_fail[code] = now
                return None
            props = data.get("properties") if isinstance(data.get("properties"), dict) else {}
            name = str(props.get("name") or "").strip()
            state = str(props.get("state") or st or "").strip().upper()
            if not name:
                self._same_name_fail[code] = now
                return None
            label = f"{name}, {state}" if state else name
            self._same_name_cache[code] = label
            return label

    async def _sf_area_text_from_same_codes(self, same_codes: list[str]) -> str:
        """Resolve SAME codes to a '; '-joined area label string for station feed ERN items."""
        if not self.cfg.station_feed.ern_area_names:
            return ""
        codes = [str(x).strip() for x in (same_codes or []) if str(x).strip()]
        if not codes:
            return ""
        results = await asyncio.gather(*(self._same6_area_label(c) for c in codes), return_exceptions=True)
        out: list[str] = []
        seen: set[str] = set()
        for r in results:
            if isinstance(r, Exception) or not r:
                continue
            s = str(r).strip()
            if not s or s in seen:
                continue
            seen.add(s)
            out.append(s)
        return "; ".join(out)

    def _zonecounty_enabled(self) -> bool:
        return self.cfg.zonecounty.enabled

    def _zonecounty_dbx_url(self) -> str:
        return self.cfg.zonecounty.dbx_url.strip()

    def _zonecounty_cache_days(self) -> int:
        return self.cfg.zonecounty.cache_days

    def _zonecounty_dbx_path(self) -> Path:
        return self._cache_dir / "zonecounty.dbx"

    async def _ensure_zonecounty_loaded(self) -> None:
        # ZONECOUNTY_DBX_DISCOVERY_PATCH_v1
        # Hard rule: never delete the last-known-good DBX on a failed refresh.
        if self._zonecounty_loaded:
            return

        async with self._zonecounty_lock:
            if self._zonecounty_loaded:
                return

            if not self._zonecounty_enabled():
                self._zonecounty_loaded = True
                return

            dbx_path = self._zonecounty_dbx_path()
            dbx_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = dbx_path.with_suffix(".tmp")
            lastgood_path = dbx_path.with_suffix(".lastgood")

            url = (self._zonecounty_dbx_url() or "").strip()
            max_age_days = max(1, int(self._zonecounty_cache_days()))
            now = dt.datetime.now(tz=self._tz)

            def _cache_is_fresh() -> bool:
                if not dbx_path.exists():
                    return False
                try:
                    mtime = dt.datetime.fromtimestamp(dbx_path.stat().st_mtime, tz=self._tz)
                    age_s = (now - mtime).total_seconds()
                    return age_s <= (max_age_days * 86400) and dbx_path.stat().st_size > 1024
                except Exception:
                    return False

            async def _try_fetch(candidate_url: str) -> dict[str, list[str]] | None:
                try:
                    client = await self._ensure_zone_client()  # reuse UA/timeouts/headers
                    r = await client.get(candidate_url)
                    if r.status_code != 200 or not r.content or len(r.content) <= 1024:
                        log.warning(
                            "ZoneCounty DBX fetch failed (status=%s url=%s).",
                            r.status_code,
                            candidate_url,
                        )
                        return None

                    # Write to temp then validate by parsing.
                    tmp_path.write_bytes(r.content)
                    parsed = await asyncio.to_thread(_ugc.parse_zonecounty_dbx, tmp_path)

                    if not parsed:
                        log.warning("ZoneCounty DBX candidate parsed 0 zones (url=%s).", candidate_url)
                        try:
                            tmp_path.unlink(missing_ok=True)
                        except Exception:
                            pass
                        return None

                    # Backup lastgood and atomically replace.
                    if dbx_path.exists():
                        try:
                            shutil.copy2(dbx_path, lastgood_path)
                        except Exception:
                            log.warning("ZoneCounty lastgood backup failed (file=%s).", dbx_path)

                    os.replace(str(tmp_path), str(dbx_path))
                    log.info("ZoneCounty DBX refreshed: %s (%d bytes) src=%s", dbx_path, len(r.content), candidate_url)
                    return parsed
                except Exception:
                    log.exception("ZoneCounty DBX download/validate failed (url=%s).", candidate_url)
                    return None

            # If cache is fresh, just load it and bail early.
            if _cache_is_fresh():
                try:
                    self._zonecounty_map = await asyncio.to_thread(_ugc.parse_zonecounty_dbx, dbx_path)
                    log.info("ZoneCounty loaded: zones=%d file=%s", len(self._zonecounty_map), dbx_path)
                except Exception:
                    log.exception("ZoneCounty parse failed; disabling ZoneCounty mapping for this run")
                    self._zonecounty_map = {}
                self._zonecounty_loaded = True
                return

            # Refresh path: try explicit URL first (unless 'auto'), then discover from index page.
            updated_map: dict[str, list[str]] | None = None

            if url and url.lower() != "auto":
                updated_map = await _try_fetch(url)

            if updated_map is None:
                # Discovery: scrape https://www.weather.gov/gis/ZoneCounty for bp*.dbx tokens.
                index_url = (self.cfg.zonecounty.index_url or "").strip()
                base_url = (self.cfg.zonecounty.base_url or "").strip()
                if base_url and not base_url.endswith("/"):
                    base_url += "/"

                token_re = re.compile(r"\bbp\d{2}[a-z]{2}\d{2}\.dbx\b", re.IGNORECASE)
                mon_map = {
                    "ja": 1,
                    "fe": 2,
                    "mr": 3,
                    "ap": 4,
                    "my": 5,
                    "jn": 6,
                    "jl": 7,
                    "au": 8,
                    "se": 9,
                    "oc": 10,
                    "no": 11,
                    "de": 12,
                }

                def tok_key(tok: str) -> tuple[int, int, int]:
                    # bp18mr25.dbx -> (2025, 3, 18)
                    try:
                        t = tok.lower()
                        dd = int(t[2:4])
                        mm = mon_map.get(t[4:6], 0)
                        yy = 2000 + int(t[6:8])
                        return (yy, mm, dd)
                    except Exception:
                        return (0, 0, 0)

                try:
                    client = await self._ensure_zone_client()
                    r = await client.get(index_url)
                    if r.status_code == 200 and r.text:
                        toks = sorted(
                            {m.group(0).lower() for m in token_re.finditer(r.text)}, key=tok_key, reverse=True
                        )
                        # Try newest-first; cap tries to avoid hammering.
                        for tok in toks[:20]:
                            cand = base_url + tok
                            updated_map = await _try_fetch(cand)
                            if updated_map is not None:
                                break
                    else:
                        log.warning("ZoneCounty discovery fetch failed (status=%s url=%s).", r.status_code, index_url)
                except Exception:
                    log.exception("ZoneCounty discovery failed (index_url=%s).", index_url)

            # Load from updated_map if we refreshed successfully, otherwise fall back to existing cache.
            if updated_map is not None:
                self._zonecounty_map = updated_map
                self._zonecounty_loaded = True
                return

            if not dbx_path.exists():
                log.warning("ZoneCounty DBX not available (no cache file). Zone->SAME mapping will be unavailable.")
                self._zonecounty_loaded = True
                self._zonecounty_map = {}
                return

            try:
                self._zonecounty_map = await asyncio.to_thread(_ugc.parse_zonecounty_dbx, dbx_path)
                log.info("ZoneCounty loaded: zones=%d file=%s", len(self._zonecounty_map), dbx_path)
            except Exception:
                log.exception("ZoneCounty parse failed; disabling ZoneCounty mapping for this run")
                self._zonecounty_map = {}

            self._zonecounty_loaded = True

    def _mareas_enabled(self) -> bool:
        return self.cfg.mareas.enabled

    def _mareas_url(self) -> str:
        """Optional URL for a mareas*.txt style crosswalk."""
        return self.cfg.mareas.url.strip()

    def _mareas_cache_days(self) -> int:
        return self.cfg.mareas.cache_days

    def _mareas_path(self) -> Path:
        return self._cache_dir / "mareas.txt"

    async def _ensure_mareas_loaded(self) -> None:
        if self._mareas_loaded:
            return

        async with self._mareas_lock:
            if self._mareas_loaded:
                return

            if not self._mareas_enabled():
                self._mareas_loaded = True
                self._mareas_map = {}
                return

            path = self._mareas_path()
            path.parent.mkdir(parents=True, exist_ok=True)

            url = self._mareas_url()
            max_age_days = max(1, int(self._mareas_cache_days()))
            now = dt.datetime.now(tz=self._tz)

            need_download = True
            if path.exists():
                try:
                    mtime = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=self._tz)
                    need_download = (now - mtime).total_seconds() > (max_age_days * 86400)
                except Exception:
                    need_download = True

            if url and need_download:
                try:
                    client = await self._ensure_zone_client()
                    r = await client.get(url)
                    if r.status_code == 200 and r.content and len(r.content) > 256:
                        tmp = path.with_suffix(".tmp")
                        tmp.write_bytes(r.content)
                        tmp.replace(path)
                        log.info("Marine areas .txt database refreshed: %s (%d bytes)", path, len(r.content))
                    else:
                        log.warning(
                            "Marine areas .txt database fetch failed (status=%s). Using cache if present.",
                            r.status_code,
                        )
                except Exception:
                    log.exception("Marine areas .txt database download failed; using cache if present")

            if not path.exists():
                log.info(
                    "Marine areas .txt database not available (no cache file). Marine zone->SAME mapping unavailable."
                )
                self._mareas_loaded = True
                self._mareas_map = {}
                return

            try:
                self._mareas_map = await asyncio.to_thread(_ugc.parse_mareas_txt, path)
                log.info("Marine areas loaded: zones=%d file=%s", len(self._mareas_map), path)
            except Exception:
                log.exception("Marine areas parse failed; disabling marine mapping for this run")
                self._mareas_map = {}

            self._mareas_loaded = True

    async def _ensure_zone_client(self) -> httpx2.AsyncClient:
        if self._zone_client is not None:
            return self._zone_client

        # Use an explicit UA for NWS (required by their policy).
        ua = (self.cfg.nws.user_agent or "").strip()
        if not ua:
            ua = (self.cfg.cap.user_agent or "").strip()
        if not ua:
            ua = "SeasonalWeather (NWS zone mapper)"

        self._zone_client = httpx2.AsyncClient(
            timeout=httpx2.Timeout(15.0, connect=8.0),
            headers={
                "User-Agent": ua,
                "Accept": "application/geo+json, application/json;q=0.9, */*;q=0.8",
            },
        )
        return self._zone_client

    async def _get_zone_json(self, zone_type: str, zone_id: str) -> dict | None:
        """
        Fetch https://api.weather.gov/zones/<zone_type>/<zone_id>
        Returns parsed JSON dict on success, else None.
        """
        zt = (zone_type or "").strip().lower()
        zid = (zone_id or "").strip().upper()
        if not zt or not zid:
            return None

        url = f"https://api.weather.gov/zones/{zt}/{zid}"
        client = await self._ensure_zone_client()

        try:
            r = await client.get(url)
            if r.status_code != 200:
                return None
            return r.json()
        except Exception:
            return None

    def _same_list_from_zone_json(self, data: dict) -> list[str]:
        """
        Prefer geocode.SAME if present. Otherwise, fall back to parsing county URLs.
        """
        if not isinstance(data, dict):
            return []
        props = data.get("properties") if isinstance(data.get("properties"), dict) else {}
        geo = props.get("geocode") if isinstance(props.get("geocode"), dict) else {}

        same_vals = geo.get("SAME") if isinstance(geo.get("SAME"), (list, tuple)) else None
        out: list[str] = []
        seen: set[str] = set()

        def add_same(x: str) -> None:
            s = "".join(ch for ch in str(x).strip() if ch.isdigit())
            if not s:
                return
            # SAME codes are 6 digits; some sources omit leading zero. Pad-left.
            if len(s) < 6:
                s = s.zfill(6)
            if len(s) != 6:
                return
            if s in seen:
                return
            seen.add(s)
            out.append(s)

        if same_vals:
            for x in same_vals:
                add_same(str(x))
            return out

        # Fall back: if this forecast zone has "county" URLs, parse their IDs like "MDC031"
        counties = props.get("county")
        if isinstance(counties, str):
            counties = [counties]
        if isinstance(counties, (list, tuple)):
            for u in counties:
                cid = str(u).strip().rstrip("/").split("/")[-1].upper()
                m = re.fullmatch(r"([A-Z]{2})C(\d{3})", cid)
                if not m:
                    continue
                same = self._same_from_state_county(m.group(1), m.group(2))
                if same:
                    add_same(same)

        return out

    async def _ugc_zone_to_same(self, zone_id: str) -> list[str]:
        """
        Map a UGC zone token (e.g., MDZ008, PAC021, ANZ530) to SAME county/marine codes.
        Uses:
          - direct conversion for XXC### when possible
          - NWS zone endpoints for others, preferring geocode.SAME
        """
        zid = (zone_id or "").strip().upper()
        if not zid:
            return []

        # Cache
        if zid in self._zone_cache_same:
            return list(self._zone_cache_same[zid])

        # short-term backoff for repeated failures (prevents hammering)
        fail_ts = self._zone_cache_fail.get(zid)
        if fail_ts and (dt.datetime.now(tz=self._tz) - fail_ts).total_seconds() < 300:
            return []

        # Direct county-zone conversion: "PAC021" etc.
        m_direct = re.fullmatch(r"([A-Z]{2})C(\d{3})", zid)
        if m_direct:
            same = self._same_from_state_county(m_direct.group(1), m_direct.group(2))
            if same:
                self._zone_cache_same[zid] = [same]
                return [same]

        # Forecast/public zones: use ZoneCounty DBX crosswalk first (NOAA/NWS recommended).
        if re.fullmatch(r"[A-Z]{2}Z\d{3}", zid):
            try:
                await self._ensure_zonecounty_loaded()
                lst = self._zonecounty_map.get(zid)
                if lst:
                    self._zone_cache_same[zid] = list(lst)
                    return list(lst)
            except Exception:
                pass

        # Marine zones: try mareas crosswalk (ANZ/AMZ/GMZ/LMZ/PZZ/etc)
        if re.fullmatch(r"[A-Z]{3}\d{3}", zid):
            try:
                await self._ensure_mareas_loaded()
                lst2 = self._mareas_map.get(zid)
                if lst2:
                    self._zone_cache_same[zid] = list(lst2)
                    return list(lst2)
            except Exception:
                pass
        # Otherwise, ask NWS API. Most UGC tokens are forecast zones; marine ones might be under "marine".
        # We try a small ordered list.
        async with self._zone_lock:
            # Check again after acquiring lock (double-checked caching)
            if zid in self._zone_cache_same:
                return list(self._zone_cache_same[zid])

            types_to_try = ["forecast", "public", "marine", "fire", "offshore"]
            data: dict | None = None
            for zt in types_to_try:
                data = await self._get_zone_json(zt, zid)
                if data:
                    break

            if not data:
                self._zone_cache_fail[zid] = dt.datetime.now(tz=self._tz)
                return []

            same_list = self._same_list_from_zone_json(data)
            if same_list:
                self._zone_cache_same[zid] = list(same_list)
                return list(same_list)

            # If no SAME codes could be derived, treat as failure but don't spam retries.
            self._zone_cache_fail[zid] = dt.datetime.now(tz=self._tz)
            return []

    async def _nwws_same_targets_from_texts(
        self, primary_text: str, secondary_text: str
    ) -> tuple[list[str], list[str], str, bool, "dt.datetime | None"]:
        """
        Returns:
          zones_found, in_area_same, source_label, mapping_success, ugc_expires_utc
        mapping_success indicates we successfully derived at least one SAME from zones.
        ugc_expires_utc is the product's UGC expiry as an aware UTC datetime, or None.
        """
        zones = _ugc.extract_ugc_zones(primary_text)
        src = "raw"
        if not zones:
            zones = _ugc.extract_ugc_zones(secondary_text)
            src = "official" if zones else "none"

        # Capture UGC expiry for downstream use (AlertTracker, cycle scheduling).
        # parse_ugc_block tries primary text first, falls back to secondary.
        _ugc_blk = _ugc.parse_ugc_block(primary_text or secondary_text or "")
        ugc_expires_utc: dt.datetime | None = _ugc_blk.expires_utc if _ugc_blk else None

        if not zones:
            return ([], [], src, False, ugc_expires_utc)

        # Map zones -> SAME
        all_same: list[str] = []
        any_mapped = False
        for z in zones[:250]:  # safety cap
            sames = await self._ugc_zone_to_same(z)
            if sames:
                any_mapped = True
                all_same.extend(sames)

        # De-dupe while preserving order
        dedup: list[str] = []
        seen: set[str] = set()
        for s in all_same:
            s2 = str(s).strip()
            if not s2 or s2 in seen:
                continue
            seen.add(s2)
            dedup.append(s2)

        in_area = self._filter_same_locations_to_service_area(dedup)
        return (zones, in_area, src, any_mapped, ugc_expires_utc)

    async def _nwws_wcn_watch_same_targets_from_area_desc(self, official_text: str) -> list[str]:
        """
        Derive in-area SAME codes for WCN watch products that lack a UGC block.

        Some NWWS WCN watch county notifications carry clean county/state body
        text but no UGC county-zone line.  We only need to recover the local
        service-area intersection for SAME tone generation, so resolve configured
        SAME codes to county labels and match those labels against the WCN area
        block.
        """
        area_desc = _extract_nwws_wcn_area_desc(official_text or "")
        if not area_desc:
            return []

        candidates = [str(x).strip() for x in (self.cfg.service_area.same_fips_all or []) if str(x).strip()]
        # Statewide/wildcard SAME entries cannot be matched to specific WCN counties.
        candidates = [c for c in candidates if re.fullmatch(r"\d{6}", c) and not c.endswith("000")]
        if not candidates:
            return []

        labels = await asyncio.gather(*(self._same6_area_label(c) for c in candidates), return_exceptions=True)
        label_by_code: dict[str, str] = {}
        for code, label in zip(candidates, labels):
            if isinstance(label, Exception) or not label:
                continue
            label_by_code[str(code).strip()] = str(label).strip()

        matched = _match_nwws_wcn_area_same(area_desc, label_by_code)
        return self._filter_same_locations_to_service_area(matched)
