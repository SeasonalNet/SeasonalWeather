# SWNWWS8001 — NWWS source generation was fenced

A superseded source instance observed a newer controller configuration
generation. It was prevented from delivering further products, preserving the
replacement source as the only authoritative instance.

Recovery: allow the controller-owned replacement lifecycle to settle.

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
