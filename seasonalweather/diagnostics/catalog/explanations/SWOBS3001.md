# SWOBS3001 — Optional observability destination failed

## Meaning

A configured optional observability transport failed during bounded delivery.

## Trigger

The destination refused, timed out, reset, or otherwise failed a syslog, OTLP, Alertmanager, or notification delivery.

## Correction or recovery

Inspect the destination's bounded transport and trust logs. Restore it or leave it disabled if it is not required.

## Operational effect

The failed record is retained only by the bounded local sink statistics; the canonical local stream and broadcast path remain available.

## Rationale

External visibility is useful but cannot own alert processing or publication continuity.

## Alternatives or migration

Use local structured logs and controller metrics until the external destination recovers.

## Related diagnostics

Related conditions are SWOBS4001 and SWOBS6001.
