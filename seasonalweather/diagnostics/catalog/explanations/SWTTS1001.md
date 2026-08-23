# SWTTS1001 — TTS response audio was malformed

## Meaning

A local engine or remote provider result failed the authoritative media policy.

## Trigger

RIFF/WAVE, PCM16, sample-rate, channel, duration, size, or finalization checks
reject the staged result.

## Correction or recovery

Discard the staged result and use only policy-permitted fallback or LKG audio.

## Operational effect

Malformed audio cannot reach controller promotion or broadcast publication.

## Rationale

Audio bytes are untrusted until the common validator proves them.

## Alternatives or migration

Correct the engine/provider output profile and retry within the original policy.

## Related diagnostics

- `SWTTS3001` reports execution dependency failure.
- `SWTTS4001` reports permitted fallback use.
