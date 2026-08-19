"""Typed independent segment-builder results.

The registry declares which ``CycleBuilder`` method produces each static
segment.  This module contains only the bounded result contract shared by
those methods and the controller-owned segment store; it is not a second
segment policy registry.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

if TYPE_CHECKING:
    from .cycle import CycleContext, CycleSegment

_MAX_TEXT = 12_000
_MAX_FIELD = 256
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_URL_RE = re.compile(r"(?i)\bhttps?://[^\s<>]+")
_SECRET_RE = re.compile(
    r"(?ix)(?<![a-z0-9_])"
    r"(?P<key>authorization|bearer|token|access[\s_-]*token|"
    r"refresh[\s_-]*token|password|passphrase|secret|client[\s_-]*secret|"
    r"api[\s_-]*key)"
    r"[\"']?\s*(?::|=)\s*"
    r"(?P<value>(?:bearer\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,;)}\]]+))"
)


def _bounded(value: str | None, limit: int = _MAX_FIELD) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]


def _clean_credential_values(text: str) -> str:
    def redact(match: re.Match[str]) -> str:
        return f"{match.group('key')}=[redacted]"

    text = _SECRET_RE.sub(redact, text)
    return re.sub(r"(?i)\bbearer\s+[^\s,;)}\]]+", "bearer [redacted]", text)


def _public_url(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        if not parsed.scheme or not parsed.netloc:
            return None
        hostname = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port is not None else ""
        return urlunsplit((parsed.scheme.lower(), hostname + port, parsed.path[:256], "", ""))[:512]
    except Exception:
        return None


def sanitize_source_reference(value: str | None) -> str | None:
    """Keep a public source identity while removing credentials/query data."""
    text = _bounded(re.sub(r"[\x00-\x1f\x7f]", " ", str(value)) if value is not None else None, 512)
    if text is None:
        return None
    public_url = _public_url(text)
    if public_url is not None:
        return public_url
    text = _URL_RE.sub(lambda match: _public_url(match.group(0)) or "[redacted-url]", text)
    return re.sub(r"\s+", " ", _clean_credential_values(text.split("?", 1)[0].split("#", 1)[0])).strip()[:512]


def sanitize_error(value: BaseException | str | None) -> str | None:
    """Return fail-closed, bounded operator evidence.

    Error text is an evidence field, never a transport for exception details.
    URLs are reduced to their public path and all recognized credential forms
    are replaced before the bounded value is persisted.
    """
    if value is None:
        return None
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value)).strip()

    def clean_url(match: re.Match[str]) -> str:
        return sanitize_source_reference(match.group(0)) or "[redacted-url]"

    text = _URL_RE.sub(clean_url, text)
    text = _clean_credential_values(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_MAX_FIELD] or "build failed"


@dataclass(frozen=True)
class SegmentSourceEvidence:
    """Typed evidence returned by a real upstream source boundary."""

    source_name: str | None = None
    product_identifier: str | None = None
    product_type: str | None = None
    issuing_office: str | None = None
    issuance_time: str | None = None
    fetched_at: dt.datetime | None = None
    source_reference: str | None = None


@dataclass(frozen=True)
class SegmentProvenance:
    source_name: str | None = None
    product_identifier: str | None = None
    product_type: str | None = None
    issuing_office: str | None = None
    issuance_time: str | None = None
    fetch_time: str | None = None
    last_successful_synthesis: str | None = None
    current_content_hash: str | None = None
    source_reference: str | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    stale: bool = False
    placeholder: bool = False
    last_aired: str | None = None
    next_eligible_airtime: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "source_name",
            "product_identifier",
            "product_type",
            "issuing_office",
            "issuance_time",
            "fetch_time",
            "last_successful_synthesis",
            "last_error",
            "last_aired",
            "next_eligible_airtime",
        ):
            value = getattr(self, name)
            safe = sanitize_error(value) if value is not None else None
            object.__setattr__(self, name, _bounded(safe))
        object.__setattr__(self, "source_reference", sanitize_source_reference(self.source_reference))
        object.__setattr__(self, "consecutive_failures", max(0, min(int(self.consecutive_failures), 1_000_000)))
        if self.current_content_hash is not None and not _HASH_RE.fullmatch(self.current_content_hash):
            object.__setattr__(self, "current_content_hash", None)

    def after_success(
        self,
        *,
        text: str,
        fetch_time: str | None,
        synthesis_time: str,
        source: SegmentProvenance | None = None,
    ) -> SegmentProvenance:
        incoming = source or self
        return SegmentProvenance(
            source_name=incoming.source_name,
            product_identifier=incoming.product_identifier,
            product_type=incoming.product_type,
            issuing_office=incoming.issuing_office,
            issuance_time=incoming.issuance_time,
            fetch_time=fetch_time or incoming.fetch_time,
            last_successful_synthesis=synthesis_time,
            current_content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            source_reference=incoming.source_reference,
            last_error=None,
            consecutive_failures=0,
            stale=False,
            placeholder=False,
            last_aired=self.last_aired,
            next_eligible_airtime=self.next_eligible_airtime,
        )

    def after_failure(self, error: BaseException | str) -> SegmentProvenance:
        return SegmentProvenance(
            source_name=self.source_name,
            product_identifier=self.product_identifier,
            product_type=self.product_type,
            issuing_office=self.issuing_office,
            issuance_time=self.issuance_time,
            fetch_time=self.fetch_time,
            last_successful_synthesis=self.last_successful_synthesis,
            current_content_hash=self.current_content_hash,
            source_reference=self.source_reference,
            last_error=sanitize_error(error),
            consecutive_failures=min(self.consecutive_failures + 1, 1_000_000),
            stale=self.stale,
            placeholder=self.placeholder,
            last_aired=self.last_aired,
            next_eligible_airtime=self.next_eligible_airtime,
        )


@dataclass(frozen=True)
class SegmentBuildInput:
    key: str
    context: CycleContext
    station_name: str
    service_area_name: str
    disclaimer: str
    configuration_generation: int = 0
    deadline: float | None = None


@dataclass(frozen=True)
class SegmentCandidate:
    key: str
    title: str
    text: str
    provenance: SegmentProvenance = SegmentProvenance()

    def __post_init__(self) -> None:
        if not self.key.strip() or not self.title.strip():
            raise ValueError("segment candidate identity must be non-empty")
        bounded_text = self.text.strip()[:_MAX_TEXT]
        if not bounded_text:
            raise ValueError("segment candidate text must be non-empty")
        object.__setattr__(self, "text", bounded_text)

    @classmethod
    def from_cycle_segment(
        cls,
        segment: CycleSegment,
        *,
        source_name: str | None = "nws",
        product_type: str | None = None,
        issuing_office: str | None = None,
        fetched_at: dt.datetime | None = None,
        source_reference: str | None = None,
        evidence: SegmentSourceEvidence | None = None,
    ) -> SegmentCandidate:
        source = evidence
        if source is not None:
            source_name = source.source_name
            product_type = source.product_type
            issuing_office = source.issuing_office
            fetched_at = source.fetched_at
            source_reference = source.source_reference
        return cls(
            key=segment.key,
            title=segment.title,
            text=segment.text,
            provenance=SegmentProvenance(
                source_name=source_name,
                product_identifier=source.product_identifier if source is not None else None,
                product_type=product_type,
                issuing_office=issuing_office,
                issuance_time=source.issuance_time if source is not None else None,
                fetch_time=(fetched_at.replace(microsecond=0).isoformat() if fetched_at else None),
                source_reference=source_reference,
            ),
        )


__all__ = [
    "SegmentBuildInput",
    "SegmentCandidate",
    "SegmentProvenance",
    "SegmentSourceEvidence",
    "sanitize_error",
    "sanitize_source_reference",
]
