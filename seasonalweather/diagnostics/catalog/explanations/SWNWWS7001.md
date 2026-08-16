# SWNWWS7001 — NWWS lifecycle operation exceeded its bound

Connection, drain, reconnect waiting, or transport cleanup exceeded its
declared time bound. The source never waits indefinitely for a join or
shutdown operation.

Recovery: inspect the bounded health state and controller shutdown evidence.

## Meaning

The NWWS adapter keeps this condition bounded and before controller policy.

## Trigger

The source observes the specific condition described above.

## Correction or recovery

Inspect bounded source health and sanitized runtime-diagnostic evidence.

## Operational effect

The affected session or message is fenced, dropped, or recovered without changing controller ownership.

## Rationale

NWWS transport and lifecycle behavior must remain isolated behind the normalized source contract.

## Alternatives or migration

A replacement implementation must preserve the same contract and replay behavior.

## Related diagnostics

See `SWNWWS8002`.
