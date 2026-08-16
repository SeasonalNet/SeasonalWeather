# SWNWWS8002 — NWWS source lifecycle cleanup failed safely

The source reached its bounded cleanup boundary with a sanitized failure. The
adapter preserves deterministic shutdown and does not leak a thread, task,
credential, or raw transport exception into consumers.

Recovery: inspect source health and controller lifecycle evidence before any
operator-authorized restart.

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

See `SWNWWS7001`.
