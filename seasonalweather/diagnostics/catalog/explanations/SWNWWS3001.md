# SWNWWS3001 — NWWS transport connection failed

The source could not establish its external connection. This includes bounded
DNS, socket, and transport failures. The controller remains available while
the optional source follows its configured reconnect bound.

Recovery: inspect source health and network reachability without exposing raw
transport state or credentials.

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

See `SWNWWS4001` and `SWNWWS7001`.
