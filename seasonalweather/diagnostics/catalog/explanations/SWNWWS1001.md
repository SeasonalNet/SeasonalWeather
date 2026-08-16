# SWNWWS1001 — NWWS inbound product was malformed

The NWWS adapter rejected an inbound message before it crossed the normalized
source boundary. The controller does not expose malformed transport data to
alert policy.

Recovery: inspect the bounded source health counters and the sanitized replay
fixture or peer behavior. Product text and transport credentials are not
included in the occurrence.

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

See `SWNWWS2001` for an incompatible protocol exchange.
