from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import httpx


DEFAULT_UA = "SeasonalWeather/2.0 (automated IP radio system for weather; contact: info@seasonalnet.org)"


@dataclass(frozen=True)
class NWSProductReference:
    product_id: str
    issuance_time: str | None = None
    product_type: str | None = None
    wfo: str | None = None


@dataclass
class NWSProduct:
    product_id: str
    product_text: str
    issuance_time: str | None = None
    product_type: str | None = None
    wfo: str | None = None


class NWSApi:
    def __init__(self, timeout: float = 8.0, user_agent: str = DEFAULT_UA) -> None:
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "User-Agent": user_agent,
                "Accept": "application/geo+json, application/ld+json, application/json",
            },
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get_json(self, url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        # simple retry loop
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                r = await self._client.get(url, params=params)
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last_exc = e
                await asyncio.sleep(0.4 * (attempt + 1))
        raise RuntimeError(f"NWS API request failed: {url}") from last_exc

    @staticmethod
    def _issuance_sort_key(value: str | None) -> dt.datetime:
        raw = str(value or "").strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = dt.datetime.fromisoformat(raw)
        except ValueError:
            return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)

    async def list_product_references(
        self,
        product_type: str,
        wfo: str,
        *,
        limit: int | None = None,
    ) -> List[NWSProductReference]:
        """Return product-list metadata newest first.

        api.weather.gov generally returns product indexes newest first, but the
        ordering is not part of the local recovery contract. Sort explicitly by
        issuanceTime so callers never backfill an older sibling ahead of a newer
        product for the same office.
        """
        product_type = str(product_type or "").strip().upper()
        wfo = str(wfo or "").strip().upper()
        url = f"https://api.weather.gov/products/types/{product_type}/locations/{wfo}"
        data = await self._get_json(url)

        items: List[Dict[str, Any]] = []
        if isinstance(data.get("products"), list):
            items = data["products"]
        elif isinstance(data.get("@graph"), list):
            items = data["@graph"]
        elif isinstance(data.get("graph"), list):
            items = data["graph"]

        refs: List[NWSProductReference] = []
        for item in items:
            pid = item.get("id") or item.get("@id") or item.get("productId")
            if not isinstance(pid, str) or not pid.strip():
                continue
            refs.append(
                NWSProductReference(
                    product_id=pid.rstrip("/").split("/")[-1],
                    issuance_time=item.get("issuanceTime") or item.get("issuance_time"),
                    product_type=(item.get("productCode") or item.get("product_code") or product_type),
                    wfo=(item.get("issuingOffice") or item.get("wfo") or item.get("wfoCode") or wfo),
                )
            )

        refs.sort(
            key=lambda item: self._issuance_sort_key(item.issuance_time),
            reverse=True,
        )
        if limit is not None:
            return refs[: max(0, int(limit))]
        return refs

    async def latest_product_id(self, product_type: str, wfo: str) -> Optional[str]:
        refs = await self.list_product_references(product_type, wfo, limit=1)
        return refs[0].product_id if refs else None

    async def get_product(self, product_id: str) -> Optional[NWSProduct]:
        url = f"https://api.weather.gov/products/{product_id}"
        data = await self._get_json(url)
        text = data.get("productText") or data.get("product_text") or data.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        return NWSProduct(
            product_id=product_id,
            product_text=text,
            issuance_time=data.get("issuanceTime") or data.get("issuance_time"),
            product_type=data.get("productCode") or data.get("product_code"),
            wfo=data.get("issuingOffice") or data.get("wfo") or data.get("wfoCode"),
        )

    async def point_forecast_periods(self, lat: float, lon: float) -> List[Dict[str, Any]]:
        point = await self._get_json(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}")
        props = point.get("properties", {})
        forecast_url = props.get("forecast")
        if not isinstance(forecast_url, str) or not forecast_url:
            return []
        fc = await self._get_json(forecast_url)
        return list((fc.get("properties", {}) or {}).get("periods", []) or [])

    async def latest_observation(self, station_id: str) -> Optional[Dict[str, Any]]:
        data = await self._get_json(f"https://api.weather.gov/stations/{station_id}/observations/latest")
        return data.get("properties")

    async def active_alerts(self, areas: List[str]) -> List[Dict[str, Any]]:
        # areas = state/territory abbreviations (e.g. ["MD","VA","DC","WV"])
        params = {"area": ",".join(areas), "status": "actual"}
        data = await self._get_json("https://api.weather.gov/alerts/active", params=params)
        feats = data.get("features")
        return list(feats) if isinstance(feats, list) else []

    async def zone_forecast_periods(self, zone_id: str) -> List[Dict[str, Any]]:
        # Fetch ZFP forecast periods for a public forecast zone (e.g. 'MDZ010').
        # Endpoint: /zones/forecast/{zoneId}/forecast
        # Returns period dicts with at minimum 'name' and 'detailedForecast'.
        url = f"https://api.weather.gov/zones/forecast/{zone_id}/forecast"
        data = await self._get_json(url)
        return list((data.get("properties", {}) or {}).get("periods", []) or [])

    async def coastal_waters_forecast_text(self, office: str) -> Optional[str]:
        # Fetch the latest CWF (Coastal Waters Forecast) product text
        # for the given WFO office (e.g. 'LWX').  Returns raw text or None.
        pid = await self.latest_product_id("CWF", office)
        if not pid:
            return None
        prod = await self.get_product(pid)
        if not prod or not prod.product_text:
            return None
        return prod.product_text

    async def coastal_waters_forecast_product(self, office: str) -> Optional[NWSProduct]:
        """Return the latest CWF product without discarding its identity."""
        pid = await self.latest_product_id("CWF", office)
        if not pid:
            return None
        return await self.get_product(pid)
