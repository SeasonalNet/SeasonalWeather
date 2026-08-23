from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
import time
from typing import Any

from ..alerts.active import _vtec_track_id
from ..alerts.builder import strip_nws_product_headers
from ..alerts.vtec import (
    VTEC_FIND_RE as _VTEC_FIND_RE,
)
from ..alerts.vtec import (
    VTEC_PARSE_RE as _VTEC_PARSE_RE,
)
from ..alerts.vtec import (
    phen_sig_label as _vtec_phen_sig_label,
)
from ..config import AppConfig
from ..database.station_feed import StationFeedRepository
from ..diagnostics.bindings import FOUNDATION_CODES
from ..same.events import (
    label_or_code as _same_label_or_code,
)
from ..same.events import (
    org_broadcast_prefix as _eas_org_broadcast_prefix,
)
from .station_feed import FeedSender, StationFeedAlert, build_station_feed_payload

log = logging.getLogger("seasonalweather")

_NWS_HEADER_ISSUED_RE = re.compile(
    r"^(?P<hhmm>\d{3,4})\s*(?P<ampm>AM|PM)\s*(?P<tz>[A-Z]{2,4})\s+"
    r"(?P<dow>[A-Za-z]{3})\s+(?P<mon>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+(?P<year>\d{4})\s*$"
)

_APP_CFG: AppConfig | None = None
_station_feed_diagnostic_sink: object | None = None


def set_app_config(cfg: AppConfig | None) -> None:
    global _APP_CFG
    _APP_CFG = cfg


def set_diagnostic_sink(sink: object | None) -> None:
    global _station_feed_diagnostic_sink
    _station_feed_diagnostic_sink = sink


def _sf_diagnose(exception: BaseException, message: str) -> None:
    emit = getattr(_station_feed_diagnostic_sink, "emit", None)
    if not callable(emit):
        return
    try:
        _ = emit(
            FOUNDATION_CODES["database.operation_failed"],
            component="station-feed",
            message=message,
            operational_effect="The public handled-alerts read model may be stale while canonical alert processing continues.",
            recovery_action="Inspect the bounded database failure and allow the next station-feed maintenance or update operation to retry.",
            exception=exception,
            source_id="station-feed",
        )
    except Exception:
        return

def _sf_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _sf_parse_dt(value):
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return dt.datetime.fromisoformat(s)
        except Exception:
            return None
    return None


def _sf_iso(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    return str(value)


def _sf_sha1_12(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "ignore")).hexdigest()[:12]



_STATION_FEED_STATE: dict[str, tuple[StationFeedAlert, float]] = {}
_STATION_FEED_REPO: StationFeedRepository | None = None

def _sf_enabled() -> bool:
    if StationFeedAlert is None or build_station_feed_payload is None:
        return False
    if _APP_CFG is None:
        return False
    return _APP_CFG.station_feed.enabled


def _sf_cfg():
    if _APP_CFG is None:
        return "seasonalweather", "seasonalweather", 24, 7200
    sf = _APP_CFG.station_feed
    return sf.station_id, sf.source, sf.max_items, sf.ttl_seconds


def _sf_set_repository(repo: StationFeedRepository | None) -> None:
    global _STATION_FEED_REPO
    _STATION_FEED_REPO = repo


def _sf_alert_payload(alert):
    station_id, source, _max_items, _ttl_s = _sf_cfg()
    payload = build_station_feed_payload(
        station_id=station_id,
        source=source,
        generated_at_iso=_sf_now_iso(),
        alerts=[alert],
    )
    alerts = payload.get("alerts") if isinstance(payload, dict) else None
    if isinstance(alerts, list) and alerts and isinstance(alerts[0], dict):
        return alerts[0]
    return {}


def _sf_repo_upsert(alert, *, expires_at=None) -> None:
    if _STATION_FEED_REPO is None:
        return
    try:
        payload = _sf_alert_payload(alert)
        if payload:
            _STATION_FEED_REPO.upsert_alert(
                alert_id=str(getattr(alert, "id", "") or ""),
                payload=payload,
                expires_at=expires_at or payload.get("expires") or payload.get("ends"),
            )
    except Exception as exc:
        log.exception("Station feed: failed to persist alert to SQLite read model")
        _sf_diagnose(exc, "Station-feed alert persistence failed.")


def _sf_repo_delete(ids) -> None:
    if _STATION_FEED_REPO is None:
        return
    try:
        _STATION_FEED_REPO.delete_alerts(str(x) for x in (ids or []))
    except Exception as exc:
        log.exception("Station feed: failed to delete alerts from SQLite read model")
        _sf_diagnose(exc, "Station-feed alert deletion failed.")


def _sf_repo_housekeep(now_ts: float, *, max_items: int) -> int:
    if _STATION_FEED_REPO is None:
        return 0
    try:
        now = dt.datetime.fromtimestamp(float(now_ts), tz=dt.timezone.utc)
        removed = _STATION_FEED_REPO.prune_expired(now=now, grace_seconds=_sf_hk_grace_s())
        removed += _STATION_FEED_REPO.trim_to_max(max_items)
        return removed
    except Exception as exc:
        log.exception("Station feed housekeeping: SQLite read model prune failed")
        _sf_diagnose(exc, "Station-feed read-model housekeeping failed.")
        return 0


def _sf_prune(now_ts: float, *, max_items: int) -> list[str]:
    removed: list[str] = []
    expired = [k for k, (_, exp) in _STATION_FEED_STATE.items() if exp <= now_ts]
    for k in expired:
        if _STATION_FEED_STATE.pop(k, None) is not None:
            removed.append(str(k))
    if len(_STATION_FEED_STATE) > max_items:
        items = sorted(_STATION_FEED_STATE.items(), key=lambda kv: kv[1][1], reverse=True)
        keep_keys = {str(k) for k, _v in items[:max_items]}
        for k in list(_STATION_FEED_STATE.keys()):
            if str(k) not in keep_keys:
                _STATION_FEED_STATE.pop(k, None)
                removed.append(str(k))
    if removed:
        _sf_repo_delete(removed)
    return removed



