# SWNWWS6001 — NWWS authentication failed

The NWWS account authentication or trust authorization was rejected. The
diagnostic is deliberately credential-free; account identifiers, passwords,
and raw authentication state are not retained.

Recovery: correct credentials through the existing configuration and secret
owners, then restart or replace the source through controller lifecycle.

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

See `SWNWWS3002`.
