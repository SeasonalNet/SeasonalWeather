# SWLQS3001 — Liquidsoap control failed

## Meaning

A bounded Liquidsoap control or connection operation failed.

## Trigger

The playout control endpoint rejects, drops, or times out before the mutation
is proven.

## Correction or recovery

Apply the existing bounded control recovery and inspect the playout health
boundary.

## Operational effect

The requested queue mutation is not claimed as successful.

## Rationale

Broadcast publication must not infer playout state from a failed command.

## Alternatives or migration

Retain the controller-owned artifact and retry only through the owning policy.

## Related diagnostics

- `SWLQS8001` reports ambiguous publication evidence requiring reconciliation.
