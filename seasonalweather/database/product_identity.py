"""Archive identity evidence; independent of broadcast policy."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid5

from .postgresql_archive import ArchiveOrigin

_NAMESPACE = UUID("e0c49d1b-85a7-40ae-9f72-e13defa21d42")


def stable_id(*parts: str) -> UUID:
    return uuid5(_NAMESPACE, json.dumps(parts, ensure_ascii=True))


def _alias(*parts: str) -> str:
    return json.dumps(parts, separators=(",", ":"))


@dataclass(frozen=True, kw_only=True)
class ProductDelivery:
    """One source occurrence with evidence supplied by its adapter."""

    source: str
    delivery_key: str
    origin: ArchiveOrigin
    received_at: datetime
    text: str
    api_product_id: str = ""
    wmo_heading: str = ""
    awips_id: str = ""
    office: str = ""
    issued_at: datetime | None = None
    vtec_event_ids: tuple[str, ...] = ()
    import_id: UUID | None = None

    def __post_init__(self) -> None:
        if not self.source.strip() or not self.delivery_key.strip() or not self.text.strip():
            raise ValueError("Source, delivery key, and product text are required")
        if self.received_at.utcoffset() is None or (self.issued_at and self.issued_at.utcoffset() is None):
            raise ValueError("Product timestamps must be timezone-aware")

    @property
    def id(self) -> UUID:
        return stable_id(
            "delivery", self.source, self.delivery_key, self.origin.value, str(self.import_id), self.content_hash
        )

    @property
    def content_hash(self) -> str:
        normalized = "\n".join(" ".join(line.split()) for line in self.text.splitlines()).strip()
        return hashlib.sha256(normalized.encode()).hexdigest()

    def aliases(self) -> list[str]:
        api_id = self.api_product_id.strip()
        heading = " ".join(self.wmo_heading.upper().split())
        office = self.office.strip().upper()
        awips = self.awips_id.strip().upper()
        issued = self.issued_at.astimezone(UTC).isoformat() if self.issued_at else ""
        aliases = {_alias("api", api_id)} if api_id else set()
        if heading and office and issued:
            aliases.add(_alias("wmo", heading, office, issued, awips))
        aliases.update(self._vtec_aliases(office, awips, issued))
        return sorted(aliases)

    def _vtec_aliases(self, office: str, awips: str, issued: str) -> set[str]:
        if not (issued and office and awips):
            return set()
        return {
            _alias("vtec", event.strip().upper(), office, awips, issued)
            for event in self.vtec_event_ids
            if event.strip()
        }
