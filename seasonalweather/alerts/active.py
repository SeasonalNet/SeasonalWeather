"""
active_alerts.py — persistent active-alert registry for the broadcast cycle.

All alert-like audio that should remain in cycle rotation is registered here
while active: CAP, NWWS-OI, IPAWS, ERN relay, and allowed PNS cycle-only
statements.

The SQLite active_alerts tables are authoritative.  Legacy JSON sidecar
compatibility was removed so active state cannot diverge from the database.

Cycle order is operational-priority driven, not arrival-order driven:
warnings/emergencies before watches, watches before advisories, advisories
before statements, then low-priority service/PNS statements.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

from ..database.alerts import AlertStateRepository
from ..database.core import SeasonalDatabase
from ..diagnostics.bindings import FOUNDATION_CODES

log = logging.getLogger("seasonalweather.active_alerts")


def _parse_iso_dt(value: str | None) -> dt.datetime:
    s = (value or "").strip()
    if not s:
        return dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        out = dt.datetime.fromisoformat(s)
        if out.tzinfo is None:
            out = out.replace(tzinfo=dt.timezone.utc)
        return out.astimezone(dt.timezone.utc)
    except Exception:
        return dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)

# VTEC track-id parser: OFFICE.PHEN.SIG.ETN
_VTEC_TRACK_RE = re.compile(
    r"/[A-Z]\.(?:[A-Z]{3})\."
    r"(?P<office>[A-Z]{4})\."
    r"(?P<phen>[A-Z0-9]{2})\."
    r"(?P<sig>[A-Z])\."
    r"(?P<etn>\d{4})\.",
)


def _vtec_track_id(vtec_str: str) -> str | None:
    """Extract 'OFFICE.PHEN.SIG.ETN' from a raw VTEC string, or None."""
    s = "".join(vtec_str.split()).strip()
    m = _VTEC_TRACK_RE.search(s)
    if not m:
        return None
    return f"{m.group('office')}.{m.group('phen')}.{m.group('sig')}.{m.group('etn')}"


_SOURCE_RANK: dict[str, int] = {
    "IPAWS": 0,
    "CAP": 1,
    "NWWS": 1,
    "ERN": 4,
    "PNS_CYCLE": 8,
}

_CRITICAL_CODES: frozenset[str] = frozenset({
    "EAN", "EVI", "CEM", "LAE", "CDW", "NUW", "RHW", "LEW",
    "TOE", "TOR", "FFW", "SVR", "SMW", "EWW",
})
_WATCH_CODES: frozenset[str] = frozenset({"TOA", "SVA", "FFA", "HUA", "WSA", "TRA"})
_ADVISORY_CODES: frozenset[str] = frozenset({
    "ADR", "CAE", "CFW", "CFA", "EWW", "FAW", "FLW", "FLS", "HLS",
    "SPS", "SVS", "MWS", "NPW", "WSW",
})


def _vtec_significance_rank(alert: "ActiveAlert") -> int | None:
    """Return a severity bucket from VTEC significance when present."""
    best: int | None = None
    for tid in alert.vtec_track_ids():
        parts = tid.split(".")
        if len(parts) != 4:
            continue
        sig = parts[2].upper()
        if sig == "W":
            rank = 0
        elif sig == "A":
            rank = 10
        elif sig == "Y":
            rank = 20
        elif sig == "S":
            rank = 30
        else:
            rank = 40
        best = rank if best is None else min(best, rank)
    return best


def alert_priority_sort_key(alert: "ActiveAlert") -> tuple[int, int, dt.datetime, str]:
    """
    Sort key for on-air active-alert rotation.

    Lower buckets are more important.  Chronology is only a tie-breaker inside
    the same operational class; it must never cause a statement to outrank a
    watch or a watch to outrank a warning.
    """
    vtec_rank = _vtec_significance_rank(alert)
    code = (alert.code or "").strip().upper()
    event = (alert.event or alert.headline or "").strip().lower()
    source = (alert.source or "").strip().upper()

    if source == "PNS_CYCLE":
        severity = 60
    elif vtec_rank is not None:
        severity = vtec_rank
    elif code in _CRITICAL_CODES or "emergency" in event or "warning" in event:
        severity = 0
    elif code in _WATCH_CODES or "watch" in event:
        severity = 10
    elif code in _ADVISORY_CODES or "advisory" in event:
        severity = 20
    elif "statement" in event or source == "ERN":
        severity = 30
    else:
        severity = 40

    source_rank = _SOURCE_RANK.get(source, 5)
    first_seen = _parse_iso_dt(alert.first_aired or alert.issued)
    return (severity, source_rank, first_seen, alert.id)


@dataclass
class ActiveAlert:
    # ---- Identity ----
    id: str
    """
    Stable key for this alert slot.
    Prefer VTEC-derived: "CAP:KLWX.TO.A.0045" or "NWWS:KLWX.TO.A.0045".
    Falls back to CAP alert urn or sha1.
    """
    source: str           # "CAP" | "NWWS" | "ERN" | "PNS_CYCLE"
    event: str            # "Tornado Watch", "Severe Thunderstorm Warning", ...
    code: str             # SAME code: "TOA", "SVR", "TOR", ...

    # ---- VTEC ----
    vtec: list[str]       # raw VTEC strings  (may be empty for ERN/PNS)

    # ---- Content ----
    headline: str
    script_text: str      # TTS script that was (or will be) broadcast
    audio_path: Optional[str]   # rendered WAV path on disk (may be stale after restart)

    # ---- Timing ----
    expires: str          # ISO-8601 UTC  (e.g. "2026-03-12T00:00:00+00:00")
    issued: str           # ISO-8601 issued time

    # ---- Targeting ----
    same_locs: list[str]  # in-area FIPS codes used at time of origination

    # ---- Flags ----
    cycle_only: bool = False
    """
    True → voice segment in cycle only; no SAME tones, no 1050 Hz.
    Used for: PNS SEVERE WEATHER SAFETY RULES, advisory-level CAP voice, etc.
    """

    # ---- Watch metadata ----
    watch_number: Optional[int] = None
    """ETN (watch number) from VTEC, populated for TOA/SVA events."""

    # ---- Bookkeeping ----
    first_aired: Optional[str] = None
    last_aired: Optional[str] = None
    airing_count: int = 0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def expires_dt(self) -> dt.datetime:
        s = (self.expires or "").strip()
        if not s:
            return dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            return dt.datetime.fromisoformat(s)
        except Exception:
            return dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)

    def is_expired(self, now: Optional[dt.datetime] = None) -> bool:
        now = now or dt.datetime.now(dt.timezone.utc)
        try:
            return now >= self.expires_dt()
        except Exception:
            return False

    def audio_exists(self) -> bool:
        if not self.audio_path:
            return False
        try:
            return Path(self.audio_path).exists()
        except Exception:
            return False

    def vtec_track_ids(self) -> list[str]:
        """Return all track IDs present in this alert's VTEC list."""
        out: list[str] = []
        for v in (self.vtec or []):
            t = _vtec_track_id(v)
            if t:
                out.append(t)
        return out


