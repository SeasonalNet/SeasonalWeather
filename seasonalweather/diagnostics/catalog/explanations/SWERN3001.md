# SWERN3001 — ERN transport or decoder failed

## Meaning

The ERN audio transport, FFmpeg process, or SAME decoder failed.

## Trigger

The bounded ERN lifecycle observes a transport, process, or decoding failure.

## Correction or recovery

Apply the ERN reconnect, process replacement, or decoder recovery policy and
retain sanitized evidence.

## Operational effect

The affected ERN delivery is not trusted as valid continuous audio.

## Rationale

Audio and SAME boundaries must fail closed when their dependency fails.

## Alternatives or migration

Use the configured alternate relay or local operating mode where permitted.

## Related diagnostics

- `SWERN4001` reports a stream that remains available but degraded.
