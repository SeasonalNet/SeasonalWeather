# SWJOB8001 — Job result requires reconciliation

## Meaning

A job lease, attempt, completion, or acknowledgment has ambiguous evidence.

## Trigger

Restart, interruption, or duplicate delivery prevents immediate durable result
commitment.

## Correction or recovery

Use the controller-owned reconciliation path with the existing lease, attempt,
generation, and result fences.

## Operational effect

The result is not treated as committed or replayed blindly.

## Rationale

WebSocket events and worker claims are not durable truth by themselves.

## Alternatives or migration

Keep the affected job pending or failed according to its typed retry policy.

## Related diagnostics

- `SWJOB2001` reports an incompatible job contract.
