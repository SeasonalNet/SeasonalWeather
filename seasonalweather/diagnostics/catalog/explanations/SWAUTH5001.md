# SWAUTH5001 — API authenticator unavailable

## Meaning

An authenticator required by the configured API mode is unavailable.

## Trigger

The API cannot use the authenticator required to process a request.

## Correction or recovery

Review authentication initialization and investigate any originating dependency diagnostic first.

## Operational effect

Requests requiring that authenticator are unavailable.

## Rationale

This condition is not a rejection of the caller's credentials.

## Alternatives or migration

Do not bypass authentication to restore access.

## Related diagnostics

`SWAUTH6001` indicates credential rejection instead.
