# SWAUTH6001 — API credentials rejected

## Meaning

The API request did not present usable credentials.

## Trigger

A credential is missing, invalid, expired, or revoked.

## Correction or recovery

Check the authorization scheme and use a current credential. Do not share or log it.

## Operational effect

The request does not authenticate. A routine denial does not create a durable incident.

## Rationale

The denial belongs to the API credential boundary.

## Alternatives or migration

Use an approved replacement credential when required.

## Related diagnostics

`SWAUTH6002` covers policy denials.