def _sf_purge_legacy_synthetic_alerts() -> int:
    """Remove obsolete synthetic CAP rows created by the retired startup seeder."""
    legacy_name = "CAP restore"
    removed_ids: set[str] = set()

    for alert_id, (alert, _expires_at) in list(_STATION_FEED_STATE.items()):
        sender = getattr(alert, "from_", None)
        sender_name = str(getattr(sender, "name", "") or "").strip()
        sender_kind = str(getattr(sender, "kind", "") or "").strip().lower()
        if sender_name.casefold() == legacy_name.casefold() and sender_kind == "relay":
            _STATION_FEED_STATE.pop(alert_id, None)
            removed_ids.add(str(alert_id))

    if _STATION_FEED_REPO is not None:
        try:
            removed_ids.update(_STATION_FEED_REPO.delete_legacy_synthetic_alerts())
        except Exception as exc:
            log.exception("Station feed: failed removing legacy synthetic CAP rows")
            _sf_diagnose(exc, "Station-feed legacy-row cleanup failed.")

    return len(removed_ids)


def _sf_hydrate_persisted_alerts() -> int:
    """Hydrate the runtime cache from exact persisted station-feed payloads."""
    if not _sf_enabled() or _STATION_FEED_REPO is None:
        return 0

    now = dt.datetime.now(dt.timezone.utc)
    _station_id, _source, max_items, _ttl_s = _sf_cfg()
    try:
        payloads = _STATION_FEED_REPO.load_alerts(now=now, max_items=max_items)
    except Exception as exc:
        log.exception("Station feed: failed loading persisted rows into runtime cache")
        _sf_diagnose(exc, "Station-feed persisted-state hydration failed.")
        return 0

    hydrated = 0
    for payload in payloads:
        try:
            alert_id = str(payload.get("id") or "").strip()
            expires_raw = payload.get("expires") or payload.get("ends")
            expires_dt = _sf_parse_dt(expires_raw)
            if not alert_id or expires_dt is None:
                continue
            if expires_dt.tzinfo is None:
                expires_dt = expires_dt.replace(tzinfo=dt.timezone.utc)
            else:
                expires_dt = expires_dt.astimezone(dt.timezone.utc)
            if expires_dt <= now:
                continue

            sender_raw = payload.get("from")
            sender = None
            if isinstance(sender_raw, dict):
                sender_name = str(sender_raw.get("name") or "").strip()
                sender_kind = str(sender_raw.get("kind") or "unknown").strip().lower()
                if sender_kind not in {"relay", "origin", "unknown"}:
                    sender_kind = "unknown"
                if sender_name:
                    sender = FeedSender(name=sender_name, kind=sender_kind)

            links_raw = payload.get("links")
            links = dict(links_raw) if isinstance(links_raw, dict) else None
            same_codes_raw = payload.get("sameCodes")
            same_codes = (
                [str(value).strip() for value in same_codes_raw if str(value).strip()]
                if isinstance(same_codes_raw, list)
                else []
            )

            alert = StationFeedAlert(
                id=alert_id,
                event=str(payload.get("event") or "Alert"),
                headline=str(payload.get("headline") or ""),
                severity=str(payload.get("severity") or "Unknown"),
                urgency=str(payload.get("urgency") or "Unknown"),
                certainty=str(payload.get("certainty") or "Unknown"),
                area=str(payload.get("area") or ""),
                effective=_sf_iso(payload.get("effective")),
                ends=_sf_iso(payload.get("ends")),
                expires=_sf_iso(payload.get("expires")),
                sent=_sf_iso(payload.get("sent")),
                sameCodes=same_codes,
                from_=sender,
                links=links,
            )
            _STATION_FEED_STATE[alert_id] = (alert, expires_dt.timestamp())
            hydrated += 1
        except Exception:
            log.exception("Station feed: skipped malformed persisted row during cache hydration")

    return hydrated


def _station_feed_note_required_test(
    *,
    code: str,
    headline: str,
    area_text: str,
    same_codes,
    out_wav: str,
) -> None:
    if not _sf_enabled() or StationFeedAlert is None:
        return
    try:
        now_utc = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        exp_utc = now_utc + dt.timedelta(minutes=30)
        label = _same_label_or_code(code)
        sender = FeedSender(name="SeasonalWeather", kind="origin") if FeedSender else None
        alert = StationFeedAlert(
            id=_sf_sha1_12(f"test:{code}:{now_utc.isoformat()}"),
            event=str(label),
            headline=str(headline),
            severity="Unknown",
            urgency="Unknown",
            certainty="Observed",
            area=str(area_text),
            effective=now_utc.isoformat(),
            ends=exp_utc.isoformat(),
            expires=exp_utc.isoformat(),
            sent=now_utc.isoformat(),
            sameCodes=[str(x) for x in (same_codes or [])][:32],
            from_=sender,
            links={"mode": "TEST", "wav": str(out_wav), "via": "local-scheduler"},
        )
        _sf_emit(alert, expires_at=exp_utc)
    except Exception:
        log.exception("Station feed: failed to note originated %s test", code)


def _station_feed_note_manual(
    *,
    event_code: str,
    headline: str,
    voice_mode: str,
    same_codes,
    area_text: str,
    out_wav: str,
    sender: str | None = None,
    expires_in_minutes: int | None = None,
    actor: str | None = None,
) -> None:
    if not _sf_enabled() or StationFeedAlert is None:
        return
    try:
        event_text = _same_label_or_code(event_code)
        now_utc = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        expires_at = now_utc + dt.timedelta(minutes=max(1, int(expires_in_minutes or 30)))
        sender_name = (sender or "SeasonalWeather").strip()
        if voice_mode == "full_eas":
            sf_headline = _sf_make_eas_headline(
                org="WXR",
                event_text=event_text,
                area_text=area_text,
                start_utc=now_utc,
                end_utc=expires_at,
                sender=sender_name,
            )
        else:
            sf_headline = (headline or event_text or "Manual message").strip()

        links = {
            "mode": ("FULL" if voice_mode == "full_eas" else "VOICE"),
            "wav": str(out_wav),
            "via": "local-api",
        }
        if actor:
            links["actor"] = str(actor).strip()[:64]

        alert = StationFeedAlert(
            id=_sf_sha1_12(f"api:{event_code}:{headline}:{out_wav}:{now_utc.isoformat()}"),
            event=str(event_text),
            headline=str(sf_headline),
            severity="Unknown",
            urgency="Unknown",
            certainty="Observed",
            area=str(area_text),
            effective=_sf_iso(now_utc),
            ends=_sf_iso(expires_at),
            expires=_sf_iso(expires_at),
            sent=_sf_iso(now_utc),
            sameCodes=[str(x).strip() for x in (same_codes or []) if str(x).strip()],
            from_=(FeedSender(name=sender_name, kind="origin") if FeedSender else None),
            links=links,
        )
        _sf_emit(alert, expires_at=expires_at)
    except Exception:
        log.exception("Station feed: failed to note manual origination")






