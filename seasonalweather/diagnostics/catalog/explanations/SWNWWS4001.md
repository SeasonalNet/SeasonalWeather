# SWNWWS4001 — NWWS source is reconnecting

The source lost a session and is recovering through bounded reconnect and
backoff. This is a degraded optional-source condition, not a new scheduler,
worker, or daemon lifecycle.

Recovery: observe the source health counters and allow the bounded recovery
attempt to complete.

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

See `SWNWWS3001` and `SWNWWS4002`.
