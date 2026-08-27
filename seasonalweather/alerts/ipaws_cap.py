from __future__ import annotations

# =========================================================================================
#      MP"""""`MM                                                       dP              MM'"""'YMM
#      M  mmmmm..M                                                       88              M' .mmm. `M
#      M.      `YM .d8888b. .d8888b. .d8888b. .d8888b. 88d888b. .d8888b. 88              M  MMMMMooM dP    dP 88d888b. 88d888b. .d8888b. 88d888b. .d8888b. dP    dP
#      MMMMMMM.  M 88ooood8 88'  `88 Y8ooooo. 88'  `88 88'  `88 88'  `88 88              M  MMMMMMMM 88    88 88'  `88 88'  `88 88ooood8 88'  `88 88'  `"" 88    88
#      M. .MMM'  M 88.  ... 88.  .88       88 88.  .88 88    88 88.  .88 88              M. `MMM' .M 88.  .88 88       88       88.  ... 88    88 88.  ... 88.  .88
#      Mb.     .dM `88888P' `88888P8 `88888P' `88888P' dP    dP `88888P8 dP              MM.     .dM `88888P' dP       dP       `88888P' dP    dP `88888P' `8888P88
#      MMMMMMMMMMM                                                Seasonal_Currency      MMMMMMMMMMM                                                            .88
#                                                                                                                                                           d8888P.
# =========================================================================================

"""
ipaws_cap.py — FEMA IPAWS CAP timed-feed poller for SeasonalWeather.

Polls the FEMA IPAWS Open CAP endpoint:
  https://apps.fema.gov/IPAWSOPEN_EAS_SERVICE/rest/eas/recent/2019-12-31T11:59:59Z

The static date parameter is intentional — FEMA's server returns everything currently
active on the feed regardless of the date value.

Design notes:
  - Transport integrity via HTTPS/TLS is sufficient.  XMLDSig Signature blocks
    present in each alert are SILENTLY DISCARDED during parse — we strip all
    elements whose tag falls in the http://www.w3.org/2000/09/xmldsig# namespace
    before touching any CAP content.  This avoids a heavyweight lxml + xmlsec1
    dependency for zero practical security gain over TLS for a hobbyist station.
    Real ENDECs receiving IPAWS over EAS-IP don't expose their verification either.
  - Only the first <info> block with language="en-US" (or "en") is used.  If
    none, falls back to the first <info> block regardless of language.
  - DMO (Practice/Demo Warning), RWT (Required Weekly Test), and RMT (Required
    Monthly Test) are filtered at the poller level and NEVER emitted.
    SeasonalWeather manages its own test schedule independently.
  - Deduplication: in-memory fast path + persistent CapLedger (DB-backed or JSON
    fallback) keyed on identifier|sent, matching cap_nws.py behaviour exactly.
    The shared cap_seen_ledger table is reused — IPAWS keys are prefixed "IPAWS:"
    so they never collide with NWS CAP entries.
"""

import asyncio
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2

from .cap_ledger import CapLedger
from ..same.locations import (
    normalize_same_allow_set,
    normalize_same_location,
    same_locations_intersect_service_area,
)

log = logging.getLogger("seasonalweather.ipaws")

# ---------------------------------------------------------------------------
# XML namespace constants
# ---------------------------------------------------------------------------

_DSIG_NS = "http://www.w3.org/2000/09/xmldsig#"
# Regex to strip Clark-notation namespace prefix: "{ns}local" → "local"
_NS_RE = re.compile(r"^\{[^}]+\}")

# ---------------------------------------------------------------------------
# senderName cleaning
# ---------------------------------------------------------------------------

# IPAWS org IDs prefix: leading digits followed by a comma.
_SENDER_ID_RE = re.compile(r"^\d+,\s*")

# Organisation-type keywords indicating the first segment is already a
# complete authority name (city/state suffix not needed).
_ORG_KEYWORDS = frozenset(
    {
        "county",
        "department",
        "authority",
        "district",
        "bureau",
        "office",
        "agency",
        "commission",
        "service",
        "management",
        "corps",
        "fire",
        "police",
        "sheriff",
        "emergency",
        "ema",
        "oem",
    }
)

# Generic placeholder names produced by some CAP authoring tools.
_GENERIC_SENDERS = frozenset(
    {
        "public alert system",
        "public warning system",
        "public safety",
        "publicsafetyalert",
        "alert system",
        "emergency alert",
        "emergency alert system",
        "mass notification system",
        "nixle",
        "hyper-reach",
        "everbridge",
        "onsolve",
        "omnilert",
        "regroup",
        "alertmedia",
    }
)

