# SWCAP1001 — CAP product could not be normalized

## Meaning

A CAP, IPAWS, JSON-LD, or API product was rejected before normalization.

## Trigger

Required product structure, identity, time, or bounded content is invalid.

## Correction or recovery

Retain the bounded source evidence, correct the source or parser contract, and
allow a later delivery to enter the normal ingest path.

## Operational effect

The product cannot affect targeting, deduplication, lifecycle, or broadcast.

## Rationale

Malformed source input must not become an alert decision.

## Alternatives or migration

Use another authoritative source delivery for the same product when available.

## Related diagnostics

- `SWCAP3001` reports a failed source request rather than malformed content.
