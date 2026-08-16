# SWNWWS2001 — NWWS protocol exchange was incompatible

The source peer or adapter protocol did not satisfy the accepted bounded
exchange. The adapter retains ownership of XMPP and does not turn the failure
into a worker or job protocol.

Recovery: verify the supported adapter and peer protocol, then allow bounded
reconnect recovery.

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

See `SWNWWS1001` and `SWNWWS3002`.