# 2-letter US state/territory abbreviations used for "City, ST" reconstruction.
_STATE_ABBRS = frozenset(
    {
        "AL",
        "AK",
        "AZ",
        "AR",
        "CA",
        "CO",
        "CT",
        "DE",
        "FL",
        "GA",
        "HI",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NV",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
        "DC",
        "PR",
        "GU",
        "VI",
        "AS",
        "MP",
    }
)


def _clean_ipaws_sender(raw: str) -> str | None:
    """
    Clean an IPAWS senderName field for TTS delivery.

    Input patterns and expected outputs:

      "202478, Fredericksburg, TX, Fredericksburg, TX"  → "Fredericksburg, TX"
      "202867,MO Worth County,"                         → "MO Worth County"
      "201203,Hutchinson County Public Alerts,..."      → "Hutchinson County Public Alerts"
      "San Angelo Fire Dept"                            → "San Angelo Fire Dept"
      "PRPC,Amarillo,TX"                                → "PRPC, Amarillo, TX"
      "Public Alert System"                             → None  (generic → caller falls back)
      "202028, Rockwall County OEM, Rockwall County OEM" → "Rockwall County OEM"

    Returns None when the result is generic/empty; callers should substitute
    "local authorities" or similar.
    """
    s = (raw or "").strip()
    if not s:
        return None

    # 1. Strip leading numeric IPAWS org ID ("202478, " or "202867,").
    s = _SENDER_ID_RE.sub("", s)

    # 2. Strip trailing commas and whitespace that some tools leave behind.
    s = s.strip(" ,")
    if not s:
        return None

    # 3. Split on commas, strip each segment, drop empty ones.
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if not parts:
        return None

    # 4. Deduplicate consecutive or repeated identical segments (case-insensitive).
    #    "Fredericksburg, TX, Fredericksburg, TX"  → ["Fredericksburg", "TX"]
    #    "Rockwall County OEM, Rockwall County OEM" → ["Rockwall County OEM"]
    deduped: list[str] = []
    seen: set[str] = set()
    for part in parts:
        key = part.lower()
        if key not in seen:
            seen.add(key)
            deduped.append(part)

    # 5. Decide how many segments to keep.
    if len(deduped) == 1:
        result = deduped[0]
    elif len(deduped) == 2:
        first, second = deduped[0], deduped[1]
        first_lower = first.lower()
        first_has_org = any(kw in first_lower for kw in _ORG_KEYWORDS)
        second_is_state = second.upper() in _STATE_ABBRS

        if second_is_state and not first_has_org:
            # "Fredericksburg, TX" — city + state.
            result = f"{first}, {second}"
        else:
            # Multi-segment org: first segment is enough.
            result = first
    elif len(deduped) >= 3:
        # Three or more segments: check whether the last segment is a state
        # abbreviation (e.g. "PRPC, Amarillo, TX" or "202867, Worth County, MO").
        # If so, expose the penultimate + last as a city+state suffix.
        last = deduped[-1]
        second_last = deduped[-2]
        first = deduped[0]
        first_lower = first.lower()
        first_has_org = any(kw in first_lower for kw in _ORG_KEYWORDS)

        if last.upper() in _STATE_ABBRS:
            if first_has_org:
                # Org name already contains location context; just keep it.
                result = first
            else:
                # Acronym or short name + city + state → keep all three parts.
                result = f"{first}, {second_last}, {last}"
        else:
            # No trailing state — first meaningful segment wins.
            result = first
    else:
        result = deduped[0]

    # 6. Reject generic / useless sender names.
    if result.lower() in _GENERIC_SENDERS:
        return None

    return result


# ---------------------------------------------------------------------------
# SAME FIPS normalisation  (mirrors cap_nws.py)
# ---------------------------------------------------------------------------


def _norm_same(s: str) -> str | None:
    return normalize_same_location(s)


# ---------------------------------------------------------------------------
# XML namespace-strip / Signature-drop
# ---------------------------------------------------------------------------


