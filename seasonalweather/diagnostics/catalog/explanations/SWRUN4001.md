# SWRUN4001 — Optional supervised task degraded the controller

## Meaning

An optional controller-owned task failed, so its feature is unavailable while the controller continues.

## Trigger

The task supervisor observes an exception from a task declared optional outside intentional shutdown.

## Correction or recovery

Inspect the bounded exception evidence, correct the failing dependency or feature, and restart through the normal drain procedure when needed.

## Operational effect

Core controller processing may continue, but the named optional component is degraded for this process.

## Rationale

An actionable degradation needs durable evidence without converting an optional failure into a fatal controller outcome.

## Alternatives or migration

If the component becomes required by deployment policy, configure and supervise it through the required-task lifecycle instead.

## Related diagnostics

- `SWRUN5001` reports a failure that terminates the controller.
