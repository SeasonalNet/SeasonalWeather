# SWOBS6001 — Optional observability destination trust failed

## Meaning

Destination trust or authorization prevented an optional observability delivery.

## Trigger

TLS verification, authentication, authorization, or SNMPv3 user/security policy rejected the delivery.

## Correction or recovery

Correct the destination trust chain, credentials, or security profile without placing secret values in configuration or logs.

## Operational effect

The optional delivery is rejected and the controller continues using canonical local outputs.

## Rationale

Trust failures must fail closed and must not be hidden by retries or secret-bearing diagnostics.

## Alternatives or migration

Temporarily disable the optional destination and use a local collector with an independently managed trust policy.

## Related diagnostics

Related conditions are SWOBS3001 and SWOBS2001.