def _strip_namespaces(elem: ET.Element) -> ET.Element | None:
    """
    Return a cloned element tree with Clark-notation namespace prefixes stripped.
    Elements in the XMLDSig namespace are dropped entirely (Signature blocks).
    Produces a plain namespace-free tree: root.find("info/event") works directly.
    """
    raw_tag = elem.tag
    if raw_tag.startswith(f"{{{_DSIG_NS}}}"):
        return None

    local = _NS_RE.sub("", raw_tag)
    new_elem = ET.Element(local)
    new_elem.text = elem.text
    new_elem.tail = elem.tail
    new_elem.attrib = {_NS_RE.sub("", k): v for k, v in elem.attrib.items()}

    for child in elem:
        stripped = _strip_namespaces(child)
        if stripped is not None:
            new_elem.append(stripped)

    return new_elem


# ---------------------------------------------------------------------------
# IpawsCapEvent dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IpawsCapEvent:
    """
    A single IPAWS CAP alert, parsed and normalised for SeasonalWeather.

    Fields are a strict subset of what IpawsCapPoller extracts from the XML.
    There is no VTEC on IPAWS civil alerts — event_code is the SAME EAS code
    (e.g. "CEM", "LAE").
    """

    # CAP message-level
    identifier: str
    sender: str | None
    sent: str | None
    status: str | None
    message_type: str | None  # Alert / Update / Cancel

    # From the best <info> block
    event: str | None  # Human-readable, e.g. "Civil Emergency Message"
    event_code: str | None  # SAME code, e.g. "CEM"
    severity: str | None
    urgency: str | None
    certainty: str | None

    sender_name_raw: str | None  # <senderName> verbatim
    sender_name_clean: str | None  # cleaned for TTS (may be None)
    headline: str | None
    description: str | None
    instruction: str | None

    effective: str | None
    expires: str | None

    same_fips: list[str]  # 6-digit PSSCCC from <geocode>
    parameters: dict[str, list[str]]
    eas_org: str | None  # EAS-ORG param: CIV / WXR / …

    # Compatibility alias so shared helpers can treat this like CapAlertEvent
    @property
    def alert_id(self) -> str:
        return self.identifier


# ---------------------------------------------------------------------------
# IpawsCapPoller
# ---------------------------------------------------------------------------


