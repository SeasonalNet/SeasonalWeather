# SWTTS6001 — TTS credential or trust validation failed

## Meaning

Provider authentication, authorization, credential, or SeasonalCA trust failed.

## Trigger

Credential exchange, token policy, certificate validation, or provider trust
checks reject the selected backend.

## Correction or recovery

Correct the file-backed credential or trust deployment and retry only through
the bounded provider policy.

## Operational effect

The provider request is rejected without exposing credential material.

## Rationale

Trust failures are security conditions, not generic provider outages.

## Alternatives or migration

Use a qualified local fallback if the synthesis purpose permits it.

## Related diagnostics

- `SWTTS3001` reports other provider or engine failures.
