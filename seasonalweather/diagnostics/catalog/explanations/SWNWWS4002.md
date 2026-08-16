# SWNWWS4002 — NWWS source is connected but silent

The source joined successfully but delivered no product within its explicit
silence threshold. The adapter treats this as degraded availability and
reconnects within the same controller-owned lifecycle.

Recovery: verify peer delivery and source health; CAP/API recovery remains a
separate existing source path.

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

See `SWNWWS4001`.
