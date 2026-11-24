# SWAUTH6002 — API access denied by policy

## Meaning

The API declined the operation under its configured access policy.

## Trigger

A request is denied by scope, route, client network, or authentication-mode policy.

## Correction or recovery

Verify the applicable policy and use the approved credential and endpoint.

## Operational effect

The operation does not proceed. A routine denial does not create a durable incident.

## Rationale

The authorization or access policy, not a database dependency, rejected the operation.

## Alternatives or migration

Use an approved access path instead of weakening policy.

## Related diagnostics

`SWAUTH6001` covers credential rejection.
