# SWTTS4001 — TTS fallback or last-known-good audio was used

## Meaning

Configured fallback or controller-accepted last-known-good audio supplied the
result.

## Trigger

The primary synthesis path failed and the existing purpose policy allowed a
valid fallback within the original deadline.

## Correction or recovery

Restore the primary backend and clear the degraded condition only after fresh
successful synthesis evidence.

## Operational effect

Broadcast continuity is preserved with an explicitly degraded synthesis result.

## Rationale

Fallback must be visible and must not become an independent publication path.

## Alternatives or migration

Suppress optional content when policy disallows fallback.

## Related diagnostics

- `SWTTS3001` identifies the primary execution failure.
- `SWTTS7001` identifies a bound or deadline failure.