class AlertTracker:
    """
    Registry of currently active alerts for the broadcast cycle.

    Persists to the configured SQLite alert-state repository — service
    restarts do not lose active watches or warnings from the on-air rotation.

    All callers run in the same asyncio event loop so no extra asyncio.Lock
    is required (Python GIL + single-threaded asyncio protect the dict).
    """

    def __init__(
        self,
        state_path: str | Path,
        database: SeasonalDatabase | None = None,
        diagnostic_sink: object | None = None,
    ) -> None:
        self._path = Path(state_path)
        self._alerts: dict[str, ActiveAlert] = {}
        self._db = database
        self._repo = AlertStateRepository(database) if database is not None else None
        self._diagnostic_sink: object | None = diagnostic_sink
        self._on_change: Callable[[str], None] | None = None
        self._warned_no_repo = False

    def set_change_callback(self, callback: Callable[[str], None] | None) -> None:
        """Install a synchronous best-effort callback for tracker mutations."""
        self._on_change = callback

    def _emit_change(self, reason: str) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change(reason)
        except Exception:
            log.debug("AlertTracker: change callback failed reason=%s", reason, exc_info=True)

    @staticmethod
    def _loc_set(values: list[str] | None) -> set[str]:
        return {str(v).strip() for v in (values or []) if str(v).strip()}

    def _remove_shadowed_ern_relays_for(self, alert: ActiveAlert) -> int:
        """
        Drop ERN relay entries once an authoritative CAP/NWWS/IPAWS entry fully
        covers the same SAME-code/location set.
        """
        source = (alert.source or "").strip().upper()
        if source == "ERN":
            return 0
        code = (alert.code or "").strip().upper()
        authoritative_locs = self._loc_set(alert.same_locs)
        if not code or not authoritative_locs:
            return 0

        removed = 0
        for aid, existing in list(self._alerts.items()):
            if (existing.source or "").strip().upper() != "ERN":
                continue
            if (existing.code or "").strip().upper() != code:
                continue
            relay_locs = self._loc_set(existing.same_locs)
            if relay_locs and relay_locs.issubset(authoritative_locs):
                self._alerts.pop(aid, None)
                removed += 1
                log.info(
                    "AlertTracker: removed shadowed ERN relay id=%s code=%s authoritative=%s",
                    aid, code, alert.id,
                )
        return removed

    def remove_matching_source(
        self,
        *,
        source: str,
        code: str | None = None,
        same_locs: list[str] | None = None,
        reason: str = "",
    ) -> int:
        """Remove active alerts from one source matching code and SAME coverage."""
        source_u = (source or "").strip().upper()
        code_u = (code or "").strip().upper()
        wanted_locs = self._loc_set(same_locs)
        removed = 0
        for aid, existing in list(self._alerts.items()):
            if (existing.source or "").strip().upper() != source_u:
                continue
            if code_u and (existing.code or "").strip().upper() != code_u:
                continue
            existing_locs = self._loc_set(existing.same_locs)
            if wanted_locs and existing_locs and not existing_locs.issubset(wanted_locs) and not wanted_locs.issubset(existing_locs):
                continue
            self._alerts.pop(aid, None)
            removed += 1
            log.info(
                "AlertTracker: removed source=%s id=%s code=%s reason=%s",
                source_u, aid, existing.code, reason,
            )
        if removed:
            self._persist()
            self._emit_change(f"remove-matching:{source_u}:{reason}")
        return removed

    # ------------------------------------------------------------------ #
    #  Mutation                                                            #
    # ------------------------------------------------------------------ #

    def add_or_update(self, alert: ActiveAlert) -> None:
        """Register or refresh an alert slot. Persists immediately."""
        prev = self._alerts.get(alert.id)
        if prev is not None:
            # Preserve original chronology so updates do not jump the queue.
            alert.issued = prev.issued or alert.issued
            alert.first_aired = prev.first_aired or alert.first_aired
            alert.last_aired = prev.last_aired or alert.last_aired
            alert.airing_count = max(int(prev.airing_count or 0), int(alert.airing_count or 0))
            if alert.watch_number is None:
                alert.watch_number = prev.watch_number
        self._remove_shadowed_ern_relays_for(alert)
        self._alerts[alert.id] = alert
        self._persist()
        self._emit_change(f"add-or-update:{alert.source}:{alert.id}")

    def remove(self, alert_id: str) -> bool:
        """Remove by ID. Returns True if it existed."""
        existed = alert_id in self._alerts
        if existed:
            self._alerts.pop(alert_id)
            self._persist()
            self._emit_change(f"remove:{alert_id}")
        return existed

    def mark_aired(self, alert_id: str) -> None:
        a = self._alerts.get(alert_id)
        if not a:
            return
        now_iso = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
        if a.first_aired is None:
            a.first_aired = now_iso
        a.last_aired = now_iso
        a.airing_count += 1
        self._persist()

    def update_audio_path(self, alert_id: str, audio_path: str) -> None:
        a = self._alerts.get(alert_id)
        if a:
            a.audio_path = audio_path
            self._persist()
            self._emit_change(f"update-audio:{alert_id}")

    def update_script(self, alert_id: str, script_text: str) -> None:
        a = self._alerts.get(alert_id)
        if a:
            a.script_text = script_text
            self._persist()
            self._emit_change(f"update-script:{alert_id}")

    # ------------------------------------------------------------------ #
    #  Queries                                                             #
    # ------------------------------------------------------------------ #

    def get_active(self, now: Optional[dt.datetime] = None) -> list[ActiveAlert]:
        """Return non-expired alerts sorted by operational priority."""
        now = now or dt.datetime.now(dt.timezone.utc)
        active = [a for a in self._alerts.values() if not a.is_expired(now)]
        active.sort(key=alert_priority_sort_key)
        return active

    def get_cycle_alerts(self, now: Optional[dt.datetime] = None) -> list[ActiveAlert]:
        """All active alerts (for cycle segment injection)."""
        return self.get_active(now)

    def has_active(self, now: Optional[dt.datetime] = None) -> bool:
        return bool(self.get_active(now))

    def is_known(self, alert_id: str) -> bool:
        return alert_id in self._alerts

    def find_by_vtec_track(self, track_id: str) -> ActiveAlert | None:
        """Find first alert whose VTEC list contains the given track id."""
        for a in self._alerts.values():
            if track_id in a.vtec_track_ids():
                return a
        return None

    # ------------------------------------------------------------------ #
    #  Housekeeping                                                        #
    # ------------------------------------------------------------------ #

    def purge_expired(self, now: Optional[dt.datetime] = None) -> int:
        now = now or dt.datetime.now(dt.timezone.utc)
        dead = [aid for aid, a in self._alerts.items() if a.is_expired(now)]
        for aid in dead:
            self._alerts.pop(aid)
        if dead:
            self._persist()
            self._emit_change(f"purge-expired:{len(dead)}")
        return len(dead)

    def remove_by_vtec_tracks(self, track_ids: set[str], reason: str = "") -> int:
        """
        Remove alerts whose VTEC contains any of the given track IDs.
        Returns the number of entries removed.
        """
        removed = 0
        for aid in list(self._alerts.keys()):
            a = self._alerts.get(aid)
            if not a:
                continue
            for tid in a.vtec_track_ids():
                if tid in track_ids:
                    self._alerts.pop(aid, None)
                    removed += 1
                    log.info(
                        "AlertTracker: removed id=%s event=%s track=%s reason=%s",
                        aid, a.event, tid, reason,
                    )
                    break
        if removed:
            self._persist()
            self._emit_change(f"remove-by-vtec:{reason}")
        return removed

    # ------------------------------------------------------------------ #
    #  Persistence                                                         #
    # ------------------------------------------------------------------ #

    def load(self) -> int:
        """Load state from SQLite when the database is enabled."""
        self._alerts = {}
        if self._repo is None:
            log.warning("AlertTracker: SQLite repository unavailable; active-alert state is memory-only")
            return 0
        try:
            rows = self._repo.load_active_alerts()
            for raw in rows:
                self._alerts[str(raw["id"])] = ActiveAlert(**raw)  # type: ignore[arg-type]
            if self._alerts:
                log.info("AlertTracker: loaded %d entries from SQLite", len(self._alerts))
            return len(self._alerts)
        except Exception as exc:
            log.exception("AlertTracker: SQLite load failed")
            self._diagnose(exc)
            return 0

    def _persist(self) -> None:
        if self._repo is None:
            if not self._warned_no_repo:
                log.warning("AlertTracker: SQLite repository unavailable; legacy JSON fallback is disabled")
                self._warned_no_repo = True
            return
        try:
            now_iso = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
            payload = []
            for alert in self._alerts.values():
                raw = asdict(alert)
                raw.setdefault("created_at", alert.issued or now_iso)
                raw["updated_at"] = now_iso
                payload.append(raw)
            self._repo.replace_active_alerts(payload)
        except Exception as exc:
            log.exception("AlertTracker: SQLite persist failed; legacy JSON fallback is disabled")
            self._diagnose(exc)

    def _diagnose(self, exception: BaseException) -> None:
        sink = self._diagnostic_sink
        emit = getattr(sink, "emit", None)
        if not callable(emit):
            return
        try:
            _ = emit(
                FOUNDATION_CODES["database.operation_failed"],
                component="alert-tracker",
                message="Active-alert state persistence failed at the SQLite authority boundary.",
                operational_effect="Active-alert continuity across restart or subsequent cycle updates may be degraded.",
                recovery_action="Inspect the bounded database failure and reconcile active-alert state before relying on restart recovery.",
                exception=exception,
                source_id="alert-tracker",
            )
        except Exception:
            return
