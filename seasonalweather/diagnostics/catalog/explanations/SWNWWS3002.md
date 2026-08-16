# SWNWWS3002 — NWWS TLS or transport protocol failed

TLS trust negotiation or the XMPP transport protocol failed inside the NWWS
adapter. No XMPP-native object or raw trust state crosses the source boundary.

Recovery: verify the accepted trust and transport configuration, then use the
bounded reconnect path.

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

See `SWNWWS2001` and `SWNWWS6001`.