def _sf_emit(alert, *, expires_at=None) -> None:
    if not _sf_enabled():
        return
    try:
        _station_id, _source, max_items, ttl_s = _sf_cfg()
        _sf_station_feed_hk_start()
        now_ts = time.time()
        exp_dt = _sf_parse_dt(expires_at)
        exp_ts = exp_dt.timestamp() if exp_dt else (now_ts + ttl_s)
        _STATION_FEED_STATE[alert.id] = (alert, exp_ts)
        _sf_repo_upsert(alert, expires_at=exp_dt or expires_at or dt.datetime.fromtimestamp(exp_ts, tz=dt.timezone.utc))
    except Exception:
        log.exception("Station feed: failed to update handled-alerts feed")


def _sf_remove_ids(ids) -> int:
    if not _sf_enabled():
        return 0
    removed = 0
    try:
        now_ts = time.time()
        requested_ids = []
        for raw in ids or []:
            sid = str(raw or "").strip()
            if not sid:
                continue
            requested_ids.append(sid)
            if _STATION_FEED_STATE.pop(sid, None) is not None:
                removed += 1
        if requested_ids:
            _sf_repo_delete(requested_ids)
    except Exception:
        log.exception("Station feed: failed removing ids=%s", ids)
    return removed


def _sf_remove_by_vtec_tracks(tracks) -> int:
    track_ids = {(t[0] if isinstance(t, tuple) else t) for t in (tracks or []) if (t[0] if isinstance(t, tuple) else t)}
    return _sf_remove_ids(track_ids)


def _sf_remove_ern_relays_matching(code: str | None, same_locs) -> int:
    """Remove ERN relay feed entries shadowed by authoritative alert state."""
    if not _sf_enabled():
        return 0
    code_u = (code or "").strip().upper()
    wanted_locs = {str(x).strip() for x in (same_locs or []) if str(x).strip()}
    if not code_u or not wanted_locs:
        return 0
    wanted_event = _same_label_or_code(code_u)
    remove_ids: list[str] = []
    try:
        for sid, item in list(_STATION_FEED_STATE.items()):
            alert = item[0]
            links = getattr(alert, "links", None) or {}
            sender = getattr(alert, "from_", None)
            sender_kind = getattr(sender, "kind", "") if sender is not None else ""
            is_ern = str(links.get("via", "")).upper() == "ERN/GWES" or str(sender_kind).lower() == "relay"
            if not is_ern:
                continue
            event_text = str(getattr(alert, "event", "") or "").strip()
            if event_text and event_text.upper() not in {code_u, wanted_event.upper()}:
                continue
            alert_locs = {str(x).strip() for x in (getattr(alert, "sameCodes", None) or []) if str(x).strip()}
            if alert_locs and alert_locs.issubset(wanted_locs):
                remove_ids.append(str(sid))
    except Exception:
        log.exception("Station feed: failed scanning ERN relay entries for code=%s", code_u)
        return 0
    return _sf_remove_ids(remove_ids)