class IpawsCapPoller:
    """
    Polls the FEMA IPAWS timed CAP endpoint and enqueues service-area-matching
    alerts as IpawsCapEvent objects.

    Constructor is parallel to NwsCapPoller:
      IpawsCapPoller(out_queue=..., same_fips_allow=..., poll_seconds=...,
                     user_agent=..., url=?, ledger_path=?, ledger_max_age_days=?,
                     database=?)

    Key differences from NwsCapPoller:
      - Parses CAP 1.2 XML, not GeoJSON (stdlib xml.etree only — no new deps).
      - XMLDSig Signature blocks are discarded during parse.
      - Product-type policy is not applied here; the controller owns routing.
      - senderName is cleaned for TTS on ingestion.
      - Dedupe keys are prefixed "IPAWS:" to coexist in the shared cap_seen_ledger.
    """

    _DEFAULT_URL = "https://apps.fema.gov/IPAWSOPEN_EAS_SERVICE/rest/eas/recent/2019-12-31T11:59:59Z"

    def __init__(
        self,
        *,
        out_queue: asyncio.Queue[IpawsCapEvent],
        same_fips_allow: list[Any],
        poll_seconds: int = 90,
        user_agent: str = "SeasonalWeather (IPAWS monitor)",
        url: str | None = None,
        ledger_path: str | None = None,
        ledger_max_age_days: int = 14,
        database: Any | None = None,
        diagnostic_sink: object | None = None,
    ) -> None:
        self.out_queue = out_queue
        self.poll_seconds = max(30, int(poll_seconds))
        self.user_agent = user_agent.strip() or "SeasonalWeather (IPAWS monitor)"
        self.url = (url or "").strip() or self._DEFAULT_URL

        self.allow_set = normalize_same_allow_set(same_fips_allow)

        # In-memory dedupe (fast path within one process lifetime)
        self._seen_keys: set[str] = set()

        if not ledger_path:
            raise ValueError("ledger_path must be supplied from the configured operational state root")

        # Persistent dedupe (restart-safe, DB-backed or JSON fallback)
        self._ledger = CapLedger(
            path=Path(ledger_path),
            max_age_days=max(3, int(ledger_max_age_days)),
            database=database,
        )
        self._diagnostic_sink = diagnostic_sink

        self._client: httpx2.AsyncClient | None = None

    def _diagnose(self, code: str, message: str, exception: BaseException | None = None) -> None:
        sink = self._diagnostic_sink
        emit = getattr(sink, "emit", None)
        if not callable(emit):
            return
        emit(
            code,
            component="ipaws-poller",
            message=message,
            operational_effect="IPAWS CAP ingestion is degraded within its bounded polling policy.",
            recovery_action="Inspect the bounded source or parser failure; the next poll remains the recovery boundary.",
            exception=exception,
            source_id="ipaws-cap",
        )

    async def _get_client(self) -> httpx2.AsyncClient:
        if self._client is None or self._client.is_closed:
            # Use the system CA bundle rather than the certifi snapshot bundled
            # with the venv.  FEMA's cert is issued by Cloudflare TLS Issuing ECC
            # CA 1 which may not appear in an older certifi release, but is always
            # present in the Debian system store that curl uses successfully.
            self._client = httpx2.AsyncClient(
                headers={"User-Agent": self.user_agent},
                timeout=httpx2.Timeout(connect=10.0, read=30.0, write=10.0, pool=5.0),
                follow_redirects=True,
                verify="/etc/ssl/certs/ca-certificates.crt",
            )
        return self._client

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            try:
                await self._client.aclose()
            except Exception:
                pass
        self._client = None

    async def _fetch_xml(self) -> str:
        client = await self._get_client()
        resp = await client.get(self.url)
        resp.raise_for_status()
        return resp.text

    # ------------------------------------------------------------------
    # XML parse
    # ------------------------------------------------------------------

    def _parse_alerts(self, xml_text: str) -> list[IpawsCapEvent]:
        """Parse the IPAWS envelope and return matching IpawsCapEvent objects."""
        from ..diagnostics.bindings import FOUNDATION_CODES

        try:
            root = ET.fromstring(xml_text.strip())
        except ET.ParseError as exc:
            log.warning("IPAWS XML parse error: %s", exc)
            self._diagnose(
                FOUNDATION_CODES["cap.product_invalid"],
                "An IPAWS CAP XML envelope could not be parsed.",
                exc,
            )
            return []

        # The envelope is <ns1:alerts>.  Its direct children are <alert>
        # elements in the CAP namespace (or no namespace on some feeds).
        # We iterate raw children, identify <alert> by local name after
        # stripping the namespace prefix.
        out: list[IpawsCapEvent] = []
        for child in root:
            local = _NS_RE.sub("", child.tag)
            if local == "alert":
                ev = self._parse_single(child)
                if ev is not None:
                    out.append(ev)
        return out

    def _parse_single(self, raw_alert: ET.Element) -> IpawsCapEvent | None:
        """
        Parse one raw <alert> element.  Returns None to discard.
        Applies transport/status, msgType, and service-area filters.  Product
        type admission is deliberately deferred to the controller policy.
        """
        alert = _strip_namespaces(raw_alert)
        if alert is None:
            return None

        def _t(tag: str) -> str | None:
            el = alert.find(tag)
            return el.text.strip() if el is not None and el.text else None

        identifier = _t("identifier") or ""
        if not identifier:
            return None

        status = _t("status")
        if (status or "").strip().lower() != "actual":
            return None

        message_type = _t("msgType")
        if (message_type or "").strip().lower() not in {"alert", "update", "cancel"}:
            return None

        sender = _t("sender")
        sent = _t("sent")

        # Best <info> block: prefer en-US / en, fall back to first.
        info_el: ET.Element | None = None
        for info in alert.findall("info"):
            lang = info.findtext("language") or ""
            if lang.lower().startswith("en"):
                info_el = info
                break
        if info_el is None:
            info_el = alert.find("info")
        if info_el is None:
            return None

        # SAME event code from <eventCode><valueName>SAME</valueName><value>XXX</value>
        event_code: str | None = None
        for ec in info_el.findall("eventCode"):
            vn = ec.findtext("valueName") or ""
            if vn.strip().upper() == "SAME":
                event_code = (ec.findtext("value") or "").strip().upper() or None
                break

        # SAME FIPS from all <area> blocks.
        same_fips: list[str] = []
        seen_fips: set[str] = set()
        for area in info_el.findall("area"):
            for gc in area.findall("geocode"):
                vn = gc.findtext("valueName") or ""
                if vn.strip().upper() == "SAME":
                    s6 = _norm_same(gc.findtext("value") or "")
                    if s6 and s6 not in seen_fips:
                        seen_fips.add(s6)
                        same_fips.append(s6)

        # Service-area filter — if nothing intersects our allow set, discard.
        if not self._matches_service_area(same_fips):
            return None

        # Parameters dict.
        params: dict[str, list[str]] = {}
        for param in info_el.findall("parameter"):
            vn = (param.findtext("valueName") or "").strip()
            val = (param.findtext("value") or "").strip()
            if vn and val:
                params.setdefault(vn, []).append(val)

        eas_org = ((params.get("EAS-ORG") or [""])[0]).strip() or None

        sender_name_raw = (info_el.findtext("senderName") or "").strip() or None
        sender_name_clean = _clean_ipaws_sender(sender_name_raw) if sender_name_raw else None

        return IpawsCapEvent(
            identifier=identifier,
            sender=sender,
            sent=sent,
            status=status,
            message_type=message_type,
            event=(info_el.findtext("event") or "").strip() or None,
            event_code=event_code,
            severity=(info_el.findtext("severity") or "").strip() or None,
            urgency=(info_el.findtext("urgency") or "").strip() or None,
            certainty=(info_el.findtext("certainty") or "").strip() or None,
            sender_name_raw=sender_name_raw,
            sender_name_clean=sender_name_clean,
            headline=(info_el.findtext("headline") or "").strip() or None,
            description=(info_el.findtext("description") or "").strip() or None,
            instruction=(info_el.findtext("instruction") or "").strip() or None,
            effective=(info_el.findtext("effective") or "").strip() or None,
            expires=(info_el.findtext("expires") or "").strip() or None,
            same_fips=same_fips,
            parameters=params,
            eas_org=eas_org,
        )

    def _matches_service_area(self, same_list: list[str]) -> bool:
        if not same_list:
            return False
        if not self.allow_set:
            return False
        return same_locations_intersect_service_area(same_list, self.allow_set)

    def _dedupe_key(self, ev: IpawsCapEvent) -> str:
        # "IPAWS:" prefix keeps these separate from NWS CAP entries in the
        # shared cap_seen_ledger table without needing a second table.
        return f"IPAWS:{ev.identifier}|{ev.sent or ''}"

    # ------------------------------------------------------------------
    # Main poll loop
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        """Poll loop. Never returns unless cancelled."""
        from ..diagnostics.bindings import FOUNDATION_CODES

        log.info(
            "IPAWS CAP poller starting (poll=%ss url=%s service_area_fips=%d)",
            self.poll_seconds,
            self.url,
            len(self.allow_set),
        )
        try:
            while True:
                t0 = time.time()
                marked_any = False
                emitted = 0

                try:
                    xml_text = await self._fetch_xml()
                    events = self._parse_alerts(xml_text)

                    for ev in events:
                        k = self._dedupe_key(ev)

                        if k in self._seen_keys:
                            continue

                        try:
                            if self._ledger.has(k):
                                self._seen_keys.add(k)
                                continue
                        except Exception:
                            log.exception("IPAWS ledger has() failed (continuing)")

                        try:
                            self._ledger.mark(k)
                            marked_any = True
                        except Exception:
                            log.exception("IPAWS ledger mark() failed (continuing)")

                        self._seen_keys.add(k)

                        try:
                            self.out_queue.put_nowait(ev)
                            emitted += 1
                        except asyncio.QueueFull:
                            log.warning(
                                "IPAWS queue full; dropping id=%s code=%s",
                                ev.identifier,
                                ev.event_code,
                            )

                    if marked_any:
                        try:
                            self._ledger.flush()
                        except Exception:
                            log.exception("IPAWS ledger flush() failed (non-fatal)")

                    log.info(
                        "IPAWS poll: emitted=%d in-area=%d (total-from-feed=%d)",
                        emitted,
                        len(events),
                        len(events),
                    )

                    if len(self._seen_keys) > 50_000:
                        log.warning(
                            "IPAWS in-memory dedupe set is large (%d); clearing",
                            len(self._seen_keys),
                        )
                        self._seen_keys.clear()

                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.exception("IPAWS poll loop error")
                    self._diagnose(
                        FOUNDATION_CODES["cap.source_failed"],
                        "IPAWS polling failed within the bounded source operation.",
                        exc,
                    )

                elapsed = time.time() - t0
                sleep_s = max(1.0, float(self.poll_seconds) - elapsed)
                await asyncio.sleep(sleep_s)

        finally:
            await self.aclose()
