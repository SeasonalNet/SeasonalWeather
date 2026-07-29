# SWRUN8001 — Previous controller shutdown was incomplete

## Meaning

A prior controller marker survived without authoritative clean-shutdown completion.

## Trigger

The next controller instance safely preserves and reconciles a valid marker left by another instance.

## Correction or recovery

Review the prior lifecycle stage and external service or kernel records. Confirm current readiness after startup.

## Operational effect

The controller records durable reconciliation evidence without claiming whether the cause was a crash, forced termination, resource loss, or power interruption.

## Rationale

In-process reporting cannot run after every abrupt failure, so next-start evidence closes part of the observability gap.

## Alternatives or migration

System service-manager and kernel records remain authoritative for exact exit and host-level causes when available.

## Related diagnostics

- `SWRUN5001` reports a fatal failure observed by the process boundary.