def _sf_cap_reference_ids(ev) -> list[str]:
    refs = getattr(ev, "references", None)
    if not isinstance(refs, (list, tuple)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in refs:
        s = str(raw or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _sf_vtec_track_ids(vtec_list) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in (vtec_list or []):
        tid = _vtec_track_id(str(raw))
        if not tid or tid in seen:
            continue
        seen.add(tid)
        out.append(tid)
    return out


def _sf_vtec_tracks(vtec_list) -> list[tuple[str, str]]:
    """
    Module-level VTEC parser for station-feed helpers.
    Returns [(track_id, action)] where track_id := OFFICE.PHEN.SIG.ETN.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in (vtec_list or []):
        s = "".join(str(raw).split()).strip()
        if not s:
            continue
        m = _VTEC_PARSE_RE.search(s)
        if not m:
            continue
        track = f"{m.group('office')}.{m.group('phen')}.{m.group('sig')}.{m.group('etn')}"
        act = (m.group('act') or '').upper()
        key = f"{track}|{act}"
        if key in seen:
            continue
        seen.add(key)
        out.append((track, act))
    return out


def _sf_nws_alert_url(alert_ref) -> str | None:
    s = str(alert_ref or "").strip()
    if not s:
        return None
    if s.startswith(("https://", "http://")):
        return s
    return f"https://api.weather.gov/alerts/{s}"


def _sf_is_vtec_track_id(value) -> bool:
    s = str(value or "").strip().upper()
    if not s:
        return False
    return bool(re.fullmatch(r"[A-Z]{4}\.[A-Z]{2}\.[A-Z]\.[0-9]{4}", s))


def _sf_bad_nws_alert_link_track(value) -> str | None:
    s = str(value or "").strip()
    if not s:
        return None
    prefix = "https://api.weather.gov/alerts/"
    if not s.startswith(prefix):
        return None
    target = s[len(prefix):].strip()
    return target if _sf_is_vtec_track_id(target) else None


def _sf_active_alerts_for_link_repair() -> list[dict]:
    try:
        import requests  # type: ignore
        r = requests.get(
            "https://api.weather.gov/alerts/active",
            headers={"User-Agent": "(seasonalnet.org, info@seasonalnet.org)"},
            timeout=10,
        )
        if not r.ok:
            return []
        feats = (r.json() or {}).get("features") or []
        return [f for f in feats if isinstance(f, dict)]
    except Exception:
        return []


def _sf_repair_restored_links(alert_id, item, links, *, active_features=None):
    out = dict(links or {})
    bad_track = _sf_bad_nws_alert_link_track(out.get("nws"))
    if not bad_track:
        return out

    track_ids = _sf_vtec_track_ids(out.get("vtec") or [])
    if not track_ids and _sf_is_vtec_track_id(alert_id):
        track_ids = [str(alert_id).strip()]
    if bad_track not in track_ids:
        track_ids.insert(0, bad_track)

    candidates = []
    for feat in (active_features or []):
        try:
            props = feat.get("properties") if isinstance(feat, dict) else None
            if not isinstance(props, dict):
                continue
            params = props.get("parameters") if isinstance(props.get("parameters"), dict) else {}
            raw_vtec = params.get("VTEC") if isinstance(params, dict) else []
            feat_tracks = _sf_vtec_track_ids(raw_vtec or [])
            if not feat_tracks or not any(t in feat_tracks for t in track_ids):
                continue

            score = 0
            if bad_track in feat_tracks:
                score += 100

            feat_event = str(props.get("event") or "")
            item_event = str(item.get("event") or "")
            if feat_event and item_event and feat_event == item_event:
                score += 20

            feat_sent = _sf_iso(props.get("sent"))
            item_sent = _sf_iso(item.get("sent"))
            if feat_sent and item_sent and feat_sent == item_sent:
                score += 15

            feat_effective = _sf_iso(props.get("effective"))
            item_effective = _sf_iso(item.get("effective"))
            if feat_effective and item_effective and feat_effective == item_effective:
                score += 10

            feat_ends = _sf_iso(props.get("ends") or props.get("eventEndingTime"))
            item_ends = _sf_iso(item.get("ends"))
            if feat_ends and item_ends and feat_ends == item_ends:
                score += 10

            feat_area = str(props.get("areaDesc") or "")
            item_area = str(item.get("area") or "")
            if feat_area and item_area and feat_area == item_area:
                score += 10

            feat_same = {str(x).strip() for x in (((props.get("geocode") or {}).get("SAME") or []) if isinstance(props.get("geocode"), dict) else []) if str(x).strip()}
            item_same = {str(x).strip() for x in (item.get("sameCodes") or []) if str(x).strip()}
            if feat_same and item_same:
                if feat_same == item_same:
                    score += 20
                elif feat_same & item_same:
                    score += 5

            url = _sf_nws_alert_url(props.get("id") or props.get("@id") or feat.get("id"))
            if not url:
                continue
            candidates.append((score, url))
        except Exception:
            continue

    if not candidates:
        return out

    candidates.sort(key=lambda x: x[0], reverse=True)
    best_score, best_url = candidates[0]
    if best_score <= 0:
        return out
    tied = {url for score, url in candidates if score == best_score}
    if len(tied) != 1:
        return out

    out["nws"] = best_url
    try:
        log.info("Station feed: repaired restored NWS link id=%s track=%s url=%s", alert_id, bad_track, best_url)
    except Exception:
        pass
    return out


def _sf_eas_article(word: str) -> str:
    w = (word or "").strip()
    return "an" if (w[:1].lower() in "aeiou") else "a"


# EAS org prefix and event labels are now authoritative in:
#   .same.events  → _eas_org_broadcast_prefix(), _same_label_for(), _same_label_or_code()
# The shim below keeps _sf_make_eas_headline() working without further changes.
_SF_EAS_ORG_PREFIX = None  # unused sentinel; callers use _eas_org_broadcast_prefix()

# Minimal SAME/ERN relay event map (retained for ERN relay path only).
# For all other label lookups use _same_label_or_code() / _same_label_for().
_SF_EAS_EVENT_LABELS = {
    "RWT": "Required Weekly Test",
    "RMT": "Required Monthly Test",
    "DMO": "Practice/Demo Warning",
    "FFW": "Flash Flood Warning",
    "FFA": "Flash Flood Watch",
    "FLW": "Flood Warning",
    "FLS": "Flood Statement",
    "SVR": "Severe Thunderstorm Warning",
    "SVA": "Severe Thunderstorm Watch",
    "TOR": "Tornado Warning",
    "TOA": "Tornado Watch",
    "SMW": "Special Marine Warning",
    "SPS": "Special Weather Statement",
}


# ZCZC-ORG-EEE-LLLLLL-LLLLLL+TTTT-JJJHHMM-SENDER-
_SF_ZCZC_RE = re.compile(
    r"^ZCZC-"
    r"(?P<org>[A-Z]{3})-"
    r"(?P<event>[A-Z0-9]{3})-"
    r"(?P<locs>\d{6}(?:-\d{6})*)"
    r"\+(?P<dur>\d{4})-"
    r"(?P<jday>\d{3})(?P<hh>\d{2})(?P<mm>\d{2})-"
    r"(?P<sender>[^-]{1,16})-?$"
)

def _sf_same_jday_to_utc(jday: int, hh: int, mm: int):
    now = dt.datetime.now(dt.timezone.utc)
    base = dt.datetime(now.year, 1, 1, tzinfo=dt.timezone.utc)
    cand = base + dt.timedelta(days=jday - 1, hours=hh, minutes=mm)

    # Year rollover sanity: choose the closest plausible year
    cands = [cand]
    try:
        cands.append(cand.replace(year=cand.year - 1))
        cands.append(cand.replace(year=cand.year + 1))
    except Exception:
        pass
    return min(cands, key=lambda x: abs((x - now).total_seconds()))

def _sf_parse_same_header(zczc_text):
    s = str(zczc_text or "").strip()
    # Sometimes downstream strings may include extra whitespace/newlines
    s = "".join(s.split())
    m = _SF_ZCZC_RE.match(s)
    if not m:
        return None
    org = m.group("org")
    event_code = m.group("event")
    same_codes = [x for x in m.group("locs").split("-") if x]
    dur = m.group("dur")
    jday = int(m.group("jday"))
    hh = int(m.group("hh"))
    mm = int(m.group("mm"))
    sender = m.group("sender")

    start_utc = _sf_same_jday_to_utc(jday, hh, mm)
    end_utc = start_utc + dt.timedelta(hours=int(dur[:2]), minutes=int(dur[2:]))

    return {
        "org": org,
        "event_code": event_code,
        "same_codes": same_codes,
        "sender": sender,
        "start_utc": start_utc,
        "end_utc": end_utc,
        "raw": s,
    }

def _sf_fmt_local(dt_obj):
    try:
        return dt_obj.astimezone().strftime("%-I:%M %p on %b %-d, %Y")
    except Exception:
        try:
            return dt_obj.isoformat()
        except Exception:
            return str(dt_obj)

def _sf_make_eas_headline(*, org, event_text, area_text, start_utc, end_utc, sender):
    prefix = _eas_org_broadcast_prefix(org)
    event_text = str(event_text or "Alert").strip() or "Alert"
    area_text = str(area_text or "").strip() or "Unknown area"
    article = _sf_eas_article(event_text)
    return (
        f"{prefix} {article.upper()} {event_text.upper()} for the following counties or areas: "
        f"{area_text}; at {_sf_fmt_local(start_utc)} Effective until {_sf_fmt_local(end_utc)}. "
        f"Message from {sender}."
    )



# VTEC phen.sig → event label lookup is now authoritative in:
#   .alerts.vtec.phen_sig_label()  (imported as _vtec_phen_sig_label)
# Config-level overrides are merged in _sf_nwws_event_label() below.


_DEFAULT_SF_NWWS_TZ_OFFSETS: dict[str, dt.tzinfo] = {
    "UTC": dt.timezone.utc,
    "GMT": dt.timezone.utc,
    "EST": dt.timezone(dt.timedelta(hours=-5)),
    "EDT": dt.timezone(dt.timedelta(hours=-4)),
    "CST": dt.timezone(dt.timedelta(hours=-6)),
    "CDT": dt.timezone(dt.timedelta(hours=-5)),
    "MST": dt.timezone(dt.timedelta(hours=-7)),
    "MDT": dt.timezone(dt.timedelta(hours=-6)),
    "PST": dt.timezone(dt.timedelta(hours=-8)),
    "PDT": dt.timezone(dt.timedelta(hours=-7)),
    "AKST": dt.timezone(dt.timedelta(hours=-9)),
    "AKDT": dt.timezone(dt.timedelta(hours=-8)),
    "HST": dt.timezone(dt.timedelta(hours=-10)),
    "AST": dt.timezone(dt.timedelta(hours=-4)),
    "ADT": dt.timezone(dt.timedelta(hours=-3)),
}


def _sf_nwws_tzinfo_from_override(value):
    if value is None:
        return None
    if isinstance(value, dt.tzinfo):
        return value
    s = str(value).strip()
    if not s:
        return None
    su = s.upper()
    if su in {"UTC", "GMT", "Z"}:
        return dt.timezone.utc
    m = re.fullmatch(r"([+-])(\d{1,2}):(\d{2})", s)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        hours = int(m.group(2))
        minutes = int(m.group(3))
        return dt.timezone(sign * dt.timedelta(hours=hours, minutes=minutes))
    if re.fullmatch(r"[+-]?\d+", s):
        try:
            mins = int(s)
            return dt.timezone(dt.timedelta(minutes=mins))
        except Exception:
            return None
    return None


def _sf_nwws_tz_offsets() -> dict[str, dt.tzinfo]:
    out = dict(_DEFAULT_SF_NWWS_TZ_OFFSETS)
    try:
        cfg = getattr(getattr(_APP_CFG, "station_feed", None), "nwws", None)
        overrides = getattr(cfg, "tz_abbrev_overrides", {}) if cfg is not None else {}
        if isinstance(overrides, dict):
            for raw_key, raw_val in overrides.items():
                key = str(raw_key or "").strip().upper()
                if not key:
                    continue
                tzinfo = _sf_nwws_tzinfo_from_override(raw_val)
                if tzinfo is not None:
                    out[key] = tzinfo
    except Exception:
        pass
    return out


def _sf_nwws_event_label(prod_type: str, *, vtec_list=None, text: str = "") -> str:
    """
    Resolve an NWWS product to a human-readable event label.

    Priority:
      1. Config-level vtec_event_labels overrides (if any)
      2. vtec.phen_sig_label() — authoritative table in vtec.py
      3. _sf_nwws_event_from_text() — product body text extraction
      4. EAS event code via events.label_or_code() as last resort
    """
    # Load any config-level overrides
    config_overrides: dict[str, str] = {}
    try:
        cfg = getattr(getattr(_APP_CFG, "station_feed", None), "nwws", None)
        raw_ovr = getattr(cfg, "vtec_event_labels", {}) if cfg is not None else {}
        if isinstance(raw_ovr, dict):
            for raw_key, raw_val in raw_ovr.items():
                key = str(raw_key or "").strip().upper()
                val = str(raw_val or "").strip()
                if key and val:
                    config_overrides[key] = val
    except Exception:
        pass

    for raw in (vtec_list or []):
        m = _VTEC_PARSE_RE.search(str(raw or ""))
        if not m:
            continue
        phen_sig = f"{m.group('phen')}.{m.group('sig')}"
        # Config overrides take highest priority
        if phen_sig in config_overrides:
            return config_overrides[phen_sig]
        label = _vtec_phen_sig_label(phen_sig)
        if label:
            return label

    text_label = _sf_nwws_event_from_text(text)
    if text_label:
        return text_label
    return _same_label_or_code(prod_type)


def _sf_nwws_titlecase_event(text: str) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip(" .")
    if not s:
        return ""
    if s.upper() == s:
        s = s.title().replace("Nws", "NWS")
    return s


def _sf_nwws_parse_header_issued_dt(text: str):
    tz_map = _sf_nwws_tz_offsets()
    for ln in (text or "").splitlines()[:120]:
        s = (ln or "").strip()
        m = _NWS_HEADER_ISSUED_RE.match(s)
        if not m:
            continue
        hhmm = m.group("hhmm")
        if len(hhmm) == 3:
            hour = int(hhmm[0]); minute = int(hhmm[1:])
        else:
            hour = int(hhmm[:2]); minute = int(hhmm[2:])
        ampm = m.group("ampm").upper()
        if ampm == "AM":
            hour = 0 if hour == 12 else hour
        else:
            hour = 12 if hour == 12 else hour + 12
        month = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}.get(m.group("mon").strip().upper())
        tzinfo = tz_map.get(m.group("tz").strip().upper())
        if month is None or tzinfo is None:
            continue
        try:
            return dt.datetime(int(m.group("year")), month, int(m.group("day")), hour, minute, tzinfo=tzinfo)
        except Exception:
            continue
    return None


def _sf_nwws_best_issued_dt(parsed, official_text: str):
    issued = _sf_parse_dt(getattr(parsed, "issued", None))
    if issued is not None:
        if issued.tzinfo is None:
            issued = issued.replace(tzinfo=dt.timezone.utc)
        return issued
    return _sf_nwws_parse_header_issued_dt(official_text)


def _sf_nwws_extract_issuer(text: str, fallback_wfo: str = "") -> str:
    for ln in (text or "").splitlines()[:80]:
        s = re.sub(r"\s+", " ", (ln or "").strip())
        if s.lower().startswith("national weather service "):
            return "NWS " + s[len("National Weather Service "):].strip()
    f = (fallback_wfo or "").strip()
    return f"NWS {f}".strip() if f else "NWS"


def _sf_nwws_event_from_text(text: str) -> str:
    for ln in (strip_nws_product_headers(text or "") or "").splitlines()[:80]:
        s = re.sub(r"\s+", " ", (ln or "").strip())
        if not s:
            continue
        m = re.match(r"^\.\.\.(?P<ev>.+?)(?:\s+(?:NOW\s+)?IN EFFECT.*)?\.\.\.$", s, flags=re.IGNORECASE)
        if m:
            ev = _sf_nwws_titlecase_event(m.group("ev"))
            if re.search(r"\b(?:warning|watch|advisory|statement|emergency|message)\b", ev, flags=re.IGNORECASE):
                return ev
        if re.search(r"\b(?:warning|watch|advisory|statement|emergency|message)\b$", s, flags=re.IGNORECASE):
            return _sf_nwws_titlecase_event(s)
    return ""



def _sf_nwws_area_from_text(text: str) -> str:
    lines = [re.sub(r"\s+", " ", (ln or "").strip()) for ln in (strip_nws_product_headers(text or "") or "").splitlines()]
    lines = [ln for ln in lines if ln]
    for i, ln in enumerate(lines):
        if re.match(r"^\*\s*WHERE\.\.\.", ln, flags=re.IGNORECASE):
            parts = [re.sub(r"^\*\s*WHERE\.\.\.\s*", "", ln, flags=re.IGNORECASE).strip()]
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if re.match(r"^\*\s*[A-Z][A-Z /-]*\.\.\.", nxt) or nxt.startswith("*"):
                    break
                parts.append(nxt.strip())
                j += 1
            out = re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip(" .")
            if out:
                return out
    for i, ln in enumerate(lines):
        if re.match(r"^\*\s+.+?\s+for\.\.\.$", ln, flags=re.IGNORECASE):
            parts = []
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if nxt.startswith("*") or re.match(r"^(At|HAZARD\.\.\.|SOURCE\.\.\.|IMPACT\.\.\.|TORNADO\.|MAX )", nxt, flags=re.IGNORECASE):
                    break
                parts.append(nxt.strip(" ."))
                j += 1
            out = re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip(" .")
            if out:
                return out
    return ""


def _sf_fmt_issued_until(issued_dt, end_dt, issuer: str) -> str:
    bits = []
    if issued_dt is not None:
        try:
            bits.append(issued_dt.astimezone().strftime("issued %B %-d at %-I:%M%p %Z"))
        except Exception:
            bits.append(f"issued {_sf_iso(issued_dt)}")
    if end_dt is not None:
        try:
            bits.append(end_dt.astimezone().strftime("until %B %-d at %-I:%M%p %Z"))
        except Exception:
            bits.append(f"until {_sf_iso(end_dt)}")
    if issuer:
        bits.append(f"by {issuer}")
    return " ".join(bits).strip()


def _sf_nwws_make_headline(event_text: str, *, issued_dt=None, end_dt=None, issuer: str = "") -> str:
    ev = str(event_text or "Alert").strip() or "Alert"
    suffix = _sf_fmt_issued_until(issued_dt, end_dt, issuer)
    return f"{ev} {suffix}".strip() if suffix else ev


### STATION_FEED_HOUSEKEEPING_PATCH ###


def _sf_hk_interval_s() -> int:
    if _APP_CFG is None:
        return 60
    return max(5, _APP_CFG.station_feed.housekeeping.interval_sec)

def _sf_hk_grace_s() -> int:
    if _APP_CFG is None:
        return 5
    return max(0, _APP_CFG.station_feed.housekeeping.grace_sec)





# Periodic station-feed housekeeping for the SQLite read model and in-memory cache.
_STATION_FEED_HK_STARTED = False

def _sf_station_feed_housekeeping_once():
    """
    Prune the in-memory StationFeed cache and SQLite read model.
    """
    if not _sf_enabled():
        return
    if _APP_CFG is not None and not _APP_CFG.station_feed.housekeeping.enabled:
        return

    try:
        now_ts = time.time()
        _station_id, _source, max_items, _ttl_s = _sf_cfg()

        _sf_prune(now_ts, max_items=max_items)
        _sf_repo_housekeep(now_ts, max_items=max_items)
    except Exception as exc:
        log.exception("Station feed housekeeping: tick failed")
        _sf_diagnose(exc, "Station-feed housekeeping tick failed.")

def _sf_station_feed_hk_loop():
    while True:
        try:
            _sf_station_feed_housekeeping_once()
        except Exception:
            log.exception("Station feed housekeeping: loop error")
        time.sleep(_sf_hk_interval_s())

def _sf_station_feed_hk_start():
    global _STATION_FEED_HK_STARTED
    if _STATION_FEED_HK_STARTED:
        return
    if not _sf_enabled():
        return

    try:
        import threading

        t = threading.Thread(
            target=_sf_station_feed_hk_loop,
            name="station-feed-housekeeping",
            daemon=True,
        )
        t.start()
        _STATION_FEED_HK_STARTED = True

        try:
            log.info(
                "Station feed housekeeping enabled (interval=%ss)",
                _sf_hk_interval_s(),
            )
        except Exception:
            pass
    except Exception:
        try:
            log.exception("Station feed housekeeping: failed to start")
        except Exception:
            pass

# Housekeeping is started by Orchestrator.__init__ after cfg is loaded.


def _sf_is_non_alert_station_item(*, alert_id=None, source=None, event=None, headline=None, cycle_only=False) -> bool:
    """Return True for internal cycle-only items that should not appear in StationFeed."""
    try:
        aid = str(alert_id or "").strip()
        src = str(source or "").strip().upper()
        ev = str(event or "").strip().lower()
        hd = str(headline or "").strip().lower()
        if aid.startswith("PNS_SAFETY:"):
            return True
        if src == "PNS_CYCLE":
            return True
        if cycle_only and (ev == "severe weather safety rules" or hd == "severe weather safety rules"):
            return True
    except Exception:
        return False
    return False



def _station_feed_note_cap(ev, *, mode: str, same_locations, out_wav: str, same_code=None, vtec=None) -> None:
    if not _sf_enabled():
        return
    try:
        vtec_list = list(vtec or [])
        vtec_tracks = _sf_vtec_track_ids(vtec_list)
        vtec_actions = {act for (_track, act) in _sf_vtec_tracks(vtec_list)} if vtec_list else set()
        if vtec_actions & {"CAN", "EXP"}:
            _sf_remove_by_vtec_tracks(vtec_tracks)
            _sf_remove_ids(_sf_cap_reference_ids(ev) + [getattr(ev, "alert_id", None)])
            return
        cap_alert_ref = getattr(ev, "alert_id", None) or getattr(ev, "id", None)
        alert_id = (vtec_tracks[0] if vtec_tracks else None) or cap_alert_ref or _sf_sha1_12(str(ev))
        nws_alert_url = _sf_nws_alert_url(cap_alert_ref)
        event = getattr(ev, "event", None) or "Alert"
        headline = getattr(ev, "headline", None) or event
        severity = getattr(ev, "severity", None) or "Unknown"
        urgency = getattr(ev, "urgency", None) or "Unknown"
        certainty = getattr(ev, "certainty", None) or "Unknown"
        area = getattr(ev, "area_desc", None) or getattr(ev, "area", None) or ""
        # Times: internal CAP event objects don't always carry these fields.
        # Prefer explicit ev.* fields; otherwise derive END from VTEC; optionally backfill from api.weather.gov.
        effective_raw = getattr(ev, "effective", None)
        ends_raw = getattr(ev, "ends", None)
        expires_raw = getattr(ev, "expires", None)
        sent_raw = getattr(ev, "sent", None)

        def _sf_best_end_from_vtec(vtec_list):
            # Pull the END token after '-' if present: ...-YYYYMMDDThhmmZ/
            try:
                ends = []
                for raw in vtec_list or []:
                    ss = "".join(str(raw).split()).strip()
                    if not ss:
                        continue
                    m = re.search(r"-((?:\d{8}|\d{6})T\d{4}Z)", ss)
                    if not m:
                        continue
                    txt = m.group(1).upper()
                    mm = re.fullmatch(r"(\d{8}|\d{6})T(\d{4})Z", txt)
                    if not mm:
                        continue

                    d = mm.group(1)
                    hm = mm.group(2)

                    if len(d) == 8:
                        year = int(d[0:4])
                        month = int(d[4:6])
                        day = int(d[6:8])
                    else:
                        year = 2000 + int(d[0:2])
                        month = int(d[2:4])
                        day = int(d[4:6])

                    hour = int(hm[0:2])
                    minute = int(hm[2:4])

                    ends.append(dt.datetime(year, month, day, hour, minute, tzinfo=dt.timezone.utc))

                return max(ends) if ends else None
            except Exception:
                return None

        # Try to derive end time from VTEC if missing
        vtec_list = vtec or getattr(ev, "vtec", None) or getattr(ev, "vtec_list", None)
        if not isinstance(vtec_list, list):
            vtec_list = [vtec_list] if vtec_list else []
        vtec_end = _sf_best_end_from_vtec(vtec_list)

        if vtec_end:
            ends_raw = ends_raw or vtec_end
            expires_raw = expires_raw or vtec_end
            effective_raw = effective_raw or sent_raw  # best-effort

        # Optional: backfill from NWS alert detail endpoint (handles urn:oid IDs)
        if (_APP_CFG.station_feed.fetch_nws if _APP_CFG else False) and nws_alert_url:
            try:
                import requests  # type: ignore
                r = requests.get(
                    nws_alert_url,
                    headers={"User-Agent": "(seasonalnet.org, info@seasonalnet.org)"},
                    timeout=8,
                )
                if r.ok:
                    props = (r.json() or {}).get("properties", {}) or {}
                    sent_raw = sent_raw or props.get("sent")
                    effective_raw = effective_raw or props.get("effective")
                    ends_raw = ends_raw or props.get("ends") or props.get("eventEndingTime")
                    expires_raw = expires_raw or props.get("expires")
            except Exception:
                pass


        # Final safety fallback so station-feed entries don't end up immortal/blank
        # (some CAP paths, especially SPS-ish cases, may arrive without ends/expires/VTEC end)
        if not ends_raw and not expires_raw:
            try:
                sent_dt = _sf_parse_dt(sent_raw) if sent_raw else None
            except Exception:
                sent_dt = None
            if sent_dt is not None:
                fallback_end = sent_dt + dt.timedelta(hours=6)
                ends_raw = ends_raw or fallback_end
                expires_raw = expires_raw or fallback_end
                effective_raw = effective_raw or sent_dt

        effective = _sf_iso(effective_raw)
        ends = _sf_iso(ends_raw)
        expires = _sf_iso(expires_raw)
        sent = _sf_iso(sent_raw)

        expires_at = expires_raw or ends_raw

        if (_APP_CFG.station_feed.debug if _APP_CFG else False):
            try:
                keys = sorted(getattr(ev, "__dict__", {}).keys())
            except Exception:
                keys = []
            log.info(
                "Station feed CAP: id=%r sent=%r effective=%r expires=%r ends=%r vtec=%r keys=%s",
                alert_id, sent, effective, expires, ends, vtec_list, keys
            )

        wfo = getattr(ev, "wfo", None) or getattr(ev, "office", None)
        sender_name = f"NWS CAP{f'/{wfo}' if wfo else ''}"
        sender = FeedSender(name=sender_name, kind="origin") if FeedSender else None

        links = {"mode": mode, "wav": out_wav}
        if nws_alert_url:
            links["nws"] = nws_alert_url
        if same_code:
            links["same"] = f"same:{same_code}"
        if vtec:
            links["vtec"] = vtec

        alert = StationFeedAlert(
            id=str(alert_id),
            event=str(event),
            headline=str(headline),
            severity=str(severity),
            urgency=str(urgency),
            certainty=str(certainty),
            area=str(area),
            effective=effective,
            ends=ends,
            expires=expires,
            sent=sent,
            sameCodes=[str(x) for x in (same_locations or [])],
            from_=sender,
            links=links,
        )
        _sf_emit(alert, expires_at=expires_at)
    except Exception:
        log.exception("Station feed: failed to note CAP alert")


def _station_feed_note_ern(ev, *, same_locations, out_wav: str) -> None:
    if not _sf_enabled():
        return
    try:
        raw_text = getattr(ev, "text", None) or ""
        parsed = _sf_parse_same_header(raw_text)

        # Sender badge: use header sender if we parsed it; otherwise fall back
        sender_name = None
        if parsed:
            sender_name = parsed.get("sender")
        sender_name = sender_name or getattr(ev, "sender", None) or "ERN"
        # Keep this "unknown" so the UI doesn't auto-append "(relay)" if it uses sender.kind in labels
        sender = FeedSender(name=str(sender_name), kind="relay") if FeedSender else None

        # Event text: prefer ev.event if already human-readable, else map from SAME event code
        ev_event = (getattr(ev, "event", None) or "").strip()
        event_text = ev_event
        if parsed:
            code = str(parsed.get("event_code") or "").upper()
            if (not event_text) or (event_text.upper() == code):
                event_text = _same_label_or_code(code)
        if not event_text:
            event_text = "EAS Alert"

        # Area text: prefer any precomputed area string, else join whatever same_locations contains
        area_text = str(getattr(ev, "area", None) or "").strip()
        if not area_text:
            area_text = "; ".join([str(x) for x in (same_locations or []) if str(x).strip()])

        # SAME codes + timestamps from header when available
        same_codes = [str(x) for x in (same_locations or [])]
        sent_iso = _sf_iso(getattr(ev, "sent", None))
        effective_iso = None
        ends_iso = None
        expires_iso = None
        expires_at = None

        if parsed:
            same_codes = [str(x) for x in (parsed.get("same_codes") or [])] or same_codes
            start_utc = parsed["start_utc"]
            end_utc = parsed["end_utc"]
            sent_iso = sent_iso or _sf_iso(start_utc)
            effective_iso = _sf_iso(start_utc)
            ends_iso = _sf_iso(end_utc)
            expires_iso = _sf_iso(end_utc)
            expires_at = end_utc

            headline = _sf_make_eas_headline(
                org=parsed.get("org"),
                event_text=event_text,
                area_text=area_text,
                start_utc=start_utc,
                end_utc=end_utc,
                sender=str(sender_name),
            )
        else:
            # Fallback if SAME parse fails: at least don't explode
            headline = getattr(ev, "headline", None) or raw_text or str(event_text)

        alert_id = getattr(ev, "id", None) or _sf_sha1_12(
            f"ern:{event_text}:{headline}:{sender_name}:{out_wav}"
        )

        links = {"mode": "REL", "wav": out_wav, "via": "ERN/GWES"}

        alert = StationFeedAlert(
            id=str(alert_id),
            event=str(event_text),
            headline=str(headline),
            severity="Unknown",
            urgency="Unknown",
            certainty="Unknown",
            area=str(area_text or "Unknown area"),
            effective=effective_iso,
            ends=ends_iso,
            expires=expires_iso,
            sent=sent_iso,
            sameCodes=same_codes,
            from_=sender,
            links=links,
        )
        _sf_emit(alert, expires_at=expires_at)
    except Exception:
        log.exception("Station feed: failed to note ERN relay")


def _station_feed_note_nwws(
    parsed,
    *,
    mode: str,
    same_locations,
    out_wav: str,
    product_id=None,
    expires_at=None,
    vtec=None,
    official_text=None,
    issued_at=None,
    event_text=None,
    headline=None,
    area_text=None,
) -> None:
    if not _sf_enabled():
        return
    try:
        awips = getattr(parsed, "awips_id", None) or getattr(parsed, "awips", None) or ""
        wfo = getattr(parsed, "wfo", None) or ""
        prod_type = getattr(parsed, "product_type", None) or "NWWS"
        base_text = str(official_text or getattr(parsed, "raw_text", "") or "")
        raw_vtec = [str(x) for x in (vtec or []) if str(x).strip()]
        if not raw_vtec and base_text:
            raw_vtec = _VTEC_FIND_RE.findall(base_text)
        vtec_tracks = _sf_vtec_track_ids(raw_vtec)
        vtec_actions = {act for (_track, act) in _sf_vtec_tracks(raw_vtec)} if raw_vtec else set()
        if vtec_actions & {"CAN", "EXP"}:
            _sf_remove_by_vtec_tracks(vtec_tracks)
            return

        key = f"nwws:{prod_type}:{awips}:{wfo}:{issued_at or getattr(parsed, 'issued', None)}"
        alert_id = vtec_tracks[0] if vtec_tracks else _sf_sha1_12(key)
        sender = FeedSender(name="NWWS-OI", kind="origin") if FeedSender else None

        issued_dt = _sf_parse_dt(issued_at) if issued_at is not None else _sf_nwws_best_issued_dt(parsed, base_text)
        if issued_dt is not None and issued_dt.tzinfo is None:
            issued_dt = issued_dt.replace(tzinfo=dt.timezone.utc)

        end_raw = (
            expires_at
            or getattr(parsed, "expires", None)
            or getattr(parsed, "expires_at", None)
            or getattr(parsed, "end", None)
            or getattr(parsed, "end_time", None)
            or getattr(parsed, "valid_until", None)
        )
        if not end_raw and issued_dt is not None:
            end_raw = issued_dt + dt.timedelta(hours=6)
        end_dt = _sf_parse_dt(end_raw)

        event_display = str(event_text or _sf_nwws_event_label(prod_type, vtec_list=raw_vtec, text=base_text)).strip()
        issuer = _sf_nwws_extract_issuer(base_text, fallback_wfo=wfo)
        headline_display = str(headline or _sf_nwws_make_headline(event_display, issued_dt=issued_dt, end_dt=end_dt, issuer=issuer)).strip()
        area_display = str(area_text or "").strip() or _sf_nwws_area_from_text(base_text) or str(wfo)

        links = {"mode": mode, "wav": out_wav}
        if product_id:
            links["nws"] = f"https://api.weather.gov/products/{product_id}"
        if raw_vtec:
            links["vtec"] = raw_vtec

        alert = StationFeedAlert(
            id=str(alert_id),
            event=event_display,
            headline=headline_display,
            severity="Unknown",
            urgency="Unknown",
            certainty="Unknown",
            area=str(area_display),
            effective=_sf_iso(issued_dt),
            ends=_sf_iso(end_dt or end_raw),
            expires=_sf_iso(end_dt or end_raw),
            sent=_sf_iso(issued_dt),
            sameCodes=[str(x) for x in (same_locations or [])],
            from_=sender,
            links=links,
        )
        _sf_emit(alert, expires_at=(end_dt or end_raw))
    except Exception:
        log.exception("Station feed: failed to note NWWS toneout")



# Public aliases used by main.py while StationFeed runtime is being extracted.
set_repository = _sf_set_repository
enabled = _sf_enabled
cfg = _sf_cfg
parse_dt = _sf_parse_dt
iso = _sf_iso
sha1_12 = _sf_sha1_12
repo_upsert = _sf_repo_upsert
prune = _sf_prune
station_feed_housekeeping_start = _sf_station_feed_hk_start
purge_legacy_synthetic_alerts = _sf_purge_legacy_synthetic_alerts
hydrate_persisted_alerts = _sf_hydrate_persisted_alerts
is_non_alert_station_item = _sf_is_non_alert_station_item
remove_ids = _sf_remove_ids
remove_by_vtec_tracks = _sf_remove_by_vtec_tracks
remove_ern_relays_matching = _sf_remove_ern_relays_matching
cap_reference_ids = _sf_cap_reference_ids
vtec_track_ids = _sf_vtec_track_ids
vtec_tracks = _sf_vtec_tracks
nwws_best_issued_dt = _sf_nwws_best_issued_dt
nwws_event_label = _sf_nwws_event_label
nwws_area_from_text = _sf_nwws_area_from_text
nwws_make_headline = _sf_nwws_make_headline
nwws_extract_issuer = _sf_nwws_extract_issuer
make_eas_headline = _sf_make_eas_headline
note_cap = _station_feed_note_cap
note_ern = _station_feed_note_ern
note_nwws = _station_feed_note_nwws
note_manual = _station_feed_note_manual
note_required_test = _station_feed_note_required_test
