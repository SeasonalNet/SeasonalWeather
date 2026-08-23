# SWLQS8001 — Broadcast publication requires reconciliation

## Meaning

A queue mutation or active-target publication has ambiguous evidence.

## Trigger

Control interruption occurs after submission but before the result is proven.

## Correction or recovery

Reconcile through the controller publication authority and existing durable
journal before retrying.

## Operational effect

The prior audio remains authoritative until publication is proven.

## Rationale

Playout success cannot be established from an unacknowledged control event.

## Alternatives or migration

Use the prior active target or bounded recovery procedure while reconciliation is
pending.

## Related diagnostics

- `SWLQS3001` reports a direct Liquidsoap control failure.
