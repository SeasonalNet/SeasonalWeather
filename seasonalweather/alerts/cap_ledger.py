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
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

from ..database.alerts import CapLedgerRepository
from ..database.core import SeasonalDatabase


def _utcnow() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


def _parse_iso(s: str) -> dt.datetime | None:
    try:
        if not s:
            return None
        # Accept "Z" or offset forms; normalize to aware UTC
        s2 = s.strip().replace("Z", "+00:00")
        t = dt.datetime.fromisoformat(s2)
        # Treat naive timestamps as UTC (older ledgers, edits, bugs).
        t = t.replace(tzinfo=dt.UTC) if t.tzinfo is None else t.astimezone(dt.UTC)
        return t
    except Exception:
        return None


@dataclass
class CapLedger:
    """
    Tiny persistent "seen" ledger to prevent CAP restart spam.

    Keys are typically "{logical_alert_id}|{sent_iso}" so updates still emit once.
    """

    path: Path | None = None
    max_age_days: int = 14
    database: SeasonalDatabase | None = None

    _seen: dict[str, str] = field(default_factory=dict)
    _loaded: bool = False
    _repo: CapLedgerRepository | None = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        self._repo = CapLedgerRepository(self.database) if self.database is not None else None

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        self._seen = {}

        if self._repo is not None:
            try:
                self._seen = self._repo.load_entries()
            except Exception:
                self._seen = {}

        self.cleanup()

    def _save(self) -> None:
        if self._repo is not None:
            try:
                self._repo.replace_entries(self._seen)
                return
            except Exception:
                return

    @staticmethod
    def make_key(alert_id: str, sent: str | None) -> str:
        a = (alert_id or "").strip()
        s = (sent or "").strip()
        return f"{a}|{s}"

    def has(self, key: str) -> bool:
        self._load()
        return key in self._seen

    def mark(self, key: str) -> None:
        self._load()
        self._seen[key] = _utcnow().isoformat()

    def cleanup(self) -> None:
        # Avoid recursion weirdness: if not loaded, load will call cleanup once.
        if not self._loaded:
            self._load()
            return

        cutoff = _utcnow() - dt.timedelta(days=max(3, int(self.max_age_days)))
        out: dict[str, str] = {}

        for k, v in self._seen.items():
            t = _parse_iso(v)

            # If it won't parse, repair it to "now" (prevents permanent non-expiring junk)
            if t is None:
                t = _utcnow()
                v = t.isoformat()

            # t is guaranteed aware UTC now -> safe compare
            if t >= cutoff:
                out[k] = v

        self._seen = out

    def flush(self) -> None:
        self.cleanup()
        self._save()
