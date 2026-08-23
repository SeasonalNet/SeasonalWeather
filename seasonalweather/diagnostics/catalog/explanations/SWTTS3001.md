# SWTTS3001 — TTS engine or provider failed

## Meaning

The selected local engine or remote synthesis provider failed.

## Trigger

The bounded synthesis operation receives a typed engine, transport, or provider
failure.

## Correction or recovery

Apply the existing purpose, retry, deadline, and fallback policy.

## Operational effect

No output is accepted unless a valid primary or permitted fallback result is
finalized and fenced.

## Rationale

Provider failure must remain separate from malformed media and policy rejection.

## Alternatives or migration

Use a qualified local backend or last-known-good artifact when authorized.

## Related diagnostics

- `SWTTS1001` reports malformed output.
- `SWTTS4001` reports degraded fallback.
