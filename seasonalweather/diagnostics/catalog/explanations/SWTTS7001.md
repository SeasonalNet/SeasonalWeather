# SWTTS7001 — TTS synthesis exceeded its bound

## Meaning

Synthesis exceeded an input, response, provider, engine, cancellation, or
overall deadline bound.

## Trigger

The operation reaches a configured size or time fence before valid publication.

## Correction or recovery

Reduce the input or adjust the bounded deployment policy; do not extend a
completed request after its deadline.

## Operational effect

The operation cannot publish. Global deadline and cancellation do not initiate
new fallback synthesis.

## Rationale

On-air work must remain bounded and must not wait indefinitely behind synthesis.

## Alternatives or migration

Use existing last-known-good audio only when its freshness and purpose fences
remain valid.

## Related diagnostics

- `SWTTS4001` reports permitted fallback use.
- `SWTTS1001` reports invalid completed media.
